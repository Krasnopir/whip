"""Telegram API polling and messaging."""
import asyncio
import json
import logging
import pathlib
import re
from typing import Any

import httpx

log = logging.getLogger("whip.tg")

_TELEGRAM_TEXT_LIMIT = 4090
_PLAYWRIGHT_SESSION = pathlib.Path.home() / ".whip" / "playwright_session.json"
_playwright_lock = asyncio.Lock()


def _truncate_tg(text: str) -> str:
    if len(text) <= _TELEGRAM_TEXT_LIMIT:
        return text
    return text[: _TELEGRAM_TEXT_LIMIT - 20] + "\n… (обрезано)"


def _collect_strings(obj: Any, out: list[str], needle: str) -> None:
    if isinstance(obj, dict):
        for v in obj.values():
            _collect_strings(v, out, needle)
    elif isinstance(obj, list):
        for v in obj:
            _collect_strings(v, out, needle)
    elif isinstance(obj, str) and needle in obj:
        out.append(obj.strip())

_OFFSET_FILE = pathlib.Path.home() / ".whip" / "tg_offset"

# Подсказка в Telegram, если не задан WHIP_CLAUDE_WEB_COOKIE (это не JWT — сырые Cookie браузера).
CLAUDE_COOKIE_HOWTO = (
    "Нет WHIP_CLAUDE_WEB_COOKIE в ~/.whip/.env\n\n"
    "Это не JWT. Это одна длинная строка «Cookie», как отправляет браузер на claude.ai "
    "пока ты залогинен.\n\n"
    "Chrome / Brave / Edge:\n"
    "• Открой https://claude.ai и войди в аккаунт\n"
    "• F12 (DevTools) → вкладка Network\n"
    "• Обнови страницу (F5)\n"
    "• Кликни запрос с именем claude.ai или document (первая строка)\n"
    "• Headers → Request Headers → найди cookie: (или Cookie:)\n"
    "• Скопируй всё значение после cookie: — целиком одной строкой\n\n"
    "Firefox: F12 → Сеть → запрос → Заголовки → Cookie.\n"
    "Safari: включи меню «Разработка» → Web Inspector → Network — то же.\n\n"
    "В ~/.whip/.env добавь строку без кавычек:\n"
    "WHIP_CLAUDE_WEB_COOKIE=sessionKey=...; другие=значения; ...\n\n"
    "Перезапусти whip start. Храни как пароль."
)


async def get_claude_usage_html() -> tuple[str | None, str]:
    """
    GET claude.ai/settings/usage с cookie. Возвращает (html, ошибка).
    html is None при ошибке.
    """
    import os

    cookie = os.environ.get("WHIP_CLAUDE_WEB_COOKIE", "").strip()
    url = os.environ.get("WHIP_CLAUDE_USAGE_URL", "https://claude.ai/settings/usage").strip()
    if not cookie:
        return None, CLAUDE_COOKIE_HOWTO

    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                url,
                headers={
                    "Cookie": cookie,
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
                timeout=25,
                follow_redirects=True,
            )
    except Exception as e:
        log.debug("Claude web usage fetch error: %s", e)
        return None, f"Не удалось открыть страницу: {e}"

    if r.status_code != 200:
        return None, (
            f"claude.ai ответил HTTP {r.status_code}.\n"
            "Обычно сессия протухла: снова зайди в claude.ai, F12 → Network → "
            "запрос → Headers → скопируй строку cookie в WHIP_CLAUDE_WEB_COOKIE."
        )

    return r.text, ""


