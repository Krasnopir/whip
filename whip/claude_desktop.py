"""
Read claude.ai session cookies directly from the Claude Desktop app (macOS).

Claude Desktop is an Electron app that stores cookies in:
  ~/Library/Application Support/Claude/Cookies  (SQLite, Chromium format)

Cookies are encrypted with AES-128-CBC using a key from macOS Keychain
(same mechanism as Chrome's "safe storage"):
  key = PBKDF2(keychain_password, salt=b'saltysalt', iterations=1003, dklen=16)
  plaintext = AES-CBC-decrypt(key, IV=b' '*16, ciphertext=enc[3:])
  value = plaintext[32:]   # skip 32-byte nonce prefix Chromium prepends
"""
import hashlib
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import Optional

import httpx

log = logging.getLogger("whip.claude_desktop")

_DB_PATH = os.path.expanduser(
    "~/Library/Application Support/Claude/Cookies"
)
_KEYCHAIN_SERVICE = "Claude Safe Storage"
_ORG_ID_ENV = "WHIP_CLAUDE_ORG_ID"

# Org ID is embedded in the oauth token cache key; set here as default.
# Users can override with WHIP_CLAUDE_ORG_ID env var.
_DEFAULT_ORG_ID = "6beb42bc-9145-487f-9cf7-593836b160bc"


# --------------------------------------------------------------------------- decryption

def _get_keychain_key() -> bytes:
    """Derive the AES key from macOS Keychain (Claude Safe Storage)."""
    pw = subprocess.check_output(
        ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
        stderr=subprocess.DEVNULL,
        text=True,
    ).strip()
    return hashlib.pbkdf2_hmac("sha1", pw.encode(), b"saltysalt", 1003, dklen=16)


def _decrypt_cookie(enc_bytes: bytes, key: bytes) -> str:
    """Decrypt a single Chromium v10-encrypted cookie value."""
    b = bytes(enc_bytes)
    if not b:
        return ""
    if b[:3] != b"v10":
        # Unencrypted (rare)
        return b.decode("utf-8", "replace")

    try:
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        raise RuntimeError(
            "cryptography package required: uv add cryptography"
        )

    cipher = Cipher(
        algorithms.AES(key), modes.CBC(b" " * 16), backend=default_backend()
    ).decryptor()
    plain = cipher.update(b[3:]) + cipher.finalize()
    pad = plain[-1]
    plain = plain[:-pad]
    # Chromium prepends 32 bytes of nonce before the actual value
    return plain[32:].decode("utf-8", "replace")


def read_claude_cookies() -> dict[str, str]:
    """
    Read and decrypt all claude.ai cookies from Claude Desktop's Chromium store.
    Returns dict of cookie_name → cookie_value.
    Raises RuntimeError if Claude Desktop is not installed or unavailable.
    """
    if not os.path.exists(_DB_PATH):
        raise RuntimeError("Claude Desktop not found (no Cookies DB)")

    key = _get_keychain_key()

    # Copy DB to temp file (original may be locked by Claude Desktop)
    tmp = tempfile.mktemp(suffix=".db")
    try:
        shutil.copy2(_DB_PATH, tmp)
        conn = sqlite3.connect(tmp)
        rows = conn.execute(
            "SELECT name, encrypted_value FROM cookies WHERE host_key LIKE '%claude%'"
        ).fetchall()
        conn.close()
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass

    result: dict[str, str] = {}
    for name, enc_val in rows:
        try:
            v = _decrypt_cookie(bytes(enc_val), key)
            if v:
                result[name] = v
        except Exception as e:
            log.debug("Cookie decrypt error [%s]: %s", name, e)

    return result


def cookies_to_header(cookies: dict[str, str]) -> str:
    """Build a Cookie: header string from a dict, skipping non-printable values."""
    parts = []
    for k, v in cookies.items():
        # Only include cookies where value looks valid (printable ASCII-ish)
        if v and all(0x20 <= ord(c) < 0x7F for c in v[:10]):
            parts.append(f"{k}={v}")
    return "; ".join(parts)


# --------------------------------------------------------------------------- API

async def fetch_usage() -> dict:
    """
    Fetch claude.ai usage JSON using Claude Desktop's cookies.
    Returns the parsed JSON dict.
    Raises on error.
    """
    cookies = read_claude_cookies()
    cookie_header = cookies_to_header(cookies)
    org_id = os.environ.get(_ORG_ID_ENV, _DEFAULT_ORG_ID)

    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"https://claude.ai/api/organizations/{org_id}/usage",
            headers={
                "Cookie": cookie_header,
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json",
                "Referer": "https://claude.ai/settings/usage",
            },
            timeout=15,
            follow_redirects=True,
        )

    if r.status_code == 403:
        raise RuntimeError(
            "Cloudflare 403 (Claude Desktop cookies expired). "
            "Открой Claude Desktop и оставь его запущенным, потом попробуй снова."
        )
    if r.status_code != 200:
        raise RuntimeError(f"claude.ai/api/usage returned HTTP {r.status_code}")

    return r.json()


def format_usage(data: dict) -> str:
    """Format usage JSON into a human-readable Telegram message."""
    lines: list[str] = []

    def _until(resets_at_str: str) -> str:
        try:
            dt = datetime.fromisoformat(resets_at_str)
            now = datetime.now(tz=timezone.utc)
            secs = max(0, int((dt - now).total_seconds()))
            h, rem = divmod(secs, 3600)
            m = rem // 60
            local = dt.astimezone().strftime("%H:%M")
            return f"через {h}h{m}m (в {local})"
        except Exception:
            return resets_at_str

    five = data.get("five_hour")
    if five:
        pct = five.get("utilization", 0)
        until = _until(five.get("resets_at", ""))
        bar = _bar(pct)
        lines.append(f"⏱ Сессия (5ч):  {bar} {pct:.0f}%  {until}")

    seven = data.get("seven_day")
    if seven:
        pct = seven.get("utilization", 0)
        until = _until(seven.get("resets_at", ""))
        bar = _bar(pct)
        lines.append(f"📅 Неделя (7д):  {bar} {pct:.0f}%  {until}")

    extra = data.get("extra_usage")
    if extra and extra.get("is_enabled"):
        used = extra.get("used_credits", 0)
        limit = extra.get("monthly_limit", 0)
        pct = extra.get("utilization", 0)
        bar = _bar(pct)
        lines.append(f"💳 Extra credits: {bar} {used:.0f}/{limit} ({pct:.0f}%)")

    return "\n".join(lines) if lines else "Нет данных об использовании."


def _bar(pct: float, width: int = 8) -> str:
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def next_reset_seconds(data: dict) -> Optional[int]:
    """Return seconds until the next five_hour session reset (for /reset scheduling)."""
    five = data.get("five_hour", {})
    resets_at = five.get("resets_at")
    if not resets_at:
        return None
    try:
        dt = datetime.fromisoformat(resets_at)
        now = datetime.now(tz=timezone.utc)
        secs = int((dt - now).total_seconds())
        return secs if secs > 0 else None
    except Exception:
        return None
