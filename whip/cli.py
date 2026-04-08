"""Whip CLI — setup, start, status, notify."""
import json
import os
import shutil
import sys
from pathlib import Path

import click
import httpx


def _config_dir() -> Path:
    return Path(os.getenv("WHIP_CONFIG_DIR", Path.home() / ".whip"))


def _daemon_url() -> str:
    port = os.getenv("WHIP_DAEMON_PORT", "7331")
    host = os.getenv("WHIP_DAEMON_HOST", "127.0.0.1")
    return f"http://{host}:{port}"


@click.group()
@click.version_option()
def cli():
    """Whip — remote control for AI coding agents via Telegram."""


# ------------------------------------------------------------------ setup

@cli.command()
@click.option("--token", default=None, help="Telegram Bot Token (from @BotFather)")
@click.option("--chat-id", default=None, help="Your Telegram Chat ID (from @userinfobot)")
@click.option("--port", default="7331", show_default=True, help="Daemon port")
@click.option("--no-hooks", is_flag=True, default=False, help="Skip Claude Code hook installation")
def setup(token, chat_id, port, no_hooks):
    """Configure whip and install Claude Code hooks."""
    if not token:
        token = click.prompt("Telegram Bot Token (get from @BotFather)")
    if not chat_id:
        chat_id = click.prompt("Telegram Chat ID (get from @userinfobot)")

    config_dir = _config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)

    (config_dir / ".env").write_text(
        f"WHIP_TELEGRAM_TOKEN={token}\n"
        f"WHIP_TELEGRAM_CHAT_ID={chat_id}\n"
        f"WHIP_DAEMON_PORT={port}\n"
        f"WHIP_TIMEOUT=1800\n"
    )
    click.echo(f"Config saved → {config_dir}/.env")

    if not no_hooks:
        _install_claude_hooks(config_dir, port)

    click.echo("\nDone! Start the daemon with:  whip start")


def _install_claude_hooks(config_dir: Path, port: str):
    import whip
    pkg_hooks = Path(whip.__file__).parent / "hooks"

    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(exist_ok=True)
    hooks_dir = claude_dir / "hooks"
    hooks_dir.mkdir(exist_ok=True)

    stop_dst = hooks_dir / "whip_stop.py"
    pre_dst = hooks_dir / "whip_pre_tool.py"

    shutil.copy(pkg_hooks / "stop.py", stop_dst)
    shutil.copy(pkg_hooks / "pre_tool.py", pre_dst)
    stop_dst.chmod(0o755)
    pre_dst.chmod(0o755)

    python = sys.executable
    env_prefix = f"WHIP_DAEMON_PORT={port} WHIP_CONFIG_DIR={config_dir}"

    settings_path = claude_dir / "settings.json"
    try:
        settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    except json.JSONDecodeError:
        settings = {}

    settings.setdefault("hooks", {})
    settings["hooks"]["Stop"] = [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": f"{env_prefix} {python} {stop_dst}",
                }
            ],
        }
    ]
    settings["hooks"]["PreToolUse"] = [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": f"{env_prefix} {python} {pre_dst}",
                }
            ],
        }
    ]

    settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False))
    click.echo(f"Claude Code hooks installed → {settings_path}")


# ------------------------------------------------------------------ start

