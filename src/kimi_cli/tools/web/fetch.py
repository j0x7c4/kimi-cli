from pathlib import Path
from typing import override

import aiohttp
import trafilatura
from kosong.tooling import CallableTool2, ToolReturnValue
from pydantic import BaseModel, Field

from kimi_cli.config import Config
from kimi_cli.constant import USER_AGENT
from kimi_cli.soul.agent import Runtime
from kimi_cli.soul.toolset import get_current_tool_call_or_none
from kimi_cli.tools.utils import ToolResultBuilder, load_desc
from kimi_cli.utils.aiohttp import new_client_session
from kimi_cli.utils.logging import logger


class Params(BaseModel):
    url: str = Field(description="The URL to fetch content from.")


# Modern browser headers to reduce bot detection
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}


class FetchURL(CallableTool2[Params]):
    name: str = "FetchURL"
    description: str = load_desc(Path(__file__).parent / "fetch.md", {})
    params: type[Params] = Params

    def __init__(self, config: Config, runtime: Runtime):
        super().__init__()
        self._runtime = runtime
        self._service_config = config.services.moonshot_fetch

    @override
    async def __call__(self, params: Params) -> ToolReturnValue:
        if self._service_config:
            ret = await self._fetch_with_service(params)
            if not ret.is_error:
                return ret
            logger.warning("Failed to fetch URL via service: {error}", error=ret.message)
            # fallback to local fetch if service fetch fails
        return await self.fetch_with_http_get(params)

    @staticmethod
    async def fetch_with_http_get(params: Params) -> ToolReturnValue:
        builder = ToolResultBuilder(max_line_length=None)
        resp_text: str | None = None
        try:
            # Fetching arbitrary web pages can take a while on large/slow sites.
            fetch_timeout = aiohttp.ClientTimeout(total=180, sock_read=60, sock_connect=15)
            async with (
                new_client_session(timeout=fetch_timeout) as session,
                session.get(
                    params.url,
                    headers=_BROWSER_HEADERS.copy(),
                ) as response,
            ):
                if response.status >= 400:
                    logger.warning(
                        "FetchURL HTTP error: status={status}, url={url}",
                        status=response.status,
                        url=params.url,
                    )
                    # Fallback to Playwright for 403 (bot detection)
                    if response.status == 403:
                        logger.info("FetchURL falling back to Playwright for {url}", url=params.url)
                        return await FetchURL._fetch_with_playwright(params)
                    return builder.error(
                        (
                            f"Failed to fetch URL. Status: {response.status}. "
                            f"This may indicate the page is not accessible or the server is down."
                        ),
                        brief=f"HTTP {response.status} error",
                    )

                resp_text = await response.text()

                content_type = response.headers.get(aiohttp.hdrs.CONTENT_TYPE, "").lower()
                if content_type.startswith(("text/plain", "text/markdown")):
                    builder.write(resp_text)
                    return builder.ok("The returned content is the full content of the page.")
        except TimeoutError:
            logger.warning("FetchURL timed out: url={url}", url=params.url)
            return builder.error(
                "Failed to fetch URL: request timed out. The server may be slow or unreachable.",
                brief="Request timed out",
            )
        except aiohttp.ClientError as e:
            logger.warning("FetchURL network error: {error}, url={url}", error=e, url=params.url)
            return builder.error(
                (
                    f"Failed to fetch URL due to network error: {e}. "
                    "This may indicate the URL is invalid or the server is unreachable."
                ),
                brief="Network error",
            )

        if not resp_text:
            return builder.ok(
                "The response body is empty.",
                brief="Empty response body",
            )

        extracted_text = trafilatura.extract(
            resp_text,
            include_comments=True,
            include_tables=True,
            include_formatting=False,
            output_format="txt",
            with_metadata=True,
        )

        if not extracted_text:
            return builder.error(
                (
                    "Failed to extract meaningful content from the page. "
                    "This may indicate the page content is not suitable for text extraction, "
                    "or the page requires JavaScript to render its content."
                ),
                brief="No content extracted",
            )

        builder.write(extracted_text)
        return builder.ok("The returned content is the main text content extracted from the page.")

    @staticmethod
    async def _fetch_with_playwright(params: Params) -> ToolReturnValue:
        """Fallback fetch using Playwright + Chromium for bot-protected sites."""
        builder = ToolResultBuilder(max_line_length=None)
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return builder.error(
                "Playwright is not available. Cannot fetch bot-protected pages.",
                brief="Playwright not installed",
            )

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
                context = await browser.new_context(
                    user_agent=_BROWSER_HEADERS["User-Agent"],
                    viewport={"width": 1920, "height": 1080},
                )
                page = await context.new_page()
                await page.goto(
                    params.url,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                # Wait a moment for any lazy-loaded content
                await page.wait_for_timeout(2000)
                content = await page.content()
                await browser.close()
        except Exception as e:
            logger.warning(
                "FetchURL Playwright fallback failed: {error}, url={url}",
                error=e,
                url=params.url,
            )
            return builder.error(
                f"Failed to fetch URL via browser fallback: {e}",
                brief="Browser fetch failed",
            )

        if not content:
            return builder.ok(
                "The page content is empty.",
                brief="Empty page content",
            )

        extracted_text = trafilatura.extract(
            content,
            include_comments=True,
            include_tables=True,
            include_formatting=False,
            output_format="txt",
            with_metadata=True,
        )

        if not extracted_text:
            return builder.error(
                (
                    "Failed to extract meaningful content from the page. "
                    "The page may require interaction or specific browser features."
                ),
                brief="No content extracted via browser",
            )

        builder.write(extracted_text)
        return builder.ok("The returned content was fetched via browser automation.")

    async def _fetch_with_service(self, params: Params) -> ToolReturnValue:
        assert self._service_config is not None

        tool_call = get_current_tool_call_or_none()
        assert tool_call is not None, "Tool call is expected to be set"

        builder = ToolResultBuilder(max_line_length=None)
        api_key = self._runtime.oauth.resolve_api_key(
            self._service_config.api_key, self._service_config.oauth
        )
        if not api_key:
            return builder.error(
                "Fetch service is not configured. You may want to try other methods to fetch.",
                brief="Fetch service not configured",
            )
        headers = {
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {api_key}",
            "Accept": "text/markdown",
            "X-Msh-Tool-Call-Id": tool_call.id,
            **self._runtime.oauth.common_headers(),
            **(self._service_config.custom_headers or {}),
        }

        try:
            async with (
                new_client_session() as session,
                session.post(
                    self._service_config.base_url,
                    headers=headers,
                    json={"url": params.url},
                ) as response,
            ):
                if response.status != 200:
                    logger.warning(
                        "FetchURL service HTTP error: status={status}, url={url}",
                        status=response.status,
                        url=params.url,
                    )
                    return builder.error(
                        f"Failed to fetch URL via service. Status: {response.status}.",
                        brief="Failed to fetch URL via fetch service",
                    )

                content = await response.text()
                builder.write(content)
                return builder.ok(
                    "The returned content is the main content extracted from the page."
                )
        except TimeoutError:
            logger.warning("FetchURL service timed out: url={url}", url=params.url)
            return builder.error(
                "Failed to fetch URL via service: request timed out.",
                brief="Service request timed out",
            )
        except aiohttp.ClientError as e:
            logger.warning(
                "FetchURL service network error: {error}, url={url}", error=e, url=params.url
            )
            return builder.error(
                (
                    f"Failed to fetch URL via service due to network error: {e}. "
                    "This may indicate the service is unreachable."
                ),
                brief="Network error when calling fetch service",
            )
