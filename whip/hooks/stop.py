#!/usr/bin/env python3
"""
Claude Code Stop hook.
Receives hook payload on stdin, sends summary to Telegram via whip daemon,
blocks until the user responds, then outputs their message to stdout so Claude
Code continues (or outputs nothing to let Claude Code actually stop).
"""
import json
import os
import sys


def read_last_assistant_text(transcript_path: str) -> str:
    try:
        with open(transcript_path) as f:
            lines = f.readlines()
        for line in reversed(lines):
            try:
                entry = json.loads(line)
                role = entry.get("role", "")
                if role != "assistant":
                    continue
                content = entry.get("content", "")
                if isinstance(content, str):
                    return content[:2000]
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            return block.get("text", "")[:2000]
            except (json.JSONDecodeError, KeyError):
                continue
    except Exception:
        pass
    return "Задача выполнена."


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    transcript_path = payload.get("transcript_path", "")
    cwd = payload.get("cwd", os.getcwd())

    summary = read_last_assistant_text(transcript_path) if transcript_path else "Задача выполнена."

    port = os.getenv("WHIP_DAEMON_PORT", "7331")
    host = os.getenv("WHIP_DAEMON_HOST", "127.0.0.1")

    try:
        import httpx
        resp = httpx.post(
            f"http://{host}:{port}/stop",
            json={"summary": summary, "cwd": cwd},
            timeout=1800,  # wait up to 30 min for user
        )
        data = resp.json()
    except Exception:
        # Daemon not running — just let Claude Code stop normally
        sys.exit(0)

    action = data.get("action", "done")
    message = data.get("message", "").strip()

    if action == "continue" and message:
        # Printing to stdout makes Claude Code treat this as a new user message
        print(message)

    sys.exit(0)


if __name__ == "__main__":
    main()
