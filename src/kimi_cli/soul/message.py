from __future__ import annotations

import base64
from collections.abc import Sequence
from io import BytesIO

from kosong.message import Message
from kosong.tooling.error import ToolRuntimeError

from kimi_cli.llm import ModelCapability
from kimi_cli.utils.logging import logger
from kimi_cli.wire.types import (
    ContentPart,
    ImageURLPart,
    TextPart,
    ThinkPart,
    ToolResult,
    VideoURLPart,
)

# MIME types vision LLMs (Kimi/OpenAI/Anthropic) reliably accept for image_url parts.
LLM_SAFE_IMAGE_MIMES: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/gif"}
)


def system(message: str) -> ContentPart:
    return TextPart(text=f"<system>{message}</system>")


def system_reminder(message: str) -> TextPart:
    return TextPart(text=f"<system-reminder>\n{message}\n</system-reminder>")


def is_system_reminder_message(message: Message) -> bool:
    """Check whether a message is an internal system-reminder user message."""
    if message.role != "user" or len(message.content) != 1:
        return False
    part = message.content[0]
    return isinstance(part, TextPart) and part.text.strip().startswith("<system-reminder>")


def tool_result_to_message(tool_result: ToolResult) -> Message:
    """Convert a tool result to a message."""
    if tool_result.return_value.is_error:
        assert tool_result.return_value.message, "Error return value should have a message"
        message = tool_result.return_value.message
        if isinstance(tool_result.return_value, ToolRuntimeError):
            message += "\nThis is an unexpected error and the tool is probably not working."
        content: list[ContentPart] = [system(f"ERROR: {message}")]
        if tool_result.return_value.output:
            content.extend(_output_to_content_parts(tool_result.return_value.output))
    else:
        content: list[ContentPart] = []
        if tool_result.return_value.message:
            content.append(system(tool_result.return_value.message))
        if tool_result.return_value.output:
            content.extend(_output_to_content_parts(tool_result.return_value.output))
        if not content:
            content.append(system("Tool output is empty."))
        elif not any(isinstance(part, TextPart) for part in content):
            # Ensure at least one TextPart exists so the LLM API won't reject
            # the message with "text content is empty" (see #1663).
            content.insert(0, system("Tool returned non-text content."))

    return Message(
        role="tool",
        content=content,
        tool_call_id=tool_result.tool_call_id,
    )


def _output_to_content_parts(
    output: str | ContentPart | Sequence[ContentPart],
) -> list[ContentPart]:
    content: list[ContentPart] = []
    match output:
        case str(text):
            if text:
                content.append(TextPart(text=text))
        case ContentPart():
            content.append(output)
        case _:
            content.extend(output)
    return content


def check_message(
    message: Message, model_capabilities: set[ModelCapability]
) -> set[ModelCapability]:
    """Check the message content, return the missing model capabilities."""
    capabilities_needed = set[ModelCapability]()
    for part in message.content:
        if isinstance(part, ImageURLPart):
            capabilities_needed.add("image_in")
        elif isinstance(part, VideoURLPart):
            capabilities_needed.add("video_in")
        elif isinstance(part, ThinkPart):
            capabilities_needed.add("thinking")
    return capabilities_needed - model_capabilities


def _parse_data_url(url: str) -> tuple[str, bytes] | None:
    """Parse a ``data:<mime>;base64,<data>`` URL. Returns (mime, bytes) or None."""
    if not url.startswith("data:"):
        return None
    try:
        header, _, payload = url[5:].partition(",")
        if ";base64" not in header:
            return None
        mime = header.split(";", 1)[0].strip().lower()
        if not mime:
            return None
        return mime, base64.b64decode(payload)
    except Exception:
        return None


def _transcode_image_part_to_jpeg(data: bytes) -> bytes | None:
    """Decode arbitrary image bytes and re-encode as JPEG. None on failure.

    Relies on ``pillow_heif.register_heif_opener()`` having been called
    elsewhere (see ``tools/file/read_media.py``) so HEIC/HEIF/AVIF decode.
    """
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        with Image.open(BytesIO(data)) as image:
            image.load()
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            buf = BytesIO()
            image.save(buf, format="JPEG", quality=90)
            return buf.getvalue()
    except Exception:
        return None


