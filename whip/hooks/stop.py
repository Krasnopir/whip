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


def read_summary(transcript_path: str) -> str:
    """
    Return only the LAST assistant text from the transcript.
    The Stop hook fires right after the agent writes its final response,
    so the last entry is exactly what we want to show.
    If the last message is very short (<80 chars), also prepend the previous one.
    """
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

    last = chunks[-1][:3000]

    # If last message is a short one-liner, also show the previous one for context
    if len(last) < 80 and len(chunks) >= 2:
        prev = chunks[-2][:2000]
        return f"{prev}\n\n---\n\n{last}"

    return last


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    transcript_path = payload.get("transcript_path", "")
    cwd = payload.get("cwd", os.getcwd())

    # Claude Code fires Stop hook before flushing the final message to the transcript.
    # Wait briefly so the file is complete before we read it.
    if transcript_path:
        import time
        time.sleep(1.5)

    summary = read_summary(transcript_path) if transcript_path else ""

    port = os.getenv("WHIP_DAEMON_PORT", "7331")
    host = os.getenv("WHIP_DAEMON_HOST", "127.0.0.1")

    # Print summary to terminal too — not just TG
    project = os.path.basename(cwd) if cwd else "?"
    sys.stderr.write(f"\n{'─'*50}\n")
    sys.stderr.write(f"[whip] ✅ [{project}] Агент закончил\n\n")
    if summary:
        sys.stderr.write(summary[:1000] + "\n")
    sys.stderr.write(f"\n{'─'*50}\n")
    sys.stderr.write("[whip]    → whip go  /  whip go 'команда'  /  whip go s  (или в Telegram)\n")
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
