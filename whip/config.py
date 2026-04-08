import os
from pathlib import Path
from dotenv import load_dotenv


def _load() -> dict:
    # Priority: env vars > ~/.whip/.env > ./.env
    config_dir = Path(os.getenv("WHIP_CONFIG_DIR", Path.home() / ".whip"))
    env_file = config_dir / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=False)
    else:
        load_dotenv(override=False)

    return {
        "telegram_token": os.getenv("WHIP_TELEGRAM_TOKEN", ""),
        "telegram_chat_id": os.getenv("WHIP_TELEGRAM_CHAT_ID", ""),
        "daemon_host": os.getenv("WHIP_DAEMON_HOST", "127.0.0.1"),
        "daemon_port": int(os.getenv("WHIP_DAEMON_PORT", "7331")),
        "timeout": int(os.getenv("WHIP_TIMEOUT", "1800")),
    }


CONFIG = _load()
