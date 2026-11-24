"""
bot.py - Universal Web Page PNG Archiver & Interactive Browser Telegram Bot

Powered by a lightweight Pure-Python Async Native CDP Engine (zero Playwright!):
- Captures ANY web page (full page, lossless HiDPI PNG documents, cleanly sliced if tall).
- Full interactive browsing directly inside Telegram:
  * Reload page (/u or button)
  * Interactive Click Mode with numbered color-coded DOM badges (click by button, number,
    "N texto" combo, visible text typed in chat, CSS selector, or x,y coordinates)
  * Direct Chat-to-Page Typing Mode (anything written in chat is typed into the focused field)
  * Interactive Web Search Mode (DuckDuckGo, Google, Wikipédia, Bing, YouTube)
  * Interactive Find-in-Page Mode (highlights matches & jumps Previous/Next)
  * Scroll, Back/Forward history, and 1-tap Full-Page PNG extraction at any moment.
"""

from __future__ import annotations

import html
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import List, Optional, Set

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from cdp_engine import CDPBrowser, CDPError, InteractiveElement, NavigationError
from renderer import (
    InvalidURLError,
    WebPageRenderer,
    extract_and_normalize_url,
)
from session_manager import (
    BrowserSession,
    InteractiveSnapshot,
    SEARCH_ENGINES,
    SessionManager,
    SessionMode,
    build_search_url,
)

load_dotenv()

# Logging setup
log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_name, logging.INFO)
logging.basicConfig(
    format="%(asctime)s - [%(name)s] - %(levelname)s - %(message)s",
    level=log_level,
)
logger = logging.getLogger("WebArcBot")

# Optional access restriction via comma-separated Telegram user IDs
_raw_allowed = os.getenv("ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS: Optional[Set[int]] = (
    {int(uid.strip()) for uid in _raw_allowed.split(",") if uid.strip().isdigit()}
    if _raw_allowed
    else None
)

# Shared engine & service instances
cdp_browser: Optional[CDPBrowser] = None
web_renderer: Optional[WebPageRenderer] = None
session_manager: Optional[SessionManager] = None

# Parsing helpers for interactive chat input
NUMBER_ONLY_RE = re.compile(r"^#?(\d{1,3})$")
NUMBER_AND_TEXT_RE = re.compile(r"^#?(\d{1,3})\s+(.+)$", re.DOTALL)
COORDS_RE = re.compile(r"^(\d{1,4})\s*[,xX ]\s*(\d{1,4})$")

BADGES_PER_PAGE = 10


def _is_authorized(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    user = update.effective_user
    return user is not None and user.id in ALLOWED_USER_IDS


def _ensure_services() -> None:
    """Lazily initializes shared CDP browser, renderer, and session manager."""
    global cdp_browser, web_renderer, session_manager
    if cdp_browser is None:
        cdp_browser = CDPBrowser()
    if web_renderer is None:
        web_renderer = WebPageRenderer(browser=cdp_browser)
    if session_manager is None:
        session_manager = SessionManager(browser=cdp_browser)


async def post_init(application: Application) -> None:
    """Starts shared lightweight CDP services, idle reaper, and Telegram command menu."""
    _ensure_services()
    assert session_manager is not None
    session_manager.start_reaper()
    logger.info("Native CDP Browser Engine & SessionManager initialized.")

    try:
        await application.bot.set_my_commands(
            [
                BotCommand("start", "Iniciar e ver opções rápidas"),
                BotCommand("png", "Extrair página completa em PNG [url]"),
                BotCommand("i", "Abrir navegador interativo [url]"),
                BotCommand("s", "Pesquisar na web [termo]"),
                BotCommand("f", "Achar texto na página [termo]"),
                BotCommand("t", "Digitar no campo [nº] <texto>"),
                BotCommand("c", "Clicar em elemento (nº, texto, seletor ou x,y)"),
                BotCommand("u", "Atualizar a página atual"),
                BotCommand("b", "Voltar no histórico"),
                BotCommand("n", "Avançar no histórico"),
                BotCommand("r", "Rolar página (cima/baixo/topo/fim)"),
                BotCommand("exit", "Fechar sessão interativa e liberar RAM"),
                BotCommand("help", "Ver guia de uso"),
            ]
        )
    except Exception as exc:
        logger.debug(f"Could not register bot commands: {exc}")


async def post_shutdown(application: Application) -> None:
    """Gracefully closes all interactive sessions and the CDP browser."""
    global cdp_browser, web_renderer, session_manager
    logger.info("Shutting down sessions and native CDP browser...")
    if session_manager:
        await session_manager.close_all()
        session_manager = None
    if web_renderer:
        await web_renderer.close()
        web_renderer = None
    if cdp_browser:
        await cdp_browser.close()
        cdp_browser = None


# ---------------------------------------------------------------------------
# UI Builders (HTML Captions & Inline Keyboards)
# ---------------------------------------------------------------------------


def _short_url(url: str, max_len: int = 58) -> str:
    clean = re.sub(r"^https?://", "", url).rstrip("/")
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 1] + "…"


def _kind_icon(el: InteractiveElement) -> str:
    if el.kind == "input":
        return "🟠"
    if el.kind == "button":
        return "🟢"
    return "🔵"


def _search_engines_legend() -> str:
    return " · ".join(f"{meta['emoji']} {meta['name']}" for meta in SEARCH_ENGINES.values())


def _build_search_prompt_text(active_engine: str) -> str:
    eng = SEARCH_ENGINES.get(active_engine, SEARCH_ENGINES["duckduckgo"])
    return (
        f"🔍 <b>Pesquisa Web</b> · {eng['emoji']} <b>{eng['name']}</b>\n"
        f"<i>{_search_engines_legend()}</i>\n\n"
        "Escreva o que deseja buscar no chat:"
    )


