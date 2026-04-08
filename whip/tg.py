"""Telegram API polling and messaging."""
import asyncio
import logging

import httpx

log = logging.getLogger("whip.tg")


class TelegramBridge:
    """Long-polls Telegram and routes responses back to pending daemon requests."""

    def __init__(self, config: dict, state: "DaemonState"):
        self.token = config["telegram_token"]
        self.chat_id = str(config["telegram_chat_id"])
        self.state = state
        self.offset = 0
        self.base = f"https://api.telegram.org/bot{self.token}"

    # ------------------------------------------------------------------ send

    async def send(self, text: str, buttons: list[list[dict]], request_id: str) -> None:
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
            if not r.json().get("ok"):
                log.warning("TG sendMessage failed: %s", r.text)

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
        # Acknowledge immediately so Telegram removes the loading spinner
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
        if not pending:
            return

        kind = pending["type"]

        if kind == "stop":
            if action == "continue":
                pending["response"] = {"action": "continue", "message": "продолжай"}
            elif action == "custom":
                # Mark as waiting for text reply
                pending["awaiting_text"] = True
                await self.send_plain("Напиши команду — отправлю агенту:")
                return
            else:
                pending["response"] = {"action": "done", "message": ""}

        elif kind == "approve":
            if action == "approve":
                pending["response"] = {"decision": "approve"}
            elif action == "approve_all":
                self.state.approve_all = True
                pending["response"] = {"decision": "approve"}
                await self.send_plain("🔥 Режим «да на всё» включён до конца сессии")
            else:  # deny
                pending["response"] = {"decision": "block", "reason": "Отклонено через Telegram"}

        pending["event"].set()

    async def _handle_message(self, msg: dict) -> None:
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text", "").strip()

        if chat_id != self.chat_id or not text or text.startswith("/"):
            return

        # Find a pending request that's waiting for text input
        for rid, pending in list(self.state.pending.items()):
            if pending.get("awaiting_text"):
                pending["awaiting_text"] = False
                pending["response"] = {"action": "continue", "message": text}
                pending["event"].set()
                return

        # Otherwise route to most recent stop request
        for rid, pending in reversed(list(self.state.pending.items())):
            if pending["type"] == "stop":
                pending["response"] = {"action": "continue", "message": text}
                pending["event"].set()
                return
