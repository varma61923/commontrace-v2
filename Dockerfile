# CommonTrace Hub server image.
#
# Multi-stage so the runtime layer carries no build toolchain: argon2-cffi
# and asyncpg both compile C extensions, which needs gcc at build time but
# not at run time.
#
# Builds only the Hub server (hub/ + the protocol schemas it validates
# against). The `commontrace` CLI is a separate, pip-installable client and
# deliberately does not ship in this image.

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY hub/requirements.txt ./hub/requirements.txt
# Install into a self-contained prefix we can copy wholesale into the
# runtime stage.
RUN pip install --prefix=/install -r hub/requirements.txt


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    HUB_HOST=0.0.0.0 \
    HUB_PORT=8420

COPY --from=builder /install /usr/local

# Run as an unprivileged user. Nothing in the Hub writes to disk at runtime
# (state lives in Postgres), so the app directory stays read-only to it.
RUN useradd --create-home --uid 10001 hub
WORKDIR /app

# hub/ is the server. protocol/schemas/ is a *runtime* dependency, not just
# docs: hub/schema_validation.py loads those JSON files from disk on every
# validate call (single source of truth -- the schema is never copied into
# Python). Omitting them would make the container start and then fail on the
# first contribute_trace.
COPY --chown=hub:hub hub/ /app/hub/
COPY --chown=hub:hub protocol/ /app/protocol/

USER hub
EXPOSE 8420

# Liveness only -- deliberately not /readyz. A container healthcheck that
# fails restarts the container, and restarting every replica because the
# database blipped turns a recoverable outage into a worse one. Point your
# orchestrator's *readiness* probe at /readyz instead (see hub/DEPLOYMENT.md).
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
sys.exit(0) if urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('HUB_PORT','8420')}/healthz\", timeout=2).status == 200 else sys.exit(1)"

# Migrations are NOT run here. Running `alembic upgrade head` on container
# start means N replicas racing the same migration on every deploy. Run it
# once as a separate job/step before rolling out -- see hub/DEPLOYMENT.md.
CMD ["python", "-m", "hub.main"]
