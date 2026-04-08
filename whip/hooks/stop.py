#!/usr/bin/env python3
"""
Claude Code Stop hook.
Sends summary to Telegram AND prints info in terminal.
Continue from phone (button/text) or from another terminal tab (whip go "message").
First channel wins.
"""
import json
import os
import sys


def read_summary(transcript_path: str, max_chars: int = 3000) -> str:
    try:
        lines = open(transcript_path).readlines()
    except Exception:
        return ""

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

    selected: list[str] = []
    used = 0
    for chunk in reversed(chunks):
        if used + len(chunk) > max_chars:
            if not selected:
                selected.append(chunk[-(max_chars - used):])
            break
        selected.append(chunk)
        used += len(chunk) + 1
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

    # Print to terminal
    sys.stderr.write(
        "\n[whip] ✅ Агент закончил\n"
        "[whip]    → другой таб: whip go  /  whip go 'команда'  /  whip go s (стоп)\n"
        "[whip]    → или ответь в Telegram\n"
    )
    sys.stderr.flush()

    try:
        import httpx
        resp = httpx.post(
            f"http://{host}:{port}/stop",
            json={"summary": summary, "cwd": cwd},
            timeout=1800,
        )
        data = resp.json()
    except Exception:
        sys.exit(0)

    action = data.get("action", "done")
    message = data.get("message", "").strip()
    source = data.get("source", "tg")

    if action == "continue" and message:
        sys.stderr.write(f"[whip] ▶ ({source}): {message}\n")
        print(message)
    else:
        sys.stderr.write(f"[whip] ✅ Стоп ({source})\n")

    sys.exit(0)


if __name__ == "__main__":
    main()
