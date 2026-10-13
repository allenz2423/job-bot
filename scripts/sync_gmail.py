#!/usr/bin/env python3
"""Sync headers for messages matching the configured Gmail query.

Message bodies and snippets are intentionally never fetched or persisted.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1"


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


def save_json_private(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    temp = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
    os.replace(temp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def refresh_access_token(token_path: Path, token: dict[str, object]) -> dict[str, object]:
    refresh_token = str(token.get("refresh_token", ""))
    client_id = str(token.get("client_id", ""))
    client_secret = str(token.get("client_secret", ""))
    if not all((refresh_token, client_id, client_secret)):
        raise RuntimeError("OAuth token is missing refresh credentials; run scripts/authorize_google.py again.")

    request = Request(
        TOKEN_ENDPOINT,
        data=urlencode(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }
        ).encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            refreshed = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        if error.code in (400, 401):
            raise RuntimeError("Google rejected the saved refresh token; rerun scripts/authorize_google.py.") from None
        raise RuntimeError(f"Google token refresh failed (HTTP {error.code}).") from None
    except (URLError, TimeoutError) as error:
        raise RuntimeError(f"Could not reach Google's token service: {error.reason if isinstance(error, URLError) else 'timeout'}.") from None

    token["token"] = refreshed["access_token"]
    token["expiry"] = datetime.fromtimestamp(
        time.time() + int(refreshed.get("expires_in", 3600)), timezone.utc
    ).isoformat().replace("+00:00", "Z")
    save_json_private(token_path, token)
    return token


def access_token(token_path: Path) -> str:
    try:
        token = json.loads(token_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RuntimeError("OAuth token not found; run scripts/authorize_google.py first.") from None
    except json.JSONDecodeError:
        raise RuntimeError("OAuth token file is invalid; rerun scripts/authorize_google.py.") from None

    expiry = token.get("expiry", "")
    try:
        expires_at = datetime.fromisoformat(str(expiry).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        expires_at = 0
    if not token.get("token") or expires_at <= time.time() + 60:
        token = refresh_access_token(token_path, token)
    return str(token["token"])


def gmail_get(path: str, params: list[tuple[str, str]], bearer: str) -> dict[str, object]:
    url = f"{GMAIL_API}/{path}?{urlencode(params)}"
    request = Request(url, headers={"Authorization": f"Bearer {bearer}"})
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        # Do not print the response body; it may contain account or request details.
        if error.code in (401, 403):
            raise RuntimeError(f"Gmail API denied the request (HTTP {error.code}); check consent and Gmail API setup.") from None
        raise RuntimeError(f"Gmail API request failed (HTTP {error.code}).") from None
    except (URLError, TimeoutError) as error:
        reason = error.reason if isinstance(error, URLError) else "timeout"
        raise RuntimeError(f"Could not reach the Gmail API: {reason}.") from None


def parse_headers(message: dict[str, object]) -> dict[str, str]:
    payload = message.get("payload", {})
    raw_headers = payload.get("headers", []) if isinstance(payload, dict) else []
    headers = {
        str(item.get("name", "")).lower(): str(item.get("value", ""))
        for item in raw_headers
        if isinstance(item, dict)
    }
    sender_name, sender_address = parseaddr(headers.get("from", ""))
    subject = headers.get("subject", "")[:500]
    # Keep verification codes and verification-related subjects out of durable state.
    if re.search(r"\b(verification|verify|security|one[- ]time|passcode|otp|code|login|authentication|confirm)\b", subject, re.I):
        subject = "[verification subject redacted]"
    else:
        subject = re.sub(r"(?<!\d)\d{4,}(?!\d)", "[redacted]", subject)
    return {
        "sender_name": sender_name[:200],
        "sender_address": sender_address[:320],
        "subject": subject,
        "date_header": headers.get("date", "")[:200],
    }


def open_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    connection = sqlite3.connect(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS gmail_messages (
            message_id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            sender_name TEXT NOT NULL DEFAULT '',
            sender_address TEXT NOT NULL DEFAULT '',
            subject TEXT NOT NULL DEFAULT '',
            date_header TEXT NOT NULL DEFAULT '',
            internal_date TEXT NOT NULL DEFAULT '',
            label_ids_json TEXT NOT NULL DEFAULT '[]',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )
    """)
    connection.commit()
    return connection


