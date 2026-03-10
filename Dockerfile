# ------------------------------------------------------------------------------
# This Dockerfile defines the backup worker image for Compose-based deployments.
#
# The image uses a multi-stage build to separate dependency installation and
# binary fetch steps from the final runtime stage. Runtime data is externalised
# via mounted volumes for configuration, backup output, and logs.
# ------------------------------------------------------------------------------

# ------------------------------------------------------------------------------
# Pin Alpine image tag and digest for reproducible builds.
# ------------------------------------------------------------------------------
ARG ALP_VER
ARG MCK_VER=1.3.0
ARG ALPINE_IMAGE=alpine:${ALP_VER}

FROM ${ALPINE_IMAGE} AS python-deps

# ------------------------------------------------------------------------------
# Install Python tooling to build an isolated virtual environment.
#
# 1. "python3" provides the interpreter used by the worker.
# 2. "py3-pip" provides package installation for requirements.
# 3. "ca-certificates" enables trusted TLS for package downloads.
# ------------------------------------------------------------------------------
RUN apk add --no-cache \
    python3 \
    py3-pip \
    ca-certificates

# ------------------------------------------------------------------------------
# Build Python dependencies in "/opt/venv" for transfer into the runtime stage.
# ------------------------------------------------------------------------------
WORKDIR /build
COPY requirements.txt /build/requirements.txt
RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir -r /build/requirements.txt

# ------------------------------------------------------------------------------
# Fetch architecture-specific "microcheck" as a required runtime binary.
#
# N.B.
# Pinning "MCK_VER" makes the build reproducible across environments.
# ------------------------------------------------------------------------------
FROM ghcr.io/tarampampam/microcheck:${MCK_VER} AS microcheck

# ------------------------------------------------------------------------------
# Build the final runtime image with only required runtime dependencies.
# ------------------------------------------------------------------------------
FROM ${ALPINE_IMAGE}

# ------------------------------------------------------------------------------
# Add OCI metadata labels for image provenance and tooling integrations.
# ------------------------------------------------------------------------------
LABEL org.opencontainers.image.title="iCloud Drive Backup Container" \
      org.opencontainers.image.description="Incremental iCloud Drive backups with Telegram control." \
      org.opencontainers.image.source="https://github.com/papalozarou/iclouddd-docker" \
      org.opencontainers.image.licenses="UNLICENSED"

# ------------------------------------------------------------------------------
# Configure Python runtime defaults for container-friendly behaviour.
#
# 1. Disable bytecode file writes to keep writable layers clean.
# 2. Enable unbuffered stdout and stderr for immediate log visibility.
# 3. Prefer binaries from the transferred virtual environment.
# ------------------------------------------------------------------------------
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"

# ------------------------------------------------------------------------------
# Install runtime packages only.
#
# 1. "python3" provides the interpreter used by installed dependencies.
# 2. "su-exec" supports user switching in entrypoint workflows.
# 3. "ca-certificates" supports secure outbound HTTPS requests.
# 4. "tzdata" ensures timezone-aware behaviour when required.
# ------------------------------------------------------------------------------
RUN apk add --no-cache \
    python3 \
    su-exec \
    ca-certificates \
    tzdata

# ------------------------------------------------------------------------------
# Set the application working directory for all following instructions.
# ------------------------------------------------------------------------------
WORKDIR /app

# ------------------------------------------------------------------------------
# Copy runtime assets from previous build stages.
# ------------------------------------------------------------------------------
COPY --from=python-deps /opt/venv /opt/venv
COPY --from=microcheck /bin/parallel /bin/parallel

# ------------------------------------------------------------------------------
# Copy worker application source code and operational scripts into the image.
# ------------------------------------------------------------------------------
COPY app /app/app
COPY scripts/entrypoint.sh /app/scripts/entrypoint.sh
COPY scripts/start.sh /app/scripts/start.sh
COPY scripts/healthcheck.sh /app/scripts/healthcheck.sh

# ------------------------------------------------------------------------------
# Mark startup scripts as executable so entrypoint and launcher can run.
# ------------------------------------------------------------------------------
RUN chmod +x /app/scripts/entrypoint.sh /app/scripts/start.sh /bin/parallel

# ------------------------------------------------------------------------------
# Declare persistent mount points used by Compose volume bindings.
#
# 1. "/config" stores authentication and state files.
# 2. "/output" stores backup outputs.
# 3. "/logs" stores heartbeat and runtime log artefacts.
# ------------------------------------------------------------------------------
VOLUME ["/config", "/output", "/logs"]

# ------------------------------------------------------------------------------
# Define container health check policy for worker heartbeat monitoring.
#
# 1. Run every minute.
# 2. Time out after ten seconds.
# 3. Allow thirty seconds start period.
# 4. Mark unhealthy after three consecutive failures.
# ------------------------------------------------------------------------------
HEALTHCHECK --interval=1m --timeout=10s --start-period=30s --retries=3 \
  CMD /app/scripts/healthcheck.sh

# ------------------------------------------------------------------------------
# Start the worker entrypoint script.
#
# N.B.
# Compose "init: true" is expected to provide PID 1 init behaviour.
# ------------------------------------------------------------------------------
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
