#!/usr/bin/env python3
"""Create a local-only ntfy config with a random topic and topic-scoped ACL."""

from __future__ import annotations

import os
import secrets
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config" / "ntfy"
ENV_FILE = ROOT / "config" / "ntfy.env"
SERVER_FILE = CONFIG_DIR / "server.yml"


def private_write(path: Path, content: str) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content)
    os.replace(temp, path)
    os.chmod(path, 0o600)


def main() -> int:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(CONFIG_DIR, 0o700)
    if ENV_FILE.exists() or SERVER_FILE.exists():
        raise SystemExit("ntfy config already exists; refusing to replace its topic or access rules.")

    topic = f"jobbot-{secrets.token_hex(20)}"
    private_write(
        ENV_FILE,
        "# Local ntfy topic. Keep this file private.\n"
        "NTFY_SERVER_URL=http://ntfy:80\n"
        f"NTFY_TOPIC={topic}\n",
    )
    private_write(
        SERVER_FILE,
        'base-url: "http://localhost:8090"\n'
        'cache-file: "/var/cache/ntfy/cache.db"\n'
        'auth-file: "/var/lib/ntfy/user.db"\n'
        'auth-default-access: "deny-all"\n'
        "auth-access:\n"
        f'  - "*:{topic}:rw"\n',
    )
    print("Private local ntfy config created; the topic is stored in ignored config/ntfy.env.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
