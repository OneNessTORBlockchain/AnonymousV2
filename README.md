# Anonymous V2

<img src="browser.png" alt="Anonymous V2" width="96" align="right" />

A free-agency, multi-agent security workbench: one objective in, a fleet of
autonomous agents out, every command executed inside a Kali sandbox and every
result shared on one board so the agents build on each other instead of
repeating each other.

Built and maintained by **OneNess Technologies** — <https://www.onenesstech.ca>
Released as open source under a **non-commercial licence** (see [LICENSE](LICENSE)).

---

## What AnonymousV2 is — and what it is not

AnonymousV2 is an **orchestration and execution platform**, not an AI model. The
models are interchangeable: `AV2_LLM_PROVIDER`, `AV2_OLLAMA_BASE_URL`,
`AV2_PLANNER_MODEL`, `AV2_WORKER_MODEL` and `AV2_FLEET_MODELS` decide which
provider and model ids the agents run on, and the **Provider** panel changes
them at runtime. Every other part of the system — planning, specialist roles,
shared board, deduplication, sandbox execution, evidence, durable learning —
stays the same when the model changes.

A general-purpose model on its own is not an engagement team. It has no
assignment discipline, no shared memory between calls, no deduplication of
work, no persistent working directory and no audit trail. AnonymousV2 supplies
those things around the model.

Model quality still matters. Output depends on:

- the model's reasoning capability;
- its tool-calling compatibility (native function calling vs. text protocol);
- its context-window size and how much of the board/transcript fits;
- the system instructions and operator configuration;
- how well assignments are coordinated between agents;
- task planning and re-planning quality;
- evidence management and deduplication;
- execution reliability of the sandbox and the provider endpoint.

No claim is made that every model has identical capabilities. The platform's
job is to make a *capable* model materially more useful than it would be alone,
and to let the operator swap models without rebuilding the workflow.

## The orchestration layer

The capabilities below are implemented in the current codebase; module names
are given so the behaviour can be checked rather than taken on trust.

### 1. Intelligent task planning

- The strategist (`SwarmManager.plan_round` in `backend/app/core/swarm.py`)
  receives the objective, the shared board, ledger counters, every lane's state,
  findings, unresolved lanes and the operator's newest directive, and returns
  one JSON plan: `analysis`, `strategy`, `stop`, and a list of
  `{lane, mission, success, hypothesis, avoid}` assignments.
- Assignments are distributed across the squad's specialists, not repeated to
  every agent. Round 1 uses deterministic role shards (`_FALLBACK_MISSIONS`) so
  work starts immediately even when the planner call is slow or unavailable.
- `_plan_from` guarantees **one mission per lane** and adds a shard boundary when
  two lanes would otherwise receive identical text.
- Later rounds see what previous rounds proved, so the plan adapts to evidence
  (new endpoints, refuted hypotheses, failed tools) instead of restarting.
- Lanes without a recorded finding stay in `run.unresolved` and are re-tasked
  with a different technique instead of stopping silently.

### 2. Specialist agent coordination

Each squad is exactly five specialists, one per role:

| role | dedicated responsibility |
| --- | --- |
| `recon` | reconnaissance, discovery and attack-surface mapping |
| `web` | web/API assessment, authentication and access-control testing |
| `exploit` | controlled vulnerability validation and blast-radius proof |
| `coder` | tools, scripts, parsers and validation utilities |
| `reporter` | evidence organisation, findings and the final report |

Role missions live in `AGENT_META` (`backend/app/core/agents.py`) and in the
fallback assignment tables in `swarm.py`. Agents run independently, in their own
terminal and workdir, but they read the same board and build on each other's
discoveries. The strategist's job is explicitly to merge hypotheses, drop dead
ends and split the remaining surface — not to run five copies of one prompt.

The cross-check/review function that used to be the sixth `analyst` role is now
a **coordination duty**: the strategist audits the board each round, and the
completion gate (`verify_completion`) independently checks the recorded findings
before a run may end. There is no sixth active specialist.

### 3. Shared state and blackboard

- Every run owns an append-only board (`SwarmMemory`, `board.jsonl`) recording
  plans, lane results, tool calls, findings, operator notes, knowledge reports
  and the synthesis.
