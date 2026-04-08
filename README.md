# whip

Remote control for AI coding agents (Claude Code, Codex, anything) via Telegram.

When the agent finishes a task → you get a Telegram message with a summary and buttons.
When the agent asks for approval → same thing. You click from the couch.

```
Agent stops  →  Telegram message
                 [🚀 Ебаш дальше]
                 [✏️ Написать команду]
                 [✅ Стоп]

Agent wants to edit a file / run bash?  →  Telegram message
                 [✅ Да]  [❌ Нет]
                 [🔥 Да на всё в этой сессии]
```

## Install

```bash
pip install whip-agent
```

Or from source:

```bash
git clone https://github.com/yourname/whip
cd whip
pip install -e .
```

## Setup

1. Create a Telegram bot via [@BotFather](https://t.me/BotFather) → get token
2. Get your chat ID from [@userinfobot](https://t.me/userinfobot)

```bash
whip setup
# prompts for token + chat ID, installs Claude Code hooks automatically
```

## Usage

Start the daemon (keep it running while you work):

```bash
whip start          # foreground
whip start -d       # background
```

That's it. Now just use Claude Code normally — every time it stops you'll get a Telegram message.

## Send from any agent/script

```bash
whip notify "Finished deploying!" -b "Continue" -b "Rollback"
```

Works from bash, Python, Makefile, whatever. Use it with Codex too.

## How it works

```
Claude Code hooks (Stop / PreToolUse)
        │
        ▼  HTTP POST (blocks until user responds)
whip daemon (localhost:7331)
        │
        ▼  Telegram Bot API (long-polling)
Your phone
```

- **Stop hook** — fires when Claude Code finishes. Daemon holds the connection open,
  sends a Telegram message, waits. When you tap a button or type a reply, the hook
  receives it and either lets the agent stop or injects your message as a new user turn.

- **PreToolUse hook** — fires before destructive operations (bash rm/push, file writes, etc.).
  You approve or deny from Telegram. "Да на всё" approves everything until the session ends.

- **`whip notify`** — a CLI command any script can call to fire a message without hooks.

## Config

`~/.whip/.env`:

```
WHIP_TELEGRAM_TOKEN=...
WHIP_TELEGRAM_CHAT_ID=...
WHIP_DAEMON_PORT=7331
WHIP_TIMEOUT=1800
```

## Commands

```
whip setup          Configure + install Claude Code hooks
whip start          Start daemon (foreground)
whip start -d       Start daemon (background)
whip stop-daemon    Stop background daemon
whip status         Check if daemon is running
whip notify TEXT    Send notification to Telegram
```

## License

MIT