def build_web_capture_keyboard() -> InlineKeyboardMarkup:
    """Compact emoji controls beneath a delivered full-page website PNG."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔄", callback_data="act:reload_web"),
                InlineKeyboardButton("🖱️", callback_data="act:open_interactive"),
                InlineKeyboardButton("🔦", callback_data="act:mode_find"),
                InlineKeyboardButton("🔍", callback_data="act:mode_search"),
            ]
        ]
    )


def build_search_prompt_keyboard(active_engine: str) -> InlineKeyboardMarkup:
    """Compact emoji keyboard for quick search engine selection."""
    eng_row = [
        InlineKeyboardButton(
            f"• {meta['emoji']}" if key == active_engine else meta["emoji"],
            callback_data=f"act:engine:{key}",
        )
        for key, meta in SEARCH_ENGINES.items()
    ]
    return InlineKeyboardMarkup(
        [
            eng_row,
            [InlineKeyboardButton("❌", callback_data="act:mode_click")],
        ]
    )


def build_interactive_keyboard(session: BrowserSession) -> InlineKeyboardMarkup:
    """Builds a sleek, emoji-first interactive mini-browser keyboard."""
    rows: List[List[InlineKeyboardButton]] = []

    # 1. Context-specific top strip
    if session.mode == SessionMode.TYPE:
        auto_lbl = "⏎🟢" if session.auto_enter else "⏎⚪"
        rows.append(
            [
                InlineKeyboardButton("⏎", callback_data="act:key:Enter"),
                InlineKeyboardButton("🧹", callback_data="act:clear_input"),
                InlineKeyboardButton("⇥", callback_data="act:key:Tab"),
                InlineKeyboardButton("⎋", callback_data="act:key:Escape"),
                InlineKeyboardButton(auto_lbl, callback_data="act:auto_enter"),
            ]
        )
    elif session.mode == SessionMode.WEB_SEARCH:
        rows.append(
            [
                InlineKeyboardButton(
                    f"• {meta['emoji']}" if key == session.search_engine else meta["emoji"],
                    callback_data=f"act:engine:{key}",
                )
                for key, meta in SEARCH_ENGINES.items()
            ]
        )
    elif session.mode == SessionMode.PAGE_FIND or session.find_total > 0:
        rows.append(
            [
                InlineKeyboardButton("◀️", callback_data="act:find_prev"),
                InlineKeyboardButton(
                    f"🔦 {session.find_index}/{session.find_total}" if session.find_total else "🔦",
                    callback_data="act:mode_find",
                ),
                InlineKeyboardButton("▶️", callback_data="act:find_next"),
                InlineKeyboardButton("🧹", callback_data="act:find_clear"),
            ]
        )

    # 2. Color-coded numbered clickable positions (paginated 10 per page)
    if session.elements and session.mode != SessionMode.TYPE:
        total = len(session.elements)
        max_page = max(0, (total - 1) // BADGES_PER_PAGE)
        page_idx = min(max(0, session.badge_page), max_page)
        session.badge_page = page_idx
        start = page_idx * BADGES_PER_PAGE
        chunk = session.elements[start : start + BADGES_PER_PAGE]

        for r_start in range(0, len(chunk), 5):
            sub = chunk[r_start : r_start + 5]
            rows.append(
                [
                    InlineKeyboardButton(f"{_kind_icon(el)}{el.id}", callback_data=f"act:click:{el.id}")
                    for el in sub
                ]
            )

        if max_page > 0:
            nav_badges = []
            if page_idx > 0:
                nav_badges.append(InlineKeyboardButton("◀️", callback_data=f"act:bpage:{page_idx - 1}"))
            nav_badges.append(
                InlineKeyboardButton(
                    f"{start + 1}–{start + len(chunk)} / {total}",
                    callback_data="act:noop",
                )
            )
            if page_idx < max_page:
                nav_badges.append(InlineKeyboardButton("▶️", callback_data=f"act:bpage:{page_idx + 1}"))
            rows.append(nav_badges)

    # 3. Browser navigation strip
    rows.append(
        [
            InlineKeyboardButton("⬅️", callback_data="act:back"),
            InlineKeyboardButton("🔄", callback_data="act:ireload"),
            InlineKeyboardButton("➡️", callback_data="act:forward"),
            InlineKeyboardButton("⬆️", callback_data="act:scroll:up"),
            InlineKeyboardButton("⬇️", callback_data="act:scroll:down"),
        ]
    )

    # 4. Mode switcher strip (single 🖱️/⌨️ toggle button + 🔍 + 🔦)
    if session.mode == SessionMode.TYPE:
        click_type_lbl = "• ⌨️"
    elif session.mode == SessionMode.CLICK:
        click_type_lbl = "• 🖱️"
    else:
        click_type_lbl = "🖱️"
    search_lbl = "• 🔍" if session.mode == SessionMode.WEB_SEARCH else "🔍"
    find_lbl = "• 🔦" if session.mode == SessionMode.PAGE_FIND else "🔦"
    rows.append(
        [
            InlineKeyboardButton(click_type_lbl, callback_data="act:toggle_click_type"),
            InlineKeyboardButton(search_lbl, callback_data="act:mode_search"),
            InlineKeyboardButton(find_lbl, callback_data="act:mode_find"),
        ]
    )

    # 5. Export & session controls (pure emojis)
    tags_lbl = "🏷️🟢" if session.show_tags else "🏷️⚪"
    rows.append(
        [
            InlineKeyboardButton("📄", callback_data="act:full_png"),
            InlineKeyboardButton("🖼️", callback_data="act:vp_png"),
            InlineKeyboardButton(tags_lbl, callback_data="act:tags"),
            InlineKeyboardButton("❌", callback_data="act:close"),
        ]
    )

    return InlineKeyboardMarkup(rows)


def build_interactive_caption(session: BrowserSession, snap: InteractiveSnapshot) -> str:
    """Builds a minimal, HTML-safe caption for the interactive screenshot."""
    eng = SEARCH_ENGINES.get(session.search_engine, SEARCH_ENGINES["duckduckgo"])
    mode_badges = {
        SessionMode.CLICK: "🖱️",
        SessionMode.TYPE: "⌨️",
        SessionMode.WEB_SEARCH: f"🔍 {eng['emoji']} {eng['name']}",
        SessionMode.PAGE_FIND: "🔦",
        SessionMode.IDLE: "🌐",
    }
    mode_badge = mode_badges.get(session.mode, "🌐")
    title_clean = html.escape((snap.title or "Sem título")[:64])
    url_clean = html.escape(_short_url(snap.url, 56))

    lines = [
        f"<b>{title_clean}</b>",
        f"{mode_badge}  ·  ↕️ {snap.scroll.percent}%  ·  <code>{url_clean}</code>",
    ]

    if snap.status_note:
        lines.append(f"› <i>{html.escape(snap.status_note[:120])}</i>")

    if session.find_total > 0 and session.find_query:
        lines.append(
            f"🔦 <b>{html.escape(session.find_query[:28])}</b> · {session.find_index}/{session.find_total}"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core Delivery Helpers
# ---------------------------------------------------------------------------


async def send_png_documents(
    bot_message_target: Message,
    image_paths: List[Path],
    base_caption_html: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """Sends one or more lossless uncompressed PNG files via reply_document."""
    total_parts = len(image_paths)
    for idx, img_path in enumerate(image_paths, start=1):
        is_last = idx == total_parts
        part_suffix = f"\n📦 <i>Parte {idx}/{total_parts}</i>" if total_parts > 1 else ""
        caption = f"{base_caption_html}{part_suffix}"
        if len(caption) > 1020:
            caption = caption[:1015] + "..."

        with open(img_path, "rb") as doc_file:
            await bot_message_target.reply_document(
                document=doc_file,
                filename=img_path.name,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup if is_last else None,
            )


async def capture_and_send_full_web_png(
    target_msg: Message,
    session: BrowserSession,
    url: Optional[str] = None,
    from_active_page: bool = False,
) -> None:
    """
    Captures an ENTIRE web page as lossless HiDPI PNG document(s) and delivers them
    with Reload + Interactive Mode buttons.
    """
    _ensure_services()
    assert web_renderer is not None

    display_target = url or session.last_url or "página atual"
    status_msg = await target_msg.reply_text(
        f"🌐 <b>Extraindo página inteira em PNG...</b>\n"
        f"<code>{html.escape(_short_url(display_target, 75))}</code>",
        parse_mode=ParseMode.HTML,
    )

    with tempfile.TemporaryDirectory(prefix="webarc_png_") as temp_dir:
        temp_path = Path(temp_dir)
        try:
            await target_msg.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
            if from_active_page and session.has_open_page:
                image_paths, final_url, title = await session.export_full_page(temp_path)
            else:
                target_url = url or session.last_url
                if not target_url:
                    await status_msg.edit_text(
                        "⚠️ Nenhuma URL informada ou aberta no momento.",
                        parse_mode=ParseMode.HTML,
                    )
                    return
                image_paths, final_url, title = await web_renderer.render_url_to_images(
                    url=target_url,
                    output_dir=temp_path,
                )
                session.last_url = final_url
                session.last_title = title
                session.touch()

            if not image_paths:
                await status_msg.edit_text("❌ Não foi possível capturar o PNG desta página.", parse_mode=ParseMode.HTML)
                return

            if len(image_paths) > 1:
                await status_msg.edit_text(
                    f"📦 <b>Página extensa!</b> Dividida em {len(image_paths)} partes PNG de alta definição.\n"
                    "Enviando documentos sem compressão...",
                    parse_mode=ParseMode.HTML,
                )

            title_html = html.escape((title or "Página Web")[:80])
            url_html = html.escape(_short_url(final_url, 80))
            base_caption = f"🌐 <b>{title_html}</b>\n<code>{url_html}</code>"

            await send_png_documents(
                bot_message_target=target_msg,
                image_paths=image_paths,
                base_caption_html=base_caption,
                reply_markup=build_web_capture_keyboard(),
            )
            try:
                await status_msg.delete()
            except Exception:
                pass
        except NavigationError as nav_err:
            await status_msg.edit_text(
                f"❌ <b>Falha ao acessar a página:</b>\n<code>{html.escape(str(nav_err))}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.exception(f"Unexpected error capturing {display_target}: {exc}")
            await status_msg.edit_text(
                f"⚠️ <b>Erro ao capturar página:</b>\n<code>{html.escape(str(exc))}</code>",
                parse_mode=ParseMode.HTML,
            )


async def render_and_show_interactive(
    chat_target_msg: Message,
    session: BrowserSession,
    status_note: str = "",
    edit_existing: bool = False,
    callback_message: Optional[Message] = None,
) -> None:
    """
    Captures an interactive viewport snapshot with numbered element badges and either
    updates the active interactive Telegram message in-place or sends a fresh one.
    """
    with tempfile.TemporaryDirectory(prefix="webarc_iv_") as temp_dir:
        snap_path = Path(temp_dir) / "viewport.png"
        snap = await session.snapshot(snap_path, status_note=status_note)
        caption = build_interactive_caption(session, snap)
        keyboard = build_interactive_keyboard(session)

        # In-place seamless screen refresh when user taps an inline button
        if edit_existing and callback_message is not None and callback_message.photo:
            try:
                with open(snap.image_path, "rb") as img_f:
                    await callback_message.edit_media(
                        media=InputMediaPhoto(
                            media=img_f,
                            caption=caption,
                            parse_mode=ParseMode.HTML,
                        ),
                        reply_markup=keyboard,
                    )
                session.interactive_msg_id = callback_message.message_id
                return
            except BadRequest as exc:
                # Telegram raises "Message is not modified" if screenshot+caption are identical
                if "not modified" in str(exc).lower():
                    return
                logger.debug(f"edit_media fallback to new message: {exc}")

        await chat_target_msg.chat.send_action(ChatAction.UPLOAD_PHOTO)
        with open(snap.image_path, "rb") as img_f:
            sent = await chat_target_msg.reply_photo(
                photo=img_f,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        session.interactive_msg_id = sent.message_id


# ---------------------------------------------------------------------------
# Command Handlers
# ---------------------------------------------------------------------------


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update) or not update.message:
        return

    welcome = (
        "🌐 <b>WebArc</b>\n"
        "<i>PNG completo de qualquer página &amp; navegador interativo no Telegram</i>\n\n"
        "• Envie uma <b>URL</b> → PNG completo sem compressão\n"
        "• <code>/i [url]</code> → abre navegador ao vivo\n"
        "• <code>/s [termo]</code> → busca na web\n\n"
        "<i>Toque 🔍 para pesquisar ou 📖 para ver todos os comandos e botões.</i>"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔍", callback_data="act:mode_search"),
                InlineKeyboardButton("📖", callback_data="act:help"),
            ]
        ]
    )
    await update.message.reply_text(welcome, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update) or not update.message:
        return
    await update.message.reply_text(_build_help_text(), parse_mode=ParseMode.HTML)


def _build_help_text() -> str:
    return (
        "📖 <b>WebArc — Ajuda</b>\n\n"
        "<b>No chat</b>\n"
        "<code>URL</code> — captura a página inteira em PNG\n"
        "<code>5</code> — clica no item numerado (modo 🖱️)\n"
        "<code>1 termo</code> — preenche o campo e envia Enter\n"
        "<code>texto</code> — clica por texto (🖱️) ou digita (⌨️)\n\n"
        "<b>Comandos</b>\n"
        "<code>/png [url]</code> — PNG completo sem compressão\n"
        "<code>/i [url]</code> — abre navegador ao vivo\n"
        "<code>/s [termo]</code> — busca na web\n"
        "<code>/f [termo]</code> — destaca texto na página\n"
        "<code>/c [alvo]</code> — clica (nº, texto, <code>#seletor</code> ou <code>x,y</code>)\n"
        "<code>/t [nº] texto</code> — digita direto na página\n"
        "<code>/u</code> atualizar · <code>/b</code> voltar · <code>/n</code> avançar\n"
        "<code>/r [cima|baixo|topo|fim]</code> rolar · <code>/exit</code> fechar\n\n"
        "<b>Botões</b>\n"
        "🔵 Link · 🟢 Botão · 🟠 Campo\n"
        "🖱️/⌨️ Alterna Clique e Digitação\n"
        "🔍 Busca Web · 🔦 Achar na página\n"
        "⬅️ 🔄 ➡️ ⬆️ ⬇️ Navegação e rolagem\n"
        "📄 PNG inteiro · 🖼️ PNG tela · 🏷️ Tags · ❌ Fechar\n"
        "<i>No ⌨️:</i> ⏎ Enter · 🧹 Limpar · ⇥ Tab · ⎋ Esc · ⏎🟢/⚪ Auto-Enter"
    )


async def png_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Extracts full-page lossless PNG of provided URL or currently active page."""
    if not _is_authorized(update) or not update.message:
        return
    _ensure_services()
    assert session_manager is not None

    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    arg = " ".join(context.args).strip() if context.args else ""
    session = await session_manager.get_session(chat_id)

    async with session.lock:
        if arg:
            try:
                url = extract_and_normalize_url(arg)
            except InvalidURLError as err:
                await update.message.reply_text(f"❌ {html.escape(str(err))}", parse_mode=ParseMode.HTML)
                return
            if not url:
                await update.message.reply_text(
                    "❌ URL inválida. Exemplo: <code>/png https://example.com</code>",
                    parse_mode=ParseMode.HTML,
                )
                return
            await capture_and_send_full_web_png(update.message, session, url=url, from_active_page=False)
        elif session.has_open_page:
            await capture_and_send_full_web_png(update.message, session, from_active_page=True)
        elif session.last_url:
            await capture_and_send_full_web_png(update.message, session, url=session.last_url)
        else:
            await update.message.reply_text(
                "ℹ️ Envie uma URL com o comando: <code>/png https://example.com</code>",
                parse_mode=ParseMode.HTML,
            )