def sync(config: dict[str, str]) -> tuple[int, int]:
    if config_value(config, "GMAIL_ENABLED", "false").lower() not in {"1", "true", "yes", "on"}:
        raise RuntimeError("Gmail sync is disabled. Set GMAIL_ENABLED=true in config/gmail.env to enable it.")

    query = config_value(config, "GMAIL_SEARCH_QUERY")
    if not query:
        raise RuntimeError("Set GMAIL_SEARCH_QUERY to a narrow Gmail search, ideally a dedicated label.")
    try:
        max_messages = max(1, min(500, int(config_value(config, "GMAIL_SYNC_MAX_MESSAGES", "100"))))
    except ValueError:
        raise RuntimeError("GMAIL_SYNC_MAX_MESSAGES must be a number from 1 to 500.") from None

    token_path = resolve_path(config_value(config, "GMAIL_TOKEN_FILE"), "secrets/gmail/token.json")
    bearer = access_token(token_path)
    user_id = quote(config_value(config, "GMAIL_USER_ID", "me"), safe="")
    db_path = resolve_path(config_value(config, "GMAIL_DB_PATH"), "state/jobbot.sqlite3")
    connection = open_database(db_path)
    seen_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    fetched = inserted = 0
    page_token = ""
    try:
        while fetched < max_messages:
            page_size = min(100, max_messages - fetched)
            params = [("q", query), ("maxResults", str(page_size))]
            if page_token:
                params.append(("pageToken", page_token))
            page = gmail_get(f"users/{user_id}/messages", params, bearer)
            refs = page.get("messages", [])
            if not isinstance(refs, list) or not refs:
                break

            for ref in refs:
                if not isinstance(ref, dict) or not ref.get("id"):
                    continue
                message_id = str(ref["id"])
                details = gmail_get(
                    f"users/{user_id}/messages/{quote(message_id, safe='')}",
                    [("format", "metadata"), ("metadataHeaders", "From"),
                     ("metadataHeaders", "Subject"), ("metadataHeaders", "Date")],
                    bearer,
                )
                headers = parse_headers(details)
                labels = details.get("labelIds", [])
                labels_json = json.dumps(labels if isinstance(labels, list) else [])
                internal_date = str(details.get("internalDate", ""))
                if internal_date.isdigit():
                    internal_date = datetime.fromtimestamp(
                        int(internal_date) / 1000, timezone.utc
                    ).isoformat().replace("+00:00", "Z")
                is_new = connection.execute(
                    "SELECT 1 FROM gmail_messages WHERE message_id = ?", (message_id,)
                ).fetchone() is None
                connection.execute(
                    """
                    INSERT INTO gmail_messages (
                        message_id, thread_id, sender_name, sender_address, subject,
                        date_header, internal_date, label_ids_json, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(message_id) DO UPDATE SET
                        thread_id=excluded.thread_id,
                        sender_name=excluded.sender_name,
                        sender_address=excluded.sender_address,
                        subject=excluded.subject,
                        date_header=excluded.date_header,
                        internal_date=excluded.internal_date,
                        label_ids_json=excluded.label_ids_json,
                        last_seen_at=excluded.last_seen_at
                    """,
                    (
                        message_id,
                        str(details.get("threadId", ref.get("threadId", ""))),
                        headers["sender_name"],
                        headers["sender_address"],
                        headers["subject"],
                        headers["date_header"],
                        internal_date,
                        labels_json,
                        seen_at,
                        seen_at,
                    ),
                )
                inserted += int(is_new)
                fetched += 1
                if fetched % 25 == 0:
                    connection.commit()
            connection.commit()
            page_token = str(page.get("nextPageToken", ""))
            if not page_token:
                break
    finally:
        connection.close()
    return fetched, inserted


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync job-related Gmail message headers.")
    parser.add_argument(
        "--watch",
        action="store_true",
        help="repeat using GMAIL_POLL_INTERVAL_SECONDS; keep this process running",
    )
    args = parser.parse_args()
    config = read_env(ROOT / "config" / "gmail.env")

    def run_once() -> bool:
        try:
            fetched, inserted = sync(config)
        except (OSError, RuntimeError, sqlite3.Error) as error:
            print(f"Gmail sync failed: {error}", file=sys.stderr, flush=True)
            return False
        print(
            f"Gmail sync complete: {fetched} matching message headers checked; "
            f"{inserted} new messages recorded.",
            flush=True,
        )
        return True

    if not args.watch:
        return 0 if run_once() else 1

    if config_value(config, "GMAIL_ENABLED", "false").lower() not in {"1", "true", "yes", "on"}:
        print("Gmail sync is disabled. Set GMAIL_ENABLED=true in config/gmail.env to enable it.", file=sys.stderr)
        return 1
    try:
        interval = max(30, min(86400, int(config_value(config, "GMAIL_POLL_INTERVAL_SECONDS", "300"))))
    except ValueError:
        print("GMAIL_POLL_INTERVAL_SECONDS must be a number of seconds.", file=sys.stderr)
        return 1
    print(f"Gmail watch started; polling every {interval} seconds. Press Ctrl+C to stop.", flush=True)
    try:
        while True:
            started = time.monotonic()
            run_once()
            time.sleep(max(0, interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print("Gmail watch stopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