@cli.command()
@click.option("--port", default=None, help="Override daemon port")
@click.option("--daemon", "-d", is_flag=True, default=False, help="Run in background (nohup)")
def start(port, daemon):
    """Start the whip daemon."""
    # Reload config with possible override
    if port:
        os.environ["WHIP_DAEMON_PORT"] = port

    from whip.config import CONFIG  # re-import after env change

    if daemon:
        log_file = _config_dir() / "daemon.log"
        pid_file = _config_dir() / "daemon.pid"
        import subprocess
        proc = subprocess.Popen(
            [sys.executable, "-m", "whip.cli", "start"],
            stdout=open(log_file, "w"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        pid_file.write_text(str(proc.pid))
        click.echo(f"Daemon started (pid {proc.pid}). Logs: {log_file}")
        return

    import uvicorn
    from whip.daemon import app

    p = CONFIG["daemon_port"]
    h = CONFIG["daemon_host"]
    click.echo(f"Whip daemon → http://{h}:{p}")
    uvicorn.run(app, host=h, port=p, log_level="info")


# ------------------------------------------------------------------ stop daemon

@cli.command("stop-daemon")
def stop_daemon():
    """Stop a background daemon process."""
    pid_file = _config_dir() / "daemon.pid"
    if not pid_file.exists():
        click.echo("No pid file found — daemon may not be running.")
        return
    pid = int(pid_file.read_text().strip())
    try:
        os.kill(pid, 15)  # SIGTERM
        pid_file.unlink()
        click.echo(f"Daemon (pid {pid}) stopped.")
    except ProcessLookupError:
        click.echo("Process not found — already dead.")
        pid_file.unlink(missing_ok=True)


# ------------------------------------------------------------------ status

@cli.command()
def status():
    """Check daemon status."""
    try:
        r = httpx.get(f"{_daemon_url()}/health", timeout=2)
        data = r.json()
        click.echo(f"Daemon running — pending: {data['pending']}, approve_all: {data['approve_all']}")
    except Exception:
        click.echo("Daemon not running. Run: whip start")


# ------------------------------------------------------------------ local approve/deny/go

@cli.command()
def approve():
    """Approve the pending tool request from terminal (no phone needed)."""
    try:
        r = httpx.post(f"{_daemon_url()}/local-approve", json={"decision": "approve"}, timeout=5)
        data = r.json()
        if data.get("ok"):
            click.echo(f"✅ Approved [{data.get('rid')}]")
        else:
            click.echo(f"Nothing pending to approve.")
    except Exception as e:
        click.echo(f"Daemon unreachable: {e}", err=True)


@cli.command()
@click.argument("reason", default="", required=False)
def deny(reason):
    """Deny the pending tool request from terminal."""
    try:
        r = httpx.post(f"{_daemon_url()}/local-approve",
                       json={"decision": "block", "message": reason}, timeout=5)
        data = r.json()
        if data.get("ok"):
            click.echo(f"❌ Denied [{data.get('rid')}]")
        else:
            click.echo("Nothing pending to deny.")
    except Exception as e:
        click.echo(f"Daemon unreachable: {e}", err=True)


@cli.command()
@click.argument("message", default="продолжай", required=False)
def go(message):
    """Unblock a waiting Stop hook from terminal — agent continues."""
    try:
        r = httpx.post(f"{_daemon_url()}/local-approve",
                       json={"decision": "approve", "message": message}, timeout=5)
        data = r.json()
        if data.get("ok"):
            click.echo(f"🚀 Sent [{data.get('rid')}]: {message}")
        else:
            click.echo("No pending stop request.")
    except Exception as e:
        click.echo(f"Daemon unreachable: {e}", err=True)


# ------------------------------------------------------------------ tail

@cli.command()
def tail():
    """
    Watch all whip events in real time — run this in a separate terminal tab.

    Shows approvals, stop events, and responses as they happen.
    Use whip approve / whip deny / whip go right in the same tab.
    """
    activity_log = _config_dir() / "activity.log"
    activity_log.parent.mkdir(parents=True, exist_ok=True)
    activity_log.touch()

    click.echo("── whip activity log ─────────────────────────────")
    click.echo("   whip approve / deny / go  to respond from here")
    click.echo("──────────────────────────────────────────────────")

    import subprocess
    try:
        subprocess.run(["tail", "-f", str(activity_log)])
    except KeyboardInterrupt:
        pass


# ------------------------------------------------------------------ reset-in

@cli.command("reset-in")
@click.argument("duration")
@click.option("--message", "-m", default="", help="Custom notification text")
def reset_in(duration, message):
    """
    Schedule a Telegram notification after a duration (persistent, survives restarts).

    Examples:
        whip reset-in 4h40m
        whip reset-in 30m -m "Go again!"
        whip reset-in list    — show scheduled
        whip reset-in clear   — cancel all
    """
    import re, time as _time
    from datetime import datetime, timedelta

    if duration == "list":
        try:
            r = httpx.get(f"{_daemon_url()}/schedule", timeout=5)
            items = r.json()
            if not items:
                click.echo("Нет запланированных уведомлений.")
            for i in items:
                secs = i["in_seconds"]
                h, rem = divmod(secs, 3600)
                m, s = divmod(rem, 60)
                click.echo(f"  ⏱ через {h}h{m}m{s}s — \"{i['text']}\"")
        except Exception as e:
            click.echo(f"Daemon unreachable: {e}", err=True)
        return

    if duration == "clear":
        try:
            httpx.delete(f"{_daemon_url()}/schedule", timeout=5)
            click.echo("Все расписания очищены.")
        except Exception as e:
            click.echo(f"Daemon unreachable: {e}", err=True)
        return

    # Parse duration: "4h40m", "30m", "2h", "90s"
    total = 0
    for val, unit in re.findall(r"(\d+)([hms])", duration.lower()):
        v = int(val)
        if unit == "h":
            total += v * 3600
        elif unit == "m":
            total += v * 60
        elif unit == "s":
            total += v

    if total == 0:
        click.echo(f"Не могу разобрать: {duration!r}  (формат: 4h40m / 30m / 2h)")
        return

    fires_at = _time.time() + total
    fires_at_dt = datetime.now() + timedelta(seconds=total)
    text = message or f"🔄 Сессия Claude обновилась! Можно работать снова."

    try:
        r = httpx.post(
            f"{_daemon_url()}/schedule",
            json={"fire_at": fires_at, "text": text},
            timeout=5,
        )
        if r.json().get("ok"):
            click.echo(f"⏱  Уведомление в {fires_at_dt.strftime('%H:%M:%S')} (через {duration})")
            click.echo(f"   \"{text}\"")
            click.echo(f"   Персистентно — переживёт рестарт демона.")
        else:
            click.echo(f"Ошибка: {r.text}", err=True)
    except Exception as e:
        click.echo(f"Daemon unreachable: {e}", err=True)


# ------------------------------------------------------------------ notify

@cli.command()
@click.argument("text")
@click.option("--button", "-b", multiple=True, help="Add a quick-reply button (can repeat)")
def notify(text, button):
    """
    Send a message to Telegram from any script or agent.

    Example (from a Codex post-step script):
        whip notify "Finished step 3!" -b "Continue" -b "Stop"
    """
    try:
        r = httpx.post(
            f"{_daemon_url()}/notify",
            json={"text": text, "buttons": list(button)},
            timeout=10,
        )
        if r.json().get("ok"):
            click.echo("Sent.")
        else:
            click.echo(f"Error: {r.text}", err=True)
    except Exception as e:
        click.echo(f"Daemon unreachable: {e}", err=True)
        sys.exit(1)
