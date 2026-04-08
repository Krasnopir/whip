#!/usr/bin/env python3
"""
Claude Code PreToolUse hook.
Sends approval request to BOTH Telegram AND shows prompt in terminal.
First channel to respond wins — no need to use the phone if you're at the laptop.
"""
import json
import os
import queue
import sys
import threading


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

    preview = ""
    if tool_name == "Bash":
        preview = tool_input.get("command", "")[:80]

    result_q: queue.Queue = queue.Queue(maxsize=1)

    # --- Thread 1: wait for daemon (TG button or whip approve CLI) ---
    def wait_daemon():
        try:
            import httpx
            resp = httpx.post(
                f"http://{host}:{port}/approve",
                json={"tool_name": tool_name, "tool_input": tool_input},
                timeout=300,
            )
            data = resp.json()
            decision = data.get("decision", "approve")
            # Echo TG response to terminal
            if decision == "approve":
                sys.stderr.write(f"\n[whip] 📱 из TG: ✅ разрешено\n")
            else:
                sys.stderr.write(f"\n[whip] 📱 из TG: ❌ отклонено\n")
            sys.stderr.flush()
            result_q.put(data)
        except Exception:
            result_q.put({"decision": "approve"})

    # --- Thread 2: show prompt in terminal immediately (same time as TG) ---
    def wait_terminal():
        try:
            import httpx
            tty = open("/dev/tty", "r")
            sys.stderr.write(
                f"\n[whip] 🔧 {tool_name}: {preview}\n"
                f"[whip]    Enter/y = разрешить    n = отклонить\n"
                f"[whip] > "
            )
            sys.stderr.flush()
            line = tty.readline().strip().lower()
            if result_q.empty():  # phone was faster — don't double-resolve
                decision = "block" if line in ("n", "no", "нет") else "approve"
                httpx.post(
                    f"http://{host}:{port}/local-approve",
                    json={"decision": decision},
                    timeout=5,
                )
        except Exception:
            pass

    t_daemon = threading.Thread(target=wait_daemon, daemon=True)
    t_terminal = threading.Thread(target=wait_terminal, daemon=True)
    t_daemon.start()
    t_terminal.start()

    try:
        data = result_q.get(timeout=300)
    except queue.Empty:
        data = {"decision": "approve"}

    decision = data.get("decision", "approve")

    if decision == "block":
        sys.stderr.write(f"[whip] ❌ Отклонено: {tool_name}\n")
        print(json.dumps({
            "decision": "block",
            "reason": data.get("reason", "Отклонено"),
        }))
    else:
        sys.stderr.write(f"[whip] ✅ Разрешено: {tool_name}\n")
        print(json.dumps({"decision": "approve"}))

    sys.exit(0)


if __name__ == "__main__":
    main()
