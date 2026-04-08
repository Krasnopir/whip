#!/usr/bin/env python3
"""
Claude Code PreToolUse hook.
Sends approval request to Telegram AND prints info in terminal.
Approve from phone (button) or from another terminal tab (whip approve / whip deny).
First channel wins.
"""
import json
import os
import sys

ALWAYS_ALLOW = {
    "Read", "Glob", "Grep", "LS",
    "TodoRead", "TodoWrite",
    "WebSearch", "WebFetch",
    "NotebookRead",
}

DANGEROUS_BASH = [
    "rm ", "rmdir", "unlink",
    "git push", "git reset --hard", "git clean",
    "drop table", "drop database", "truncate",
    "> /",
    "sudo ",
    "chmod 777", "chmod -R",
    "kill ", "pkill",
    "launchctl", "systemctl",
]


def is_dangerous_bash(cmd: str) -> bool:
    return any(p in cmd.lower() for p in DANGEROUS_BASH)


def should_ask(tool_name: str, tool_input: dict) -> bool:
    if tool_name in ALWAYS_ALLOW:
        return False
    if tool_name == "Bash":
        return is_dangerous_bash(tool_input.get("command", ""))
    return False


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})

    if not should_ask(tool_name, tool_input):
        sys.exit(0)

    port = os.getenv("WHIP_DAEMON_PORT", "7331")
    host = os.getenv("WHIP_DAEMON_HOST", "127.0.0.1")
    preview = tool_input.get("command", "")[:80] if tool_name == "Bash" else ""

    # Print to terminal — user can approve from another tab
    sys.stderr.write(
        f"\n[whip] 🔧 {tool_name}: {preview}\n"
        f"[whip]    → другой таб: whip approve  /  whip deny\n"
        f"[whip]    → или нажми кнопку в Telegram\n"
    )
    sys.stderr.flush()

    try:
        import httpx
        resp = httpx.post(
            f"http://{host}:{port}/approve",
            json={"tool_name": tool_name, "tool_input": tool_input},
            timeout=300,
        )
        data = resp.json()
    except Exception:
        print(json.dumps({"decision": "approve"}))
        sys.exit(0)

    decision = data.get("decision", "approve")

    if decision == "block":
        sys.stderr.write(f"[whip] ❌ Отклонено ({data.get('source','tg')})\n")
        print(json.dumps({"decision": "block", "reason": data.get("reason", "Отклонено")}))
    else:
        sys.stderr.write(f"[whip] ✅ Разрешено ({data.get('source','tg')})\n")
        print(json.dumps({"decision": "approve"}))

    sys.exit(0)


if __name__ == "__main__":
    main()
