FROM node:22-bookworm-slim

ARG INSTALL_APPLICATION_VNC=0

ENV HOME=/home/node \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && runtime_packages="python3 python3-cryptography poppler-utils build-essential ca-certificates chromium chromium-sandbox xvfb xauth" \
    && if [ "$INSTALL_APPLICATION_VNC" = "1" ]; then runtime_packages="$runtime_packages x11vnc novnc"; fi \
    && apt-get install -y --no-install-recommends $runtime_packages \
    && npm install --global @bitwarden/cli@2026.9.1 \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --chown=node:node scripts/ /app/scripts/
RUN chmod 0755 /app/scripts/docker-entrypoint.sh \
    && mkdir -p /app/config /app/state /app/private-secrets /home/node/.config \
    && chown -R node:node /app /home/node/.config

USER node
ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["python3", "scripts/sync_gmail.py", "--watch"]