async def interactive_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Opens a URL (or activates the current page) in Interactive Click Mode."""
    if not _is_authorized(update) or not update.message:
        return
    _ensure_services()
    assert session_manager is not None

    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    arg = " ".join(context.args).strip() if context.args else ""
    session = await session_manager.get_session(chat_id, will_open_tab=True)

    async with session.lock:
        target_url: Optional[str] = None
        if arg:
            try:
                target_url = extract_and_normalize_url(arg)
            except InvalidURLError as err:
                await update.message.reply_text(f"❌ {html.escape(str(err))}", parse_mode=ParseMode.HTML)
                return
            if not target_url:
                # Treat non-URL argument as an instant interactive search!
                url, meta = build_search_url(arg, session.search_engine)
                session.last_search_query = arg
                target_url = url
                note = f"Pesquisa ({meta['name']}): {arg}"
            else:
                note = "Modo interativo ativado"
        elif session.has_open_page:
            session.mode = SessionMode.CLICK
            await render_and_show_interactive(update.message, session, status_note="Modo clique ativado")
            return
        elif session.last_url:
            target_url = session.last_url
            note = "Página aberta no modo interativo"
        else:
            await update.message.reply_text(
                "🖱️ Informe uma URL ou termo para abrir no modo interativo:\n"
                "<code>/i https://news.ycombinator.com</code>\n"
                "ou <code>/s algo</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        status_msg = await update.message.reply_text(
            f"⏳ Abrindo <code>{html.escape(_short_url(target_url, 70))}</code> no modo interativo...",
            parse_mode=ParseMode.HTML,
        )
        try:
            await session.navigate(target_url)
            session.mode = SessionMode.CLICK
            await render_and_show_interactive(update.message, session, status_note=note)
            try:
                await status_msg.delete()
            except Exception:
                pass
        except Exception as exc:
            await status_msg.edit_text(
                f"❌ <b>Erro ao abrir página:</b>\n<code>{html.escape(str(exc))}</code>",
                parse_mode=ParseMode.HTML,
            )


async def reload_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reloads the active interactive page or last webpage PNG."""
    if not _is_authorized(update) or not update.message:
        return
    _ensure_services()
    assert session_manager is not None

    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    session = await session_manager.get_session(chat_id)

    async with session.lock:
        if session.has_open_page:
            status_msg = await update.message.reply_text("🔄 Atualizando página atual...", parse_mode=ParseMode.HTML)
            try:
                await session.reload()
                await render_and_show_interactive(update.message, session, status_note="Página atualizada!")
                try:
                    await status_msg.delete()
                except Exception:
                    pass
            except Exception as exc:
                await status_msg.edit_text(
                    f"❌ Erro ao atualizar: <code>{html.escape(str(exc))}</code>",
                    parse_mode=ParseMode.HTML,
                )
        elif session.last_url:
            await capture_and_send_full_web_png(update.message, session, url=session.last_url)
        else:
            await update.message.reply_text(
                "ℹ️ Nenhuma página foi carregada ainda para atualizar.",
                parse_mode=ParseMode.HTML,
            )


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Interactive Web Search mode (with immediate query or interactive prompt)."""
    if not _is_authorized(update) or not update.message:
        return
    _ensure_services()
    assert session_manager is not None

    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    query = " ".join(context.args).strip() if context.args else ""
    session = await session_manager.get_session(chat_id, will_open_tab=bool(query))

    async with session.lock:
        if not query:
            session.mode = SessionMode.WEB_SEARCH
            await update.message.reply_text(
                _build_search_prompt_text(session.search_engine),
                parse_mode=ParseMode.HTML,
                reply_markup=build_search_prompt_keyboard(session.search_engine),
            )
            return

        await _execute_web_search(update.message, session, query)


async def _execute_web_search(target_msg: Message, session: BrowserSession, query: str) -> None:
    url, meta = build_search_url(query, session.search_engine)
    session.last_search_query = query
    status_msg = await target_msg.reply_text(
        f"🔍 Pesquisando <b>{html.escape(query)}</b> no {meta['emoji']} {meta['name']}...",
        parse_mode=ParseMode.HTML,
    )
    try:
        await session.navigate(url)
        session.mode = SessionMode.CLICK
        await render_and_show_interactive(
            target_msg,
            session,
            status_note=f"Resultados ({meta['name']}): {query}",
        )
        try:
            await status_msg.delete()
        except Exception:
            pass
    except Exception as exc:
        await status_msg.edit_text(
            f"❌ <b>Erro na pesquisa:</b>\n<code>{html.escape(str(exc))}</code>",
            parse_mode=ParseMode.HTML,
        )


async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Interactive Find-in-Page mode (highlights matches inside the current page)."""
    if not _is_authorized(update) or not update.message:
        return
    _ensure_services()
    assert session_manager is not None

    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    query = " ".join(context.args).strip() if context.args else ""
    session = await session_manager.get_session(chat_id, will_open_tab=True)

    async with session.lock:
        if not session.has_open_page and not session.last_url:
            await update.message.reply_text(
                "ℹ️ Abra uma página primeiro (envie uma URL ou use <code>/s</code>) para pesquisar dentro dela.",
                parse_mode=ParseMode.HTML,
            )
            return

        if not session.has_open_page and session.last_url:
            await session.navigate(session.last_url)

        if not query:
            session.mode = SessionMode.PAGE_FIND
            await update.message.reply_text(
                "🔎 <b>Modo de Pesquisa na Página ativado!</b>\n"
                "Escreva no chat o termo que deseja localizar e destacar na página atual.",
                parse_mode=ParseMode.HTML,
            )
            return

        res = await session.find_in_page(query)
        session.mode = SessionMode.PAGE_FIND if res.total > 0 else SessionMode.CLICK
        note = (
            f'Encontrado "{query}": {res.current_index}/{res.total}'
            if res.total > 0
            else f'Nenhuma ocorrência de "{query}" nesta página.'
        )
        await render_and_show_interactive(update.message, session, status_note=note)


