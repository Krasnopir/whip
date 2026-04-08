"""Whip daemon — FastAPI server that bridges Claude Code hooks and Telegram."""
import asyncio
import logging
import pathlib
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import CONFIG
from .tg import TelegramBridge

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("whip.daemon")

_ACTIVITY_LOG = pathlib.Path.home() / ".whip" / "activity.log"


def _activity(line: str) -> None:
    """Append a human-readable event line to the activity log."""
    ts = datetime.now().strftime("%H:%M:%S")
    try:
        with _ACTIVITY_LOG.open("a") as f:
            f.write(f"[{ts}] {line}\n")
    except Exception:
        pass


class DaemonState:
    def __init__(self):
        # request_id -> {event, response, type, expires_at, awaiting_text}
        self.pending: dict[str, dict] = {}
        self.approve_all: bool = False  # sticky "yes to all" flag
        self.last_cwd: str = ""         # for project name in approve messages
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


@app.post("/local-approve")
async def local_approve(request: Request):
    """Approve or deny the most recent pending approve request from terminal."""
    data = await request.json()
    decision = data.get("decision", "approve")  # "approve" or "block"
    message = data.get("message", "")

    # Find most recent pending approve
    for rid, pending in reversed(list(state.pending.items())):
        if pending["type"] == "approve":
            if decision == "approve":
                pending["response"] = {"decision": "approve", "source": "terminal"}
                label = "✅ Одобрено с ноута"
            else:
                pending["response"] = {"decision": "block", "reason": message or "Отклонено локально", "source": "terminal"}
                label = "❌ Отклонено с ноута"

            # Edit TG message to show it was resolved locally
            msg_id = pending.get("message_id")
            if msg_id and state.tg:
                original = f"(решено с ноута)"
                asyncio.create_task(state.tg._edit_after_tap(msg_id, original, label))

            pending["event"].set()
            return JSONResponse({"ok": True, "rid": rid, "decision": decision})

    # Also handle stop requests (continue from local)
    for rid, pending in reversed(list(state.pending.items())):
        if pending["type"] == "stop":
            pending["response"] = {"action": "continue", "message": message or "продолжай", "source": "terminal"}
            pending["event"].set()
            return JSONResponse({"ok": True, "rid": rid, "action": "continue"})

    return JSONResponse({"ok": False, "error": "no pending requests"}, status_code=404)


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

    # Reset approve_all when a new session starts, remember cwd for approve messages
    state.approve_all = False
    if cwd:
        state.last_cwd = cwd

    import os as _os
    project = _os.path.basename(cwd) if cwd else "unknown"
    short_summary = summary[:2000] if summary else "(нет текста)"
    text = (
        f"📁 *{project}*\n"
        f"✅ Агент закончил\n\n"
        f"```\n{short_summary}\n```"
    )
    buttons = [
        [{"text": "🚀 Ебаш дальше", "data": "continue"}],
        [{"text": "✏️ Написать команду", "data": "custom"}],
        [{"text": "✅ Стоп, всё готово", "data": "done"}],
    ]

    _activity(f"✅ [{project}] Агент закончил")
    _activity(f"   {short_summary[:200].splitlines()[0] if short_summary else ''}")
    _activity(f"   → whip go / whip go 'команда' / whip go s")

    msg_id = await state.tg.send(text, buttons, rid)
    state.pending[rid]["message_id"] = msg_id
    log.info("Stop hook [%s] waiting for Telegram...", rid)

    try:
        await asyncio.wait_for(event.wait(), timeout=CONFIG["timeout"])
    except asyncio.TimeoutError:
        state.pop(rid)
        _activity(f"   ⏱ Таймаут ожидания — агент остановлен")
        return JSONResponse({"action": "done", "message": ""})

    resp = state.pop(rid)
    result = resp.get("response", {"action": "done", "message": ""})
    src = result.get("source", "?")
    action = result.get("action", "done")
    msg = result.get("message", "")
    _activity(f"   ▶ [{src}] {msg if action == 'continue' else 'стоп'}")
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

    import os as _os
    project = _os.path.basename(state.last_cwd) if getattr(state, "last_cwd", "") else "?"
    tool_text = _format_tool(tool_name, tool_input)
    text = f"📁 *{project}*\n🔧 Разрешить?\n\n*{tool_name}*\n{tool_text}"

    preview = tool_input.get("command", "")[:80] if tool_name == "Bash" else str(tool_input)[:80]
    _activity(f"🔧 [{project}] {tool_name}: {preview}")
    _activity(f"   → whip approve  /  whip deny")
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
    src = result.get("source", "?")
    decision = result.get("decision", "approve")
    _activity(f"   {'✅' if decision == 'approve' else '❌'} [{src}] {decision}")
    log.info("Approve hook [%s] resolved: %s", rid, decision)
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
