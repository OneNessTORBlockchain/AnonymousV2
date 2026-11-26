# Deploying Anonymous V2 on Render

The first Render deploy failed for three unrelated reasons, and only the first
one is a Render setting. They are listed here because the log lines look alike.

| What the log said | What it actually was |
| --- | --- |
| `model endpoint unreachable - ollama.com answered with an error (HTTP 307: )` | `AV2_OLLAMA_BASE_URL` was the bare host. With `AV2_LLM_PROVIDER=openai` the client always speaks OpenAI-style, so the probe called `GET https://ollama.com/models`, which is the marketing site and answers `307 -> /search` with an empty body. The API base is `https://ollama.com/v1`. |
| `sandbox could not prepare lane terminals: executable not found: docker` | The container has no Docker CLI/daemon. Every target-facing command is `docker exec av2-kali ...` (`backend/app/core/sandbox.py`), so no Docker host means no tool execution. |
| `fleet finished: failed` | Consequence of the first line: the pre-flight probe aborts the run before dispatch. Fix the base URL and the run starts (it still needs a Docker host to do work - see *Sandbox*). |

## What the repository now ships

| File | Purpose |
| --- | --- |
| `Dockerfile` | `python:3.12-slim` + `requirements.txt`, copies `backend/` and `frontend/`, runs `uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port $PORT`. |
| `requirements.txt` | The five pinned packages the app imports (`fastapi`, `uvicorn[standard]`, `httpx`, `pydantic`, `pydantic-settings`). |
| `render.yaml` | Blueprint for the Docker web service, health check and env vars. |
| `.dockerignore` | Keeps `.env`, `storage/`, `.git` and the Windows helper scripts out of the image. |
| `/api/livez` (new route) | Millisecond liveness probe. `/api/health` is unchanged and still does the real model + sandbox diagnosis. |

## Deploy (Blueprint, recommended)

1. Push these files to the repository Render watches. This checkout's `origin` is
   `git@github.com:OneNessTORBlockchain/AnonymousV2.git`; the requested target was
   `github.com/crocrodil666/AnonymousV2`. The service must point at the copy that
   actually contains the `Dockerfile`.
2. Render dashboard -> **New +** -> **Blueprint** -> select the repo. Render reads
   `render.yaml` and asks for `AV2_OLLAMA_API_KEY` before the first build.
3. Wait for the build (`pip install` + two `COPY` layers) and the first
   `/api/livez` 200. Then open the service URL: the bundled UI is served from `/`
   by the same process, so there is no separate static site.

Manual equivalent: **New +** -> **Web Service** -> **Docker** -> Dockerfile path
`./Dockerfile`, health check path `/api/livez`, no start command (the image's
`CMD` already binds `$PORT`).

### Repository access

`render.yaml` needs no keys: connect GitHub through the Render GitHub App.
If the repo stays private *and* you prefer a deploy key, add the **public** key
(`ssh-ed25519 AAAA...`) under GitHub -> Settings -> **Deploy keys** with *Allow
write access* unchecked. That is exactly what such a key is for; the private half
never leaves the machine that generated it, and the e-mail address is only the git
commit identity, not a Render setting.

## Environment

| Variable | Value on Render | Why |
| --- | --- | --- |
| `AV2_LLM_PROVIDER` | `openai` | Ollama Cloud speaks the OpenAI protocol. `ollama` would select the native `/api/chat` path. |
| `AV2_OLLAMA_BASE_URL` | `https://ollama.com/v1` | The versioned API base. `https://ollama.com` is a web page (`307 -> /search`). |
| `AV2_OLLAMA_API_KEY` | *secret, set in the dashboard* | Never commit it; `.env` is git-ignored and excluded from the image. |
| `AV2_PLANNER_MODEL`, `AV2_WORKER_MODEL`, `AV2_FLEET_MODELS` | `deepseek-v4.1-flash:cloud` | The `:cloud` suffix is part of the routable id. |
| `AV2_PLANNER_NUM_CTX`, `AV2_WORKER_NUM_CTX` | `131072` | Must stay inside what the provider allows (`docs/CONTEXT-WINDOW.md`). |
| `AV2_FLEET_GROUPS` | `1` | One squad = five lanes (recon, web, exploit, coder, reporter). |
| `AV2_SANDBOX_EGRESS` | `direct` | There is no Tor sidecar inside a Render container; `tor` would make every exec wait on a dead proxy. |
| `PORT` | *injected by Render* | The `CMD` uses `${PORT:-8420}`; never set it by hand. |

`.env` is **not** in the image (see `.dockerignore`), so anything the app needs on
Render must exist as a Render environment variable.

## Verify

```powershell
curl.exe -s https://<service>.onrender.com/api/livez    # {"ok":true}
curl.exe -s https://<service>.onrender.com/api/health   # status + detail
```

Expected on Render: `status` is `degraded` (not an error), `ollama` is `true`,
`ollama_detail` is `https://ollama.com/v1 ok`, `sandbox` is `false` with
`sandbox_detail` containing `executable not found: docker`. `degraded` is honest:
the control plane is up, the tool plane has nowhere to run.


## Sandbox (read this before expecting findings)

Render containers cannot run Docker-in-Docker, so `av2-kali` cannot live in the
same service. A render-hosted instance is therefore a **UI + API + model**
deployment: fleet lanes plan and reason, but every tool call fails with
`executable not found: docker`.

Three ways forward, in increasing cost:

1. **Control plane on Render, tools on your machine** - run the fleet *here*,
   locally, where `docker/docker-compose.yml` already provides `av2-kali` + Tor:
   `docker compose -f docker/docker-compose.yml up -d` then `.\start.ps1`.
2. **Remote engine via `DOCKER_HOST`** - build the image with the Docker CLI and
   point it at a Docker host you control (a VPS running `dockerd` behind TLS, or
   an SSH-reachable host). Add to the `Dockerfile`:
   ```dockerfile
   ARG DOCKER_CLI_VERSION=27.5.1
   RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" \
       | tar -xz --strip-components=1 -C /usr/local/bin docker/docker \
    && apt-get purge -y curl && rm -rf /var/lib/apt/lists/*
   ```
   then set `DOCKER_HOST`, `DOCKER_TLS_VERIFY=1` and `DOCKER_CERT_PATH` on the
   service. The Kali + Tor stack still has to run on that host
   (`docker/docker-compose.yml`), and `AV2_KALI_WORKDIR` must match its `/work`.
3. **Full stack on a Docker host** - `docker compose -f docker/docker-compose.yml
   up -d`, then run the API next to it (`.\start.ps1` on Windows, the same
   `uvicorn` line on Linux). This is the only option where egress identity, scope
   guard and the evidence chain behave exactly as documented in `docs/EGRESS.md`.

## Persistence

`storage/` (audit log, findings, evidence chain, scope, sessions, the SQLite
learning board) lives on the container filesystem by default, so **every deploy
wipes it**. To keep it, attach a Render disk and mount it at `/app/storage`
(uncomment the `disk:` block in `render.yaml`; disks require a paid instance
type). The directory is created by the image and by `Settings.ensure_dirs()`.

## Local parity

```powershell
docker build -t anonymous-v2 .
docker run --rm -p 8420:8420 --env-file .env anonymous-v2
curl.exe -s http://127.0.0.1:8420/api/livez
```

The image and `start.ps1` run the same command line, so a bug reproduced in one
reproduces in the other; the only difference is the absence of Docker inside.
