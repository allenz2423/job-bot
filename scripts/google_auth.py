"""Shared OAuth helpers for the local Google API adapters."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def config_value(config: dict[str, str], key: str, default: str = "") -> str:
    return os.environ.get(key, config.get(key, default)).strip()


def resolve_path(value: str, default: str) -> Path:
    path = Path(value or default).expanduser()
    return path if path.is_absolute() else ROOT / path


def save_token(path: Path, token: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(token, stream, indent=2)
        stream.write("\n")
    os.replace(temporary, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def access_token(token_path: Path) -> str:
    try:
        token = json.loads(token_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RuntimeError("Google OAuth token not found; run scripts/authorize_google.py first.") from None
    except json.JSONDecodeError:
        raise RuntimeError("Google OAuth token file is invalid; rerun scripts/authorize_google.py.") from None

    try:
        expiry = datetime.fromisoformat(str(token.get("expiry", "")).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        expiry = 0
    if token.get("token") and expiry > time.time() + 60:
        return str(token["token"])

    refresh_token = str(token.get("refresh_token", ""))
    client_id = str(token.get("client_id", ""))
    client_secret = str(token.get("client_secret", ""))
    if not all((refresh_token, client_id, client_secret)):
        raise RuntimeError("OAuth token is missing refresh credentials; rerun scripts/authorize_google.py.")

    request = Request(
        TOKEN_ENDPOINT,
        data=urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }).encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            refreshed = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        if error.code in (400, 401):
            raise RuntimeError("Google rejected the refresh token; rerun scripts/authorize_google.py.") from None
        raise RuntimeError(f"Google token refresh failed (HTTP {error.code}).") from None
    except (URLError, TimeoutError) as error:
        reason = error.reason if isinstance(error, URLError) else "timeout"
        raise RuntimeError(f"Could not reach Google's token service: {reason}.") from None

    token["token"] = refreshed["access_token"]
    token["expiry"] = datetime.fromtimestamp(
        time.time() + int(refreshed.get("expires_in", 3600)), timezone.utc
    ).isoformat().replace("+00:00", "Z")
    save_token(token_path, token)
    return str(token["token"])