async def click_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clicks an element by badge number, visible text, CSS selector, or x,y coordinates."""
    if not _is_authorized(update) or not update.message:
        return
    _ensure_services()
    assert session_manager is not None

    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    arg = " ".join(context.args).strip() if context.args else ""
    session = await session_manager.get_session(chat_id)

    async with session.lock:
        if not session.has_open_page:
            if session.last_url:
                await session.navigate(session.last_url)
            else:
                await update.message.reply_text(
                    "ℹ️ Nenhuma página aberta. Use <code>/i URL</code> primeiro.",
                    parse_mode=ParseMode.HTML,
                )
                return

        if not arg:
            session.mode = SessionMode.CLICK
            await render_and_show_interactive(update.message, session, status_note="Modo clique ativado")
            return

        if not session.elements and session.page is not None:
            session.elements = await session.page.collect_and_tag_elements(show_overlay=False)

        await _perform_click_argument(update.message, session, arg)


async def _perform_click_argument(
    target_msg: Message,
    session: BrowserSession,
    arg: str,
    edit_existing: bool = False,
    callback_message: Optional[Message] = None,
) -> None:
    """Interprets `arg` as badge ID, x,y coordinates, CSS selector, or visible text and clicks it."""
    num_match = NUMBER_ONLY_RE.match(arg)
    coords_match = COORDS_RE.match(arg)

    if num_match:
        res = await session.click_by_id(int(num_match.group(1)))
    elif coords_match:
        res = await session.click_at(float(coords_match.group(1)), float(coords_match.group(2)))
    elif arg.startswith(("#", ".", "[")) or " > " in arg:
        res = await session.click_by_selector(arg)
    else:
        res = await session.click_by_text(arg)

    if not res.success:
        session.pending_text = arg
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("⌨️", callback_data="act:quick_type"),
                    InlineKeyboardButton("🔦", callback_data="act:quick_find"),
                    InlineKeyboardButton("🔍", callback_data="act:quick_search"),
                ]
            ]
        )
        await target_msg.reply_text(
            f"⚠️ {html.escape(res.message)}\n"
            "<i>⌨️ digitar · 🔦 achar na página · 🔍 pesquisar na web</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        return

    # Auto-switch into Typing Mode if the clicked element is a text input!
    if res.is_input:
        session.mode = SessionMode.TYPE
        note = f"{res.message} — Campo focado! Escreva no chat para digitar."
    else:
        session.mode = SessionMode.CLICK
        note = res.message

    await render_and_show_interactive(
        target_msg,
        session,
        status_note=note,
        edit_existing=edit_existing,
        callback_message=callback_message,
    )


async def type_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Enters Typing Mode or directly types text (optionally targeting badge ID) into the page."""
    if not _is_authorized(update) or not update.message:
        return
    _ensure_services()
    assert session_manager is not None

    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    arg = " ".join(context.args).strip() if context.args else ""
    session = await session_manager.get_session(chat_id)

    async with session.lock:
        if not session.has_open_page:
            if session.last_url:
                await session.navigate(session.last_url)
            else:
                await update.message.reply_text(
                    "ℹ️ Abra uma página primeiro com <code>/i URL</code>.",
                    parse_mode=ParseMode.HTML,
                )
                return

        if not arg:
            session.mode = SessionMode.TYPE
            await render_and_show_interactive(
                update.message,
                session,
                status_note="Modo digitação ativo! Tudo que escrever no chat vai para a página.",
            )
            return

        # Support `/t 3 texto` or `/t texto`
        combo = NUMBER_AND_TEXT_RE.match(arg)
        element_id: Optional[int] = None
        text_to_type = arg
        if combo:
            candidate_id = int(combo.group(1))
            if any(el.id == candidate_id for el in session.elements):
                element_id = candidate_id
                text_to_type = combo.group(2)

        navigated, note = await session.type_text(text_to_type, element_id=element_id)
        if navigated:
            session.mode = SessionMode.CLICK
        await render_and_show_interactive(update.message, session, status_note=note)


