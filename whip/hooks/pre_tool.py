#!/usr/bin/env python3
"""
Claude Code PreToolUse hook.
Intercepts potentially destructive tool calls and asks for approval via Telegram.
Outputs JSON decision to stdout: {"decision": "approve"} or {"decision": "block", ...}
"""
import json
import os
import sys

# Tools that are always safe — never ask
ALWAYS_ALLOW = {
    "Read", "Glob", "Grep", "LS",
    "TodoRead", "TodoWrite",
    "WebSearch", "WebFetch",
    "NotebookRead",
}

# Bash patterns that warrant approval
DANGEROUS_BASH = [
    "rm ", "rmdir", "unlink",
    "git push", "git reset --hard", "git clean",
    "drop table", "drop database", "truncate",
    "curl", "wget",         # network ops
    "> /",                  # overwrite system files
    "sudo ",
    "chmod 777", "chmod -R",
    "kill ", "pkill",
    "launchctl", "systemctl",
]


def is_dangerous_bash(cmd: str) -> bool:
    low = cmd.lower()
    return any(p in low for p in DANGEROUS_BASH)


def should_ask(tool_name: str, tool_input: dict) -> bool:
    if tool_name in ALWAYS_ALLOW:
        return False
    if tool_name == "Bash":
        return is_dangerous_bash(tool_input.get("command", ""))
    # Write/Edit — skip, too noisy. Only ask for Bash destructive ops.
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

    try:
        import httpx
        resp = httpx.post(
            f"http://{host}:{port}/approve",
            json={"tool_name": tool_name, "tool_input": tool_input},
            timeout=300,
        )
        data = resp.json()
    except Exception:
        # Daemon not running — approve by default
        sys.exit(0)

    decision = data.get("decision", "approve")

    if decision == "block":
        print(f"[whip] ❌ Отклонено: {tool_name}", file=sys.stderr)
        print(json.dumps({
            "decision": "block",
            "reason": data.get("reason", "Отклонено через Telegram"),
        }))
    else:
        print(f"[whip] ✅ Разрешено: {tool_name}", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
