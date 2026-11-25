# Anonymous V2

<img src="browser.png" alt="Anonymous V2" width="96" align="right" />

A free-agency, multi-agent security workbench: one objective in, a fleet of
autonomous agents out, every command executed inside a Kali sandbox and every
result shared on one board so the agents build on each other instead of
repeating each other.

Built and maintained by **OneNess Technologies** — <https://www.onenesstech.ca>
Released as open source under a **non-commercial licence** (see [LICENSE](LICENSE)).

---

## Fleet: 1–5 squads × 5 roles

Agents are organised in **squads of five**, one agent per role:

| role | what it does |
| --- | --- |
| `recon` | discovery, enumeration, attack-surface mapping |
| `web` | HTTP/API/browser work, bundles, endpoints, auth flows |
| `exploit` | vulnerability validation, payloads, privilege paths |
| `coder` | tooling, scripts, parsers, exploit/dev code |
| `reporter` | evidence, findings, reproduction steps, the final report |

Pick **1, 2, 3, 4 or 5 squads** and the run launches **5, 10, 15, 20 or 25
agents** at the same time. Every agent gets its own terminal, its own workdir and
its own browser profile; models are handed out round-robin so a single squad is
already spread across the configured fleet models.

`AV2_FLEET_GROUPS` sets the default (1–5). The composer's **squads** selector,
the Swarm panel's **groups** selector, `POST /api/chat` → `groups`, and
`POST /api/swarm` → `groups` all take the same number.

## Providers

AV2 is provider-agnostic. The default deployment is **Ollama Cloud** over the
OpenAI-compatible protocol:

```env
AV2_LLM_PROVIDER=openai
AV2_OLLAMA_BASE_URL=https://ollama.com/v1
AV2_OLLAMA_API_KEY=...
AV2_FLEET_MODELS=deepseek-v4.1-flash:cloud
```

- `openai`, `venice`, `openai-compatible` → `POST {base}/chat/completions`
- `ollama`, `ollama-local`, `ollama-cloud` → native `POST {base}/api/chat`
- any other label → decided by a `/v1` suffix on the base URL

Ollama Cloud **must** use `openai`: its native path serves model metadata, not
the cloud chat endpoint. The `:cloud` suffix is part of the routable model id.

The **Provider** panel in the sidebar changes endpoint, model and key at
runtime — no restart, no file editing — and persists the values to `.env`:

```
GET  /api/provider   → current endpoint, model, key hint, reachability
POST /api/provider   → {provider, base_url, model, api_key, fleet_models}
```

Empty fields keep their current value; the key is written to `.env` but never
returned by the API (only a masked hint such as `sk-a…9f3c`).

## Rounds: up to 50, or until done

- `rounds` accepts **1–50**.
- **until complete** (`until_complete`) keeps re-planning until the completion
  gate approves, with `AV2_FLEET_MAX_ROUNDS` (default 50) as the cap.
- **until done** (`unlimited`) removes the round cap entirely
  (`AV2_FLEET_UNLIMITED_ROUNDS`, default 10 000): only the completion gate, the
  token window and the wall clock can end the run.

Between rounds a strategist reads the shared board and re-tasks every lane, so
round *n+1* works the gaps round *n* proved instead of starting over.

## Shared results and deduplication

Every run owns one **dedup ledger** and one **shared board**:

- the ledger normalises each command/tool call/HTTP request and refuses a claim
  that a lane has already taken, so no agent runs the same scan twice;
- the board is append-only: each lane posts what it ran, what came back and what
  it now believes, and the digest is injected into every lane prompt and into the
  strategist's plan;
- findings, enforcement pushes, terminations and the completion verdict are all
  visible in the Swarm panel and under `GET /api/swarm/{run_id}`.

## Quick start (Windows)

```powershell
git clone git@github.com:OneNessTORBlockchain/AnonymousV2.git
cd AnonymousV2
.\install.ps1 -Docker      # Python packages, .env check, Docker + Tor sidecar
.\start.ps1 -Docker        # API + UI on http://127.0.0.1:8420
```

`install.ps1` detects Python 3.10+, installs `fastapi`, `uvicorn[standard]`,
`httpx`, `pydantic-settings` (plus `pytest`, `pytest-asyncio`,
`pytest-xprocess`), creates `.env` from `.env.example` when missing, checks the
`oneness-kali-agent:local` image and can install Docker Desktop through winget
(`-InstallDocker`). `install.cmd` is the double-click wrapper.

