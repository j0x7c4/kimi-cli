from __future__ import annotations

from pathlib import Path

from kimi_cli.memory.paths import get_knowledge_dir
from kimi_cli.utils.logging import logger

KNOWLEDGE_BASE_MAX_BYTES = 32 * 1024  # 32 KiB sanity cap on the index file.
INDEX_FILENAME = "index.md"


def load_knowledge_base(work_dir: Path) -> str | None:
    """Load ``{work_dir}/.kimi/memory/knowledge/index.md`` for the system prompt.

    Only the index is injected — it should be a short table of contents that
    describes what knowledge exists and where to find it (paths under
    ``wiki/``). Detailed content lives in ``.kimi/memory/knowledge/wiki/`` and
    is read on demand via the ``ReadFile`` tool. Returns ``None`` when the
    index is missing or empty.
    """
    index_path = get_knowledge_dir(work_dir) / INDEX_FILENAME
    if not index_path.is_file():
        return None
    try:
        content = index_path.read_text(encoding="utf-8").strip()
    except OSError as e:
        logger.warning("Failed to read knowledge index {p}: {e}", p=index_path, e=e)
        return None
    if not content:
        return None

    encoded = content.encode()
    if len(encoded) > KNOWLEDGE_BASE_MAX_BYTES:
        logger.warning(
            "Knowledge index truncated to {n} bytes: {p}",
            n=KNOWLEDGE_BASE_MAX_BYTES,
            p=index_path,
        )
        content = encoded[:KNOWLEDGE_BASE_MAX_BYTES].decode(errors="ignore").strip()

    return content or None