def _sanitize_part(part: ContentPart) -> ContentPart:
    """Return a safe replacement for ``part`` if it's an unsupported image.

    Pass-through for everything except ``ImageURLPart`` whose ``data:`` URL
    carries a MIME outside ``LLM_SAFE_IMAGE_MIMES``. Such parts are transcoded
    to JPEG (preferred) or replaced with a TextPart placeholder (fallback).
    Non-data URLs are left alone — the LLM provider fetches them itself.
    """
    if not isinstance(part, ImageURLPart):
        return part
    parsed = _parse_data_url(part.image_url.url)
    if parsed is None:
        return part  # remote URL — let the provider handle it
    mime, raw = parsed
    if mime in LLM_SAFE_IMAGE_MIMES:
        return part
    transcoded = _transcode_image_part_to_jpeg(raw)
    if transcoded is not None:
        new_url = f"data:image/jpeg;base64,{base64.b64encode(transcoded).decode('ascii')}"
        logger.warning(
            "Sanitized image part: {mime} -> image/jpeg ({old}B -> {new}B)",
            mime=mime,
            old=len(raw),
            new=len(transcoded),
        )
        return ImageURLPart(image_url=ImageURLPart.ImageURL(url=new_url, id=part.image_url.id))
    logger.warning("Dropping image part with unsupported MIME {mime} (transcode failed)", mime=mime)
    return TextPart(text=f"[image attachment removed: unsupported format {mime}]")


def strip_stale_reasoning(messages: Sequence[Message]) -> list[Message]:
    """Return a copy of ``messages`` with completed-turn reasoning removed.

    Assistant ``ThinkPart`` content that belongs to an already-finished turn (any
    assistant message positioned *before* the last ``user`` message) is dropped
    before the history is handed to the chat provider. Reasoning produced within
    the current in-flight turn — assistant / tool-continuation messages *after*
    the last user message — is preserved untouched, so provider-side within-turn
    preserved-thinking (e.g. Moonshot ``thinking.keep``) still works.

    Why: both the ``kimi`` and ``openai_legacy`` providers re-serialize a
    historical ``ThinkPart`` back into the request's ``reasoning_content`` field.
    Replaying a completed turn's reasoning into a later request makes some
    reasoning models (e.g. deepseek-v4-flash via litellm) degenerate — the stream
    never reaches a clean ``finish_reason: stop`` with visible text, so the turn
    never terminates. Dropping stale cross-turn reasoning removes the trigger.

    The originals (and the conversation history they live in) are not mutated;
    callers should pass the returned copy to the chat provider while keeping the
    full history for UI display and persistence.
    """
    last_user_idx = -1
    for idx, msg in enumerate(messages):
        if msg.role == "user":
            last_user_idx = idx

    stripped: list[Message] = []
    for idx, msg in enumerate(messages):
        if (
            idx < last_user_idx
            and msg.role == "assistant"
            and any(isinstance(part, ThinkPart) for part in msg.content)
        ):
            kept = [part for part in msg.content if not isinstance(part, ThinkPart)]
            stripped.append(msg.model_copy(update={"content": kept}))
        else:
            stripped.append(msg)
    return stripped


def sanitize_image_parts(messages: Sequence[Message]) -> list[Message]:
    """Return a copy of ``messages`` with non-LLM-safe image parts replaced.

    The originals (and the conversation history they live in) are not mutated;
    callers should pass the sanitized copy to the chat provider while keeping
    the un-sanitized history for UI display and persistence.
    """
    sanitized: list[Message] = []
    for msg in messages:
        new_parts: list[ContentPart] | None = None
        for idx, part in enumerate(msg.content):
            replacement = _sanitize_part(part)
            if replacement is part:
                if new_parts is not None:
                    new_parts.append(part)
                continue
            if new_parts is None:
                new_parts = list(msg.content[:idx])
            new_parts.append(replacement)
        if new_parts is None:
            sanitized.append(msg)
        else:
            sanitized.append(msg.model_copy(update={"content": new_parts}))
    return sanitized