async def back_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update) or not update.message:
        return
    _ensure_services()
    assert session_manager is not None
    session = await session_manager.get_session(update.effective_chat.id)  # type: ignore[union-attr]
    async with session.lock:
        if not session.has_open_page or session.page is None:
            await update.message.reply_text("ℹ️ Nenhuma aba interativa aberta.", parse_mode=ParseMode.HTML)
            return
        moved = await session.page.go_back()
        note = "Voltou para a página anterior" if moved else "Não há página anterior no histórico"
        await render_and_show_interactive(update.message, session, status_note=note)


async def forward_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update) or not update.message:
        return
    _ensure_services()
    assert session_manager is not None
    session = await session_manager.get_session(update.effective_chat.id)  # type: ignore[union-attr]
    async with session.lock:
        if not session.has_open_page or session.page is None:
            await update.message.reply_text("ℹ️ Nenhuma aba interativa aberta.", parse_mode=ParseMode.HTML)
            return
        moved = await session.page.go_forward()
        note = "Avançou no histórico" if moved else "Não há próxima página no histórico"
        await render_and_show_interactive(update.message, session, status_note=note)


async def scroll_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update) or not update.message:
        return
    _ensure_services()
    assert session_manager is not None
    session = await session_manager.get_session(update.effective_chat.id)  # type: ignore[union-attr]
    raw_dir = (context.args[0].lower() if context.args else "down")
    mapping = {
        "cima": "up",
        "up": "up",
        "subir": "up",
        "baixo": "down",
        "down": "down",
        "descer": "down",
        "topo": "top",
        "top": "top",
        "fim": "bottom",
        "bottom": "bottom",
    }
    direction = mapping.get(raw_dir, "down")
    async with session.lock:
        if not session.has_open_page or session.page is None:
            await update.message.reply_text("ℹ️ Nenhuma aba interativa aberta.", parse_mode=ParseMode.HTML)
            return
        info = await session.page.scroll(direction)
        session.badge_page = 0
        await render_and_show_interactive(
            update.message,
            session,
            status_note=f"Rolagem: {info.percent}%",
        )