The sandbox is the only place target-facing work happens:

```powershell
docker compose -f docker/docker-compose.yml up -d
```

That brings up `av2-kali` (the worker) and `av2-tor` (SOCKS5 sidecar). Sandbox
egress defaults to Tor, so a target sees an exit node rather than the operator's
address; `AV2_SANDBOX_EGRESS=direct` opts out.

Tests:

```powershell
cd backend
python -m pytest -q
```


## Configuration

Everything is overridable from the environment with the `AV2_` prefix, or from
`.env`. The settings that matter most:

| variable | default | meaning |
| --- | --- | --- |
| `AV2_LLM_PROVIDER` | `venice` | protocol label (`openai` for Ollama Cloud) |
| `AV2_OLLAMA_BASE_URL` | `https://api.venice.ai/api/v1` | endpoint, `/v1` for OpenAI-style |
| `AV2_OLLAMA_API_KEY` | empty | bearer key (never committed) |
| `AV2_PLANNER_MODEL` / `AV2_WORKER_MODEL` | `glm-5-3-flash-uncensored` | model ids |
| `AV2_FLEET_MODELS` | GLM + DeepSeek | comma-separated, spread round-robin |
| `AV2_FLEET_GROUPS` | `1` | squads of five: 1–5 → 5–25 agents |
| `AV2_FLEET_ROUNDS` | `2` | rounds for a normal turn |
| `AV2_FLEET_MAX_ROUNDS` | `50` | cap for "until complete" |
| `AV2_FLEET_UNLIMITED_ROUNDS` | `10000` | ceiling for "until done" |
| `AV2_PLANNER_NUM_CTX` / `AV2_WORKER_NUM_CTX` | `131072` | prompt budget per call |
| `AV2_FLEET_DEADLINE_S` | `300` | wall clock for one chat turn |
| `AV2_FLEET_UNTIL_COMPLETE_DEADLINE_S` | `3600` | wall clock for until-done |
| `AV2_SANDBOX_EGRESS` | `tor` | `tor` or `direct` |
| `AV2_KALI_IMAGE` / `AV2_KALI_CONTAINER` | `oneness-kali-agent:local` / `av2-kali` | worker |

## API in one screen

| method | path | purpose |
| --- | --- | --- |
| `GET` | `/api/health` | provider, sandbox, models, context window, fleet size |
| `GET` | `/api/fleet` | roster preview: groups × 5, models, rounds, deadlines |
| `GET/POST` | `/api/provider` | read / apply endpoint, model, key (persisted to `.env`) |
| `POST` | `/api/chat` | one operator turn (streams SSE; fleet turn by default) |
| `POST` | `/api/swarm` | start a swarm (`fleet`, `groups`, `rounds`, `unlimited`) |
| `GET` | `/api/swarm/{id}` | live status: lanes, ledger, board, completion |
| `POST` | `/api/swarm/{id}/message` | operator note onto the shared board |
| `POST` | `/api/swarm/{id}/stop` | stop a run |
| `GET` | `/browser.png` | brand mark (favicon + sidebar logo) |

Interactive documentation: <http://127.0.0.1:8420/docs>.

## Layout

```
backend/app/            FastAPI app, settings, schemas
backend/app/core/       orchestrator, swarm, tools, sandbox, memory, skills
backend/tests/          pytest suite
frontend/               operator console (vanilla JS, no build step)
docker/                 docker-compose for av2-kali + av2-tor
tools/                  smoke tests and connection helpers
docs/                   architecture, status and context-window notes
browser.png             brand mark served at /browser.png
install.ps1 / .cmd      dependency installer
start.ps1               start/restart the API + UI
```

## Security, legality and scope

AV2 executes offensive tooling. Use it **only** against systems you own or are
explicitly authorised to test. Authorization is attested by the operator; the
sandbox runs target-facing commands inside `av2-kali` behind Tor, with no host
bind mounts and no Docker socket. Keep `.env` out of version control — the
installer and `.gitignore` already do.

## Licence

**OneNess Technologies Non-Commercial Source-Available Licence, Version 1.0.**

You may use, copy, modify and share this software for **non-commercial**
purposes, provided the copyright notice and this licence travel with it and
modifications are marked. Commercial use requires a separate written licence
from OneNess Technologies.

- Project home: <https://www.onenesstech.ca>
- Full terms: [LICENSE](LICENSE)
- Copyright © 2026 OneNess Technologies. All rights reserved.

Anonymous V2 is provided **as is**, without warranty of any kind.
