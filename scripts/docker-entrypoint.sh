#!/bin/sh
set -eu

if [ -n "${BW_SERVER_URL:-}" ]; then
    if ! bw status --raw 2>/dev/null | grep -Eq '"status"[[:space:]]*:[[:space:]]*"(locked|unlocked)"'; then
        bw config server "$BW_SERVER_URL" >/dev/null
    fi
fi

if [ -n "${BW_PASSWORD_FILE:-}" ] && [ -s "$BW_PASSWORD_FILE" ]; then
    BW_SESSION="$(bw unlock --passwordfile "$BW_PASSWORD_FILE" --raw)"
    export BW_SESSION
fi

exec "$@"