def parse_resets_in_seconds_from_blob(blob: str) -> int | None:
    """Парсит фрагменты вида «Resets in 3 hr 51 min», «Resets in 2 days» → секунды до события."""
    blob = re.sub(r"\s+", " ", blob.replace("\xa0", " ").strip())
    m = re.search(r"resets?\s+in\s+(\d+)\s*(?:day|days)\b", blob, re.I)
    if m:
        d = int(m.group(1))
        if 0 < d <= 14:
            return d * 86400
    m = re.search(
        r"resets?\s+in\s+(?:(\d+)\s*(?:hr|h|hours?)\s*)?(?:(\d+)\s*(?:min|m|minutes?))?",
        blob,
        re.I,
    )
    if m:
        h_raw, mn_raw = m.group(1), m.group(2)
        h = int(h_raw) if h_raw else 0
        mn = int(mn_raw) if mn_raw else 0
        if h == 0 and mn == 0:
            return None
        sec = h * 3600 + mn * 60
        if 0 < sec <= 14 * 86400:
            return sec
    return None


def scrape_reset_seconds_from_usage_html(html: str) -> int | None:
    """Ищет время до ресета в HTML и в __NEXT_DATA__."""
    # 1) JSON страницы
    m = re.search(
        r'<script id="__NEXT_DATA__"\s*type="application/json">([^<]+)</script>',
        html,
    )
    if m:
        try:
            payload = json.loads(m.group(1))
            hits: list[str] = []
            for needle in ("Resets in", "resets in", "reset in"):
                _collect_strings(payload, hits, needle)
            for s in hits:
                sec = parse_resets_in_seconds_from_blob(s)
                if sec is not None:
                    return sec
        except (json.JSONDecodeError, ValueError):
            pass

    # 2) regex по сырому HTML
    for mm in re.finditer(r"Resets in[^<]{0,160}", html, re.I):
        sec = parse_resets_in_seconds_from_blob(mm.group(0))
        if sec is not None:
            return sec

    return parse_resets_in_seconds_from_blob(html)


def usage_snippets_from_html(html: str) -> str:
    """Текст для /limits (без API)."""
    snippets: list[str] = []

    m = re.search(
        r'<script id="__NEXT_DATA__"\s*type="application/json">([^<]+)</script>',
        html,
    )
    if m:
        try:
            payload = json.loads(m.group(1))
            hits: list[str] = []
            for needle in ("Resets in", "resets in", "Current session", "% used", "used ("):
                _collect_strings(payload, hits, needle)
            for s in hits:
                s = " ".join(s.split())
                if 8 < len(s) < 600 and s not in snippets:
                    if any(
                        k in s
                        for k in ("Resets", "reset", "session", "used", "limit", "%")
                    ):
                        snippets.append(s)
        except (json.JSONDecodeError, ValueError):
            pass

    for pattern in (
        r"Current session[^\n<]{0,160}",
        r"Resets in[^<\n]{5,160}",
        r"Session[^\n<]{0,80}Resets[^<\n]{5,120}",
        r"\d{1,3}\s*%\s*used",
    ):
        for mm in re.finditer(pattern, html, re.IGNORECASE):
            frag = " ".join(mm.group(0).split())
            if frag and frag not in snippets:
                snippets.append(frag[:240])

    if not snippets:
        return (
            "\n\n🌐 claude.ai: не нашёл на странице «Resets in» / «% used» "
            "(вёрстка могла поменяться)."
        )

    seen: set[str] = set()
    uniq: list[str] = []
    for s in snippets:
        key = s[:100]
        if key not in seen:
            seen.add(key)
            uniq.append(s)

    body = "\n".join(f"• {s}" for s in uniq[:15])
    return f"\n\n🌐 claude.ai:\n{body}"


async def fetch_claude_web_usage_block() -> str:
    html, err = await get_claude_usage_html()
    if err:
        return f"\n\n⚠️ {err}"
    if not html:
        return "\n\n⚠️ Пустой ответ от claude.ai."
    return usage_snippets_from_html(html)


# ------------------------------------------------------------------ Playwright

