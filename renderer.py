"""
renderer.py - Universal Web Page & 4chan Thread HiDPI PNG Renderer (Native CDP Engine)

Replaces Playwright with our lightweight native Chrome DevTools Protocol engine (cdp_engine.py).
Provides:
1. WebPageRenderer: Captures ANY website URL as crisp HiDPI PNG(s), supporting full-page
   extraction, lazy-image hydration, DOM-aligned slicing for very tall pages, and Telegram
   file-size safety guards.
2. ThreadRenderer (4chan Archiver): Preserves authentic 4chan Yotsuba B thread rendering with
   strict 1-req/sec rate limiting, concurrent base64 thumbnail embedding, HTML sanitization,
   and post-aligned PNG slicing — now powered by the same lightweight CDP engine.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from dotenv import load_dotenv
import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape

from cdp_engine import (
    CDPBrowser,
    CDPPage,
    DEFAULT_DEVICE_SCALE_FACTOR,
    DEFAULT_MAX_SLICE_HEIGHT_CSS,
    DEFAULT_VIEWPORT_HEIGHT,
    DEFAULT_VIEWPORT_WIDTH,
    NavigationError,
)

load_dotenv()

logger = logging.getLogger(__name__)

# 4chan API constants
API_BASE_URL = "https://a.4cdn.org"
IMAGE_BASE_URL = "https://i.4cdn.org"
USER_AGENT = "ChanArcTelegramBot/2.0 (Native CDP Web & 4chan Renderer)"

MAX_SLICE_HEIGHT_CSS = int(os.getenv("MAX_SLICE_HEIGHT_CSS", str(DEFAULT_MAX_SLICE_HEIGHT_CSS)))
MAX_CONCURRENT_THUMBNAIL_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_THUMBNAILS", "10"))

# Regex to extract board and thread_id from 4chan/4channel URLs
FOURCHAN_URL_REGEX = re.compile(
    r"https?://(?:boards\.)?4chan(?:nel)?\.org/(?P<board>[a-zA-Z0-9_]+)/thread/(?P<thread_id>\d+)",
    re.IGNORECASE,
)

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


class ThreadNotFoundError(Exception):
    """Raised when a 4chan thread returns 404 (deleted, pruned, or non-existent)."""


class FourChanAPIError(Exception):
    """Raised for any unexpected error communicating with 4chan's API."""


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


class AsyncRateLimiter:
    """
    Enforces a strict rate limit across async tasks.
    Per 4chan API guidelines: 'Do not make more than one request per second'.
    """

    def __init__(self, requests_per_second: float = 1.0):
        self.interval = 1.0 / requests_per_second
        self._lock = asyncio.Lock()
        self._last_call_time = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            elapsed = now - self._last_call_time
            if elapsed < self.interval:
                await asyncio.sleep(self.interval - elapsed)
            self._last_call_time = loop.time()


api_rate_limiter = AsyncRateLimiter(requests_per_second=1.0)


def sanitize_comment(raw_html: Optional[str]) -> str:
    """
    Sanitizes raw 4chan comment HTML ('com' field).
    Preserves greentext, quotelinks, spoilers, code blocks, and line breaks while
    stripping scripts, iframes, inline event handlers, and javascript: URIs.
    """
    if not raw_html:
        return ""

    soup = BeautifulSoup(raw_html, "html.parser")

    for dangerous_tag in soup(["script", "style", "iframe", "object", "embed", "form", "input", "meta", "link"]):
        dangerous_tag.decompose()

    allowed_tags = {
        "a", "span", "b", "strong", "i", "em", "s", "del", "u", "pre", "code", "br", "wbr", "p"
    }
    allowed_attributes = {
        "a": {"href", "class", "rel"},
        "span": {"class"},
        "pre": {"class"},
        "code": {"class"},
    }

    for tag in soup.find_all(True):
        if tag.name not in allowed_tags:
            tag.unwrap()
            continue

        valid_attrs = allowed_attributes.get(tag.name, set())
        attrs = dict(tag.attrs)
        for attr_name, attr_val in attrs.items():
            if attr_name not in valid_attrs:
                del tag[attr_name]
            elif attr_name == "href":
                clean_href = str(attr_val).strip().lower()
                if clean_href.startswith(("javascript:", "vbscript:", "data:")):
                    del tag[attr_name]

    return str(soup)


def format_file_size(size_bytes: int) -> str:
    """Formats file size into human-readable string (e.g. '345 KB' or '2.45 MB')."""
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f} KB"
    return f"{size_bytes / (1024 * 1024):.2f} MB"


async def fetch_thumbnail_as_base64(
    board: str,
    tim: int,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
) -> str:
    """Downloads a 4chan post thumbnail and embeds it as a base64 Data URI."""
    url = f"{IMAGE_BASE_URL}/{board}/{tim}s.jpg"
    async with semaphore:
        try:
            resp = await client.get(url, timeout=15.0)
            if resp.status_code == 200:
                b64 = base64.b64encode(resp.content).decode("ascii")
                return f"data:image/jpeg;base64,{b64}"
            logger.warning(f"Thumbnail {url} returned HTTP {resp.status_code}")
        except Exception as e:
            logger.warning(f"Failed to fetch thumbnail {url}: {e}")
    return ""


