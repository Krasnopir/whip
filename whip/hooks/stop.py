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


def read_last_assistant_text(transcript_path: str) -> str:
    try:
        lines = open(transcript_path).readlines()
    except Exception:
        return ""

    last_text = ""
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
            if isinstance(content, str) and content.strip():
                last_text = content.strip()
                continue
            if isinstance(content, list):
                texts = [
                    b.get("text", "").strip()
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()
                ]
                if texts:
                    last_text = "\n".join(texts)
        except (json.JSONDecodeError, KeyError):
            continue

    return last_text[:3000]


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    transcript_path = payload.get("transcript_path", "")
    cwd = payload.get("cwd", os.getcwd())
    summary = read_last_assistant_text(transcript_path) if transcript_path else ""

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
            result_q.put(resp.json())
        except Exception:
            result_q.put({"action": "done", "message": ""})

    # --- Thread 2: show prompt in terminal, read from /dev/tty ---
    def wait_terminal():
        try:
            import httpx
            tty = open("/dev/tty", "r")
            sys.stderr.write(
                "\n[whip] ✅ Агент закончил. Что дальше?\n"
                "[whip]    Enter = ебаш дальше    текст = команда агенту    s = стоп\n"
                "[whip] > "
            )
            sys.stderr.flush()
            line = tty.readline().strip()

            if line.lower() in ("s", "stop", "стоп", "q", "quit"):
                decision = "done"
                message = ""
            elif line == "":
                decision = "continue"
                message = "продолжай"
            else:
                decision = "continue"
                message = line

            httpx.post(
                f"http://{host}:{port}/local-approve",
                json={"decision": "approve", "message": message if decision == "continue" else ""},
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
