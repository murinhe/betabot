"""
cdp_engine.py - Lightweight Pure-Python Async Chrome DevTools Protocol (CDP) Engine

Replaces Playwright entirely (zero Node.js driver, zero greenlet, minimal memory overhead)
by driving chrome-headless-shell (or system Chromium/Chrome) directly over native CDP WebSockets.

Capabilities:
- Single shared headless browser process with automatic crash recovery & clean teardown.
- Multiplexed flattened CDP sessions over 1 WebSocket with isolated BrowserContexts per session.
- Full-page HiDPI PNG extraction with lazy-image hydration, DOM-aligned boundary slicing,
  sticky-element de-duplication on multi-slice captures, and Telegram file-size safety guard.
- Interactive browsing: color-coded numbered element overlays, real trusted CDP clicks
  (by badge ID, visible text, CSS selector, or coordinates), UTF-8 chat-to-page typing,
  key dispatch, scrolling, page reload, back/forward history, and highlighted find-in-page.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from dotenv import load_dotenv
from websockets.asyncio.client import ClientConnection, connect as ws_connect
from websockets.exceptions import ConnectionClosed

load_dotenv()

logger = logging.getLogger(__name__)

# Defaults configurable via environment
DEFAULT_VIEWPORT_WIDTH = int(os.getenv("VIEWPORT_WIDTH", "1280"))
DEFAULT_VIEWPORT_HEIGHT = int(os.getenv("VIEWPORT_HEIGHT", "900"))
DEFAULT_DEVICE_SCALE_FACTOR = float(os.getenv("DEVICE_SCALE_FACTOR", "2.0"))
DEFAULT_MAX_SLICE_HEIGHT_CSS = int(os.getenv("MAX_SLICE_HEIGHT_CSS", "14000"))
DEFAULT_NAVIGATION_TIMEOUT = float(os.getenv("NAVIGATION_TIMEOUT", "30.0"))
DEFAULT_PAGE_LOAD_SETTLE_TIMEOUT = float(os.getenv("PAGE_LOAD_SETTLE_TIMEOUT", "8.0"))
DEFAULT_PAGE_LOAD_QUIET_MS = int(os.getenv("PAGE_LOAD_QUIET_MS", "500"))
TELEGRAM_MAX_PNG_BYTES = 48 * 1024 * 1024  # 48 MB safety margin under Telegram's 50 MB limit

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

DEVTOOLS_WS_REGEX = re.compile(rb"DevTools listening on (ws://[^\s]+)")


class CDPError(Exception):
    """Raised when Chrome DevTools Protocol returns an error or disconnects unexpectedly."""


class BrowserNotFoundError(CDPError):
    """Raised when no compatible Chromium / chrome-headless-shell binary is found."""


class NavigationError(CDPError):
    """Raised when navigating to a URL fails (DNS, connection refused, TLS, timeout, etc.)."""


@dataclass
class InteractiveElement:
    """Represents a visible, interactive element annotated in the current viewport."""

    id: int
    kind: str  # "link", "button", "input"
    tag: str
    label: str
    x: float
    y: float
    width: float
    height: float
    is_input: bool = False
    input_type: str = ""
    href: str = ""


@dataclass
class ClickResult:
    """Outcome of an interactive click action."""

    success: bool
    message: str
    element: Optional[InteractiveElement] = None
    is_input: bool = False
    input_type: str = ""
    navigated: bool = False
    url: str = ""
    title: str = ""


@dataclass
class FindResult:
    """Outcome of an in-page text search."""

    query: str
    total: int
    current_index: int  # 1-based when total > 0, else 0
    snippet: str = ""


@dataclass
class ScrollInfo:
    """Current vertical scroll metrics for the page."""

    scroll_y: int
    scroll_height: int
    viewport_height: int
    percent: int


@dataclass
class PageLoadStatus:
    """Diagnostic summary returned by the hybrid JS + CDP page-load completion detector."""

    settled: bool
    reason: str  # "settled", "timeout", "error"
    ready_state: str = ""
    elapsed_ms: int = 0
    pending_js_requests: int = 0
    pending_cdp_requests: int = 0
    pending_images: int = 0
    dom_mutations: int = 0
    url: str = ""


def find_browser_executable(base_dir: Optional[Path] = None) -> Tuple[str, bool]:
    """
    Locates the lightest available Chromium-compatible binary.
    Returns (executable_path, is_headless_shell).

    Priority:
      1. BROWSER_EXECUTABLE_PATH environment variable
      2. chrome-headless-shell inside project .browsers/ or PLAYWRIGHT_BROWSERS_PATH
      3. chrome-headless-shell on system PATH
      4. System Chromium / Chrome binaries (/usr/bin/chromium, google-chrome, etc.)
      5. Any full chrome binary inside .browsers/ as last fallback
    """
    env_path = os.getenv("BROWSER_EXECUTABLE_PATH", "").strip()
    if env_path:
        p = Path(env_path).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            is_shell = "headless-shell" in p.name or "headless_shell" in p.name
            return str(p), is_shell
        logger.warning(f"BROWSER_EXECUTABLE_PATH={env_path} is not an executable file; falling back.")

    search_roots: List[Path] = []
    project_dir = base_dir or Path(__file__).resolve().parent
    search_roots.append(project_dir / ".browsers")
    pw_path = os.getenv("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if pw_path:
        p = Path(pw_path).expanduser()
        if not p.is_absolute():
            p = project_dir / p
        if p not in search_roots:
            search_roots.append(p)

    # Prefer ultra-lightweight chrome-headless-shell in local cache
    for root in search_roots:
        if root.is_dir():
            for match in sorted(root.rglob("chrome-headless-shell"), reverse=True):
                if match.is_file() and os.access(match, os.X_OK):
                    return str(match), True

    # Check PATH for chrome-headless-shell first
    shell_on_path = shutil.which("chrome-headless-shell")
    if shell_on_path:
        return shell_on_path, True

    # Check system Chromium / Chrome installations
    for candidate in (
        "chromium",
        "chromium-browser",
        "google-chrome",
        "google-chrome-stable",
        "brave-browser",
        "microsoft-edge",
    ):
        found = shutil.which(candidate)
        if found:
            return found, False

    # Final fallback: full chrome inside local .browsers/
    for root in search_roots:
        if root.is_dir():
            for name in ("chrome", "chromium"):
                for match in sorted(root.rglob(name), reverse=True):
                    if match.is_file() and os.access(match, os.X_OK):
                        return str(match), False

    raise BrowserNotFoundError(
        "Nenhum navegador Chromium / chrome-headless-shell encontrado. "
        "Instale 'chromium' ou defina BROWSER_EXECUTABLE_PATH no arquivo .env."
    )


# JavaScript injected on every new document to avoid webdriver flags, keep popups in the same tab,
# and track page-loading activity (fetch/XHR, DOM mutations, SPA history changes, lifecycle events).
_INIT_SCRIPT = r"""
(() => {
    try {
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    } catch (_) {}
    try {
        const origOpen = window.open;
        window.open = function(url, target, features) {
            if (url && typeof url === 'string' && !url.startsWith('javascript:')) {
                try {
                    window.location.href = new URL(url, window.location.href).href;
                    return window;
                } catch (_) {}
            }
            return origOpen ? origOpen.apply(this, arguments) : null;
        };
    } catch (_) {}

    // Persistent in-page page-load & stability tracker
    try {
        if (window.__chanarcLoadTracker__) return;
        const now = () => (typeof performance !== 'undefined' && performance.now ? performance.now() : Date.now());

        const state = {
            createdAt: now(),
            lastNetworkTs: now(),
            lastDomMutationTs: now(),
            lastNavTs: now(),
            domContentLoadedTs: 0,
            loadTs: 0,
            mutationCount: 0,
            networkCount: 0,
            activeRequests: new Map(), // id -> { startedAt, url }
            _nextReqId: 1,
            _observer: null,
            _observedTarget: null,
            activeRequestCount(staleMs = 4500) {
                const t = now();
                let count = 0;
                for (const [id, info] of this.activeRequests.entries()) {
                    if (t - info.startedAt > staleMs) {
                        // Age out SSE / long-polling / stuck analytics requests
                        this.activeRequests.delete(id);
                    } else {
                        count++;
                    }
                }
                return count;
            },
            markDomMutation() {
                this.lastDomMutationTs = now();
                this.mutationCount++;
            },
            markNav() {
                const t = now();
                this.lastNavTs = t;
                this.lastDomMutationTs = t;
            },
            ensureObserver() {
                startObserver();
            }
        };

        // 1. Track document lifecycle events
        if (document.readyState === 'interactive' || document.readyState === 'complete') {
            state.domContentLoadedTs = now();
        }
        if (document.readyState === 'complete') {
            state.loadTs = now();
        }
        document.addEventListener('DOMContentLoaded', () => {
            state.domContentLoadedTs = now();
            state.markDomMutation();
        }, { once: true });
        window.addEventListener('load', () => {
            const t = now();
            state.loadTs = t;
            state.lastNetworkTs = t;
        }, { once: true });

        // 2. Track fetch() in-flight requests transparently
        if (typeof window.fetch === 'function') {
            const origFetch = window.fetch;
            window.fetch = function(...args) {
                const id = state._nextReqId++;
                const reqUrl = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
                const startedAt = now();
                state.activeRequests.set(id, { startedAt, url: reqUrl });
                state.lastNetworkTs = startedAt;
                state.networkCount++;
                const finish = () => {
                    if (state.activeRequests.delete(id)) {
                        state.lastNetworkTs = now();
                    }
                };
                try {
                    const promise = origFetch.apply(window, args);
                    if (promise && typeof promise.then === 'function') {
                        promise.then(finish, finish);
                    } else {
                        finish();
                    }
                    return promise;
                } catch (err) {
                    finish();
                    throw err;
                }
            };
        }

        // 3. Track XMLHttpRequest in-flight requests transparently
        if (typeof XMLHttpRequest !== 'undefined' && XMLHttpRequest.prototype) {
            const origOpen = XMLHttpRequest.prototype.open;
            const origSend = XMLHttpRequest.prototype.send;
            XMLHttpRequest.prototype.open = function(method, url) {
                try {
                    this.__chanarcUrl = typeof url === 'string' ? url : String(url || '');
                } catch (_) {}
                return origOpen.apply(this, arguments);
            };
            XMLHttpRequest.prototype.send = function() {
                const id = state._nextReqId++;
                const startedAt = now();
                state.activeRequests.set(id, { startedAt, url: this.__chanarcUrl || '' });
                state.lastNetworkTs = startedAt;
                state.networkCount++;
                const finish = () => {
                    if (state.activeRequests.delete(id)) {
                        state.lastNetworkTs = now();
                    }
                };
                try {
                    this.addEventListener('loadend', finish, { once: true });
                    this.addEventListener('error', finish, { once: true });
                    this.addEventListener('abort', finish, { once: true });
                    this.addEventListener('timeout', finish, { once: true });
                } catch (_) {}
                try {
                    return origSend.apply(this, arguments);
                } catch (err) {
                    finish();
                    throw err;
                }
            };
        }

        // 4. Track SPA client-side history navigations
        try {
            const wrapHistory = (fnName) => {
                const orig = history[fnName];
                if (typeof orig !== 'function') return;
                history[fnName] = function() {
                    const res = orig.apply(this, arguments);
                    state.markNav();
                    return res;
                };
            };
            wrapHistory('pushState');
            wrapHistory('replaceState');
            window.addEventListener('popstate', () => state.markNav(), true);
            window.addEventListener('hashchange', () => state.markNav(), true);
        } catch (_) {}

        // 5. Track meaningful DOM mutations (ignoring our own overlays & continuous style animations)
        const isOwnNode = (node) => {
            if (!node || node.nodeType !== 1) return false;
            const id = node.id || '';
            if (id === '__chanarc_overlay_root__' || id === '__chanarc_find_style__') return true;
            if (node.classList && node.classList.contains('__chanarc_find_mark__')) return true;
            if (node.closest && node.closest('#__chanarc_overlay_root__')) return true;
            return false;
        };

        const startObserver = () => {
            const target = document.documentElement || document;
            if (!target || typeof MutationObserver === 'undefined') return;
            if (state._observedTarget === target && state._observer) return;
            if (state._observer) {
                try { state._observer.disconnect(); } catch (_) {}
                state._observer = null;
            }
            try {
                const mo = new MutationObserver((records) => {
                    for (let i = 0; i < records.length; i++) {
                        const r = records[i];
                        if (isOwnNode(r.target)) continue;
                        if (r.type === 'childList') {
                            let meaningful = false;
                            for (const n of r.addedNodes) {
                                if (isOwnNode(n)) continue;
                                if (n.nodeType === 1 || (n.nodeType === 3 && (n.nodeValue || '').trim().length > 0)) {
                                    meaningful = true;
                                    break;
                                }
                            }
                            if (!meaningful) {
                                for (const n of r.removedNodes) {
                                    if (isOwnNode(n)) continue;
                                    if (n.nodeType === 1) {
                                        meaningful = true;
                                        break;
                                    }
                                }
                            }
                            if (meaningful) {
                                state.markDomMutation();
                                return;
                            }
                        } else if (r.type === 'attributes') {
                            state.markDomMutation();
                            return;
                        }
                    }
                });
                mo.observe(target, {
                    childList: true,
                    subtree: true,
                    attributes: true,
                    attributeFilter: ['src', 'srcset', 'data-src', 'aria-busy', 'hidden']
                });
                state._observer = mo;
                state._observedTarget = target;
            } catch (_) {
                state._observer = null;
                state._observedTarget = null;
            }
        };

        if (document.documentElement) {
            startObserver();
        } else {
            document.addEventListener('DOMContentLoaded', startObserver, { once: true });
            document.addEventListener('readystatechange', startObserver);
        }

        Object.defineProperty(window, '__chanarcLoadTracker__', {
            value: state,
            writable: false,
            enumerable: false,
            configurable: false
        });
    } catch (_) {}
})();
"""

# Async JavaScript detector that waits until the page has truly finished loading:
# readyState === 'complete', fetch/XHR & ResourceTiming quiet, DOM mutations & layout height stable,
# visible loading indicators cleared, relevant images loaded & decoded, web fonts ready, and 2 paint frames.
_WAIT_PAGE_LOAD_JS = r"""
(async (opts) => {
    const quietMs = Math.max(80, Number(opts.quietMs ?? 500));
    const maxWaitMs = Math.max(quietMs + 200, Number(opts.maxWaitMs ?? 8000));
    const imgTimeoutMs = Math.max(0, Number(opts.imgTimeoutMs ?? 3000));
    const fontTimeoutMs = Math.max(0, Number(opts.fontTimeoutMs ?? 1500));
    const checkAllImages = Boolean(opts.checkAllImages);
    const waitWindowLoad = opts.waitWindowLoad !== false;
    const minWaitMs = Math.max(0, Number(opts.minWaitMs ?? 0));

    const now = () => (typeof performance !== 'undefined' && performance.now ? performance.now() : Date.now());
    const startTs = now();
    const deadline = startTs + maxWaitMs;
    const remaining = () => Math.max(0, deadline - now());
    const sleep = (ms) => new Promise(r => setTimeout(r, Math.max(0, ms)));

    const tracker = window.__chanarcLoadTracker__ || null;
    if (tracker && typeof tracker.ensureObserver === 'function') {
        try { tracker.ensureObserver(); } catch (_) {}
    }
    const initialMutations = tracker ? tracker.mutationCount : 0;

    // Fallback local MutationObserver if the document predated _INIT_SCRIPT
    let localLastMutationTs = tracker ? tracker.lastDomMutationTs : startTs;
    let localMutationCount = 0;
    let localObserver = null;

    if (!tracker && typeof MutationObserver !== 'undefined' && document.documentElement) {
        try {
            localObserver = new MutationObserver((records) => {
                for (const r of records) {
                    if (r.target && r.target.nodeType === 1 && r.target.id === '__chanarc_overlay_root__') continue;
                    localLastMutationTs = now();
                    localMutationCount++;
                    return;
                }
            });
            localObserver.observe(document.documentElement, {
                childList: true,
                subtree: true,
                attributes: true,
                attributeFilter: ['src', 'srcset', 'data-src', 'aria-busy', 'hidden']
            });
        } catch (_) {}
    }

    const cleanup = () => {
        if (localObserver) {
            try { localObserver.disconnect(); } catch (_) {}
            localObserver = null;
        }
    };

    const getLastResourceEndTs = () => {
        try {
            if (typeof performance === 'undefined' || !performance.getEntriesByType) return 0;
            const entries = performance.getEntriesByType('resource');
            let maxEnd = 0;
            for (let i = entries.length - 1; i >= Math.max(0, entries.length - 40); i--) {
                const e = entries[i];
                if (!e || e.initiatorType === 'beacon') continue;
                if (e.responseEnd && e.responseEnd > maxEnd) {
                    maxEnd = e.responseEnd;
                }
            }
            return maxEnd;
        } catch (_) {
            return 0;
        }
    };

    const getRelevantImages = () => {
        const allImgs = Array.from(document.images || []);
        const vw = window.innerWidth || 1280;
        const vh = window.innerHeight || 900;
        const relevant = [];
        for (const img of allImgs) {
            const src = img.currentSrc || img.getAttribute('src') || '';
            if (!src || src.startsWith('data:image/gif;base64,R0lGOD')) continue;
            const attrW = parseInt(img.getAttribute('width') || '', 10);
            const attrH = parseInt(img.getAttribute('height') || '', 10);
            if ((attrW === 1 && attrH === 1) || (attrW === 0 && attrH === 0)) continue;
            const cs = window.getComputedStyle(img);
            if (!cs || cs.display === 'none' || cs.visibility === 'hidden') continue;
            const rect = img.getBoundingClientRect();
            if (checkAllImages) {
                if (img.loading === 'lazy' && !img.complete && rect.width === 0 && rect.height === 0) continue;
                relevant.push(img);
            } else {
                const nearViewport = rect.bottom >= -150 && rect.top <= vh + 150 && rect.right >= -150 && rect.left <= vw + 150;
                const hasSizeOrUnloaded = (rect.width > 0 && rect.height > 0) || !img.complete;
                if (nearViewport && hasSizeOrUnloaded) {
                    relevant.push(img);
                }
            }
        }
        return relevant;
    };

    const hasVisibleLoader = () => {
        try {
            const vh = window.innerHeight || 900;
            const vw = window.innerWidth || 1280;
            const nodes = document.querySelectorAll('[aria-busy="true"], [role="progressbar"]:not([aria-valuenow])');
            for (const el of nodes) {
                if (el.closest('video, audio')) continue;
                const cs = window.getComputedStyle(el);
                if (!cs || cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity || '1') < 0.05) continue;
                const r = el.getBoundingClientRect();
                if (r.width >= 4 && r.height >= 4 && r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw) {
                    return true;
                }
            }
        } catch (_) {}
        return false;
    };

    const waitPaintFrames = () => new Promise((resolve) => {
        let done = false;
        const finish = () => {
            if (!done) {
                done = true;
                resolve();
            }
        };
        const timer = setTimeout(finish, 300);
        if (typeof requestAnimationFrame === 'function') {
            requestAnimationFrame(() => {
                requestAnimationFrame(() => {
                    clearTimeout(timer);
                    finish();
                });
            });
        } else {
            clearTimeout(timer);
            finish();
        }
    });

    try {
        // Phase 1: Wait for document.readyState === 'complete' when requested
        if (waitWindowLoad && document.readyState !== 'complete') {
            const loadBudget = Math.min(remaining(), Math.max(1500, maxWaitMs * 0.65));
            if (loadBudget > 0) {
                await new Promise((resolve) => {
                    if (document.readyState === 'complete') return resolve();
                    let finished = false;
                    const done = () => {
                        if (finished) return;
                        finished = true;
                        window.removeEventListener('load', done);
                        document.removeEventListener('readystatechange', onState);
                        clearTimeout(t);
                        resolve();
                    };
                    const onState = () => {
                        if (document.readyState === 'complete') done();
                    };
                    window.addEventListener('load', done, { once: true });
                    document.addEventListener('readystatechange', onState);
                    const t = setTimeout(done, loadBudget);
                });
            }
        }

        if (minWaitMs > 0) {
            const leftMin = minWaitMs - (now() - startTs);
            if (leftMin > 0) await sleep(Math.min(leftMin, remaining()));
        }

        // Phase 2: Wait for DOM, fetch/XHR, resources, layout height, images, and busy loaders to settle
        let lastHeight = Math.max(
            document.body ? document.body.scrollHeight : 0,
            document.documentElement ? document.documentElement.scrollHeight : 0
        );
        let lastLayoutChangeTs = startTs;
        let settled = false;
        const loaderBudgetEnd = startTs + maxWaitMs * 0.75;
        const imgDeadline = Math.min(deadline, startTs + imgTimeoutMs);

        while (now() < deadline) {
            const t = now();

            const curHeight = Math.max(
                document.body ? document.body.scrollHeight : 0,
                document.documentElement ? document.documentElement.scrollHeight : 0
            );
            if (curHeight !== lastHeight) {
                lastHeight = curHeight;
                lastLayoutChangeTs = t;
            }

            const docReady = !waitWindowLoad
                ? document.readyState !== 'loading'
                : (document.readyState === 'complete' || (document.readyState === 'interactive' && t > loaderBudgetEnd));

            const pendingReqs = tracker ? tracker.activeRequestCount(4500) : 0;
            const lastNetTs = Math.max(
                tracker ? tracker.lastNetworkTs : 0,
                getLastResourceEndTs()
            );
            const lastMutTs = Math.max(
                tracker ? tracker.lastDomMutationTs : localLastMutationTs,
                tracker ? tracker.lastNavTs : 0,
                lastLayoutChangeTs
            );

            const netQuiet = pendingReqs === 0 && (t - lastNetTs) >= quietMs;
            const domQuiet = (t - lastMutTs) >= quietMs;
            const loaderBlocking = t < loaderBudgetEnd && hasVisibleLoader();

            let pendingImgs = 0;
            if (imgTimeoutMs > 0 && t < imgDeadline) {
                const imgs = getRelevantImages();
                for (const img of imgs) {
                    if (!img.complete) pendingImgs++;
                }
            }

            if (docReady && netQuiet && domQuiet && !loaderBlocking && pendingImgs === 0) {
                settled = true;
                break;
            }

            await sleep(Math.min(80, remaining()));
        }

        // Phase 3: Decode completed relevant images so bitmaps are ready for capture (bounded)
        if (imgTimeoutMs > 0 && remaining() > 100) {
            const decodeBudget = Math.min(1200, remaining());
            try {
                const imgs = getRelevantImages();
                const decodePromises = imgs.map((img) => {
                    if (img.complete) {
                        return typeof img.decode === 'function' ? img.decode().catch(() => {}) : Promise.resolve();
                    }
                    return new Promise((r) => {
                        const done = () => r();
                        img.addEventListener('load', done, { once: true });
                        img.addEventListener('error', done, { once: true });
                    });
                });
                if (decodePromises.length > 0) {
                    await Promise.race([Promise.all(decodePromises), sleep(decodeBudget)]);
                }
            } catch (_) {}
        }

        // Phase 4: Wait for web fonts (bounded)
        if (fontTimeoutMs > 0 && document.fonts && document.fonts.ready && remaining() > 80) {
            try {
                await Promise.race([document.fonts.ready, sleep(Math.min(fontTimeoutMs, remaining()))]);
            } catch (_) {}
        }

        // Phase 5: Wait for browser paint commit (2 consecutive animation frames)
        await waitPaintFrames();

        const finalImgs = getRelevantImages().filter(img => !img.complete).length;
        return {
            settled,
            reason: settled ? 'settled' : 'timeout',
            readyState: document.readyState,
            elapsedMs: Math.round(now() - startTs),
            pendingJsRequests: tracker ? tracker.activeRequestCount(4500) : 0,
            pendingImages: finalImgs,
            domMutations: tracker ? (tracker.mutationCount - initialMutations) : localMutationCount,
            url: window.location.href
        };
    } finally {
        cleanup();
    }
})
"""

# Lightweight paint flush helper (2 animation frames with fallback timer)
_WAIT_PAINT_FRAMES_JS = r"""
new Promise((resolve) => {
    let done = false;
    const finish = () => { if (!done) { done = true; resolve(true); } };
    const t = setTimeout(finish, 250);
    if (typeof requestAnimationFrame === 'function') {
        requestAnimationFrame(() => requestAnimationFrame(() => { clearTimeout(t); finish(); }));
    } else {
        clearTimeout(t);
        finish();
    }
})
"""

# JavaScript to annotate visible interactive elements with color-coded numbered badges
_TAG_ELEMENTS_JS = r"""
((showOverlay, maxElements) => {
    // Clean any previous overlay or markers
    const oldRoot = document.getElementById('__chanarc_overlay_root__');
    if (oldRoot) oldRoot.remove();
    document.querySelectorAll('[data-chanarc-id]').forEach(el => el.removeAttribute('data-chanarc-id'));

    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const selector = [
        'a[href]',
        'button',
        'input:not([type="hidden"])',
        'textarea',
        'select',
        'summary',
        '[role="button"]',
        '[role="link"]',
        '[role="tab"]',
        '[role="menuitem"]',
        '[role="textbox"]',
        '[role="searchbox"]',
        '[role="combobox"]',
        '[contenteditable="true"]',
        '[contenteditable=""]',
        '[onclick]'
    ].join(',');

    const candidates = Array.from(document.querySelectorAll(selector));
    const items = [];
    const chosenElements = new Set();

    function isVisible(el) {
        const style = window.getComputedStyle(el);
        if (!style || style.display === 'none' || style.visibility === 'hidden') return false;
        if (parseFloat(style.opacity || '1') < 0.05) return false;
        if (el.disabled) return false;
        const rect = el.getBoundingClientRect();
        if (rect.width < 8 || rect.height < 8) return false;
        // Must have center or substantial intersection inside viewport
        const cx = Math.max(0, Math.min(vw - 1, rect.left + rect.width / 2));
        const cy = Math.max(0, Math.min(vh - 1, rect.top + rect.height / 2));
        if (rect.bottom < 2 || rect.top > vh - 2 || rect.right < 2 || rect.left > vw - 2) return false;
        const topEl = document.elementFromPoint(cx, cy);
        if (topEl && topEl !== el && !el.contains(topEl) && !topEl.contains(el)) {
            // Check another point closer to top-left in case badge/icon overlaps center
            const ax = Math.max(1, Math.min(vw - 2, rect.left + Math.min(12, rect.width / 3)));
            const ay = Math.max(1, Math.min(vh - 2, rect.top + Math.min(12, rect.height / 3)));
            const altEl = document.elementFromPoint(ax, ay);
            if (altEl && altEl !== el && !el.contains(altEl) && !altEl.contains(el)) {
                return false;
            }
        }
        return true;
    }

    function classify(el) {
        const tag = el.tagName.toLowerCase();
        const role = (el.getAttribute('role') || '').toLowerCase();
        const type = (el.getAttribute('type') || '').toLowerCase();

        if (tag === 'textarea' || el.isContentEditable || role === 'textbox' || role === 'searchbox' || role === 'combobox') {
            return { kind: 'input', isInput: true, inputType: tag === 'textarea' ? 'textarea' : 'text' };
        }
        if (tag === 'input') {
            if (['submit', 'button', 'reset', 'checkbox', 'radio', 'image', 'file'].includes(type)) {
                return { kind: 'button', isInput: false, inputType: type };
            }
            return { kind: 'input', isInput: true, inputType: type || 'text' };
        }
        if (tag === 'button' || tag === 'select' || tag === 'summary' || role === 'button' || role === 'tab') {
            return { kind: 'button', isInput: false, inputType: tag === 'select' ? 'select' : '' };
        }
        return { kind: 'link', isInput: false, inputType: '' };
    }

    function extractLabel(el) {
        const tag = el.tagName.toLowerCase();
        let text = '';
        if (tag === 'input' || tag === 'textarea') {
            text = el.getAttribute('aria-label')
                || el.getAttribute('placeholder')
                || el.getAttribute('title')
                || (el.type === 'submit' || el.type === 'button' ? el.value : '')
                || el.getAttribute('name')
                || '';
            if (!text && el.id) {
                const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                if (lbl) text = lbl.innerText || '';
            }
        } else {
            text = (el.innerText || el.textContent || '').trim();
            if (!text) {
                text = el.getAttribute('aria-label')
                    || el.getAttribute('title')
                    || '';
            }
            if (!text) {
                const img = el.querySelector('img[alt]');
                if (img) text = img.getAttribute('alt') || '';
            }
            if (!text && tag === 'a') {
                try {
                    const u = new URL(el.href, window.location.href);
                    text = u.pathname && u.pathname !== '/' ? u.pathname : u.hostname;
                } catch (_) {
                    text = el.getAttribute('href') || '';
                }
            }
        }
        text = text.replace(/\s+/g, ' ').trim();
        if (text.length > 48) text = text.slice(0, 45) + '...';
        return text || `<${tag}>`;
    }

    // Collect visible elements, avoiding duplicates where a child is inside an already-selected clickable
    for (const el of candidates) {
        if (!isVisible(el)) continue;
        // Skip if a direct ancestor is already selected (unless this is an input inside a form/container)
        const cls = classify(el);
        let hasChosenAncestor = false;
        let parent = el.parentElement;
        while (parent) {
            if (chosenElements.has(parent)) {
                hasChosenAncestor = true;
                break;
            }
            parent = parent.parentElement;
        }
        if (hasChosenAncestor && !cls.isInput) continue;

        const rect = el.getBoundingClientRect();
        // Avoid overlapping practically identical rects (e.g. wrapper + inner link)
        const cx = rect.left + rect.width / 2;
        const cy = rect.top + rect.height / 2;
        const duplicate = items.some(it => Math.abs(it.x - cx) < 10 && Math.abs(it.y - cy) < 10);
        if (duplicate) continue;

        chosenElements.add(el);
        items.push({
            el,
            kind: cls.kind,
            is_input: cls.isInput,
            input_type: cls.inputType,
            tag: el.tagName.toLowerCase(),
            label: extractLabel(el),
            href: el.tagName.toLowerCase() === 'a' ? (el.href || '') : '',
            rect,
            x: cx,
            y: cy,
            width: rect.width,
            height: rect.height
        });
    }

    // Sort in natural reading order (top-to-bottom banded by 16px, then left-to-right)
    items.sort((a, b) => {
        const bandA = Math.floor(a.rect.top / 18);
        const bandB = Math.floor(b.rect.top / 18);
        if (bandA !== bandB) return bandA - bandB;
        return a.rect.left - b.rect.left;
    });

    const selected = items.slice(0, maxElements);
    const colors = {
        link:   { bg: '#0D47A1', border: '#1976D2' },
        button: { bg: '#1B5E20', border: '#2E7D32' },
        input:  { bg: '#E65100', border: '#F57C00' }
    };

    let overlayRoot = null;
    if (showOverlay && selected.length > 0) {
        overlayRoot = document.createElement('div');
        overlayRoot.id = '__chanarc_overlay_root__';
        overlayRoot.style.cssText = [
            'position: fixed',
            'inset: 0',
            'pointer-events: none',
            'z-index: 2147483647',
            'overflow: hidden'
        ].join(';');
        document.documentElement.appendChild(overlayRoot);
    }

    const result = [];
    selected.forEach((item, idx) => {
        const id = idx + 1;
        item.el.setAttribute('data-chanarc-id', String(id));

        if (overlayRoot) {
            const c = colors[item.kind] || colors.link;
            const r = item.rect;

            // Subtle bounding outline
            const box = document.createElement('div');
            box.style.cssText = [
                'position: fixed',
                `left: ${Math.max(0, r.left)}px`,
                `top: ${Math.max(0, r.top)}px`,
                `width: ${Math.min(vw - Math.max(0, r.left), r.width)}px`,
                `height: ${Math.min(vh - Math.max(0, r.top), r.height)}px`,
                `border: 1.5px solid ${c.border}`,
                'border-radius: 3px',
                'box-sizing: border-box',
                'pointer-events: none',
                'background: rgba(255,255,255,0.03)'
            ].join(';');
            overlayRoot.appendChild(box);

            // Crisp numbered badge
            const badge = document.createElement('div');
            badge.textContent = String(id);
            const badgeLeft = Math.max(2, Math.min(vw - 26, r.left - 2));
            const badgeTop = Math.max(2, Math.min(vh - 20, r.top - 10));
            badge.style.cssText = [
                'position: fixed',
                `left: ${badgeLeft}px`,
                `top: ${badgeTop}px`,
                `background: ${c.bg}`,
                'color: #FFFFFF',
                'font: bold 11px/15px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
                'padding: 1px 5px',
                'border-radius: 4px',
                'border: 1px solid rgba(255,255,255,0.9)',
                'box-shadow: 0 1px 3px rgba(0,0,0,0.55)',
                'pointer-events: none',
                'white-space: nowrap'
            ].join(';');
            overlayRoot.appendChild(badge);
        }

        result.push({
            id,
            kind: item.kind,
            tag: item.tag,
            label: item.label,
            x: Math.round(item.x * 10) / 10,
            y: Math.round(item.y * 10) / 10,
            width: Math.round(item.width),
            height: Math.round(item.height),
            is_input: item.is_input,
            input_type: item.input_type,
            href: item.href
        });
    });

    return result;
})
"""

_CLEAR_OVERLAYS_JS = r"""
(() => {
    const root = document.getElementById('__chanarc_overlay_root__');
    if (root) root.remove();
})()
"""

_FIND_IN_PAGE_JS = r"""
((query) => {
    // Clear existing highlights first
    document.querySelectorAll('mark.__chanarc_find_mark__').forEach(mark => {
        const parent = mark.parentNode;
        if (parent) {
            parent.replaceChild(document.createTextNode(mark.textContent || ''), mark);
            parent.normalize();
        }
    });
    const oldStyle = document.getElementById('__chanarc_find_style__');
    if (oldStyle) oldStyle.remove();

    if (!query || !query.trim()) {
        return { total: 0, current_index: 0, snippet: '' };
    }

    const style = document.createElement('style');
    style.id = '__chanarc_find_style__';
    style.textContent = `
        mark.__chanarc_find_mark__ {
            background: #FFEB3B !important;
            color: #000000 !important;
            padding: 0 2px !important;
            border-radius: 2px !important;
            box-shadow: 0 0 0 1px #FBC02D !important;
        }
        mark.__chanarc_find_mark__.__chanarc_find_active__ {
            background: #FF6D00 !important;
            color: #FFFFFF !important;
            box-shadow: 0 0 0 3px rgba(255, 109, 0, 0.55) !important;
            font-weight: bold !important;
        }
    `;
    document.head.appendChild(style);

    const lowerQuery = query.toLowerCase();
    const skipTags = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'IFRAME', 'TEXTAREA', 'INPUT', 'SELECT']);
    const walker = document.createTreeWalker(
        document.body || document.documentElement,
        NodeFilter.SHOW_TEXT,
        {
            acceptNode(node) {
                if (!node.nodeValue || !node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
                const parent = node.parentElement;
                if (!parent) return NodeFilter.FILTER_REJECT;
                if (skipTags.has(parent.tagName)) return NodeFilter.FILTER_REJECT;
                if (parent.closest('#__chanarc_overlay_root__')) return NodeFilter.FILTER_REJECT;
                const cs = window.getComputedStyle(parent);
                if (cs.display === 'none' || cs.visibility === 'hidden') return NodeFilter.FILTER_REJECT;
                if (node.nodeValue.toLowerCase().includes(lowerQuery)) return NodeFilter.FILTER_ACCEPT;
                return NodeFilter.FILTER_SKIP;
            }
        }
    );

    const matchingNodes = [];
    let current;
    while ((current = walker.nextNode())) {
        matchingNodes.push(current);
        if (matchingNodes.length > 500) break;
    }

    const marks = [];
    const qLen = query.length;
    for (const textNode of matchingNodes) {
        const text = textNode.nodeValue || '';
        const lower = text.toLowerCase();
        let start = 0;
        let idx = lower.indexOf(lowerQuery, start);
        if (idx === -1) continue;

        const frag = document.createDocumentFragment();
        while (idx !== -1) {
            if (idx > start) {
                frag.appendChild(document.createTextNode(text.slice(start, idx)));
            }
            const mark = document.createElement('mark');
            mark.className = '__chanarc_find_mark__';
            mark.textContent = text.slice(idx, idx + qLen);
            frag.appendChild(mark);
            marks.push(mark);

            start = idx + qLen;
            idx = lower.indexOf(lowerQuery, start);
            if (marks.length >= 300) break;
        }
        if (start < text.length) {
            frag.appendChild(document.createTextNode(text.slice(start)));
        }
        if (textNode.parentNode) {
            textNode.parentNode.replaceChild(frag, textNode);
        }
        if (marks.length >= 300) break;
    }

    if (marks.length === 0) {
        return { total: 0, current_index: 0, snippet: '' };
    }

    marks[0].classList.add('__chanarc_find_active__');
    marks[0].scrollIntoView({ block: 'center', behavior: 'instant' });
    const ctx = (marks[0].parentElement ? marks[0].parentElement.innerText : marks[0].textContent) || '';
    return {
        total: marks.length,
        current_index: 1,
        snippet: ctx.replace(/\s+/g, ' ').trim().slice(0, 120)
    };
})
"""

_FIND_STEP_JS = r"""
((targetIndex) => {
    const marks = Array.from(document.querySelectorAll('mark.__chanarc_find_mark__'));
    if (marks.length === 0) return { total: 0, current_index: 0, snippet: '' };
    marks.forEach(m => m.classList.remove('__chanarc_find_active__'));
    const normIdx = ((targetIndex - 1) % marks.length + marks.length) % marks.length;
    const active = marks[normIdx];
    active.classList.add('__chanarc_find_active__');
    active.scrollIntoView({ block: 'center', behavior: 'instant' });
    const ctx = (active.parentElement ? active.parentElement.innerText : active.textContent) || '';
    return {
        total: marks.length,
        current_index: normIdx + 1,
        snippet: ctx.replace(/\s+/g, ' ').trim().slice(0, 120)
    };
})
"""

_CLEAR_FIND_JS = r"""
(() => {
    document.querySelectorAll('mark.__chanarc_find_mark__').forEach(mark => {
        const parent = mark.parentNode;
        if (parent) {
            parent.replaceChild(document.createTextNode(mark.textContent || ''), mark);
            parent.normalize();
        }
    });
    const oldStyle = document.getElementById('__chanarc_find_style__');
    if (oldStyle) oldStyle.remove();
})()
"""


KEY_DEFINITIONS: Dict[str, Dict[str, Any]] = {
    "Enter": {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "text": "\r"},
    "Tab": {"key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9, "text": ""},
    "Escape": {"key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27, "text": ""},
    "Backspace": {"key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8, "text": ""},
    "Delete": {"key": "Delete", "code": "Delete", "windowsVirtualKeyCode": 46, "text": ""},
    "ArrowDown": {"key": "ArrowDown", "code": "ArrowDown", "windowsVirtualKeyCode": 40, "text": ""},
    "ArrowUp": {"key": "ArrowUp", "code": "ArrowUp", "windowsVirtualKeyCode": 38, "text": ""},
    "ArrowLeft": {"key": "ArrowLeft", "code": "ArrowLeft", "windowsVirtualKeyCode": 37, "text": ""},
    "ArrowRight": {"key": "ArrowRight", "code": "ArrowRight", "windowsVirtualKeyCode": 39, "text": ""},
    "PageDown": {"key": "PageDown", "code": "PageDown", "windowsVirtualKeyCode": 34, "text": ""},
    "PageUp": {"key": "PageUp", "code": "PageUp", "windowsVirtualKeyCode": 33, "text": ""},
    "Home": {"key": "Home", "code": "Home", "windowsVirtualKeyCode": 36, "text": ""},
    "End": {"key": "End", "code": "End", "windowsVirtualKeyCode": 35, "text": ""},
    "Space": {"key": " ", "code": "Space", "windowsVirtualKeyCode": 32, "text": " "},
}


class CDPBrowser:
    """
    Lightweight persistent headless Chromium/chrome-headless-shell manager using native CDP.
    Maintains ONE WebSocket connection and multiplexes isolated BrowserContexts/tabs.
    """

    def __init__(self, executable_path: Optional[str] = None):
        self._explicit_executable = executable_path
        self._process: Optional[asyncio.subprocess.Process] = None
        self._user_data_dir: Optional[tempfile.TemporaryDirectory] = None
        self._ws: Optional[ClientConnection] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_drain_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._msg_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._listeners: Dict[Tuple[Optional[str], str], List[Callable[[Dict[str, Any]], None]]] = {}
        self._closed = False
        self.executable_path: Optional[str] = None
        self.is_headless_shell: bool = False

    def is_connected(self) -> bool:
        return (
            self._process is not None
            and self._process.returncode is None
            and self._ws is not None
            and self._reader_task is not None
            and not self._reader_task.done()
        )

    async def ensure_started(self) -> None:
        """Starts or transparently restarts the headless browser if needed."""
        async with self._lock:
            if self.is_connected():
                return
            await self._cleanup_unlocked()
            self._closed = False

            if self._explicit_executable:
                exe = self._explicit_executable
                is_shell = "headless-shell" in Path(exe).name or "headless_shell" in Path(exe).name
            else:
                exe, is_shell = find_browser_executable()

            self.executable_path = exe
            self.is_headless_shell = is_shell
            self._user_data_dir = tempfile.TemporaryDirectory(prefix="cdp_profile_")

            args: List[str] = [exe]
            if not is_shell:
                args.append("--headless=new")

            args.extend([
                "--remote-debugging-port=0",
                "--remote-allow-origins=*",
                f"--user-data-dir={self._user_data_dir.name}",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-breakpad",
                "--disable-component-update",
                "--disable-default-apps",
                "--disable-domain-reliability",
                "--disable-features=Translate,BackForwardCache,AcceptCHFrame,MediaRouter,OptimizationHints",
                "--disable-ipc-flooding-protection",
                "--disable-renderer-backgrounding",
                "--disable-sync",
                "--metrics-recording-only",
                "--mute-audio",
                "--no-first-run",
                "--no-default-browser-check",
                "--password-store=basic",
                "--use-mock-keychain",
                "--hide-scrollbars",
                "--font-render-hinting=medium",
                "--renderer-process-limit=2",
                "--js-flags=--max-old-space-size=256",
                f"--window-size={DEFAULT_VIEWPORT_WIDTH},{DEFAULT_VIEWPORT_HEIGHT}",
            ])

            logger.info(f"Launching native CDP engine: {exe} (headless_shell={is_shell})")
            self._process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )

            ws_url = await self._wait_for_ws_endpoint(timeout=20.0)
            logger.info(f"Connected to CDP endpoint: {ws_url}")

            self._ws = await ws_connect(
                ws_url,
                max_size=128 * 1024 * 1024,  # 128 MB for HiDPI full-page PNG frames
                ping_interval=20,
                ping_timeout=20,
            )
            self._reader_task = asyncio.create_task(self._read_loop(), name="cdp-ws-reader")
            # Keep draining stderr so the browser never blocks on a full pipe buffer
            self._stderr_drain_task = asyncio.create_task(self._drain_stderr(), name="cdp-stderr-drain")

    async def _wait_for_ws_endpoint(self, timeout: float = 20.0) -> str:
        """Reads browser stderr until the DevTools WebSocket URL is emitted."""
        assert self._process is not None and self._process.stderr is not None
        stderr_lines: List[str] = []

        async def _read_until_ws() -> str:
            assert self._process is not None and self._process.stderr is not None
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    rc = await self._process.wait()
                    tail = "\n".join(stderr_lines[-10:])
                    raise CDPError(f"Browser exited prematurely (code {rc}) before opening CDP.\n{tail}")
                match = DEVTOOLS_WS_REGEX.search(line)
                if match:
                    return match.group(1).decode("ascii")
                decoded = line.decode("utf-8", errors="replace").strip()
                if decoded:
                    stderr_lines.append(decoded)

        try:
            return await asyncio.wait_for(_read_until_ws(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            tail = "\n".join(stderr_lines[-10:])
            raise CDPError(f"Timed out waiting for browser CDP endpoint.\n{tail}") from exc

    async def _drain_stderr(self) -> None:
        if not self._process or not self._process.stderr:
            return
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _read_loop(self) -> None:
        """Receives CDP responses and events over the single multiplexed WebSocket."""
        assert self._ws is not None
        try:
            async for raw_message in self._ws:
                try:
                    data = json.loads(raw_message)
                except Exception:
                    continue

                if "id" in data:
                    msg_id = data["id"]
                    fut = self._pending.pop(msg_id, None)
                    if fut and not fut.done():
                        if "error" in data:
                            err = data["error"]
                            fut.set_exception(
                                CDPError(f"CDP error {err.get('code')}: {err.get('message')}")
                            )
                        else:
                            fut.set_result(data.get("result", {}))
                elif "method" in data:
                    method = data["method"]
                    session_id = data.get("sessionId")
                    params = data.get("params", {})
                    for key in ((session_id, method), (session_id, "*")):
                        for cb in list(self._listeners.get(key, ())):
                            try:
                                cb(params)
                            except Exception as exc:
                                logger.debug(f"CDP event listener error ({method}): {exc}")
        except (ConnectionClosed, asyncio.CancelledError):
            pass
        except Exception as exc:
            logger.warning(f"CDP WebSocket reader terminated with error: {exc}")
        finally:
            # Fail any in-flight RPCs immediately
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(CDPError("CDP connection closed."))
            self._pending.clear()

    def add_listener(
        self,
        session_id: Optional[str],
        method: str,
        callback: Callable[[Dict[str, Any]], None],
    ) -> None:
        self._listeners.setdefault((session_id, method), []).append(callback)

    def remove_session_listeners(self, session_id: str) -> None:
        to_delete = [k for k in self._listeners if k[0] == session_id]
        for k in to_delete:
            del self._listeners[k]

    async def send(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        timeout: float = 30.0,
    ) -> Dict[str, Any]:
        """Sends a raw CDP command and awaits its JSON result."""
        if not self.is_connected():
            await self.ensure_started()
        assert self._ws is not None

        self._msg_id += 1
        msg_id = self._msg_id
        payload: Dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            payload["params"] = params
        if session_id:
            payload["sessionId"] = session_id

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[msg_id] = fut

        try:
            await self._ws.send(json.dumps(payload))
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(msg_id, None)
            raise CDPError(f"CDP command '{method}' timed out after {timeout}s") from exc
        except Exception:
            self._pending.pop(msg_id, None)
            raise

    async def new_page(
        self,
        viewport_width: int = DEFAULT_VIEWPORT_WIDTH,
        viewport_height: int = DEFAULT_VIEWPORT_HEIGHT,
        device_scale_factor: float = DEFAULT_DEVICE_SCALE_FACTOR,
    ) -> "CDPPage":
        """Creates an isolated incognito BrowserContext and attached CDPPage."""
        await self.ensure_started()

        ctx_res = await self.send("Target.createBrowserContext", {"disposeOnDetach": True})
        browser_context_id = ctx_res["browserContextId"]

        try:
            target_res = await self.send(
                "Target.createTarget",
                {
                    "url": "about:blank",
                    "browserContextId": browser_context_id,
                    "width": viewport_width,
                    "height": viewport_height,
                },
            )
            target_id = target_res["targetId"]

            attach_res = await self.send(
                "Target.attachToTarget",
                {"targetId": target_id, "flatten": True},
            )
            session_id = attach_res["sessionId"]

            page = CDPPage(
                browser=self,
                browser_context_id=browser_context_id,
                target_id=target_id,
                session_id=session_id,
                viewport_width=viewport_width,
                viewport_height=viewport_height,
                device_scale_factor=device_scale_factor,
            )
            await page._initialize()
            return page
        except Exception:
            try:
                await self.send("Target.disposeBrowserContext", {"browserContextId": browser_context_id})
            except Exception:
                pass
            raise

    async def _cleanup_unlocked(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except Exception:
                pass
            self._reader_task = None

        if self._stderr_drain_task:
            self._stderr_drain_task.cancel()
            try:
                await self._stderr_drain_task
            except Exception:
                pass
            self._stderr_drain_task = None

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        if self._process:
            if self._process.returncode is None:
                try:
                    self._process.terminate()
                    await asyncio.wait_for(self._process.wait(), timeout=5.0)
                except Exception:
                    try:
                        self._process.kill()
                        await self._process.wait()
                    except Exception:
                        pass
            self._process = None

        if self._user_data_dir:
            try:
                self._user_data_dir.cleanup()
            except Exception:
                pass
            self._user_data_dir = None

        self._listeners.clear()
        self._pending.clear()

    async def close(self) -> None:
        """Cleanly shuts down the headless browser and temporary user profile."""
        async with self._lock:
            self._closed = True
            await self._cleanup_unlocked()
        logger.info("Native CDP browser closed.")


class CDPPage:
    """
    High-level async controller for an isolated browser tab over native CDP.
    Supports navigation, full-page & viewport HiDPI PNG screenshots, DOM-aligned slicing,
    interactive click/type/scroll/reload/history, and in-page text search.
    """

    def __init__(
        self,
        browser: CDPBrowser,
        browser_context_id: str,
        target_id: str,
        session_id: str,
        viewport_width: int = DEFAULT_VIEWPORT_WIDTH,
        viewport_height: int = DEFAULT_VIEWPORT_HEIGHT,
        device_scale_factor: float = DEFAULT_DEVICE_SCALE_FACTOR,
    ):
        self.browser = browser
        self.browser_context_id = browser_context_id
        self.target_id = target_id
        self.session_id = session_id
        self.viewport_width = viewport_width
        self.viewport_height = viewport_height
        self.device_scale_factor = device_scale_factor
        self.closed = False

        # Lifecycle & network quiet tracking
        self._main_frame_id: Optional[str] = None
        self._nav_generation: int = 0
        self._load_event = asyncio.Event()
        self._dom_event = asyncio.Event()
        self._network_idle_event = asyncio.Event()
        self._frame_started_event = asyncio.Event()
        self._spa_nav_event = asyncio.Event()
        self._inflight_requests: Dict[str, Tuple[float, str]] = {}
        self._last_network_activity: float = 0.0
        self._current_url: str = "about:blank"
        self.last_load_status: Optional[PageLoadStatus] = None

        # Cache of last collected interactive elements
        self.elements: List[InteractiveElement] = []

    async def _send(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 30.0,
    ) -> Dict[str, Any]:
        if self.closed:
            raise CDPError("CDPPage is already closed.")
        return await self.browser.send(method, params=params, session_id=self.session_id, timeout=timeout)

    async def _initialize(self) -> None:
        """Enables core CDP domains, event tracking, stealth/load-tracker script, and viewport metrics."""
        self.browser.add_listener(self.session_id, "Page.loadEventFired", self._on_load_event)
        self.browser.add_listener(self.session_id, "Page.domContentEventFired", self._on_dom_event)
        self.browser.add_listener(self.session_id, "Page.lifecycleEvent", self._on_lifecycle_event)
        self.browser.add_listener(self.session_id, "Page.frameStartedLoading", self._on_frame_started)
        self.browser.add_listener(self.session_id, "Page.frameNavigated", self._on_frame_navigated)
        self.browser.add_listener(self.session_id, "Page.navigatedWithinDocument", self._on_navigated_within_document)
        self.browser.add_listener(self.session_id, "Network.requestWillBeSent", self._on_req_start)
        self.browser.add_listener(self.session_id, "Network.loadingFinished", self._on_req_end)
        self.browser.add_listener(self.session_id, "Network.loadingFailed", self._on_req_end)
        # Auto-dismiss JS alert/confirm/prompt dialogs so interactive browsing never deadlocks
        self.browser.add_listener(self.session_id, "Page.javascriptDialogOpening", self._on_js_dialog)

        await asyncio.gather(
            self._send("Page.enable"),
            self._send("Runtime.enable"),
            self._send("Network.enable"),
            self._send("DOM.enable"),
        )
        try:
            await self._send("Page.setLifecycleEventsEnabled", {"enabled": True})
        except Exception:
            pass
        try:
            tree = await self._send("Page.getFrameTree")
            self._main_frame_id = tree.get("frameTree", {}).get("frame", {}).get("id")
        except Exception:
            pass

        await self._send(
            "Network.setUserAgentOverride",
            {
                "userAgent": DEFAULT_USER_AGENT,
                "acceptLanguage": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
                "platform": "Linux x86_64",
            },
        )
        await self._send("Page.addScriptToEvaluateOnNewDocument", {"source": _INIT_SCRIPT})
        await self.set_viewport(self.viewport_width, self.viewport_height, self.device_scale_factor)

    def _reset_load_signals(self, clear_inflight: bool = False) -> None:
        self._load_event.clear()
        self._dom_event.clear()
        self._network_idle_event.clear()
        self._frame_started_event.clear()
        self._spa_nav_event.clear()
        if clear_inflight:
            self._inflight_requests.clear()

    def _active_cdp_request_count(self, stale_sec: float = 5.0) -> int:
        """Returns number of non-stale blocking CDP network requests currently in flight."""
        now = asyncio.get_running_loop().time()
        stale_ids = [
            req_id
            for req_id, (started_at, _rtype) in self._inflight_requests.items()
            if (now - started_at) > stale_sec
        ]
        for req_id in stale_ids:
            self._inflight_requests.pop(req_id, None)
        return len(self._inflight_requests)

    def _on_load_event(self, _params: Dict[str, Any]) -> None:
        self._load_event.set()

    def _on_dom_event(self, _params: Dict[str, Any]) -> None:
        self._dom_event.set()

    def _on_lifecycle_event(self, params: Dict[str, Any]) -> None:
        if self._main_frame_id and params.get("frameId") != self._main_frame_id:
            return
        name = params.get("name", "")
        if name == "networkIdle":
            self._network_idle_event.set()
        elif name == "load":
            self._load_event.set()
        elif name == "DOMContentLoaded":
            self._dom_event.set()

    def _on_frame_started(self, params: Dict[str, Any]) -> None:
        frame_id = params.get("frameId")
        if not self._main_frame_id or frame_id == self._main_frame_id:
            self._nav_generation += 1
            self._frame_started_event.set()
            self._network_idle_event.clear()

    def _on_frame_navigated(self, params: Dict[str, Any]) -> None:
        frame = params.get("frame", {})
        if not frame.get("parentId"):
            if frame.get("id"):
                self._main_frame_id = frame["id"]
            self._current_url = frame.get("url", self._current_url)

    def _on_navigated_within_document(self, params: Dict[str, Any]) -> None:
        frame_id = params.get("frameId")
        if not self._main_frame_id or frame_id == self._main_frame_id:
            self._current_url = params.get("url", self._current_url)
            self._spa_nav_event.set()

    def _on_req_start(self, params: Dict[str, Any]) -> None:
        req_id = params.get("requestId")
        rtype = params.get("type", "")
        if rtype in ("WebSocket", "EventSource", "Ping", "Beacon"):
            return
        if req_id:
            now = asyncio.get_running_loop().time()
            self._inflight_requests[req_id] = (now, rtype)
            self._last_network_activity = now
            self._network_idle_event.clear()

    def _on_req_end(self, params: Dict[str, Any]) -> None:
        req_id = params.get("requestId")
        if req_id:
            self._inflight_requests.pop(req_id, None)
            self._last_network_activity = asyncio.get_running_loop().time()

    def _on_js_dialog(self, _params: Dict[str, Any]) -> None:
        asyncio.create_task(self._dismiss_dialog())

    async def _dismiss_dialog(self) -> None:
        try:
            await self._send("Page.handleJavaScriptDialog", {"accept": True})
        except Exception:
            pass

    async def set_viewport(
        self,
        width: int,
        height: int,
        device_scale_factor: Optional[float] = None,
    ) -> None:
        self.viewport_width = width
        self.viewport_height = height
        if device_scale_factor is not None:
            self.device_scale_factor = device_scale_factor
        await self._send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": self.viewport_width,
                "height": self.viewport_height,
                "deviceScaleFactor": self.device_scale_factor,
                "mobile": False,
            },
        )

    async def evaluate(self, expression: str, await_promise: bool = True, timeout: float = 20.0) -> Any:
        """Evaluates a JavaScript expression in the page context and returns its value."""
        res = await self._send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "userGesture": True,
            },
            timeout=timeout,
        )
        if "exceptionDetails" in res:
            details = res["exceptionDetails"]
            exc_obj = details.get("exception", {})
            desc = exc_obj.get("description") or details.get("text") or "JavaScript evaluation error"
            raise CDPError(f"JS error: {desc}")
        return res.get("result", {}).get("value")

    async def get_url(self) -> str:
        try:
            url = await self.evaluate("window.location.href", await_promise=False, timeout=5.0)
            if isinstance(url, str) and url:
                self._current_url = url
                return url
        except Exception:
            pass
        return self._current_url

    async def get_title(self) -> str:
        try:
            title = await self.evaluate("document.title || ''", await_promise=False, timeout=5.0)
            return str(title).strip() if title else ""
        except Exception:
            return ""

    async def wait_for_page_load(
        self,
        quiet_ms: int = DEFAULT_PAGE_LOAD_QUIET_MS,
        max_wait: float = DEFAULT_PAGE_LOAD_SETTLE_TIMEOUT,
        img_timeout_ms: int = 3000,
        font_timeout_ms: int = 1500,
        check_all_images: bool = False,
        wait_window_load: bool = True,
        min_wait_ms: int = 0,
        wait_cdp_network: bool = True,
    ) -> PageLoadStatus:
        """
        Hybrid JS + CDP page-load completion detector.

        Detects the true end of page loading by combining:
        - In-page JS monitoring (`_WAIT_PAGE_LOAD_JS` + `_INIT_SCRIPT` tracker):
          `document.readyState === 'complete'`, in-flight `fetch()`/`XMLHttpRequest`,
          `PerformanceResourceTiming`, `MutationObserver` DOM quietness, layout height
          stability, visible `aria-busy`/indeterminate progress loaders, relevant `<img>`
          load + bitmap decoding, `document.fonts.ready`, and 2 animation paint frames.
        - Native CDP tracking: `Page.lifecycleEvent` (`networkIdle`), main-frame redirect
          recovery (re-runs automatically if a JS/meta redirect destroys the context mid-wait),
          and non-stale in-flight CDP network requests (catches iframes, CSS assets, full bodies).
        """
        loop = asyncio.get_running_loop()
        start_t = loop.time()
        deadline = start_t + max(0.3, max_wait)
        quiet_sec = max(0.08, quiet_ms / 1000.0)
        cur_wait_window_load = wait_window_load
        last_result: Optional[Dict[str, Any]] = None

        for attempt in range(4):
            rem = deadline - loop.time()
            if rem <= 0.08:
                break

            nav_gen = self._nav_generation
            js_opts = {
                "quietMs": int(quiet_ms),
                "maxWaitMs": int(max(300, rem * 1000)),
                "imgTimeoutMs": int(min(img_timeout_ms, rem * 1000)),
                "fontTimeoutMs": int(min(font_timeout_ms, rem * 1000)),
                "checkAllImages": bool(check_all_images),
                "waitWindowLoad": bool(cur_wait_window_load),
                "minWaitMs": int(min_wait_ms if attempt == 0 else 0),
            }
            js_expr = f"({_WAIT_PAGE_LOAD_JS})({json.dumps(js_opts)})"

            try:
                res = await self.evaluate(js_expr, await_promise=True, timeout=rem + 3.0)
                if isinstance(res, dict):
                    last_result = res
            except CDPError as exc:
                err_msg = str(exc).lower()
                nav_destroyed = (
                    "execution context was destroyed" in err_msg
                    or "cannot find context" in err_msg
                    or "inspected target navigated" in err_msg
                    or self._nav_generation != nav_gen
                )
                if nav_destroyed and attempt < 3:
                    logger.debug(
                        f"Page navigated/redirected while awaiting load (attempt {attempt + 1}); "
                        "waiting for new document..."
                    )
                    rem_after = deadline - loop.time()
                    if rem_after > 0.2:
                        try:
                            await asyncio.wait_for(self._load_event.wait(), timeout=min(rem_after, 6.0))
                        except asyncio.TimeoutError:
                            pass
                    cur_wait_window_load = True
                    continue
                logger.debug(f"JS page-load detector error: {exc}")
                status = PageLoadStatus(
                    settled=False,
                    reason="error",
                    elapsed_ms=int((loop.time() - start_t) * 1000),
                    pending_cdp_requests=self._active_cdp_request_count(),
                    url=self._current_url,
                )
                self.last_load_status = status
                return status
            except Exception as exc:
                logger.debug(f"Unexpected page-load detector error: {exc}")
                break

            # If a main-frame redirect started while JS was running, wait for the new document too
            if self._nav_generation != nav_gen and attempt < 3:
                rem_after = deadline - loop.time()
                if rem_after > 0.2:
                    try:
                        await asyncio.wait_for(self._load_event.wait(), timeout=min(rem_after, 6.0))
                    except asyncio.TimeoutError:
                        pass
                    cur_wait_window_load = True
                    continue

            # Cross-check with CDP network layer (catches cross-origin iframes, CSS bg images, streaming bodies)
            if wait_cdp_network and self._active_cdp_request_count() > 0:
                cdp_wait_end = min(deadline, loop.time() + 2.0)
                waited_cdp = False
                while loop.time() < cdp_wait_end:
                    if self._network_idle_event.is_set():
                        break
                    idle_for = loop.time() - self._last_network_activity
                    if self._active_cdp_request_count() == 0 and idle_for >= min(quiet_sec, 0.35):
                        break
                    waited_cdp = True
                    await asyncio.sleep(0.08)
                # Flush 2 paint frames if late CDP resources finished downloading
                if waited_cdp and (deadline - loop.time()) > 0.1:
                    try:
                        await self.evaluate(_WAIT_PAINT_FRAMES_JS, await_promise=True, timeout=1.5)
                    except Exception:
                        pass

            break

        pending_cdp = self._active_cdp_request_count()
        elapsed_ms = int((loop.time() - start_t) * 1000)
        if last_result:
            if isinstance(last_result.get("url"), str) and last_result["url"]:
                self._current_url = last_result["url"]
            status = PageLoadStatus(
                settled=bool(last_result.get("settled", False)) and pending_cdp <= 1,
                reason=str(last_result.get("reason", "timeout")),
                ready_state=str(last_result.get("readyState", "")),
                elapsed_ms=elapsed_ms,
                pending_js_requests=int(last_result.get("pendingJsRequests", 0)),
                pending_cdp_requests=pending_cdp,
                pending_images=int(last_result.get("pendingImages", 0)),
                dom_mutations=int(last_result.get("domMutations", 0)),
                url=self._current_url,
            )
        else:
            status = PageLoadStatus(
                settled=False,
                reason="timeout",
                elapsed_ms=elapsed_ms,
                pending_cdp_requests=pending_cdp,
                url=self._current_url,
            )

        self.last_load_status = status
        logger.debug(
            f"Page load detection [{status.reason}] in {status.elapsed_ms}ms "
            f"(readyState={status.ready_state}, mutations={status.dom_mutations}, "
            f"js_req={status.pending_js_requests}, cdp_req={status.pending_cdp_requests}, "
            f"imgs={status.pending_images}) url={status.url}"
        )
        return status

    async def _wait_for_settle(
        self,
        quiet_ms: int = DEFAULT_PAGE_LOAD_QUIET_MS,
        max_wait: float = DEFAULT_PAGE_LOAD_SETTLE_TIMEOUT,
    ) -> PageLoadStatus:
        """Backwards-compatible wrapper around `wait_for_page_load` for SPA/DOM/network settling."""
        return await self.wait_for_page_load(
            quiet_ms=quiet_ms,
            max_wait=max_wait,
            wait_window_load=False,
        )

    async def goto(self, url: str, timeout: float = DEFAULT_NAVIGATION_TIMEOUT) -> str:
        """Navigates to an HTTP(S) URL and waits until the page has completely finished loading."""
        self._reset_load_signals(clear_inflight=True)
        self.elements = []

        nav_res = await self._send("Page.navigate", {"url": url}, timeout=timeout)
        if nav_res.get("errorText"):
            raise NavigationError(f"Falha ao carregar {url}: {nav_res['errorText']}")

        # Wait for full load event, tolerating sites with hung analytics scripts if DOM already loaded
        try:
            await asyncio.wait_for(self._load_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            if not self._dom_event.is_set():
                raise NavigationError(f"Tempo limite ({int(timeout)}s) excedido ao carregar {url}.")
            logger.info(f"Page.loadEventFired timed out for {url}, continuing after DOMContentLoaded.")

        await self.wait_for_page_load(
            quiet_ms=DEFAULT_PAGE_LOAD_QUIET_MS,
            max_wait=DEFAULT_PAGE_LOAD_SETTLE_TIMEOUT,
            wait_window_load=True,
        )
        return await self.get_url()

    async def set_content(self, html: str, wait_ms: int = 120) -> None:
        """Loads raw HTML directly into the page (offline rendering, e.g. 4chan Yotsuba B template)."""
        self._reset_load_signals(clear_inflight=True)
        tree = await self._send("Page.getFrameTree")
        frame_id = tree["frameTree"]["frame"]["id"]
        self._main_frame_id = frame_id
        await self._send("Page.setDocumentContent", {"frameId": frame_id, "html": html})
        await self.wait_for_page_load(
            quiet_ms=max(80, wait_ms),
            max_wait=5.0,
            img_timeout_ms=3000,
            font_timeout_ms=1500,
            check_all_images=True,
            wait_window_load=True,
            wait_cdp_network=False,
        )

    async def reload(self, ignore_cache: bool = True, timeout: float = DEFAULT_NAVIGATION_TIMEOUT) -> str:
        """Reloads the current page and waits for complete page load."""
        self._reset_load_signals(clear_inflight=True)
        self.elements = []
        await self._send("Page.reload", {"ignoreCache": ignore_cache}, timeout=timeout)
        try:
            await asyncio.wait_for(self._load_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            if not self._dom_event.is_set():
                logger.warning("Reload timed out before loadEventFired; returning current state.")
        await self.wait_for_page_load(
            quiet_ms=DEFAULT_PAGE_LOAD_QUIET_MS,
            max_wait=DEFAULT_PAGE_LOAD_SETTLE_TIMEOUT,
            wait_window_load=True,
        )
        return await self.get_url()

    async def go_back(self) -> bool:
        """Navigates back in tab history if available."""
        hist = await self._send("Page.getNavigationHistory")
        idx = hist.get("currentIndex", 0)
        entries = hist.get("entries", [])
        if idx <= 0 or not entries:
            return False
        prev_entry = entries[idx - 1]
        self._reset_load_signals(clear_inflight=True)
        self.elements = []
        await self._send("Page.navigateToHistoryEntry", {"entryId": prev_entry["id"]})
        try:
            await asyncio.wait_for(self._load_event.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            pass
        await self.wait_for_page_load(quiet_ms=400, max_wait=5.0, wait_window_load=True)
        return True

    async def go_forward(self) -> bool:
        """Navigates forward in tab history if available."""
        hist = await self._send("Page.getNavigationHistory")
        idx = hist.get("currentIndex", 0)
        entries = hist.get("entries", [])
        if idx + 1 >= len(entries):
            return False
        next_entry = entries[idx + 1]
        self._reset_load_signals(clear_inflight=True)
        self.elements = []
        await self._send("Page.navigateToHistoryEntry", {"entryId": next_entry["id"]})
        try:
            await asyncio.wait_for(self._load_event.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            pass
        await self.wait_for_page_load(quiet_ms=400, max_wait=5.0, wait_window_load=True)
        return True

    async def get_scroll_info(self) -> ScrollInfo:
        info = await self.evaluate(
            """(() => {
                const sy = Math.round(window.pageYOffset || document.documentElement.scrollTop || 0);
                const sh = Math.max(
                    document.body ? document.body.scrollHeight : 0,
                    document.documentElement ? document.documentElement.scrollHeight : 0,
                    window.innerHeight
                );
                const vh = window.innerHeight || 1;
                const maxScroll = Math.max(1, sh - vh);
                const pct = sh <= vh ? 100 : Math.min(100, Math.max(0, Math.round((sy / maxScroll) * 100)));
                return { sy, sh, vh, pct };
            })()""",
            await_promise=False,
        )
        return ScrollInfo(
            scroll_y=int(info["sy"]),
            scroll_height=int(info["sh"]),
            viewport_height=int(info["vh"]),
            percent=int(info["pct"]),
        )

    async def scroll(self, direction: str = "down", fraction: float = 0.8) -> ScrollInfo:
        """Scrolls the viewport up, down, top, or bottom and waits for newly revealed content to settle."""
        if direction == "top":
            await self.evaluate("window.scrollTo({ top: 0, behavior: 'instant' })", await_promise=False)
        elif direction == "bottom":
            await self.evaluate(
                "window.scrollTo({ top: Math.max(document.body.scrollHeight, document.documentElement.scrollHeight), behavior: 'instant' })",
                await_promise=False,
            )
        else:
            delta = int(self.viewport_height * fraction)
            if direction == "up":
                delta = -delta
            await self.evaluate(f"window.scrollBy({{ top: {delta}, behavior: 'instant' }})", await_promise=False)
        await self.wait_for_page_load(
            quiet_ms=250,
            max_wait=1.8,
            img_timeout_ms=1200,
            font_timeout_ms=400,
            check_all_images=False,
            wait_window_load=False,
            min_wait_ms=120,
        )
        return await self.get_scroll_info()

    async def _hydrate_lazy_content(self, max_steps: int = 16) -> None:
        """
        Quickly steps through a live web page to trigger lazy-loaded images and
        IntersectionObservers, scrolls back to y=0, and waits until all images/DOM settle.
        """
        try:
            scroll_height = await self.evaluate(
                "Math.max(document.body ? document.body.scrollHeight : 0, document.documentElement ? document.documentElement.scrollHeight : 0)",
                await_promise=False,
            )
            if scroll_height and scroll_height > self.viewport_height:
                # Limit total hydration scroll height so infinite-scroll feeds don't explode forever
                max_hydrate_y = min(int(scroll_height), self.viewport_height * max_steps)
                step = max(self.viewport_height - 100, 600)
                y = step
                while y < max_hydrate_y:
                    await self.evaluate(f"window.scrollTo(0, {y})", await_promise=False)
                    await asyncio.sleep(0.12)
                    y += step
                await self.evaluate("window.scrollTo(0, 0)", await_promise=False)

            await self.wait_for_page_load(
                quiet_ms=400,
                max_wait=4.5,
                img_timeout_ms=3000,
                font_timeout_ms=1000,
                check_all_images=True,
                wait_window_load=False,
            )
        except Exception as exc:
            logger.debug(f"Lazy-content hydration skipped: {exc}")

    async def clear_overlays(self) -> None:
        """Removes any injected interactive badge overlays."""
        try:
            await self.evaluate(_CLEAR_OVERLAYS_JS, await_promise=False, timeout=5.0)
        except Exception:
            pass

    async def collect_and_tag_elements(
        self,
        show_overlay: bool = True,
        max_elements: int = 40,
    ) -> List[InteractiveElement]:
        """
        Inspects visible interactive elements in the current viewport, optionally renders
        color-coded numbered badges over them, and returns the element metadata list.
        """
        js = f"({_TAG_ELEMENTS_JS})({'true' if show_overlay else 'false'}, {int(max_elements)})"
        raw = await self.evaluate(js, await_promise=False, timeout=10.0)
        elements = [
            InteractiveElement(
                id=int(it["id"]),
                kind=str(it["kind"]),
                tag=str(it["tag"]),
                label=str(it["label"]),
                x=float(it["x"]),
                y=float(it["y"]),
                width=float(it["width"]),
                height=float(it["height"]),
                is_input=bool(it["is_input"]),
                input_type=str(it.get("input_type", "")),
                href=str(it.get("href", "")),
            )
            for it in (raw or [])
        ]
        self.elements = elements
        return elements

    async def screenshot_viewport(self, output_path: Path) -> Path:
        """Captures the current viewport as a crisp PNG image."""
        res = await self._send(
            "Page.captureScreenshot",
            {"format": "png", "fromSurface": True},
            timeout=30.0,
        )
        png_bytes = base64.b64decode(res["data"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(png_bytes)
        return output_path

    async def _capture_clip_png_bytes(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        scale: float = 1.0,
    ) -> bytes:
        res = await self._send(
            "Page.captureScreenshot",
            {
                "format": "png",
                "fromSurface": True,
                "captureBeyondViewport": True,
                "clip": {
                    "x": float(x),
                    "y": float(y),
                    "width": float(width),
                    "height": float(height),
                    "scale": float(scale),
                },
            },
            timeout=45.0,
        )
        return base64.b64decode(res["data"])

    async def _set_fixed_elements_hidden(self, hide: bool) -> None:
        """Temporarily hides fixed/sticky elements during slices 2..N so navbars don't overlap content."""
        if hide:
            await self.evaluate(
                """(() => {
                    document.querySelectorAll('*').forEach(el => {
                        const pos = window.getComputedStyle(el).position;
                        if (pos === 'fixed' || pos === 'sticky') {
                            el.setAttribute('data-chanarc-prev-vis', el.style.visibility || '');
                            el.style.visibility = 'hidden';
                            el.setAttribute('data-chanarc-hidden-fixed', '1');
                        }
                    });
                })()""",
                await_promise=False,
            )
        else:
            await self.evaluate(
                """(() => {
                    document.querySelectorAll('[data-chanarc-hidden-fixed="1"]').forEach(el => {
                        el.style.visibility = el.getAttribute('data-chanarc-prev-vis') || '';
                        el.removeAttribute('data-chanarc-prev-vis');
                        el.removeAttribute('data-chanarc-hidden-fixed');
                    });
                })()""",
                await_promise=False,
            )

    async def screenshot_full_page(
        self,
        output_dir: Path,
        filename_prefix: str = "page",
        max_slice_height_css: int = DEFAULT_MAX_SLICE_HEIGHT_CSS,
        hydrate_lazy: bool = True,
        block_selector: Optional[str] = None,
        max_total_height_css: int = 84000,
    ) -> List[Path]:
        """
        Captures the ENTIRE web page as lossless HiDPI PNG file(s).
        - Clears interactive tag overlays before capture.
        - Optionally triggers lazy-loaded images along the page.
        - If total height <= max_slice_height_css: outputs 1 single full-page PNG.
        - If taller: slices cleanly at DOM block boundaries (never cutting posts/paragraphs mid-line)
          and hides fixed/sticky headers on subsequent slices.
        - Automatically scales down a slice if its raw PNG exceeds Telegram's 48 MB safety cap.
        """
        await self.clear_overlays()

        saved_scroll_y = await self.evaluate(
            "Math.round(window.pageYOffset || document.documentElement.scrollTop || 0)",
            await_promise=False,
        )

        if hydrate_lazy:
            await self._hydrate_lazy_content()

        await self.evaluate("window.scrollTo(0, 0)", await_promise=False)
        await asyncio.sleep(0.1)

        metrics = await self.evaluate(
            """(() => {
                const body = document.body;
                const doc = document.documentElement;
                const height = Math.max(
                    body ? body.scrollHeight : 0,
                    doc ? doc.scrollHeight : 0,
                    body ? body.offsetHeight : 0,
                    doc ? doc.offsetHeight : 0,
                    window.innerHeight
                );
                const width = Math.max(
                    window.innerWidth,
                    doc ? doc.clientWidth : 0
                );
                return { width: Math.ceil(width), height: Math.ceil(height) };
            })()""",
            await_promise=False,
        )

        capture_width = max(int(metrics["width"]), self.viewport_width)
        total_height = min(int(metrics["height"]), max_total_height_css)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_files: List[Path] = []

        async def _write_safe_slice(
            y_top: int,
            height: int,
            dest_path: Path,
        ) -> Path:
            # Capture at native DPR (clip.scale=1 multiplies by deviceScaleFactor in Chromium)
            png_bytes = await self._capture_clip_png_bytes(0, y_top, capture_width, height, scale=1.0)
            if len(png_bytes) > TELEGRAM_MAX_PNG_BYTES:
                logger.warning(
                    f"PNG slice {dest_path.name} exceeded 48MB ({len(png_bytes) / (1024*1024):.1f}MB); "
                    "re-capturing at 0.5x clip scale to fit Telegram limit."
                )
                png_bytes = await self._capture_clip_png_bytes(0, y_top, capture_width, height, scale=0.5)
            dest_path.write_bytes(png_bytes)
            return dest_path

        try:
            # Single clean full-page PNG if within safe canvas ceiling
            if total_height <= max_slice_height_css:
                single_path = output_dir / f"{filename_prefix}.png"
                await _write_safe_slice(0, total_height, single_path)
                output_files.append(single_path)
                return output_files

            # Multi-slice DOM-aligned boundary capture for tall pages
            selector_css = (
                block_selector
                or ".postContainer, article, section, main > div, p, li, tr, h1, h2, h3, figure, blockquote"
            )
            block_bottoms: List[int] = await self.evaluate(
                f"""(() => {{
                    const els = Array.from(document.querySelectorAll({json.dumps(selector_css)}));
                    const scrollY = window.pageYOffset || document.documentElement.scrollTop || 0;
                    const bottoms = new Set();
                    for (const el of els) {{
                        const r = el.getBoundingClientRect();
                        if (r.height >= 16 && r.height < {max_slice_height_css}) {{
                            const b = Math.round(r.bottom + scrollY);
                            if (b > 0) bottoms.add(b);
                        }}
                    }}
                    return Array.from(bottoms).sort((a, b) => a - b);
                }})()""",
                await_promise=False,
            )

            current_top = 0
            part_index = 1

            while current_top < total_height:
                remaining = total_height - current_top
                if part_index == 2:
                    # Hide fixed/sticky headers on parts 2..N so they don't stamp over page content
                    await self._set_fixed_elements_hidden(True)

                if remaining <= max_slice_height_css:
                    slice_bottom = total_height
                else:
                    target_bottom = current_top + max_slice_height_css
                    # Pick the furthest DOM block boundary <= target_bottom and reasonably below current_top
                    min_acceptable = current_top + int(max_slice_height_css * 0.25)
                    candidates = [b for b in block_bottoms if min_acceptable <= b <= target_bottom]
                    if candidates:
                        slice_bottom = min(candidates[-1] + 6, total_height)
                    else:
                        slice_bottom = min(target_bottom, total_height)

                slice_height = max(1, slice_bottom - current_top)
                slice_path = output_dir / f"{filename_prefix}_part_{part_index}.png"
                logger.info(
                    f"Capturing full-page slice {part_index}: y={current_top}..{slice_bottom} ({slice_height}px)"
                )
                await _write_safe_slice(current_top, slice_height, slice_path)
                output_files.append(slice_path)

                current_top = slice_bottom
                part_index += 1

            return output_files
        finally:
            try:
                await self._set_fixed_elements_hidden(False)
                if saved_scroll_y:
                    await self.evaluate(f"window.scrollTo(0, {int(saved_scroll_y)})", await_promise=False)
            except Exception:
                pass

    async def _dispatch_click(self, x: float, y: float) -> None:
        """Dispatches a real trusted left-click via CDP Input domain."""
        await self._send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        await self._send(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
        )
        await self._send(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
        )

    async def _settle_after_action(self, prev_url: str) -> bool:
        """Waits after a click or Enter press for full navigation, SPA route change, or DOM/AJAX updates."""
        frame_task = asyncio.create_task(self._frame_started_event.wait())
        spa_task = asyncio.create_task(self._spa_nav_event.wait())
        try:
            await asyncio.wait(
                (frame_task, spa_task),
                timeout=0.45,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for t in (frame_task, spa_task):
                if not t.done():
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass

        navigated = self._frame_started_event.is_set()
        spa_navigated = self._spa_nav_event.is_set()

        if navigated:
            try:
                await asyncio.wait_for(self._load_event.wait(), timeout=18.0)
            except asyncio.TimeoutError:
                pass
            await self.wait_for_page_load(quiet_ms=450, max_wait=6.0, wait_window_load=True)
        elif spa_navigated:
            await self.wait_for_page_load(quiet_ms=450, max_wait=5.0, wait_window_load=False)
        else:
            await self.wait_for_page_load(
                quiet_ms=350,
                max_wait=3.0,
                img_timeout_ms=1500,
                wait_window_load=False,
            )

        new_url = await self.get_url()
        return navigated or spa_navigated or (new_url != prev_url)

    async def click_element_by_id(self, element_id: int) -> ClickResult:
        """Clicks an element tagged with badge number `element_id`."""
        match = next((e for e in self.elements if e.id == element_id), None)
        if not match:
            return ClickResult(
                success=False,
                message=f"Elemento [{element_id}] não encontrado na tela atual.",
            )

        await self.clear_overlays()
        prev_url = await self.get_url()
        self._reset_load_signals(clear_inflight=False)

        # Re-read live coordinates & rewrite target="_blank" so links open in-tab
        coords = await self.evaluate(
            f"""(() => {{
                const el = document.querySelector('[data-chanarc-id="{int(element_id)}"]');
                if (!el) return null;
                const a = el.closest('a[target="_blank"]');
                if (a) a.removeAttribute('target');
                const r = el.getBoundingClientRect();
                return {{ x: r.left + r.width / 2, y: r.top + r.height / 2 }};
            }})()""",
            await_promise=False,
        )
        cx = float(coords["x"]) if coords else match.x
        cy = float(coords["y"]) if coords else match.y

        await self._dispatch_click(cx, cy)
        navigated = await self._settle_after_action(prev_url)
        new_url = await self.get_url()
        new_title = await self.get_title()

        return ClickResult(
            success=True,
            message=f"Clicou em [{match.id}] {match.label}",
            element=match,
            is_input=match.is_input,
            input_type=match.input_type,
            navigated=navigated,
            url=new_url,
            title=new_title,
        )

    async def click_by_text(self, query: str) -> ClickResult:
        """
        Matches a visible interactive element by text/aria-label/placeholder (exact first, then partial)
        and clicks it. Falls back to searching the full DOM if not in the current tag list.
        """
        q = query.strip().lower()
        if not q:
            return ClickResult(success=False, message="Texto de clique vazio.")

        # 1. Exact label match in currently tagged elements
        exact = [e for e in self.elements if e.label.strip().lower() == q]
        if exact:
            return await self.click_element_by_id(exact[0].id)

        # 2. Substring match in currently tagged elements (prefer shortest matching label)
        partial = [e for e in self.elements if q in e.label.strip().lower()]
        if partial:
            partial.sort(key=lambda e: len(e.label))
            return await self.click_element_by_id(partial[0].id)

        # 3. Search full page DOM for a clickable matching element and scroll it into view
        await self.clear_overlays()
        prev_url = await self.get_url()
        self._reset_load_signals(clear_inflight=False)

        found = await self.evaluate(
            f"""((q) => {{
                const sel = 'a[href], button, input:not([type="hidden"]), textarea, [role="button"], [role="link"], [role="tab"], summary, [onclick]';
                const els = Array.from(document.querySelectorAll(sel));
                let best = null;
                let bestLen = Infinity;
                for (const el of els) {{
                    const st = window.getComputedStyle(el);
                    if (st.display === 'none' || st.visibility === 'hidden') continue;
                    const txt = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('title') || el.value || '').replace(/\\s+/g, ' ').trim();
                    const low = txt.toLowerCase();
                    if (low && low.includes(q) && low.length < bestLen) {{
                        best = {{ el, txt }};
                        bestLen = low.length;
                    }}
                }}
                if (!best) return null;
                const a = best.el.closest('a[target="_blank"]');
                if (a) a.removeAttribute('target');
                best.el.scrollIntoView({{ block: 'center', behavior: 'instant' }});
                const r = best.el.getBoundingClientRect();
                const tag = best.el.tagName.toLowerCase();
                const type = (best.el.getAttribute('type') || '').toLowerCase();
                const isInput = tag === 'textarea' || best.el.isContentEditable || (tag === 'input' && !['submit','button','reset','checkbox','radio'].includes(type));
                return {{
                    x: r.left + r.width / 2,
                    y: r.top + r.height / 2,
                    label: best.txt.slice(0, 48),
                    tag,
                    is_input: isInput,
                    input_type: type
                }};
            }})({json.dumps(q)})""",
            await_promise=False,
        )
        if not found:
            return ClickResult(
                success=False,
                message=f"Nenhum elemento clicável encontrado contendo '{query}'.",
            )

        await self._dispatch_click(float(found["x"]), float(found["y"]))
        navigated = await self._settle_after_action(prev_url)
        return ClickResult(
            success=True,
            message=f"Clicou em '{found['label']}'",
            is_input=bool(found["is_input"]),
            input_type=str(found.get("input_type", "")),
            navigated=navigated,
            url=await self.get_url(),
            title=await self.get_title(),
        )

    async def click_by_selector(self, css_selector: str) -> ClickResult:
        """Clicks the first element matching a CSS selector."""
        await self.clear_overlays()
        prev_url = await self.get_url()
        self._reset_load_signals(clear_inflight=False)

        found = await self.evaluate(
            f"""((sel) => {{
                let el;
                try {{ el = document.querySelector(sel); }} catch (_) {{ return {{ error: 'Seletor CSS inválido.' }}; }}
                if (!el) return null;
                const a = el.closest('a[target="_blank"]');
                if (a) a.removeAttribute('target');
                el.scrollIntoView({{ block: 'center', behavior: 'instant' }});
                const r = el.getBoundingClientRect();
                const tag = el.tagName.toLowerCase();
                const type = (el.getAttribute('type') || '').toLowerCase();
                const isInput = tag === 'textarea' || el.isContentEditable || (tag === 'input' && !['submit','button','reset','checkbox','radio'].includes(type));
                return {{
                    x: r.left + r.width / 2,
                    y: r.top + r.height / 2,
                    label: (el.innerText || el.getAttribute('aria-label') || sel).replace(/\\s+/g, ' ').trim().slice(0, 48),
                    is_input: isInput,
                    input_type: type
                }};
            }})({json.dumps(css_selector)})""",
            await_promise=False,
        )
        if not found:
            return ClickResult(success=False, message=f"Seletor '{css_selector}' não encontrado.")
        if "error" in found:
            return ClickResult(success=False, message=str(found["error"]))

        await self._dispatch_click(float(found["x"]), float(found["y"]))
        navigated = await self._settle_after_action(prev_url)
        return ClickResult(
            success=True,
            message=f"Clicou em '{found['label']}'",
            is_input=bool(found["is_input"]),
            input_type=str(found.get("input_type", "")),
            navigated=navigated,
            url=await self.get_url(),
            title=await self.get_title(),
        )

    async def click_at(self, x: float, y: float) -> ClickResult:
        """Clicks directly at viewport coordinates (x, y)."""
        await self.clear_overlays()
        prev_url = await self.get_url()
        self._reset_load_signals(clear_inflight=False)

        info = await self.evaluate(
            f"""(() => {{
                const el = document.elementFromPoint({float(x)}, {float(y)});
                if (!el) return {{ is_input: false, input_type: '', label: '' }};
                const a = el.closest('a[target="_blank"]');
                if (a) a.removeAttribute('target');
                const tag = el.tagName.toLowerCase();
                const type = (el.getAttribute('type') || '').toLowerCase();
                const isInput = tag === 'textarea' || el.isContentEditable || (tag === 'input' && !['submit','button','reset','checkbox','radio'].includes(type));
                return {{
                    is_input: isInput,
                    input_type: type,
                    label: (el.innerText || el.getAttribute('aria-label') || tag).replace(/\\s+/g, ' ').trim().slice(0, 40)
                }};
            }})()""",
            await_promise=False,
        )
        await self._dispatch_click(x, y)
        navigated = await self._settle_after_action(prev_url)
        return ClickResult(
            success=True,
            message=f"Clicou em ({int(x)}, {int(y)})" + (f" ({info['label']})" if info and info.get("label") else ""),
            is_input=bool(info and info.get("is_input")),
            input_type=str(info.get("input_type", "")) if info else "",
            navigated=navigated,
            url=await self.get_url(),
            title=await self.get_title(),
        )

    async def focus_best_input(self) -> Optional[InteractiveElement]:
        """Focuses the currently active editable element, or the first visible text input on screen."""
        has_active = await self.evaluate(
            """(() => {
                const el = document.activeElement;
                if (!el) return false;
                const tag = el.tagName.toLowerCase();
                const type = (el.getAttribute('type') || '').toLowerCase();
                if (tag === 'textarea' || el.isContentEditable) return true;
                if (tag === 'input' && !['hidden','submit','button','reset','checkbox','radio'].includes(type)) return true;
                return false;
            })()""",
            await_promise=False,
        )
        if has_active:
            return None

        inputs = [e for e in self.elements if e.is_input]
        if inputs:
            await self.click_element_by_id(inputs[0].id)
            return inputs[0]

        # Fallback: locate any visible text/search input on the page
        await self.evaluate(
            """(() => {
                const el = document.querySelector('input[type="search"], input[type="text"], input:not([type]), textarea, [contenteditable="true"]');
                if (el) { el.focus(); }
            })()""",
            await_promise=False,
        )
        return None

    async def clear_focused_input(self) -> None:
        """Clears the currently focused input/textarea/contenteditable element."""
        await self.evaluate(
            """(() => {
                const el = document.activeElement;
                if (!el) return;
                if ('value' in el) {
                    const proto = Object.getPrototypeOf(el);
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                    if (setter) setter.call(el, '');
                    else el.value = '';
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                } else if (el.isContentEditable) {
                    el.textContent = '';
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                }
            })()""",
            await_promise=False,
        )

    async def press_key(self, key_name: str) -> bool:
        """Dispatches a special keyboard key (Enter, Tab, Escape, Backspace, arrows, etc.)."""
        key_def = KEY_DEFINITIONS.get(key_name)
        if not key_def:
            return False

        prev_url = await self.get_url()
        self._reset_load_signals(clear_inflight=False)

        base = {
            "key": key_def["key"],
            "code": key_def["code"],
            "windowsVirtualKeyCode": key_def["windowsVirtualKeyCode"],
            "nativeVirtualKeyCode": key_def["windowsVirtualKeyCode"],
        }
        await self._send("Input.dispatchKeyEvent", {"type": "rawKeyDown", **base})
        if key_def["text"]:
            await self._send("Input.dispatchKeyEvent", {"type": "char", "text": key_def["text"], **base})
        await self._send("Input.dispatchKeyEvent", {"type": "keyUp", **base})

        if key_name == "Enter":
            await self._settle_after_action(prev_url)
        else:
            await asyncio.sleep(0.18)
        return True

    async def type_text(
        self,
        text: str,
        press_enter: bool = True,
        clear_first: bool = False,
        element_id: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """
        Types UTF-8 text (directly from Telegram chat) into a target or focused page element.
        Returns (navigated, status_detail).
        """
        if element_id is not None:
            res = await self.click_element_by_id(element_id)
            if not res.success:
                return False, res.message
        else:
            await self.focus_best_input()

        if clear_first:
            await self.clear_focused_input()

        prev_url = await self.get_url()
        self._reset_load_signals(clear_inflight=False)

        await self._send("Input.insertText", {"text": text})
        navigated = False
        if press_enter:
            await self.press_key("Enter")
            new_url = await self.get_url()
            navigated = new_url != prev_url
        else:
            await asyncio.sleep(0.2)

        return navigated, f"Digitou '{text}'" + (" + Enter" if press_enter else "")

    async def find_in_page(self, query: str) -> FindResult:
        """Highlights all occurrences of `query` on the page and scrolls to the first match."""
        await self.clear_overlays()
        js = f"({_FIND_IN_PAGE_JS})({json.dumps(query)})"
        res = await self.evaluate(js, await_promise=False, timeout=10.0)
        return FindResult(
            query=query,
            total=int(res.get("total", 0)),
            current_index=int(res.get("current_index", 0)),
            snippet=str(res.get("snippet", "")),
        )

    async def find_goto(self, query: str, target_index: int) -> FindResult:
        """Moves the active in-page search highlight to 1-based `target_index` (wraps around)."""
        await self.clear_overlays()
        js = f"({_FIND_STEP_JS})({int(target_index)})"
        res = await self.evaluate(js, await_promise=False, timeout=10.0)
        return FindResult(
            query=query,
            total=int(res.get("total", 0)),
            current_index=int(res.get("current_index", 0)),
            snippet=str(res.get("snippet", "")),
        )

    async def clear_find(self) -> None:
        """Removes all in-page search highlights."""
        try:
            await self.evaluate(_CLEAR_FIND_JS, await_promise=False, timeout=5.0)
        except Exception:
            pass

    async def close(self) -> None:
        """Closes this tab and disposes its isolated BrowserContext."""
        if self.closed:
            return
        self.closed = True
        self.browser.remove_session_listeners(self.session_id)
        try:
            await self.browser.send("Target.closeTarget", {"targetId": self.target_id}, timeout=5.0)
        except Exception:
            pass
        try:
            await self.browser.send(
                "Target.disposeBrowserContext",
                {"browserContextId": self.browser_context_id},
                timeout=5.0,
            )
        except Exception:
            pass
