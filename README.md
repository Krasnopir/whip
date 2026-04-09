# whip

Remote control for AI coding agents via Telegram. Your agent finishes a task or asks permission — you tap a button from your phone.

```
Agent done → Telegram:

  ✅ Agent finished
  📁 myproject

  Refactored auth module, moved logic to service layer...

  [🚀 Keep going]  [✏️ Send command]  [✅ Stop]

Tap "Keep going" → agent continues
Type text → sent to agent as next instruction
```

```
Agent wants to run rm -rf → Telegram:

  🔧 Allow?
  Bash
  $ rm -rf /tmp/old_build

  [✅ Yes]  [❌ No]
  [🔥 Yes to everything this session]
```

After tapping — message edits itself, buttons disappear, your choice is visible.
Terminal also shows `[whip] ▶ continuing`.

---

## Quick start

### 1. Create a Telegram bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → get **token**
2. Message [@userinfobot](https://t.me/userinfobot) → get your **chat ID**
3. Find your bot and send `/start`

### 2. Install whip

```bash
git clone https://github.com/Krasnopir/whip.git
cd whip
uv sync          # or: python3 -m venv .venv && source .venv/bin/activate && pip install -e .
```

### 3. Configure

```bash
whip setup
# → enter bot token
# → enter chat ID
# → automatically installs hooks into Claude Code
```

Or create `~/.whip/.env` manually:

```env
WHIP_TELEGRAM_TOKEN=1234567890:ABCdef...
WHIP_TELEGRAM_CHAT_ID=123456789
WHIP_DAEMON_PORT=7331
WHIP_TIMEOUT=1800
```

### 4. Start the daemon

```bash
whip start        # foreground (separate terminal)
whip start -d     # background
```

Done. Launch Claude Code in any project — control arrives on your phone.

---

## How it works

```
Claude Code
   │ Stop hook       (agent finished a task)
   │ PreToolUse hook (agent wants to run a command)
   ▼
whip daemon (localhost:7331)   ← FastAPI, runs locally
   │
   ▼ Telegram Bot API (long-polling, no webhook needed)
Your phone
```

**Stop hook** — when Claude Code finishes, captures the moment and sends a summary to Telegram. Holds the connection open until you tap a button (up to 30 min). "Keep going" → agent gets a nudge and continues. Type text → flies to agent as the next instruction.

**PreToolUse hook** — intercepts every tool call (or just dangerous bash — configurable). "Yes to everything" enables auto-approve mode for the rest of the session.

**No cloud** — everything is local, Telegram is just the transport.

---

## Telegram commands

| Command | Description |
|---|---|
| `/limits` | Current claude.ai usage: session %, weekly %, extra credits |
| `/reset` | Schedule a reminder when the 5-hour session resets |
| `/status` | Daemon status, active project, pending requests |
| `/ebash` | Send a command to the most recently stopped agent |
| `/ebash:projectname` | Route command to a specific project |
| `/ebash:projectname text` | Send text immediately (no prompt) |

**Plain text** while agent is stopped → sent as next instruction.  
**`reset 4h30m`** (plain text, no slash) → set/update the reset reminder manually.

---

## CLI commands

```bash
whip setup          # configure + install Claude Code hooks
whip start          # start daemon (foreground)
whip start -d       # start in background
whip stop-daemon    # stop background daemon
whip status         # check daemon is alive
whip approve        # approve pending tool from terminal (no phone needed)
whip deny           # deny pending tool from terminal
whip go             # unblock a waiting Stop hook from terminal
whip go "text"      # unblock with a specific message
whip tail           # watch all whip events in real time
whip notify "text"  # send a Telegram message manually
whip notify "done" -b "Continue" -b "Stop"   # with reply buttons
whip reset-in 4h30m # schedule a Telegram notification after duration
```

---

## Approvals from laptop + phone simultaneously

The terminal shows the approval prompt as soon as Claude Code triggers a hook:

```
[whip] 🔧 Bash: rm -rf /tmp/old_build
[whip]    whip approve  /  whip deny   (or button in Telegram)
```

First to respond wins — phone button or terminal command. The other channel sees the message updated.

---

## Multi-project support

Run Claude Code in multiple projects simultaneously — whip tracks which is which.

```
/ebash            → routes to most recently stopped agent
/ebash:frontend   → routes specifically to "frontend" project
/ebash:backend "add tests" → sends text immediately to "backend"
```

If the project isn't active: `⚪ Project «frontend» is not active (active stops: backend)`.

---

## claude.ai usage data (macOS + Claude Desktop)

On macOS with Claude Desktop installed, `/limits` reads live data directly from the claude.ai API using Claude Desktop's existing session — no setup needed.

```
📋 Limits

⏱ Session (5h):   ████████ 95%  in 3h36m (at 23:00)
📅 Weekly (7d):   ██░░░░░░ 27%  in 139h36m (at Mon 13:00)
💳 Extra credits: ████████ 1709/1700 (100%)

⏱ Reset reminder: in 3h36m
```

`/limits` also auto-schedules the reset reminder if none is set.  
`/reset` fetches the reset time from the API and schedules/updates the reminder.

---

## PreToolUse modes

Control which tools require Telegram approval via `WHIP_PRETOOL_MODE` in `~/.whip/.env`:

| Mode | Behaviour |
|---|---|
| `all` (default) | Every tool call goes to Telegram |
| `dangerous` | Only bash commands with `rm`, `git push`, `sudo`, etc. |
| `safe_reads` | Skip Read/Glob/Grep/WebSearch — approve the rest |
| `off` | No approvals (pass-through) |

```env
WHIP_PRETOOL_MODE=dangerous
```

---

## Integration with Codex and other agents

For agents without built-in hook support, use `whip notify`:

```bash
codex "build feature X" && whip notify "Codex done!" -b "Keep going" -b "Stop"
```

```bash
#!/bin/bash
your-agent "$@"
whip notify "Agent finished: $1" -b "Continue" -b "Stop"
```

---

## Auto-start on macOS

Add to `~/.zshrc` or a launchd plist:

```bash
pgrep -f "whip start" > /dev/null || whip start -d
```

---

## Config reference

| Variable | Default | Description |
|---|---|---|
| `WHIP_TELEGRAM_TOKEN` | — | Bot token from BotFather |
| `WHIP_TELEGRAM_CHAT_ID` | — | Your Telegram chat ID |
| `WHIP_DAEMON_PORT` | `7331` | Daemon port |
| `WHIP_TIMEOUT` | `1800` | Max wait for user response (seconds) |
| `WHIP_PRETOOL_MODE` | `all` | Approval mode: `all`, `dangerous`, `safe_reads`, `off` |
| `WHIP_DAEMON_HOST` | `127.0.0.1` | Daemon bind host |

---

## Requirements

- Python 3.10+
- Claude Code (for Stop and PreToolUse hooks)
- A Telegram bot token + your chat ID
- macOS (for claude.ai usage data via Claude Desktop) — optional

---

## License

MIT
