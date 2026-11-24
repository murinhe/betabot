"""
session_manager.py - Per-Chat Interactive Browser Session Manager

Manages isolated interactive browsing sessions for Telegram chats on top of the shared
lightweight CDPBrowser instance. Features:
- Session modes: IDLE, CLICK (interactive element click), TYPE (direct chat-to-page input),
  WEB_SEARCH (interactive multi-engine web search), PAGE_FIND (in-page text finder).
- Per-chat asyncio.Lock to prevent overlapping button/message races.
- Configurable idle session TTL reaper & max concurrent session LRU cap to protect RAM.
- Clean support for reloading, scrolling, history navigation, and full-page PNG extraction.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
import logging
import os
from pathlib import Path
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote_plus

from dotenv import load_dotenv

from cdp_engine import (
    CDPBrowser,
    CDPPage,
    ClickResult,
    DEFAULT_DEVICE_SCALE_FACTOR,
    DEFAULT_VIEWPORT_HEIGHT,
    DEFAULT_VIEWPORT_WIDTH,
    FindResult,
    InteractiveElement,
    ScrollInfo,
)
from renderer import MAX_SLICE_HEIGHT_CSS, make_safe_filename_prefix

load_dotenv()

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "900"))  # 15 min idle timeout
MAX_CONCURRENT_SESSIONS = int(os.getenv("MAX_CONCURRENT_SESSIONS", "5"))
DEFAULT_SEARCH_ENGINE = os.getenv("DEFAULT_SEARCH_ENGINE", "duckduckgo").lower()


class SessionMode(str, Enum):
    IDLE = "idle"
    CLICK = "click"
    TYPE = "type"
    WEB_SEARCH = "web_search"
    PAGE_FIND = "page_find"


SEARCH_ENGINES: Dict[str, Dict[str, str]] = {
    "duckduckgo": {
        "name": "DuckDuckGo",
        "emoji": "🦆",
        "url": "https://duckduckgo.com/?q={query}&kl=br-pt",
    },
    "google": {
        "name": "Google",
        "emoji": "🔎",
        "url": "https://www.google.com/search?q={query}&hl=pt-BR",
    },
    "wikipedia": {
        "name": "Wikipédia",
        "emoji": "📚",
        "url": "https://pt.wikipedia.org/w/index.php?search={query}",
    },
    "bing": {
        "name": "Bing",
        "emoji": "🌐",
        "url": "https://www.bing.com/search?q={query}&setlang=pt-BR",
    },
    "youtube": {
        "name": "YouTube",
        "emoji": "🎬",
        "url": "https://www.youtube.com/results?search_query={query}",
    },
}


def build_search_url(query: str, engine: str = DEFAULT_SEARCH_ENGINE) -> Tuple[str, Dict[str, str]]:
    """Builds a search engine URL for the given query."""
    meta = SEARCH_ENGINES.get(engine, SEARCH_ENGINES["duckduckgo"])
    encoded = quote_plus(query.strip())
    return meta["url"].format(query=encoded), meta


@dataclass
class InteractiveSnapshot:
    """Data bundle returned after rendering an interactive viewport frame."""

    image_path: Path
    url: str
    title: str
    elements: List[InteractiveElement]
    scroll: ScrollInfo
    status_note: str = ""


@dataclass
class BrowserSession:
    """State of a single Telegram chat's browser session."""

    chat_id: int
    browser: CDPBrowser
    page: Optional[CDPPage] = None
    mode: SessionMode = SessionMode.IDLE
    search_engine: str = field(
        default_factory=lambda: DEFAULT_SEARCH_ENGINE if DEFAULT_SEARCH_ENGINE in SEARCH_ENGINES else "duckduckgo"
    )
    show_tags: bool = True
    auto_enter: bool = True
    badge_page: int = 0
    last_url: str = ""
    last_title: str = ""
    last_search_query: str = ""
    find_query: str = ""
    find_index: int = 0
    find_total: int = 0
    interactive_msg_id: Optional[int] = None
    pending_text: str = ""  # Last plain text message stored for quick callback actions
    elements: List[InteractiveElement] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_activity: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    @property
    def has_open_page(self) -> bool:
        return self.page is not None and not self.page.closed

    async def ensure_page(self) -> CDPPage:
        """Returns the active CDPPage for this chat, creating a fresh isolated tab if needed."""
        self.touch()
        if self.page is None or self.page.closed or not self.browser.is_connected():
            if self.page and not self.page.closed:
                try:
                    await self.page.close()
                except Exception:
                    pass
            self.page = await self.browser.new_page(
                viewport_width=DEFAULT_VIEWPORT_WIDTH,
                viewport_height=DEFAULT_VIEWPORT_HEIGHT,
                device_scale_factor=DEFAULT_DEVICE_SCALE_FACTOR,
            )
            # Re-open last URL if session recovered from browser restart
            if self.last_url and self.last_url.startswith(("http://", "https://")):
                try:
                    await self.page.goto(self.last_url)
                except Exception as exc:
                    logger.warning(f"Could not restore {self.last_url} after tab recreate: {exc}")
        return self.page

    async def navigate(self, url: str) -> Tuple[str, str]:
        """Navigates this chat's tab to `url` and updates tracked metadata."""
        page = await self.ensure_page()
        final_url = await page.goto(url)
        title = await page.get_title()
        self.last_url = final_url
        self.last_title = title
        self.badge_page = 0
        self.find_query = ""
        self.find_index = 0
        self.find_total = 0
        self.touch()
        return final_url, title

    async def reload(self) -> Tuple[str, str]:
        """Reloads the active page (or re-opens `last_url` if the tab was closed)."""
        if self.has_open_page and self.page is not None:
            final_url = await self.page.reload(ignore_cache=True)
            title = await self.page.get_title()
            self.last_url = final_url
            self.last_title = title
            self.badge_page = 0
            self.find_query = ""
            self.find_index = 0
            self.find_total = 0
            self.touch()
            return final_url, title
        if self.last_url:
            return await self.navigate(self.last_url)
        raise RuntimeError("Nenhuma página aberta nesta sessão para atualizar.")

    async def snapshot(self, output_path: Path, status_note: str = "") -> InteractiveSnapshot:
        """
        Tags visible interactive elements (if show_tags is ON), takes a crisp viewport PNG,
        and returns an InteractiveSnapshot ready to send/update in Telegram.
        """
        page = await self.ensure_page()
        elements = await page.collect_and_tag_elements(show_overlay=self.show_tags, max_elements=40)
        self.elements = elements
        scroll = await page.get_scroll_info()
        url = await page.get_url()
        title = await page.get_title()
        self.last_url = url
        self.last_title = title

        await page.screenshot_viewport(output_path)
        self.touch()
        return InteractiveSnapshot(
            image_path=output_path,
            url=url,
            title=title,
            elements=elements,
            scroll=scroll,
            status_note=status_note,
        )

    async def export_full_page(self, output_dir: Path) -> Tuple[List[Path], str, str]:
        """Extracts the full current page as lossless HiDPI PNG(s)."""
        page = await self.ensure_page()
        url = await page.get_url()
        title = await page.get_title()
        prefix = make_safe_filename_prefix(url)
        files = await page.screenshot_full_page(
            output_dir=output_dir,
            filename_prefix=prefix,
            max_slice_height_css=MAX_SLICE_HEIGHT_CSS,
            hydrate_lazy=True,
        )
        self.last_url = url
        self.last_title = title
        self.touch()
        return files, url, title

    async def click_by_id(self, element_id: int) -> ClickResult:
        page = await self.ensure_page()
        res = await page.click_element_by_id(element_id)
        if res.success:
            self.last_url = res.url or self.last_url
            self.last_title = res.title or self.last_title
            if res.navigated:
                self.badge_page = 0
                self.find_query = ""
                self.find_index = 0
                self.find_total = 0
        self.touch()
        return res

    async def click_by_text(self, query: str) -> ClickResult:
        page = await self.ensure_page()
        res = await page.click_by_text(query)
        if res.success:
            self.last_url = res.url or self.last_url
            self.last_title = res.title or self.last_title
            if res.navigated:
                self.badge_page = 0
        self.touch()
        return res

    async def click_by_selector(self, selector: str) -> ClickResult:
        page = await self.ensure_page()
        res = await page.click_by_selector(selector)
        if res.success:
            self.last_url = res.url or self.last_url
            self.last_title = res.title or self.last_title
            if res.navigated:
                self.badge_page = 0
        self.touch()
        return res

    async def click_at(self, x: float, y: float) -> ClickResult:
        page = await self.ensure_page()
        res = await page.click_at(x, y)
        if res.success:
            self.last_url = res.url or self.last_url
            self.last_title = res.title or self.last_title
            if res.navigated:
                self.badge_page = 0
        self.touch()
        return res

    async def type_text(
        self,
        text: str,
        press_enter: Optional[bool] = None,
        clear_first: bool = False,
        element_id: Optional[int] = None,
    ) -> Tuple[bool, str]:
        page = await self.ensure_page()
        do_enter = self.auto_enter if press_enter is None else press_enter
        navigated, note = await page.type_text(
            text=text,
            press_enter=do_enter,
            clear_first=clear_first,
            element_id=element_id,
        )
        self.last_url = await page.get_url()
        self.last_title = await page.get_title()
        if navigated:
            self.badge_page = 0
        self.touch()
        return navigated, note

    async def find_in_page(self, query: str) -> FindResult:
        page = await self.ensure_page()
        res = await page.find_in_page(query)
        self.find_query = res.query
        self.find_index = res.current_index
        self.find_total = res.total
        self.touch()
        return res

    async def find_step(self, delta: int) -> FindResult:
        page = await self.ensure_page()
        if not self.find_query or self.find_total <= 0:
            return FindResult(query=self.find_query, total=0, current_index=0)
        target = self.find_index + delta
        res = await page.find_goto(self.find_query, target)
        self.find_index = res.current_index
        self.find_total = res.total
        self.touch()
        return res

    async def clear_find(self) -> None:
        if self.has_open_page and self.page is not None:
            await self.page.clear_find()
        self.find_query = ""
        self.find_index = 0
        self.find_total = 0
        self.touch()

    async def close_page(self) -> None:
        """Closes only the browser tab while keeping lightweight metadata (like last_url)."""
        if self.page is not None:
            try:
                await self.page.close()
            except Exception:
                pass
            self.page = None
        self.elements = []
        self.mode = SessionMode.IDLE
        self.interactive_msg_id = None
        self.find_query = ""
        self.find_index = 0
        self.find_total = 0