async def close_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update) or not update.message:
        return
    _ensure_services()
    assert session_manager is not None
    closed = await session_manager.close_session(update.effective_chat.id)  # type: ignore[union-attr]
    if closed:
        await update.message.reply_text(
            "✅ Sessão interativa encerrada e memória liberada.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text("ℹ️ Nenhuma sessão interativa ativa.", parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# Natural Chat Message Handler ("o que for escrito no chat")
# ---------------------------------------------------------------------------


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Intelligently routes free-form chat messages depending on whether the text is a URL
    or matches the active interactive mode (WEB_SEARCH, PAGE_FIND, TYPE, CLICK, or IDLE).
    """
    if not _is_authorized(update) or not update.message or not update.message.text:
        return
    _ensure_services()
    assert session_manager is not None

    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    text = update.message.text.strip()
    if not text:
        return

    session = await session_manager.get_session(chat_id)

    # 1. Check if the message is ANY web URL (unless in TYPE / PAGE_FIND mode)
    if session.mode not in (SessionMode.TYPE, SessionMode.PAGE_FIND):
        try:
            url = extract_and_normalize_url(text)
        except InvalidURLError as err:
            await update.message.reply_text(f"❌ {html.escape(str(err))}", parse_mode=ParseMode.HTML)
            return

        if url:
            async with session.lock:
                # If already browsing interactively, navigate live inside the session
                if session.has_open_page and session.mode in (SessionMode.CLICK, SessionMode.WEB_SEARCH):
                    status_msg = await update.message.reply_text(
                        f"⏳ Navegando para <code>{html.escape(_short_url(url, 70))}</code>...",
                        parse_mode=ParseMode.HTML,
                    )
                    try:
                        await session.navigate(url)
                        session.mode = SessionMode.CLICK
                        await render_and_show_interactive(
                            update.message,
                            session,
                            status_note="Nova página aberta",
                        )
                        try:
                            await status_msg.delete()
                        except Exception:
                            pass
                    except Exception as exc:
                        await status_msg.edit_text(
                            f"❌ <b>Erro ao carregar URL:</b>\n<code>{html.escape(str(exc))}</code>",
                            parse_mode=ParseMode.HTML,
                        )
                else:
                    # Default behavior for any URL: full-page PNG extraction!
                    await capture_and_send_full_web_png(update.message, session, url=url)
            return

    # 3. Mode-driven natural chat handling ("o que for escrito no chat")
    if session.mode in (SessionMode.WEB_SEARCH, SessionMode.PAGE_FIND):
        session = await session_manager.get_session(chat_id, will_open_tab=True)

    async with session.lock:
        if session.mode == SessionMode.WEB_SEARCH:
            await _execute_web_search(update.message, session, text)
            return

        if session.mode == SessionMode.PAGE_FIND:
            if not session.has_open_page and session.last_url:
                await session.navigate(session.last_url)
            if session.has_open_page:
                res = await session.find_in_page(text)
                note = (
                    f'Encontrado "{text}": {res.current_index}/{res.total}'
                    if res.total > 0
                    else f'Nenhuma ocorrência de "{text}" nesta página.'
                )
                await render_and_show_interactive(update.message, session, status_note=note)
                return

        if session.mode == SessionMode.TYPE and session.has_open_page:
            navigated, note = await session.type_text(text)
            if navigated:
                session.mode = SessionMode.CLICK
            await render_and_show_interactive(update.message, session, status_note=note)
            return

        if session.mode == SessionMode.CLICK and session.has_open_page:
            # Support "3 termo de busca" (click/focus badge #3, type text, press Enter!)
            combo = NUMBER_AND_TEXT_RE.match(text)
            if combo:
                el_id = int(combo.group(1))
                payload = combo.group(2).strip()
                if any(e.id == el_id for e in session.elements):
                    navigated, note = await session.type_text(payload, press_enter=True, element_id=el_id)
                    if navigated:
                        session.mode = SessionMode.CLICK
                    await render_and_show_interactive(update.message, session, status_note=note)
                    return

            # Pure number, coordinates, CSS selector, or visible text click
            await _perform_click_argument(update.message, session, text)
            return

        # 4. No active interactive session & not a URL -> offer 1-tap instant Web Search!
        session.pending_text = text
        eng = SEARCH_ENGINES.get(session.search_engine, SEARCH_ENGINES["duckduckgo"])
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(f"🔍 {eng['emoji']}", callback_data="act:quick_search"),
                    InlineKeyboardButton("📖", callback_data="act:help"),
                ]
            ]
        )
        await update.message.reply_text(
            f"💬 <i>{html.escape(text[:120])}</i>\n\n"
            f"Envie uma <b>URL</b> para capturar em PNG ou toque 🔍 ({eng['name']}) para pesquisar · 📖 ajuda.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )


# ---------------------------------------------------------------------------
# Callback Query Handler (Inline Interactive Controls)
# ---------------------------------------------------------------------------


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Processes all inline keyboard actions (reload, clicks, scroll, modes, search, PNG)."""
    if not _is_authorized(update):
        if update.callback_query:
            await update.callback_query.answer("Acesso não autorizado.", show_alert=True)
        return

    query = update.callback_query
    if not query or not query.data or not isinstance(query.message, Message):
        return

    _ensure_services()
    assert session_manager is not None

    data = query.data
    msg = query.message
    chat_id = msg.chat.id

    if data == "act:noop":
        await query.answer()
        return

    if data == "act:help":
        await query.answer()
        await msg.reply_text(_build_help_text(), parse_mode=ParseMode.HTML)
        return

    will_open = data in (
        "act:open_interactive",
        "act:quick_search",
        "act:mode_find",
    )
    session = await session_manager.get_session(chat_id, will_open_tab=will_open)

    async with session.lock:
        try:
            # --- Reload full-page web capture ---
            if data == "act:reload_web":
                await query.answer("🔄 Atualizando página...")
                if session.has_open_page:
                    await session.reload()
                    await capture_and_send_full_web_png(msg, session, from_active_page=True)
                elif session.last_url:
                    await capture_and_send_full_web_png(msg, session, url=session.last_url)
                else:
                    await msg.reply_text("ℹ️ Nenhuma URL salva para atualizar.", parse_mode=ParseMode.HTML)
                return

            # --- Open last URL in live Interactive Mode ---
            if data == "act:open_interactive":
                target_url = session.last_url

                if not target_url:
                    await query.answer("Nenhuma URL disponível.", show_alert=True)
                    return

                await query.answer("🖱️ Abrindo modo interativo...")
                status = await msg.reply_text(
                    f"⏳ Carregando modo interativo para <code>{html.escape(_short_url(target_url, 65))}</code>...",
                    parse_mode=ParseMode.HTML,
                )
                try:
                    await session.navigate(target_url)
                    session.mode = SessionMode.CLICK
                    await render_and_show_interactive(msg, session, status_note="Modo interativo pronto!")
                finally:
                    try:
                        await status.delete()
                    except Exception:
                        pass
                return

            # --- Export full-page lossless PNG from active session ---
            if data == "act:full_png":
                await query.answer("📄 Extraindo página completa em PNG...")
                await capture_and_send_full_web_png(msg, session, from_active_page=session.has_open_page)
                return

            # --- Export current viewport as uncompressed PNG document ---
            if data == "act:vp_png":
                if not session.has_open_page or session.page is None:
                    await query.answer("Nenhuma aba aberta.", show_alert=True)
                    return
                await query.answer("🖼️ Gerando PNG da tela atual...")
                with tempfile.TemporaryDirectory(prefix="webarc_vp_") as temp_dir:
                    vp_path = Path(temp_dir) / "tela_atual.png"
                    await session.page.clear_overlays()
                    await session.page.screenshot_viewport(vp_path)
                    if session.show_tags:
                        await session.page.collect_and_tag_elements(show_overlay=True)
                    title_h = html.escape((session.last_title or "Tela Atual")[:70])
                    url_h = html.escape(_short_url(session.last_url, 70))
                    await send_png_documents(
                        msg,
                        [vp_path],
                        f"🖼️ <b>{title_h}</b> (Tela atual · PNG)\n<code>{url_h}</code>",
                    )
                return

            # --- Mode switches ---
            if data == "act:toggle_click_type":
                if session.mode == SessionMode.CLICK:
                    if not session.has_open_page:
                        await query.answer("Abra uma página primeiro.", show_alert=True)
                        return
                    await query.answer("⌨️ Modo Digitação")
                    session.mode = SessionMode.TYPE
                    await session.ensure_page()
                    assert session.page is not None
                    await session.page.focus_best_input()
                    await render_and_show_interactive(
                        msg,
                        session,
                        status_note="Modo Digitação — escreva no chat",
                        edit_existing=True,
                        callback_message=msg,
                    )
                else:
                    await query.answer("🖱️ Modo Clique")
                    session.mode = SessionMode.CLICK
                    if session.has_open_page:
                        await render_and_show_interactive(
                            msg,
                            session,
                            status_note="Modo Clique",
                            edit_existing=True,
                            callback_message=msg,
                        )
                    else:
                        await msg.reply_text(
                            "🖱️ Modo Clique selecionado. Envie uma URL ou use /s para começar.",
                            parse_mode=ParseMode.HTML,
                        )
                return

            if data == "act:mode_click":
                await query.answer("🖱️ Modo Clique ativado")
                session.mode = SessionMode.CLICK
                if session.has_open_page:
                    await render_and_show_interactive(
                        msg,
                        session,
                        status_note="Modo Clique ativo",
                        edit_existing=True,
                        callback_message=msg,
                    )
                else:
                    await msg.reply_text(
                        "🖱️ Modo Clique selecionado. Envie uma URL ou use /s para começar.",
                        parse_mode=ParseMode.HTML,
                    )
                return

            if data == "act:mode_type":
                if not session.has_open_page:
                    await query.answer("Abra uma página primeiro.", show_alert=True)
                    return
                await query.answer("⌨️ Modo Digitação ativado")
                session.mode = SessionMode.TYPE
                await session.ensure_page()
                assert session.page is not None
                await session.page.focus_best_input()
                await render_and_show_interactive(
                    msg,
                    session,
                    status_note="Modo Digitação ativo — escreva no chat!",
                    edit_existing=True,
                    callback_message=msg,
                )
                return

            if data == "act:mode_search":
                await query.answer("🔍 Pesquisa Web")
                session.mode = SessionMode.WEB_SEARCH
                eng = SEARCH_ENGINES.get(session.search_engine, SEARCH_ENGINES["duckduckgo"])
                if session.has_open_page and msg.photo:
                    await render_and_show_interactive(
                        msg,
                        session,
                        status_note=f"Digite no chat para pesquisar ({eng['name']})",
                        edit_existing=True,
                        callback_message=msg,
                    )
                else:
                    await msg.reply_text(
                        _build_search_prompt_text(session.search_engine),
                        parse_mode=ParseMode.HTML,
                        reply_markup=build_search_prompt_keyboard(session.search_engine),
                    )
                return

            if data == "act:mode_find":
                if not session.has_open_page and session.last_url:
                    await query.answer("🔦 Abrindo página...")
                    await session.navigate(session.last_url)
                if not session.has_open_page:
                    await query.answer("Abra uma página primeiro.", show_alert=True)
                    return
                await query.answer("🔦 Busca na página")
                session.mode = SessionMode.PAGE_FIND
                if msg.photo:
                    await render_and_show_interactive(
                        msg,
                        session,
                        status_note="Escreva no chat o termo para destacar na página",
                        edit_existing=True,
                        callback_message=msg,
                    )
                else:
                    await msg.reply_text(
                        "🔦 <b>Busca na Página</b>\nEscreva no chat o termo que deseja encontrar.",
                        parse_mode=ParseMode.HTML,
                    )
                return

            # --- Search Engine Switch ---
            if data.startswith("act:engine:"):
                eng_key = data.split(":", 2)[2]
                if eng_key in SEARCH_ENGINES:
                    session.search_engine = eng_key
                    meta = SEARCH_ENGINES[eng_key]
                    # If user already ran a search and has a live page, re-run on new engine!
                    if session.last_search_query and session.has_open_page and msg.photo:
                        await query.answer(f"{meta['emoji']} {meta['name']}...")
                        url, _ = build_search_url(session.last_search_query, eng_key)
                        await session.navigate(url)
                        session.mode = SessionMode.CLICK
                        await render_and_show_interactive(
                            msg,
                            session,
                            status_note=f"Resultados ({meta['name']}): {session.last_search_query}",
                            edit_existing=True,
                            callback_message=msg,
                        )
                    else:
                        await query.answer(f"{meta['emoji']} {meta['name']}")
                        if msg.photo and session.has_open_page:
                            await render_and_show_interactive(
                                msg,
                                session,
                                status_note=f"{meta['emoji']} {meta['name']} — digite sua busca!",
                                edit_existing=True,
                                callback_message=msg,
                            )
                        else:
                            await msg.edit_text(
                                _build_search_prompt_text(session.search_engine),
                                parse_mode=ParseMode.HTML,
                                reply_markup=build_search_prompt_keyboard(session.search_engine),
                            )
                return

            # --- Quick pending-text actions ---
            if data == "act:quick_search":
                if not session.pending_text:
                    await query.answer("Nenhum texto pendente.", show_alert=True)
                    return
                await query.answer("🔍 Pesquisando...")
                await _execute_web_search(msg, session, session.pending_text)
                return

            if data == "act:quick_type":
                if not session.pending_text or not session.has_open_page:
                    await query.answer("Nenhuma aba aberta ou texto pendente.", show_alert=True)
                    return
                await query.answer("⌨️ Digitando na página...")
                navigated, note = await session.type_text(session.pending_text)
                if navigated:
                    session.mode = SessionMode.CLICK
                await render_and_show_interactive(msg, session, status_note=note)
                return

            if data == "act:quick_find":
                if not session.pending_text or not session.has_open_page:
                    await query.answer("Nenhuma aba aberta ou texto pendente.", show_alert=True)
                    return
                await query.answer("🔎 Buscando na página...")
                res = await session.find_in_page(session.pending_text)
                session.mode = SessionMode.PAGE_FIND if res.total > 0 else SessionMode.CLICK
                note = (
                    f'Encontrado "{session.pending_text}": {res.current_index}/{res.total}'
                    if res.total > 0
                    else f'Nenhuma ocorrência de "{session.pending_text}".'
                )
                await render_and_show_interactive(msg, session, status_note=note)
                return

            # --- All actions below require an active interactive page ---
            if not session.has_open_page or session.page is None:
                await query.answer("Sessão expirada. Reabra uma URL.", show_alert=True)
                return

            # --- Click numbered badge ---
            if data.startswith("act:click:"):
                el_id = int(data.split(":", 2)[2])
                await query.answer(f"🖱️ Clicando em [{el_id}]...")
                await _perform_click_argument(
                    msg,
                    session,
                    str(el_id),
                    edit_existing=True,
                    callback_message=msg,
                )
                return

            # --- Paginate numbered badge buttons ---
            if data.startswith("act:bpage:"):
                session.badge_page = int(data.split(":", 2)[2])
                await query.answer()
                await render_and_show_interactive(
                    msg,
                    session,
                    status_note=f"Exibindo página de tags {session.badge_page + 1}",
                    edit_existing=True,
                    callback_message=msg,
                )
                return

            # --- Scroll ---
            if data.startswith("act:scroll:"):
                direction = data.split(":", 2)[2]
                await query.answer("↕️ Rolando...")
                info = await session.page.scroll(direction)
                session.badge_page = 0
                await render_and_show_interactive(
                    msg,
                    session,
                    status_note=f"Rolagem: {info.percent}%",
                    edit_existing=True,
                    callback_message=msg,
                )
                return

            # --- Reload in interactive mode ---
            if data == "act:ireload":
                await query.answer("🔄 Atualizando página...")
                await session.reload()
                await render_and_show_interactive(
                    msg,
                    session,
                    status_note="Página atualizada!",
                    edit_existing=True,
                    callback_message=msg,
                )
                return

            # --- History Back / Forward ---
            if data == "act:back":
                await query.answer("⬅️ Voltando...")
                moved = await session.page.go_back()
                session.badge_page = 0
                await render_and_show_interactive(
                    msg,
                    session,
                    status_note="Página anterior" if moved else "Início do histórico",
                    edit_existing=True,
                    callback_message=msg,
                )
                return

            if data == "act:forward":
                await query.answer("➡️ Avançando...")
                moved = await session.page.go_forward()
                session.badge_page = 0
                await render_and_show_interactive(
                    msg,
                    session,
                    status_note="Próxima página" if moved else "Fim do histórico",
                    edit_existing=True,
                    callback_message=msg,
                )
                return

            # --- Toggle visual numbered badges ---
            if data == "act:tags":
                session.show_tags = not session.show_tags
                await query.answer(f"Tags {'ativadas' if session.show_tags else 'ocultadas'}")
                await render_and_show_interactive(
                    msg,
                    session,
                    status_note=f"Tags numeradas: {'ON' if session.show_tags else 'OFF'}",
                    edit_existing=True,
                    callback_message=msg,
                )
                return

            # --- Typing helpers ---
            if data == "act:auto_enter":
                session.auto_enter = not session.auto_enter
                await query.answer(f"Auto-Enter: {'ON' if session.auto_enter else 'OFF'}")
                await render_and_show_interactive(
                    msg,
                    session,
                    status_note=f"Auto-Enter {'ativado' if session.auto_enter else 'desativado'}",
                    edit_existing=True,
                    callback_message=msg,
                )
                return

            if data == "act:clear_input":
                await query.answer("🧹 Limpando campo...")
                await session.page.clear_focused_input()
                await render_and_show_interactive(
                    msg,
                    session,
                    status_note="Campo limpo",
                    edit_existing=True,
                    callback_message=msg,
                )
                return

            if data.startswith("act:key:"):
                key_name = data.split(":", 2)[2]
                await query.answer(f"⌨️ Tecla {key_name}")
                await session.page.press_key(key_name)
                await render_and_show_interactive(
                    msg,
                    session,
                    status_note=f"Tecla enviada: {key_name}",
                    edit_existing=True,
                    callback_message=msg,
                )
                return

            # --- Find-in-page navigation ---
            if data in ("act:find_prev", "act:find_next"):
                step = -1 if data == "act:find_prev" else 1
                res = await session.find_step(step)
                await query.answer(f"Ocorrência {res.current_index}/{res.total}")
                await render_and_show_interactive(
                    msg,
                    session,
                    status_note=f'Busca "{res.query}": {res.current_index}/{res.total}',
                    edit_existing=True,
                    callback_message=msg,
                )
                return

            if data == "act:find_clear":
                await session.clear_find()
                session.mode = SessionMode.CLICK
                await query.answer("🧹 Destaques removidos")
                await render_and_show_interactive(
                    msg,
                    session,
                    status_note="Destaques de busca limpos",
                    edit_existing=True,
                    callback_message=msg,
                )
                return

            # --- Close interactive session ---
            if data == "act:close":
                await session.close_page()
                await query.answer("❌ Sessão encerrada")
                try:
                    await msg.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
                await msg.reply_text(
                    "✅ Sessão interativa encerrada e aba fechada.",
                    parse_mode=ParseMode.HTML,
                )
                return

            await query.answer()

        except (CDPError, NavigationError) as err:
            logger.warning(f"Callback CDP error ({data}): {err}")
            await query.answer("Erro na navegação.", show_alert=False)
            await msg.reply_text(
                f"⚠️ <b>Erro ao executar ação:</b>\n<code>{html.escape(str(err))}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.exception(f"Unexpected callback error ({data}): {exc}")
            await query.answer("Erro inesperado.", show_alert=False)
            await msg.reply_text(
                f"⚠️ <b>Erro inesperado:</b>\n<code>{html.escape(str(exc))}</code>",
                parse_mode=ParseMode.HTML,
            )


def build_application(token: str) -> Application:
    """Constructs and wires all Telegram handlers."""
    app = (
        ApplicationBuilder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler(["help", "ajuda"], help_command))
    app.add_handler(CommandHandler("png", png_command))
    app.add_handler(CommandHandler("i", interactive_command))
    app.add_handler(CommandHandler("u", reload_command))
    app.add_handler(CommandHandler("s", search_command))
    app.add_handler(CommandHandler("f", find_command))
    app.add_handler(CommandHandler("c", click_command))
    app.add_handler(CommandHandler("t", type_command))
    app.add_handler(CommandHandler("b", back_command))
    app.add_handler(CommandHandler("n", forward_command))
    app.add_handler(CommandHandler("r", scroll_command))
    app.add_handler(CommandHandler("exit", close_command))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    return app


def main() -> None:
    """Bot entry point."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token or token.strip() == "your_telegram_bot_token_here":
        print(
            "ERROR: TELEGRAM_BOT_TOKEN is missing or contains the default placeholder.\n"
            "Configure your token in the '.env' file:\n"
            '  TELEGRAM_BOT_TOKEN="123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ"\n',
            file=sys.stderr,
        )
        sys.exit(1)

    app = build_application(token)
    logger.info("WebArc Telegram Bot (Native CDP Engine) is starting polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
