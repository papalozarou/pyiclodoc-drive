# This Dockerfile builds a minimal Alpine-based container for non-root iCloud Drive backups.
FROM alpine:3.20

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apk add --no-cache \
    python3 \
    py3-pip \
    tini \
    su-exec \
    ca-certificates \
    curl \
    tzdata \
    jq

RUN set -eux; \
    arch="$(apk --print-arch)"; \
    target=""; \
    [ "$arch" = "x86_64" ] && target="amd64" || true; \
    [ "$arch" = "aarch64" ] && target="arm64" || true; \
    if [ -n "$target" ]; then \
      curl -fsSL "https://github.com/tarampampam/microcheck/releases/latest/download/microcheck-linux-${target}" -o /usr/local/bin/microcheck || true; \
      chmod +x /usr/local/bin/microcheck || true; \
    fi

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY scripts /app/scripts

RUN chmod +x /app/scripts/entrypoint.sh /app/scripts/start.sh

VOLUME ["/config", "/output", "/logs"]

HEALTHCHECK --interval=1m --timeout=10s --start-period=30s --retries=3 \
  CMD /app/scripts/healthcheck.sh

ENTRYPOINT ["/sbin/tini", "--", "/app/scripts/entrypoint.sh"]
