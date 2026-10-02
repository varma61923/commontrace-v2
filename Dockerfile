
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY hub/requirements.txt hub/requirements-lock.txt ./hub/
RUN pip install --prefix=/install -r hub/requirements.txt -c hub/requirements-lock.txt

FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    HUB_HOST=0.0.0.0 \
    HUB_PORT=8420

COPY --from=builder /install /usr/local

RUN useradd --create-home --uid 10001 hub
WORKDIR /app

COPY hub/ /app/hub/
COPY protocol/ /app/protocol/
COPY commontrace/ /app/commontrace/

USER hub
EXPOSE 8420

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
sys.exit(0) if urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('HUB_PORT','8420')}/healthz\", timeout=2).status == 200 else sys.exit(1)"

CMD ["python", "-m", "hub.main"]