async def fetch_claude_usage_playwright() -> tuple[str | None, str]:
    """
    Fetch claude.ai/settings/usage via Playwright using saved session.
    Returns (page_inner_text, error_message). Bypasses Cloudflare.
    """
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        return None, (
            "playwright не установлен.\n"
            "uv add playwright && playwright install chromium"
        )

    if not _PLAYWRIGHT_SESSION.exists():
        return None, (
            "Нет сессии claude.ai.\n\n"
            "Запусти в терминале:\n  whip claude-login\n"
            "Залогинься в браузере, нажми Enter — сессия сохранится."
        )

    try:
        storage_state = json.loads(_PLAYWRIGHT_SESSION.read_text())
    except Exception:
        return None, "Повреждён файл сессии. Запусти: whip claude-login"

    async with _playwright_lock:
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                ctx = await browser.new_context(storage_state=storage_state)
                page = await ctx.new_page()

                resp = await page.goto(
                    "https://claude.ai/settings/usage",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                if resp and resp.status == 403:
                    await browser.close()
                    return None, (
                        "Cloudflare отклонил (сессия устарела).\n"
                        "Запусти: whip claude-login"
                    )

                # Wait for React/JS to render usage data
                await page.wait_for_timeout(3_000)

                text = await page.inner_text("body")

                # Persist refreshed cookies for next call
                new_state = await ctx.storage_state()
                _PLAYWRIGHT_SESSION.write_text(json.dumps(new_state))

                await browser.close()
                return text, ""
        except Exception as exc:
            log.warning("Playwright fetch error: %s", exc)
            return None, f"Playwright error: {exc}"


def usage_snippets_from_text(text: str) -> str:
    """Extract usage snippets from plain page text (Playwright inner_text)."""
    snippets: list[str] = []
    for pat in (
        r"Current session[^\n]{0,160}",
        r"Resets in[^\n]{5,160}",
        r"\d{1,3}\s*%\s*used[^\n]{0,80}",
    ):
        for mm in re.finditer(pat, text, re.I):
            frag = " ".join(mm.group(0).split())
            if frag not in snippets:
                snippets.append(frag[:240])

    if not snippets:
        return "\n\n🌐 claude.ai: не нашёл «Resets in» / «% used» на странице."

    seen: set[str] = set()
    uniq: list[str] = []
    for s in snippets:
        key = s[:100]
        if key not in seen:
            seen.add(key)
            uniq.append(s)

    return "\n\n🌐 claude.ai:\n" + "\n".join(f"• {s}" for s in uniq[:10])


def scrape_reset_seconds_from_text(text: str) -> int | None:
    """Parse 'Resets in ...' from plain page inner text."""
    for mm in re.finditer(r"Resets in[^\n]{0,160}", text, re.I):
        sec = parse_resets_in_seconds_from_blob(mm.group(0))
        if sec is not None:
            return sec
    return None


def _load_offset() -> int:
    try:
        return int(_OFFSET_FILE.read_text().strip())
    except Exception:
        return 0


def _save_offset(offset: int) -> None:
    try:
        _OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
        _OFFSET_FILE.write_text(str(offset))
    except Exception:
        pass


class TelegramBridge:
    """Long-polls Telegram and routes responses back to pending daemon requests."""

    def __init__(self, config: dict, state: "DaemonState"):
        self.token = config["telegram_token"]
        self.chat_id = str(config["telegram_chat_id"])
        self.state = state
        self.offset = _load_offset()
        self.base = f"https://api.telegram.org/bot{self.token}"

    # ------------------------------------------------------------------ send

    async def send(self, text: str, buttons: list[list[dict]], request_id: str) -> int:
        """Send message with inline keyboard. Returns message_id. Raises on failure."""
        text = _truncate_tg(text)
        keyboard = {
            "inline_keyboard": [
                [{"text": b["text"], "callback_data": f"{request_id}:{b['data']}"} for b in row]
                for row in buttons
            ]
        }
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{self.base}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "reply_markup": keyboard,
                },
                timeout=30,
            )
            data = r.json()
            if data.get("ok"):
                return data["result"]["message_id"]
            raise RuntimeError(f"TG sendMessage failed: {r.text[:200]}")

    async def _edit_after_tap(self, message_id: int, original_text: str, chosen_label: str) -> None:
        """Replace buttons with a single line showing what was tapped."""
        new_text = _truncate_tg(f"{original_text}\n\n▶ {chosen_label}")
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{self.base}/editMessageText",
                json={
                    "chat_id": self.chat_id,
                    "message_id": message_id,
                    "text": new_text,
                    "reply_markup": {"inline_keyboard": []},
                },
                timeout=10,
            )
            if not r.json().get("ok"):
                await client.post(
                    f"{self.base}/editMessageReplyMarkup",
                    json={"chat_id": self.chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
                    timeout=10,
                )

    async def send_plain(self, text: str) -> None:
        text = _truncate_tg(text)
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{self.base}/sendMessage",
                json={"chat_id": self.chat_id, "text": text},
                timeout=30,
            )

    # ------------------------------------------------------------------ poll

    async def poll(self) -> None:
        log.info("Telegram poller started")
        while True:
            try:
                async with httpx.AsyncClient() as client:
                    r = await client.get(
                        f"{self.base}/getUpdates",
                        params={
                            "offset": self.offset,
                            "timeout": 30,
                            "allowed_updates": ["callback_query", "message"],
                        },
                        timeout=40,
                    )
                data = r.json()
                if data.get("ok"):
                    for update in data.get("result", []):
                        self.offset = update["update_id"] + 1
                        _save_offset(self.offset)
                        await self._handle(update)
            except Exception as e:
                log.debug("Poll error: %s", e)
                await asyncio.sleep(3)

    # ------------------------------------------------------------------ handle

    async def _handle(self, update: dict) -> None:
        if "callback_query" in update:
            await self._handle_callback(update["callback_query"])
        elif "message" in update:
            await self._handle_message(update["message"])

    async def _handle_callback(self, query: dict) -> None:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{self.base}/answerCallbackQuery",
                json={"callback_query_id": query["id"]},
                timeout=10,
            )

        data = query.get("data", "")
        if ":" not in data:
            return

        request_id, action = data.split(":", 1)
        pending = self.state.pending.get(request_id)

        # Stale button from an old message — just wipe the keyboard silently
        if not pending:
            msg_id = query.get("message", {}).get("message_id")
            if msg_id:
                async with httpx.AsyncClient() as client:
                    await client.post(
                        f"{self.base}/editMessageReplyMarkup",
                        json={"chat_id": self.chat_id, "message_id": msg_id,
                              "reply_markup": {"inline_keyboard": []}},
                        timeout=10,
                    )
            return

        kind = pending["type"]
        msg_id = query.get("message", {}).get("message_id")
        original_text = query.get("message", {}).get("text", "")

        # Label map for what to show after tap
        labels = {
            "continue":    "Ебаш дальше",
            "custom":      "Пишу команду...",
            "done":        "Стоп",
            "approve":     "Разрешил",
            "approve_all": "Разрешил всё",
            "deny":        "Отклонил",
        }
        chosen_label = labels.get(action, action)

        if kind == "stop":
            if action == "continue":
                pending["response"] = {"action": "continue", "message": "ебаш дальше", "source": "tg"}
            elif action == "custom":
                pending["awaiting_text"] = True
                if msg_id:
                    await self._edit_after_tap(msg_id, original_text, chosen_label)
                await self.send_plain("✏️ Напиши команду — отправлю агенту:")
                return
            else:
                pending["response"] = {"action": "done", "message": "", "source": "tg"}

        elif kind == "approve":
            if action == "approve":
                pending["response"] = {"decision": "approve", "source": "tg"}
            elif action == "approve_all":
                self.state.approve_all = True
                pending["response"] = {"decision": "approve", "source": "tg"}
            else:
                pending["response"] = {"decision": "block", "reason": "Отклонено через Telegram", "source": "tg"}

        if msg_id:
            await self._edit_after_tap(msg_id, original_text, chosen_label)

        pending["event"].set()

    async def _handle_message(self, msg: dict) -> None:
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text", "").strip()

        if chat_id != self.chat_id or not text:
            return

        # Bot commands
        if text.startswith("/"):
            await self._handle_command(text, msg)
            return

        # "reset 4h30m" shortcut — set/replace single claude auto-reset reminder
        _reset_m = re.match(
            r"^reset\s+((?:\d+h)?\s*(?:\d+m)?)\s*$", text, re.I
        )
        if _reset_m:
            import re as _re, time as _t
            from datetime import datetime, timedelta
            from . import daemon as _d

            dur_str = _reset_m.group(1).strip()
            total = 0
            for v, u in _re.findall(r"(\d+)([hm])", dur_str.lower()):
                total += int(v) * (3600 if u == "h" else 60)

            if total > 0:
                fire_at = _t.time() + total
                _d.upsert_claude_auto_reset(fire_at)
                when = datetime.now() + timedelta(seconds=total)
                h, rem = divmod(total, 3600)
                m = rem // 60
                await self.send_plain(
                    f"✅ Напоминание о ресете обновлено: через {h}h {m}m\n"
                    f"Срабатывает в {when.strftime('%H:%M')}"
                )
            else:
                await self.send_plain("⚠️ Не понял время. Пример: reset 4h30m")
            return

        # Find a pending request that's waiting for text input
        log.info("TG msg: %r | pending: %s", text[:60],
                 [(r, p["type"], p.get("awaiting_text")) for r, p in self.state.pending.items()])
        for rid, pending in reversed(list(self.state.pending.items())):
            if pending.get("awaiting_text"):
                log.info("Routing text to awaiting_text pending %s", rid)
                pending["awaiting_text"] = False
                pending["response"] = {"action": "continue", "message": text, "source": "tg"}
                pending["event"].set()
                await self.send_plain(f"✅ Передал агенту: {text[:200]}")
                return

        # Otherwise route to most recent stop request
        for rid, pending in reversed(list(self.state.pending.items())):
            if pending["type"] == "stop":
                log.info("Routing text to stop pending %s", rid)
                pending["response"] = {"action": "continue", "message": text, "source": "tg"}
                pending["event"].set()
                await self.send_plain(f"✅ Передал агенту: {text[:200]}")
                return

        log.info("TG msg: no pending to route to, ignoring")
        await self.send_plain("⚪ Агент не ожидает команд сейчас.")

    async def _handle_command(self, text: str, msg: dict) -> None:
        parts_cmd = text.split(maxsplit=1)
        cmd = parts_cmd[0].lower().split("@")[0]  # strip @botname suffix
        rest = parts_cmd[1].strip() if len(parts_cmd) > 1 else ""

        if cmd.startswith("/ebash"):
            # /ebash           — route to most recent stop
            # /ebash:whip      — route to project "whip" specifically
            # /ebash text      — send text to most recent stop immediately
            # /ebash:whip text — send text to "whip" project stop
            target_project: str | None = None
            if ":" in cmd:
                target_project = cmd.split(":", 1)[1]  # "/ebash:whip" → "whip"

            # Find matching pending stop
            matched_rid = None
            for rid, pending in reversed(list(self.state.pending.items())):
                if pending["type"] != "stop":
                    continue
                if target_project is None or pending.get("project") == target_project:
                    matched_rid = rid
                    break

            if matched_rid is None:
                if target_project:
                    # List active projects so user knows what's available
                    active = [p.get("project", "?") for p in self.state.pending.values() if p["type"] == "stop"]
                    if active:
                        await self.send_plain(
                            f"⚪ Проект «{target_project}» сейчас не активен.\n"
                            f"Активные стопы: {', '.join(active)}"
                        )
                    else:
                        await self.send_plain(f"⚪ Проект «{target_project}» не активен (нет ни одного стопа).")
                else:
                    await self.send_plain("⚪ Агент сейчас не в стопе — некому отправлять команду.")
                return

            pending = self.state.pending[matched_rid]
            proj_name = pending.get("project", "?")

            if rest:
                # Send text immediately to this pending
                pending["awaiting_text"] = False
                pending["response"] = {"action": "continue", "message": rest, "source": "tg"}
                pending["event"].set()
                await self.send_plain(f"✅ [{proj_name}] Передал агенту: {rest[:200]}")
            else:
                # Prompt for text
                pending["awaiting_text"] = True
                await self.send_plain(
                    f"✏️ [{proj_name}] Напиши команду:"
                )
            return

        if cmd == "/status" or cmd == "/start":
            pending_count = len(self.state.pending)
            approve_all = "🔥 да на всё" if self.state.approve_all else "выкл"
            project = self.state.last_cwd.split("/")[-1] if self.state.last_cwd else "—"
            await self.send_plain(
                f"🎯 Whip работает\n\n"
                f"Проект: {project}\n"
                f"Ожидает: {pending_count}\n"
                f"Approve all: {approve_all}"
            )

        elif cmd == "/reset":
            from . import daemon as _d
            from . import claude_desktop as _cd
            import time as _t
            from datetime import datetime, timedelta

            existing = _d.load_claude_auto_reset()
            if existing:
                secs = max(0, int(existing["fire_at"] - _t.time()))
                h, rem = divmod(secs, 3600)
                m, s = divmod(rem, 60)
                at = datetime.fromtimestamp(existing["fire_at"])
                await self.send_plain(
                    f"⏱ Уже стоит напоминание по лимитам:\n"
                    f"осталось {h}h {m}m {s}s → в {at.strftime('%d.%m %H:%M')}\n"
                    f"{existing.get('text', '')}"
                )
                return

            # Fetch from API via Claude Desktop cookies
            try:
                usage_data = await _cd.fetch_usage()
                sec = _cd.next_reset_seconds(usage_data)
            except Exception as e:
                await self.send_plain(
                    f"⚠️ {e}\n\nИли введи вручную: reset 4h30m"
                )
                return

            if sec is None:
                await self.send_plain(
                    "⚠️ Не нашёл время ресета в ответе API.\n"
                    "Введи вручную: reset 4h30m"
                )
                return

            fire_at = _t.time() + sec
            _d.upsert_claude_auto_reset(fire_at)
            when = datetime.now() + timedelta(seconds=sec)
            h, r2 = divmod(sec, 3600)
            m, _ = divmod(r2, 60)
            await self.send_plain(
                f"✅ Снял время с API: через {h}h {m}m.\n"
                f"Напомню в {when.strftime('%H:%M')} — лимиты обновились."
            )

        elif cmd == "/limits":
            from . import daemon as _d
            from . import claude_desktop as _cd
            import time as _t

            # Fetch from claude.ai API via Claude Desktop cookies
            try:
                usage_data = await _cd.fetch_usage()
                api_text = "\n\n" + _cd.format_usage(usage_data)

                # Auto-update reset schedule if not set
                sec = _cd.next_reset_seconds(usage_data)
                if sec and not _d.load_claude_auto_reset():
                    fire_at = _t.time() + sec
                    _d.upsert_claude_auto_reset(fire_at)
                    log.info("/limits auto-scheduled reset in %ds", sec)
            except Exception as e:
                api_text = f"\n\n⚠️ {e}"

            auto = _d.load_claude_auto_reset()
            if auto:
                secs = max(0, int(auto["fire_at"] - _t.time()))
                h, rem = divmod(secs, 3600)
                m = rem // 60
                reset_text = f"\n\n⏱ Напоминание о ресете: через {h}h{m}m"
            else:
                reset_text = "\n\n⏱ /reset — запланировать напоминание"

            await self.send_plain(f"📋 Лимиты{api_text}{reset_text}")
