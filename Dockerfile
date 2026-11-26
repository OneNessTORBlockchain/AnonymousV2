# syntax=docker/dockerfile:1
# Anonymous V2 - FastAPI backend + bundled UI in one image.
#
#   docker build -t anonymous-v2 .
#   docker run --rm -p 8420:8420 --env-file .env anonymous-v2
#
# This is the control plane (API, fleet orchestrator, static UI, model calls).
# It is deliberately *not* the sandbox: every target-facing command is
# `docker exec av2-kali ...` (see backend/app/core/sandbox.py), so a Docker
# socket or a remote DOCKER_HOST is still needed before lanes can run tools.
# docs/RENDER.md covers both the "UI/API only" deployment and the remote-engine
# variant.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so a code-only change reuses this layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# config.py computes ROOT as parents[2] of backend/app/config.py, i.e. /app:
# the UI is served from /app/frontend, runtime state lives in /app/storage.
COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY .env.example ./

RUN mkdir -p /app/storage

# Render injects PORT; the 8420 default keeps `docker run -p 8420:8420`
# identical to start.ps1.  --proxy-headers lets request.url see the public
# https scheme behind Render's TLS terminator.
ENV PORT=8420
EXPOSE 8420

# /api/livez is the millisecond liveness route; /api/health deliberately probes
# the model endpoint and the Kali container, which must not decide restarts.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,sys,urllib.request; p=os.environ.get('PORT','8420'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+p+'/api/livez', timeout=4).status == 200 else 1)"

CMD ["sh", "-c", "exec uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port ${PORT:-8420} --proxy-headers --forwarded-allow-ips='*'"]
