"""Whip daemon — FastAPI server that bridges Claude Code hooks and Telegram."""
import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import CONFIG
from .tg import TelegramBridge

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("whip.daemon")


class DaemonState:
    def __init__(self):
        # request_id -> {event, response, type, expires_at, awaiting_text}
        self.pending: dict[str, dict] = {}
        self.approve_all: bool = False  # sticky "yes to all" flag
        self.tg: Optional[TelegramBridge] = None

    def new_request(self, kind: str, timeout: int) -> tuple[str, asyncio.Event]:
        rid = uuid.uuid4().hex[:8]
        event = asyncio.Event()
        self.pending[rid] = {
            "type": kind,
            "event": event,
            "response": None,
            "expires_at": time.time() + timeout,
            "awaiting_text": False,
        }
        return rid, event

    def pop(self, rid: str) -> dict:
        return self.pending.pop(rid, {})

    def cleanup_expired(self):
        now = time.time()
        expired = [rid for rid, p in self.pending.items() if p["expires_at"] < now]
        for rid in expired:
            self.pending.pop(rid, None)


state = DaemonState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.tg = TelegramBridge(CONFIG, state)
    poll_task = asyncio.create_task(state.tg.poll())
    cleanup_task = asyncio.create_task(_cleanup_loop())
    log.info("Whip daemon ready on port %s", CONFIG["daemon_port"])
    yield
    poll_task.cancel()
    cleanup_task.cancel()


app = FastAPI(title="whip", lifespan=lifespan)


async def _cleanup_loop():
    while True:
        await asyncio.sleep(60)
        state.cleanup_expired()


# -------------------------------------------------------------------------- routes

@app.get("/health")
async def health():
    return {"status": "ok", "pending": len(state.pending), "approve_all": state.approve_all}


@app.post("/stop")
async def stop(request: Request):
    """
    Called by the Claude Code Stop hook.
    Blocks until the user responds via Telegram (or timeout).
    Returning {"action": "continue", "message": "..."} resumes the agent.
    Returning {"action": "done"} lets the agent stop.
    """
    data = await request.json()
    summary: str = data.get("summary", "Задача выполнена")
    cwd: str = data.get("cwd", "")

    rid, event = state.new_request("stop", CONFIG["timeout"])

    # Reset approve_all when a new session starts
    state.approve_all = False

    short_summary = summary[:2000]
    cwd_line = f"\n\n`📁 {cwd}`" if cwd else ""
    text = (
        f"✅ *Агент закончил*\n\n"
        f"```\n{short_summary}\n```"
        f"{cwd_line}"
    )
    buttons = [
        [{"text": "🚀 Ебаш дальше", "data": "continue"}],
        [{"text": "✏️ Написать команду", "data": "custom"}],
        [{"text": "✅ Стоп, всё готово", "data": "done"}],
    ]

    msg_id = await state.tg.send(text, buttons, rid)
    state.pending[rid]["message_id"] = msg_id
    log.info("Stop hook [%s] waiting for Telegram...", rid)

    try:
        await asyncio.wait_for(event.wait(), timeout=CONFIG["timeout"])
    except asyncio.TimeoutError:
        state.pop(rid)
        log.info("Stop hook [%s] timed out", rid)
        return JSONResponse({"action": "done", "message": ""})

    resp = state.pop(rid)
    result = resp.get("response", {"action": "done", "message": ""})
    log.info("Stop hook [%s] resolved: %s", rid, result.get("action"))
    return JSONResponse(result)


@app.post("/approve")
async def approve(request: Request):
    """
    Called by the Claude Code PreToolUse hook.
    Blocks until the user approves or denies via Telegram.
    Returns {"decision": "approve"} or {"decision": "block", "reason": "..."}.
    """
    data = await request.json()
    tool_name: str = data.get("tool_name", "unknown")
    tool_input: dict = data.get("tool_input", {})

    # Approve immediately if approve_all is set
    if state.approve_all:
        return JSONResponse({"decision": "approve"})

    rid, event = state.new_request("approve", 300)  # 5 min for approvals

    tool_text = _format_tool(tool_name, tool_input)
    text = f"🔧 *Разрешить?*\n\n*{tool_name}*\n{tool_text}"
    buttons = [
        [
            {"text": "✅ Да", "data": "approve"},
            {"text": "❌ Нет", "data": "deny"},
        ],
        [{"text": "🔥 Да на всё в этой сессии", "data": "approve_all"}],
    ]

    await state.tg.send(text, buttons, rid)
    log.info("Approve hook [%s] %s waiting...", rid, tool_name)

    try:
        await asyncio.wait_for(event.wait(), timeout=300)
    except asyncio.TimeoutError:
        state.pop(rid)
        log.info("Approve hook [%s] timed out — auto-approve", rid)
        return JSONResponse({"decision": "approve"})

    resp = state.pop(rid)
    result = resp.get("response", {"decision": "approve"})
    log.info("Approve hook [%s] resolved: %s", rid, result.get("decision"))
    return JSONResponse(result)


@app.post("/notify")
async def notify(request: Request):
    """
    Generic notification endpoint — any agent/script can call this.
    Fire-and-forget: sends to Telegram and immediately returns.
    """
    data = await request.json()
    text: str = data.get("text", "")
    reply_buttons: list = data.get("buttons", [])

    if not text:
        return JSONResponse({"ok": False, "error": "text required"})

    if reply_buttons:
        # Build a stop-style request so user can reply
        rid, event = state.new_request("stop", CONFIG["timeout"])
        buttons = [[{"text": b, "data": "continue"}] for b in reply_buttons]
        buttons.append([{"text": "✏️ Написать", "data": "custom"}])
        await state.tg.send(text, buttons, rid)
        # Non-blocking — caller doesn't wait
    else:
        await state.tg.send_plain(text)

    return JSONResponse({"ok": True})


# -------------------------------------------------------------------------- helpers

def _format_tool(tool_name: str, tool_input: dict) -> str:
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")[:600]
        return f"```\n$ {cmd}\n```"
    if tool_name in ("Write", "Edit", "Read"):
        path = tool_input.get("file_path", tool_input.get("path", ""))
        return f"`{path}`"
    if tool_name == "Glob":
        return f"`{tool_input.get('pattern', '')}`"
    import json
    return f"```\n{json.dumps(tool_input, ensure_ascii=False)[:400]}\n```"
