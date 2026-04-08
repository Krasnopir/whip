"""Telegram API polling and messaging."""
import asyncio
import logging
import pathlib

import httpx

log = logging.getLogger("whip.tg")

_OFFSET_FILE = pathlib.Path.home() / ".whip" / "tg_offset"


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

    @staticmethod
    def _escape(text: str) -> str:
        """Escape special chars for Telegram MarkdownV2."""
        for ch in r"\_*[]()~`>#+-=|{}.!":
            text = text.replace(ch, f"\\{ch}")
        return text

    async def send(self, text: str, buttons: list[list[dict]], request_id: str) -> int | None:
        """Send message with inline keyboard. Returns message_id for later editing."""
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
                    "parse_mode": "Markdown",
                },
                timeout=30,
            )
            data = r.json()
            if not data.get("ok"):
                # Fallback: plain text
                r = await client.post(
                    f"{self.base}/sendMessage",
                    json={"chat_id": self.chat_id, "text": text, "reply_markup": keyboard},
                    timeout=30,
                )
                data = r.json()
            if data.get("ok"):
                return data["result"]["message_id"]
            log.warning("TG sendMessage failed: %s", r.text)
            return None

    async def _edit_after_tap(self, message_id: int, original_text: str, chosen_label: str) -> None:
        """Replace buttons with a single line showing what was tapped."""
        new_text = f"{original_text}\n\n▶ {chosen_label}"
        async with httpx.AsyncClient() as client:
            # Remove keyboard + append choice to message text
            r = await client.post(
                f"{self.base}/editMessageText",
                json={
                    "chat_id": self.chat_id,
                    "message_id": message_id,
                    "text": new_text,
                    "parse_mode": "Markdown",
                    "reply_markup": {"inline_keyboard": []},
                },
                timeout=10,
            )
            if not r.json().get("ok"):
                # Fallback: just remove the keyboard
                await client.post(
                    f"{self.base}/editMessageReplyMarkup",
                    json={"chat_id": self.chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
                    timeout=10,
                )

    async def send_plain(self, text: str) -> None:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{self.base}/sendMessage",
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"},
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
                pending["response"] = {"action": "continue", "message": "продолжай", "source": "tg"}
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

        # Find a pending request that's waiting for text input
        for rid, pending in list(self.state.pending.items()):
            if pending.get("awaiting_text"):
                pending["awaiting_text"] = False
                pending["response"] = {"action": "continue", "message": text, "source": "tg"}
                pending["event"].set()
                return

        # Otherwise route to most recent stop request
        for rid, pending in reversed(list(self.state.pending.items())):
            if pending["type"] == "stop":
                pending["response"] = {"action": "continue", "message": text}
                pending["event"].set()
                return

    async def _handle_command(self, text: str, msg: dict) -> None:
        cmd = text.split()[0].lower().split("@")[0]  # strip @botname suffix

        if cmd == "/status" or cmd == "/start":
            pending_count = len(self.state.pending)
            approve_all = "🔥 да на всё" if self.state.approve_all else "выкл"
            project = self.state.last_cwd.split("/")[-1] if self.state.last_cwd else "—"
            await self.send_plain(
                f"🎯 *Whip работает*\n\n"
                f"Проект: `{project}`\n"
                f"Ожидает: {pending_count}\n"
                f"Approve all: {approve_all}"
            )

        elif cmd == "/reset":
            # Show scheduled reset or ask to set one
            from . import daemon as _d
            items = _d._load_schedules()
            import time as _t
            if items:
                lines = []
                for i in items:
                    secs = max(0, int(i["fire_at"] - _t.time()))
                    h, rem = divmod(secs, 3600)
                    m, s = divmod(rem, 60)
                    lines.append(f"⏱ через {h}h{m}m — {i['text']}")
                await self.send_plain("📅 *Запланировано:*\n\n" + "\n".join(lines))
            else:
                await self.send_plain(
                    "⏱ *Нет запланированных уведомлений*\n\n"
                    "Напиши время до ресета:\n"
                    "`reset 4h40m` — и я запомню"
                )

        elif cmd == "/limits":
            from . import daemon as _d
            items = _d._load_schedules()
            import time as _t, pathlib, json
            # Try to read usage from Claude Code stats
            stats_path = pathlib.Path.home() / ".claude" / "stats-cache.json"
            stats_text = ""
            try:
                stats = json.loads(stats_path.read_text())
                activity = stats.get("dailyActivity", [])
                if activity:
                    today = activity[-1]
                    stats_text = (
                        f"\n\n📊 *Сегодня:*\n"
                        f"Сообщений: {today.get('messageCount', '?')}\n"
                        f"Сессий: {today.get('sessionCount', '?')}\n"
                        f"Tool calls: {today.get('toolCallCount', '?')}"
                    )
            except Exception:
                pass

            reset_text = ""
            if items:
                secs = max(0, int(items[0]["fire_at"] - _t.time()))
                h, rem = divmod(secs, 3600)
                m = rem // 60
                reset_text = f"\n\n⏱ *Ресет через:* {h}h{m}m"

            await self.send_plain(f"📋 *Лимиты Claude Code*{stats_text}{reset_text}")

        elif text.lower().startswith("reset "):
            # Quick shortcut: "reset 4h40m"
            duration = text.split(None, 1)[1].strip()
            import re, time as _t
            from . import daemon as _d
            total = 0
            for val, unit in re.findall(r"(\d+)([hms])", duration.lower()):
                v = int(val)
                if unit == "h": total += v * 3600
                elif unit == "m": total += v * 60
                elif unit == "s": total += v
            if total:
                fire_at = _t.time() + total
                msg_text = "🔄 Сессия Claude обновилась! Можно снова."
                items = _d._load_schedules()
                items.append({"fire_at": fire_at, "text": msg_text})
                _d._save_schedules(items)
                from datetime import datetime, timedelta
                dt = datetime.now() + timedelta(seconds=total)
                await self.send_plain(f"✅ Напомню в {dt.strftime('%H:%M')} (через {duration})")
