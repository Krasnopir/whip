#!/usr/bin/env python3
"""
Claude Code Stop hook.
Sends summary to BOTH Telegram AND shows prompt in terminal.
First channel to respond wins — Enter at laptop or button tap on phone.
"""
import json
import os
import queue
import sys
import threading


def read_summary(transcript_path: str, max_chars: int = 3000) -> str:
    """
    Collect the last few assistant text blocks from the transcript.
    Concatenate them (newest last) until we hit max_chars.
    This gives a meaningful picture of recent agent activity, not just the
    last one-liner.
    """
    try:
        lines = open(transcript_path).readlines()
    except Exception:
        return ""

    # Collect all assistant text chunks in order
    chunks: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            outer = json.loads(line)
            if outer.get("type") != "assistant":
                continue
            msg = outer.get("message", {})
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content", [])

            texts: list[str] = []
            if isinstance(content, str) and content.strip():
                texts = [content.strip()]
            elif isinstance(content, list):
                texts = [
                    b.get("text", "").strip()
                    for b in content
                    if isinstance(b, dict)
                    and b.get("type") == "text"
                    and b.get("text", "").strip()
                ]
            if texts:
                chunks.append("\n".join(texts))
        except (json.JSONDecodeError, KeyError):
            continue

    if not chunks:
        return ""

    # Work backwards: take as many recent chunks as fit in max_chars
    selected: list[str] = []
    used = 0
    for chunk in reversed(chunks):
        if used + len(chunk) > max_chars:
            # Take a partial slice of the oldest chunk if we have nothing yet
            if not selected:
                selected.append(chunk[-(max_chars - used):])
            break
        selected.append(chunk)
        used += len(chunk) + 1  # +1 for separator
        if used >= max_chars:
            break

    selected.reverse()
    return "\n\n---\n\n".join(selected)


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    transcript_path = payload.get("transcript_path", "")
    cwd = payload.get("cwd", os.getcwd())
    summary = read_summary(transcript_path) if transcript_path else ""

    port = os.getenv("WHIP_DAEMON_PORT", "7331")
    host = os.getenv("WHIP_DAEMON_HOST", "127.0.0.1")

    result_q: queue.Queue = queue.Queue(maxsize=1)

    # --- Thread 1: send to daemon → Telegram ---
    def wait_daemon():
        try:
            import httpx
            resp = httpx.post(
                f"http://{host}:{port}/stop",
                json={"summary": summary, "cwd": cwd},
                timeout=1800,
            )
            data = resp.json()
            # Echo what came from TG so terminal user sees it
            action = data.get("action", "done")
            msg = data.get("message", "").strip()
            if action == "continue" and msg:
                sys.stderr.write(f"\n[whip] 📱 из TG: {msg}\n[whip] > ")
                sys.stderr.flush()
            result_q.put(data)
        except Exception:
            result_q.put({"action": "done", "message": ""})

    # --- Thread 2: show prompt in terminal after delay, read from /dev/tty ---
    def wait_terminal():
        try:
            import time, httpx
            # Give TG 1.5s to arrive on phone first
            time.sleep(1.5)
            if not result_q.empty():
                return  # already resolved via TG
            tty = open("/dev/tty", "r")
            sys.stderr.write(
                "\n[whip] ✅ Агент закончил. Что дальше?\n"
                "[whip]    Enter = ебаш дальше    текст+Enter = команда    s = стоп\n"
                "[whip]    (или ответь в Telegram)\n"
                "[whip] > "
            )
            sys.stderr.flush()
            line = tty.readline().strip()
            if result_q.empty():  # only act if not already resolved
                if line.lower() in ("s", "stop", "стоп", "q"):
                    msg = ""
                elif line == "":
                    msg = "продолжай"
                else:
                    msg = line
                httpx.post(
                    f"http://{host}:{port}/local-approve",
                    json={"decision": "approve", "message": msg},
                    timeout=5,
                )
        except Exception:
            pass

    t_daemon = threading.Thread(target=wait_daemon, daemon=True)
    t_terminal = threading.Thread(target=wait_terminal, daemon=True)
    t_daemon.start()
    t_terminal.start()

    try:
        data = result_q.get(timeout=1800)
    except queue.Empty:
        data = {"action": "done", "message": ""}

    action = data.get("action", "done")
    message = data.get("message", "").strip()

    if action == "continue" and message:
        sys.stderr.write(f"[whip] ▶ {message}\n")
        print(message)
    else:
        sys.stderr.write("[whip] ✅ Стоп\n")

    sys.exit(0)


if __name__ == "__main__":
    main()
