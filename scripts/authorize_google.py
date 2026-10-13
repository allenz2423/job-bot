#!/usr/bin/env python3
"""Authorize the local job bot for Gmail read-only and per-file Sheets access."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / "config" / "gmail.env"
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def save_token(path: Path, token: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    temp_path = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(token, stream, indent=2)
        stream.write("\n")
    os.replace(temp_path, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def main() -> int:
    if not ENV_FILE.is_file():
        raise SystemExit(f"Missing {ENV_FILE}. Copy config/gmail.env.example first.")

    config = read_env(ENV_FILE)
    client_id = config.get("GMAIL_OAUTH_CLIENT_ID", "")
    client_secret = config.get("GMAIL_OAUTH_CLIENT_SECRET", "")
    if not client_id or client_id.startswith("replace-"):
        raise SystemExit("Set GMAIL_OAUTH_CLIENT_ID in config/gmail.env.")
    if not client_secret or client_secret.startswith("replace-"):
        raise SystemExit("Set GMAIL_OAUTH_CLIENT_SECRET in config/gmail.env.")
    client_type = config.get("GMAIL_OAUTH_CLIENT_TYPE", "desktop").lower()
    if client_type != "desktop":
        raise SystemExit("This launcher requires a Google OAuth Desktop app client.")

    scopes = [config.get("GMAIL_SCOPES", "https://www.googleapis.com/auth/gmail.readonly")]
    sheets_scope = config.get("GOOGLE_SHEETS_SCOPE", "").strip()
    if sheets_scope:
        scopes.append(sheets_scope)
    scopes = list(dict.fromkeys(scope for scope in scopes if scope))
    if not scopes:
        raise SystemExit("No OAuth scopes configured.")

    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
    challenge = challenge.rstrip(b"=").decode("ascii")
    result: dict[str, str] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            request = urlsplit(self.path)
            params = parse_qs(request.query)
            if request.path != "/":
                self.send_error(404)
                return
            if params.get("state", [""])[0] != state:
                result["error"] = "OAuth state did not match; authorization was rejected."
                self._respond(400, "Authorization rejected. You can close this tab.")
                return
            if params.get("error"):
                result["error"] = f"Google authorization failed: {params['error'][0]}"
                self._respond(400, "Authorization was not completed. You can close this tab.")
                return
            code = params.get("code", [""])[0]
            if not code:
                result["error"] = "Google returned no authorization code."
                self._respond(400, "No authorization code was returned. You can close this tab.")
                return
            result["code"] = code
            self._respond(200, "Authorization received. You can close this tab.")

        def _respond(self, status: int, message: str) -> None:
            body = (
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<meta name='referrer' content='no-referrer'></head><body>"
                f"<p>{message}</p></body></html>"
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            # Avoid logging the callback URL, which contains the one-time code.
            return

    server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
    server.timeout = 1
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/"

    auth_params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    auth_url = AUTH_ENDPOINT + "?" + urlencode(auth_params)

    print("Open this one-time authorization link in a browser on this machine:", flush=True)
    print(auth_url, flush=True)
    print("Waiting up to 10 minutes for Google's redirect...", flush=True)

    deadline = time.monotonic() + 600
    try:
        while not result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    if "error" in result:
        raise SystemExit(result["error"])
    if "code" not in result:
        raise SystemExit("Authorization timed out. Run this command again for a new link.")

    token_request = Request(
        TOKEN_ENDPOINT,
        data=urlencode(
            {
                "code": result["code"],
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            }
        ).encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(token_request, timeout=30) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise SystemExit(f"Google token exchange failed (HTTP {error.code}). Check OAuth client type and scopes.") from None
    except URLError as error:
        raise SystemExit(f"Could not reach Google token service: {error.reason}") from None

    refresh_token = response_data.get("refresh_token")
    if not refresh_token:
        raise SystemExit("Google returned no refresh token. Run again and approve consent when prompted.")

    expires_in = int(response_data.get("expires_in", 3600))
    token_record = {
        "token": response_data["access_token"],
        "refresh_token": refresh_token,
        "token_uri": TOKEN_ENDPOINT,
        "client_id": client_id,
        "client_secret": client_secret,
        "scopes": scopes,
        "expiry": (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat().replace("+00:00", "Z"),
    }
    token_path = ROOT / config.get("GMAIL_TOKEN_FILE", "secrets/gmail/token.json")
    save_token(token_path, token_record)
    print(f"Authorization saved to {token_path.relative_to(ROOT)}. Tokens were not printed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