- `SwarmMemory.digest()` turns the board into a compact, bounded block injected
  into **every lane prompt** and into the strategist's prompt, so findings remain
  visible between planning rounds.
- Agents can read the board (`swarm_status`) and post to it; the operator can
  inject a note with `POST /api/swarm/{id}/message`.
- The board is per-run. Long-term knowledge lives in the Learning Board
  (`backend/app/core/learning.py`), which survives runs and restarts.

### 4. Evidence deduplication

- `Ledger.claim()` normalises each command, tool invocation and HTTP request into
  a stable key. A lane repeating work another lane already claimed is refused and
  the attempt is counted (`duplicates_suppressed`) instead of being executed.
- Duplicate protection is a ledger, not a prompt instruction: even a model that
  ignores the board cannot execute the same claim twice.
- Findings are sealed in a hash-chained evidence store
  (`backend/app/core/evidence.py`) with the six mandatory elements; claims
  missing an element stay `unconfirmed`.
- `_plan_from` also removes duplicate mission text inside one round.

### 5. Dynamic replanning

- The strategist runs at every round barrier and compares the board with the
  objective before issuing new assignments.
- New discoveries produce additional assignments; refuted hypotheses and failed
  tools produce alternative strategies (`avoid` lists, different missions).
- An operator directive sent while a run is live is injected between steps and
  supersedes the current mission rather than being queued behind it.
- Enforcement pushes a lane back to work when it stops without evidence; after
  the configured number of pushes the lane is terminated and its slice is
  re-assigned.
- The completion gate can refuse a `stop`, so a run ends on a proven answer, the
  round cap, the token ceiling or the wall clock — never on optimistic prose.



### 6. Persistent working environments

- Each agent gets a dedicated workdir: `<kali_workdir>/fleet/<run_id>/agent-N`.
- Each agent gets its own browser profile (`.browser`) so cookies and sessions do
  not collide.
- Persistent terminals: one shell per agent (`agent-N`) keeps `cwd` and
  environment between steps and rounds.
- Uploaded files and folders of any format are published to
  `/work/uploads/<batch>`; skills are installed to `/work/skills/<slug>` and can
  be run by any model with `skill_run`.
- Everything executes inside the Kali container in `docker/docker-compose.yml`,
  with Tor egress by default.

### 7. Autonomous execution and reporting

- Agents execute authorised assignments with the shared toolbox: sandbox
  commands, file operations, HTTP/browser probes, swarm status, delegation,
  findings, transaction verification, skills and the Learning Board.
- Results are validated (evidence sealing, completion gate) and reported as
  findings plus a strategist synthesis. The operator sees lanes, ledger,
  findings, enforcement, terminations and the final report in the Swarm panel and
  under `GET /api/swarm/{run_id}`.

## Why AnonymousV2 Is Different: The Orchestration Advantage

Most "multi-agent" setups are parallel prompting: the same question is sent to
several agents and the answers are concatenated. That scales token spend, not
capability. AnonymousV2 combines capabilities that only work together:

| capability | implementation | what it changes |
| --- | --- | --- |
| Autonomous task decomposition | strategist plan per round, `_plan_from`, role fallbacks | work is split into assignments instead of one giant prompt |
| Specialist coordination | five role missions, delegation, board | each agent has a discipline and sees the others' results |
| Shared working memory | `SwarmMemory` board + digest in every prompt | discoveries are reused inside the run |
| Evidence deduplication | `Ledger.claim()` + mission dedup | the fleet stops paying twice for the same scan |
| Iterative planning | round barrier, new plan from the board | later rounds target the gaps earlier rounds exposed |
| Persistent learning | Learning Board (SQLite, versioned, reviewable) | validated experience carries across runs and models |
| Dynamic strategy adaptation | unresolved lanes, `avoid` lists, directives, enforcement | failed approaches are replaced rather than repeated |
| Model-independent execution | provider abstraction + OpenAI/Ollama protocols | the operator can swap models without losing the workflow |

The combination is the product. Adding more identical agents mostly adds cost;
picking a particular LLM only moves the ceiling. Assignment strategy, shared
state, evidence discipline, replanning and accumulated validated knowledge are
what convert model capability into completed work — and they keep their value
when the model changes.

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

