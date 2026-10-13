"""Create the ignored local environment file for the SearXNG container."""

from __future__ import annotations

import secrets
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / "config" / "searxng.env"


def main() -> int:
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not ENV_FILE.exists():
        ENV_FILE.write_text(f"SEARXNG_SECRET={secrets.token_hex(32)}\n", encoding="ascii")
    ENV_FILE.chmod(0o600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