class SessionManager:
    """Manages active BrowserSessions across Telegram chats with TTL reaping and LRU cap."""

    def __init__(
        self,
        browser: CDPBrowser,
        ttl_seconds: int = SESSION_TTL_SECONDS,
        max_sessions: int = MAX_CONCURRENT_SESSIONS,
    ):
        self.browser = browser
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions
        self._sessions: Dict[int, BrowserSession] = {}
        self._reaper_task: Optional[asyncio.Task] = None

    def start_reaper(self) -> None:
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reaper_loop(), name="session-ttl-reaper")

    async def _reaper_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(60)
                await self.reap_expired()
        except asyncio.CancelledError:
            pass

    async def reap_expired(self) -> int:
        """Closes browser tabs that have been idle longer than `ttl_seconds`."""
        now = time.monotonic()
        closed_count = 0
        for chat_id, session in list(self._sessions.items()):
            if session.has_open_page and (now - session.last_activity) > self.ttl_seconds:
                logger.info(f"Closing idle browser tab for chat {chat_id} (idle > {self.ttl_seconds}s).")
                async with session.lock:
                    await session.close_page()
                closed_count += 1
        return closed_count

    async def _enforce_session_cap(self, exclude_chat_id: int) -> None:
        """Evicts the least-recently-used active tab if concurrent open tabs exceed cap."""
        open_sessions = [
            s for cid, s in self._sessions.items()
            if cid != exclude_chat_id and s.has_open_page
        ]
        if len(open_sessions) >= self.max_sessions:
            open_sessions.sort(key=lambda s: s.last_activity)
            victim = open_sessions[0]
            logger.info(
                f"Evicting LRU browser tab for chat {victim.chat_id} "
                f"(cap={self.max_sessions})."
            )
            async with victim.lock:
                await victim.close_page()

    async def get_session(self, chat_id: int, will_open_tab: bool = False) -> BrowserSession:
        if will_open_tab:
            await self._enforce_session_cap(exclude_chat_id=chat_id)
        session = self._sessions.get(chat_id)
        if session is None:
            session = BrowserSession(chat_id=chat_id, browser=self.browser)
            self._sessions[chat_id] = session
        session.touch()
        return session

    def peek_session(self, chat_id: int) -> Optional[BrowserSession]:
        return self._sessions.get(chat_id)

    async def close_session(self, chat_id: int) -> bool:
        session = self._sessions.get(chat_id)
        if not session:
            return False
        async with session.lock:
            had_page = session.has_open_page or session.mode != SessionMode.IDLE
            await session.close_page()
        return had_page

    async def close_all(self) -> None:
        if self._reaper_task:
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except Exception:
                pass
            self._reaper_task = None

        for session in list(self._sessions.values()):
            try:
                await session.close_page()
            except Exception:
                pass
        self._sessions.clear()