class FourChanClient:
    """Async client for fetching 4chan thread JSON with 1-req/s rate limiting."""

    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        self._internal_client = client is None
        self.client = client or httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=20.0,
        )
        self._board_names_cache: Dict[str, str] = {}

    async def close(self) -> None:
        if self._internal_client and not self.client.is_closed:
            await self.client.aclose()

    async def get_board_title(self, board: str) -> str:
        if board in self._board_names_cache:
            return self._board_names_cache[board]

        try:
            await api_rate_limiter.acquire()
            resp = await self.client.get(f"{API_BASE_URL}/boards.json")
            if resp.status_code == 200:
                data = resp.json()
                for b in data.get("boards", []):
                    self._board_names_cache[b["board"]] = b.get("title", b["board"].upper())
                return self._board_names_cache.get(board, board.upper())
        except Exception as e:
            logger.warning(f"Could not fetch boards.json: {e}")

        fallback = board.upper()
        self._board_names_cache[board] = fallback
        return fallback

    async def fetch_thread(self, board: str, thread_id: int) -> Dict[str, Any]:
        url = f"{API_BASE_URL}/{board}/thread/{thread_id}.json"
        await api_rate_limiter.acquire()

        try:
            resp = await self.client.get(url)
        except httpx.RequestError as exc:
            raise FourChanAPIError(f"Erro de rede ao contatar API do 4chan: {exc}") from exc

        if resp.status_code == 404:
            raise ThreadNotFoundError(
                f"Thread /{board}/{thread_id} não encontrada (pode ter sido arquivada ou removida)."
            )
        elif resp.status_code != 200:
            raise FourChanAPIError(f"API do 4chan retornou status HTTP inesperado: {resp.status_code}.")

        try:
            return resp.json()
        except Exception as exc:
            raise FourChanAPIError(f"Falha ao interpretar JSON da thread do 4chan: {exc}") from exc


class ThreadRenderer:
    """
    Renders 4chan threads in the authentic Yotsuba B theme to lossless HiDPI PNGs
    with clean post-aligned slicing using the lightweight native CDP engine.
    """

    def __init__(
        self,
        templates_dir: Optional[Path] = None,
        fourchan_client: Optional[FourChanClient] = None,
        browser: Optional[CDPBrowser] = None,
    ):
        base_dir = templates_dir or Path(__file__).resolve().parent
        self.jinja_env = Environment(
            loader=FileSystemLoader(str(base_dir)),
            autoescape=select_autoescape(["html", "xml"]),
        )
        self.template = self.jinja_env.get_template("template.html")
        self.fourchan_client = fourchan_client or FourChanClient()
        self._owns_browser = browser is None
        self.browser = browser or CDPBrowser()

    async def _ensure_browser(self) -> CDPBrowser:
        await self.browser.ensure_started()
        return self.browser

    async def close(self) -> None:
        if self._owns_browser:
            await self.browser.close()
        await self.fourchan_client.close()

    async def build_thread_html(self, board: str, thread_id: int) -> Tuple[str, int]:
        thread_data = await self.fourchan_client.fetch_thread(board, thread_id)
        board_title = await self.fourchan_client.get_board_title(board)

        raw_posts = thread_data.get("posts", [])
        if not raw_posts:
            raise FourChanAPIError("A thread não contém posts.")

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_THUMBNAIL_DOWNLOADS)
        thumb_tasks = []
        posts_with_images = []

        for p in raw_posts:
            if "tim" in p:
                posts_with_images.append(p)
                thumb_tasks.append(
                    fetch_thumbnail_as_base64(board, p["tim"], self.fourchan_client.client, semaphore)
                )

        if thumb_tasks:
            thumb_uris = await asyncio.gather(*thumb_tasks, return_exceptions=True)
            for p, uri in zip(posts_with_images, thumb_uris):
                p["thumb_data_uri"] = uri if isinstance(uri, str) else ""

        processed_posts = []
        for p in raw_posts:
            item = dict(p)
            item["com"] = sanitize_comment(p.get("com", ""))
            if "fsize" in p:
                item["fsize_formatted"] = format_file_size(p["fsize"])
                item["file_url"] = f"{IMAGE_BASE_URL}/{board}/{p['tim']}{p.get('ext', '')}"
            processed_posts.append(item)

        op_post = processed_posts[0]
        reply_posts = processed_posts[1:]

        rendered_html = self.template.render(
            board=board,
            board_title=board_title,
            thread_id=thread_id,
            op=op_post,
            replies=reply_posts,
        )
        return rendered_html, len(processed_posts)

    async def render_thread_to_images(
        self,
        board: str,
        thread_id: int,
        output_dir: Path,
    ) -> List[Path]:
        """Renders a 4chan thread to one or more lossless HiDPI PNG files."""
        html_content, total_posts = await self.build_thread_html(board, thread_id)
        browser = await self._ensure_browser()

        page: CDPPage = await browser.new_page(
            viewport_width=1100,
            viewport_height=800,
            device_scale_factor=2.0,
        )
        try:
            await page.set_content(html_content)
            logger.info(f"Rendering 4chan thread /{board}/{thread_id} ({total_posts} posts) via native CDP...")
            return await page.screenshot_full_page(
                output_dir=output_dir,
                filename_prefix=f"{board}_{thread_id}",
                max_slice_height_css=MAX_SLICE_HEIGHT_CSS,
                hydrate_lazy=False,
                block_selector=".postContainer",
            )
        finally:
            await page.close()


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
