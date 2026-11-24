"""
renderer.py - Universal Web Page HiDPI PNG Renderer (Native CDP Engine)

Powered by the lightweight native Chrome DevTools Protocol engine (cdp_engine.py).
Provides WebPageRenderer to capture ANY http(s) website as lossless HiDPI PNG(s),
supporting full-page extraction, lazy-image hydration, DOM-aligned slicing for very
tall pages, and Telegram file-size safety guards.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import List, Optional, Tuple
from urllib.parse import urlparse

from dotenv import load_dotenv

from cdp_engine import (
    CDPBrowser,
    CDPPage,
    DEFAULT_DEVICE_SCALE_FACTOR,
    DEFAULT_MAX_SLICE_HEIGHT_CSS,
    DEFAULT_VIEWPORT_HEIGHT,
    DEFAULT_VIEWPORT_WIDTH,
)

load_dotenv()

MAX_SLICE_HEIGHT_CSS = int(os.getenv("MAX_SLICE_HEIGHT_CSS", str(DEFAULT_MAX_SLICE_HEIGHT_CSS)))

# Regex for explicit HTTP(S) URLs anywhere in text
HTTP_URL_REGEX = re.compile(
    r"(https?://[^\s<>\"'`]+)",
    re.IGNORECASE,
)

# Regex for bare domain inputs like "example.com" or "en.wikipedia.org/wiki/Python"
BARE_DOMAIN_REGEX = re.compile(
    r"^(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,24}(?::\d{1,5})?(?:/[^\s]*)?$"
)

BLOCKED_SCHEMES = ("file:", "chrome:", "chrome-extension:", "javascript:", "data:", "vbscript:", "about:", "ftp:")


class InvalidURLError(Exception):
    """Raised when a user supplies an unsupported or unsafe URL."""


def extract_and_normalize_url(text: str) -> Optional[str]:
    """
    Extracts and normalizes a valid HTTP(S) URL from user text.
    - Accepts explicit http:// or https:// URLs.
    - Accepts bare domains without spaces (e.g., 'wikipedia.org', 'news.ycombinator.com/newest').
    - Rejects unsafe schemes (file://, chrome://, javascript:, data:, etc.).
    Returns normalized URL or None if the text does not look like a URL.
    """
    cleaned = text.strip()
    if not cleaned:
        return None

    lower = cleaned.lower()
    if any(lower.startswith(scheme) for scheme in BLOCKED_SCHEMES):
        raise InvalidURLError("Esquema de URL não permitido. Utilize apenas endereços http:// ou https://.")

    match = HTTP_URL_REGEX.search(cleaned)
    if match:
        candidate = match.group(1).rstrip(".,);]>\"'")
        parsed = urlparse(candidate)
        if parsed.scheme.lower() in ("http", "https") and parsed.netloc:
            return candidate
        return None

    # Allow bare domain only when the entire message is a single domain/path token
    if " " not in cleaned and BARE_DOMAIN_REGEX.match(cleaned):
        candidate = f"https://{cleaned}"
        parsed = urlparse(candidate)
        if parsed.netloc:
            return candidate

    return None


def make_safe_filename_prefix(url: str, max_len: int = 48) -> str:
    """Builds a clean ASCII filename prefix from a URL (e.g., 'news_ycombinator_com_newest')."""
    parsed = urlparse(url)
    host = (parsed.netloc or "page").lower()
    path = (parsed.path or "").strip("/")
    raw = f"{host}_{path}" if path else host
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", raw).strip("_")
    if not slug:
        slug = "webpage"
    return slug[:max_len]


class WebPageRenderer:
    """
    Universal full-page web renderer powered by the lightweight native CDP engine.
    Captures ANY http(s) website as uncompressed HiDPI PNG(s), either from a fresh
    isolated page or directly from an active interactive CDPPage.
    """

    def __init__(
        self,
        browser: Optional[CDPBrowser] = None,
        viewport_width: int = DEFAULT_VIEWPORT_WIDTH,
        viewport_height: int = DEFAULT_VIEWPORT_HEIGHT,
        device_scale_factor: float = DEFAULT_DEVICE_SCALE_FACTOR,
    ):
        self._owns_browser = browser is None
        self.browser = browser or CDPBrowser()
        self.viewport_width = viewport_width
        self.viewport_height = viewport_height
        self.device_scale_factor = device_scale_factor

    async def close(self) -> None:
        if self._owns_browser:
            await self.browser.close()

    async def render_url_to_images(
        self,
        url: str,
        output_dir: Path,
        max_slice_height_css: int = MAX_SLICE_HEIGHT_CSS,
    ) -> Tuple[List[Path], str, str]:
        """
        Opens `url` in a fresh isolated CDP tab, captures the entire web page as PNG(s),
        and returns (image_paths, final_url, page_title).
        """
        await self.browser.ensure_started()
        page = await self.browser.new_page(
            viewport_width=self.viewport_width,
            viewport_height=self.viewport_height,
            device_scale_factor=self.device_scale_factor,
        )
        try:
            final_url = await page.goto(url)
            title = await page.get_title()
            prefix = make_safe_filename_prefix(final_url)
            files = await page.screenshot_full_page(
                output_dir=output_dir,
                filename_prefix=prefix,
                max_slice_height_css=max_slice_height_css,
                hydrate_lazy=True,
            )
            return files, final_url, title
        finally:
            await page.close()

    async def capture_existing_page(
        self,
        page: CDPPage,
        output_dir: Path,
        max_slice_height_css: int = MAX_SLICE_HEIGHT_CSS,
    ) -> Tuple[List[Path], str, str]:
        """Extracts the full page from an already-open interactive CDPPage as lossless PNG(s)."""
        final_url = await page.get_url()
        title = await page.get_title()
        prefix = make_safe_filename_prefix(final_url)
        files = await page.screenshot_full_page(
            output_dir=output_dir,
            filename_prefix=prefix,
            max_slice_height_css=max_slice_height_css,
            hydrate_lazy=True,
        )
        return files, final_url, title