The five roles are defined once, in `FLEET_GROUP_ROLES`
(`backend/app/config.py`), and every roster, schema and prompt derives from it.
The older `agents` field is still accepted for compatibility but is clamped to
**1–5** per model (one squad's roles); it can no longer create a sixth
specialist. See [docs/SWARM.md](docs/SWARM.md) for the full contract.

## AnonymousV2 vs. ARTEX — Reported Experimental Comparison

> **Status: developer-reported, not an independently reproduced benchmark.**
> The figures below were reported by the AnonymousV2 developer. They come from
> one experiment and have not been independently verified, peer reviewed,
> repeated on neutral hardware, or published with a full transcript. Treat them
> as an anecdote that motivates a benchmark — not as a proven ranking.

Both systems were run, according to the operator, with:

- the same **DeepSeek V4.1 Flash Cloud** model;
- the same stated testing objective;
- the same target environment (operator's statement);
- a difficult security-research objective involving discovery of exposed
  hot-wallet private keys associated with funded wallets.

| Measurement | ARTEX | AnonymousV2 |
|---|---:|---:|
| Execution time | 12 hours | 30 minutes |
| Reported discoveries | 0 | 2 |
| Reported outcome | No discoveries | Two discovered keys associated with funded wallets |

### What this experiment suggests

It is interesting because the underlying model was the same. If the difference
is real, it points at the parts of the system around the model: how the task was
decomposed, how assignments were distributed, how agents coordinated, what they
shared with each other, how duplicate work was suppressed, and how execution was
driven to a result. That is the orchestration thesis of this project: agent
orchestration, assignment strategy, coordination, shared knowledge and execution
efficiency can significantly influence the usefulness of the same model.

It does **not** establish that AnonymousV2 has conclusively defeated or
outperformed ARTEX. One experiment, one objective, one model and one environment
is not enough. Differences in scope, available tooling, network conditions,
prompt configuration, operator intervention and luck can all change the result.
No discovered keys, addresses, credentials, tokens or wallet details are
published here or anywhere else in this repository.

### How future benchmarks should measure both systems

A credible comparison needs a pre-registered protocol and the same measurements
for every system:

- **time to first verified finding** (not time to first output);
- **number of independently confirmed findings** (verified by a second party);
- **false-positive rate** (reported findings that fail verification);
- **task completion rate** (objectives fully met within the budget);
- **execution success rate** (agent steps that actually ran);
- **duplicate assignment rate** (work repeated across agents);
- **tool utilisation** (useful tool calls per task, and wasted calls);
- **token consumption** (prompt + completion, per objective);
- **cost per verified finding** (provider pricing applied to measured tokens);
- **total execution time** (wall clock, including retries and waiting).

Protocol rules:

1. Use **only owned or explicitly authorised** test environments.
2. Pre-register the objective, scope, model id, provider, tooling, hardware,
   time budget and stopping criteria before either system runs.
3. Keep the full transcripts and evidence for both systems.
4. Independently verify every claimed finding; publish the verification method.
5. **Never publish** private keys, credentials, access tokens, session data or
   wallet/seed information. Describe the *class* of issue and the remediation.
6. Report failures, costs and limitations alongside any win.
7. Repeat across several objectives and several models before drawing a
   conclusion.


## Learning Board: shared institutional memory

Every agent can consult and contribute to a durable Learning Board that survives
runs, restarts and model swaps. This is the "institutional memory" half of the
orchestration advantage: the fleet does not start from zero every time.

### Structured entries

An entry is not a free-text note. It carries:

`id`, `title`, `description/body`, `category`, `tags`, `roles`, `tools`,
`preconditions`, `procedure`, `expected`, `observed`, `common_errors`,
`troubleshooting`, `status`, `result`, `confidence`, `source agent`, `created_at`,
`updated_at`, `uses`, `successes`, `failures`, `version`, `reviewed_by`,
`reviewed_at`, `flags` and the authorised engagement `scope`.

| category | purpose |
| --- | --- |
| `reconnaissance` | discovery techniques: DNS, ports, services, directories |
| `web-security` | web and API assessment: auth, injection, access control |
| `vulnerability-validation` | safe validation, controlled proofs, blast radius |
| `tool-usage` | tool configuration, flags, installation, invocation |
| `troubleshooting` | errors, timeouts and the fix that resolved them |
| `environment` | sandbox, container, proxy and execution configuration |
| `planning` | task decomposition and prioritisation |
| `agent-coordination` | collaboration, hand-off, duplicate avoidance |
| `reporting` | evidence quality, findings, report writing |
| `general-lessons` | reusable knowledge that fits no narrower category |

Validation is explicit: `proposed` (an agent suggested it), `observed` (seen in a
real transcript), `validated` (confirmed by the operator) and `deprecated`
(history only, never retrieved as advice). An agent can never write directly as
`validated`; a review call requires a named reviewer. `result` is
`success`/`failure`/`unknown`, and a failed method is still retrieved — as a
**warning** so it is not repeated.

### Retrieval is role-aware and bounded

Before each assignment the runner infers the assignment's categories and asks
the board for the few most relevant entries for that specialist and those tools:

1. agent receives an assignment;
2. the system infers category and tools;
3. the board is searched (keyword + category + tag + role + tool + status +
   outcome filters);
4. the best entries are returned (a handful, not the whole board);
5. the agent sees them as **reference data** in its prompt;
6. the agent uses or evaluates them and can record a new lesson;
7. `memory_feedback` records whether the lesson helped.

Recon prioritises reconnaissance lessons, web prioritises web/API knowledge,
explore prioritises safe validation, coder prioritises tooling and
troubleshooting, reporter prioritises evidence and reporting. Retrieval is a
local relevance model (token weights, synonym expansion, confidence and usage
bonuses) — deliberately not an external vector database.

Quality controls: duplicate and near-duplicate detection (entries are merged and
versioned), confidence scoring, source attribution, version history, mandatory
reviewer for validation, `needs-review` after repeated failures, contradiction
flags, and instruction-like content that is stored for review but never rendered
into a prompt. Secrets are redacted before storage: the board never keeps a
private key, password, API key, token, seed phrase or third-party access code.

Full details, schema and API examples: **[docs/LEARNING-BOARD.md](docs/LEARNING-BOARD.md)**.

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
| `AV2_SWARM_KNOWLEDGE_REPORT` | `true` | report lessons to the Learning Board after each task |
| `AV2_MEMORY_DIGEST_LIMIT` | `12` | how many lessons an agent digest may carry |

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
| `GET/POST` | `/api/learning` | search / create Learning Board entries |
| `GET` | `/api/learning/categories` | categories, roles, statuses, purposes |
| `GET` | `/api/learning/stats` | totals, pending reviews, reuse and flag counts |
| `GET` | `/api/learning/recommend` | role-aware retrieval for one assignment |
| `GET/PUT/DELETE` | `/api/learning/{id}` | one entry with related lessons, versions, usage |
| `POST` | `/api/learning/{id}/review` | validate / observe / deprecate (reviewer required) |
| `POST` | `/api/learning/{id}/usage` | record reference / success / failure |
| `GET` | `/api/learning/{id}/related` | related lessons |
| `GET` | `/api/learning/{id}/versions` | version history |
| `GET/POST/PUT/DELETE` | `/api/memory` | legacy alias for the same board |
| `GET` | `/browser.png` | brand mark (favicon + sidebar logo) |

Interactive documentation: <http://127.0.0.1:8420/docs>.

## Layout

```
backend/app/            FastAPI app, settings, schemas
backend/app/core/       orchestrator, swarm, tools, sandbox, learning, skills
backend/tests/          pytest suite (context window, Learning Board, squad contract)
frontend/               operator console (vanilla JS, no build step)
docker/                 docker-compose for av2-kali + av2-tor
tools/                  smoke tests and connection helpers
docs/                   architecture, status, Learning Board and context-window notes
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

The Learning Board stores sanitised knowledge, not secrets: keys, passwords,
tokens, seed phrases and third-party access codes are redacted before storage,
and entries are reference data that can never override system instructions,
scope, tool permissions or sandbox rules. Benchmark results in this repository
are developer-reported unless explicitly marked otherwise, and must never
include live credentials or wallet data.

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
