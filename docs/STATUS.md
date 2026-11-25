# Status — 2026-11-25

## Current state

| area | state |
| --- | --- |
| fleet | **squads of five** (`recon, web, exploit, coder, reporter`), 1–5 squads = 5–25 agents; single source of truth `FLEET_GROUP_ROLES` in `backend/app/config.py` |
| retired | the sixth `analyst` lane; cross-checking is now the strategist's board audit plus the completion gate |
| rounds | 1–50, "until complete" (cap `AV2_FLEET_MAX_ROUNDS=50`) and "until done" (ceiling `AV2_FLEET_UNLIMITED_ROUNDS=10000`) |
| provider | provider-agnostic; the current `.env` points at Ollama Cloud (`provider=openai`, `deepseek-v4.1-flash:cloud`) with the key set. Venice/OpenAI-compatible/local Ollama paths remain supported |
| Learning Board | structured, searchable, versioned SQLite board (`backend/app/core/learning.py`) with `/api/learning/...`, role-aware retrieval before every lane task and a **Learning Board** panel in the UI |
| tools | `memory_search`, `memory_write` (structured) and `memory_feedback`; `skill_create/run/list`; all five specialists share the toolbox |
| tests | `cd backend; python -m pytest -q` → **48 passed** (context window, squad contract/roster/API limits, Learning Board persistence/dedup/review/secrets/migration) |
| frontend | `node --check frontend/app.js` clean; `/` serves the Learning Board panel, `/browser.png` is the brand mark |
| sandbox | requires Docker (`av2-kali` + `av2-tor`); when Docker is absent the API reports `status=degraded` and no lane can execute commands |

Verification for this release:

```
python -m pytest -q                         48 passed
GET  /api/learning/categories               10 categories, 5 roles, 4 statuses
GET  /api/learning/stats                    total / validated / pending / flags
POST /api/learning                          create (proposed), merge duplicates
POST /api/learning/1/review                 reviewer required; 422 without one
GET  /api/learning/recommend?role=recon     role-aware retrieval + rendered digest
GET  /api/learning/{id}                     entry + related + versions + usage
POST /api/learning/{id}/usage               success/failure/reference signal
GET  /api/swarm                             defaults: groups, group_size=5, max_groups=5
```

Details of the architecture: [README](../README.md), [SWARM](SWARM.md),
[LEARNING-BOARD](LEARNING-BOARD.md).

## Running now (2026-11-20 snapshot)

| item            | value |
|-----------------|-------|
| API + UI        | http://127.0.0.1:8420/ (FastAPI serves `/api/*` and `frontend/`) |
| start / restart | `.\start.ps1` (add `-NoBrowser` to skip the browser, `-Port` to move) |
| sandbox preflight | `.\start.ps1 -Docker` (starts Docker Desktop, then `av2-kali` + the `av2-tor` egress proxy) |
| logs            | `storage/logs/server.out.log`, `storage/logs/server.err.log` |
| sandbox egress  | Tor exit node via `av2-tor`; `GET /api/egress` shows sandbox IP vs host IP (`docs/EGRESS.md`) |
| process         | hidden `python -m uvicorn app.main:app --app-dir backend` |

`/api/health` at last start:

```
status=ok  version=2.1.0  provider=venice  ollama=ok (https://api.venice.ai/api/v1 ok)
planner_model=glm-5-3-flash-uncensored  worker_model=glm-5-3-flash-uncensored
fleet=true  fleet_auto=true  fleet_size=6  agent_terminals=agent-1..agent-6
fleet_models=glm-5-3-flash-uncensored + deepseek-v4-1-flash-1017-uncensored
context_window=131072  context_reserve=512
planner_tools=true  godmode=scoped  godmode_skill=true  transaction_gate=true  scope_count=2
sandbox=true  docker docker 29.8.0; container av2-kali=running  egress=tor (192.42.116.100)
```

Whole stack is green: the Venice endpoint, native tool calling, the GODMODE skill,
the Kali sandbox and six live per-agent terminals.  (`ollama=` is the field name
of the model check and now reports the cloud gateway; `egress=tor` is a live Tor
exit node in the Netherlands, distinct from the host IP.)

One thing is not green, and it is billing, not wiring: the Venice key is valid but
the account has no balance, so every model turn answers `HTTP 402 Insufficient USD
or Diem balance` until it is topped up.  See *The Render GLM brain is gone* below -
`tools/connect_venice.ps1 -Verify` prints exactly that, per model, in one line.

## Provider: Venice AI cloud

* Endpoint `https://api.venice.ai/api/v1` (OpenAI chat-completions protocol),
  two models: `glm-5-3-flash-uncensored` (`CLOUD_MODEL`, planner + agents 1-3) and
  `deepseek-v4-1-flash-1017-uncensored` (`DEEPSEEK_MODEL`, agents 4-6) in
  `backend/app/config.py`.
* `OllamaClient` keeps its name but is protocol-aware: `Settings.openai_style`
  true sends `/models` + `/chat/completions`, false keeps `/api/tags` +
  `/api/chat`.  The internal transcript stays Ollama-flavoured; the client
  translates assistant `tool_calls` and `role="tool"` messages to OpenAI
  `tool_call_id` form and merges streamed tool-call fragments.
* `OLLAMA_HOST` / `OLLAMA_API_KEY` are deliberately **not** read any more: this
  host sets both machine-wide for the local daemon (`127.0.0.1:8080`) and they
  shadowed the cloud endpoint.  Only `AV2_*` names apply.  To run fully local:
  `AV2_LLM_PROVIDER=ollama`, `AV2_OLLAMA_BASE_URL=http://127.0.0.1:8080`,
  `AV2_OLLAMA_API_KEY=<ollama token>`.
* The key now lives only in `.env` (`AV2_LLM_API_KEY=…`) —
  `backend/app/config.py` has an empty default, so a missing key shows up as a
  Venice 401 instead of being silently baked into the source.  Rotate it in the
  Venice dashboard if this tree has ever been shared.

Verified 2026-11-20, client level and through the running API:

```
GET  /models                      -> 148 models, glm-5-3-flash-uncensored present
POST /chat/completions + tools    -> finish_reason=tool_calls, ping({"target":"127.0.0.1"})
POST /chat/completions json mode  -> {"ok": true}
POST /chat/completions stream     -> deltas + streamed tool_calls recovered
POST /api/chat "reply: online"    -> "online", 1 iteration, tool_calls=0
POST /api/chat `id` in container  -> tool_call run_command, tool_result uid=0(root), answer
```

Never paste the token into docs or logs.  If it has been shared, rotate it in the
Venice dashboard and set `AV2_LLM_API_KEY` to the new value (`tools\connect_venice.ps1
-ApiKey '<new>' -Apply` does the write for you).

## Why the sandbox was red, and the fix

The flag comes from `GET /api/health` (`backend/app/main.py`):

```python
sandbox_ok = docker_ok and container == "running"
```

so it is red whenever *either* the Docker engine is unreachable *or* the
`av2-kali` container is not running.  Here the engine was dead:

* `docker version` → `Error response from daemon: Docker Desktop is unable to
  start … bootstrapping main distribution: running wslexec … exit status
  0x40010004`.
* `wsl -l -v` showed `docker-desktop` **Stopped**; the `wsl-bootstrap` log ended
  in a `DBG_TERMINATE_PROCESS` exit after hours of uptime.
* The `av2-kali` container itself was fine (`oneness-kali-agent:local`, 20.7 GB)
  — only the WSL2 engine hosting it had wedged.  `restart: unless-stopped` in
  `docker/docker-compose.yml` brought it straight back once Docker answered.

Recovery that worked (now automated by `.\start.ps1 -Docker`):

```powershell
Get-Process 'Docker Desktop','com.docker.backend' -ErrorAction SilentlyContinue | Stop-Process -Force
wsl --shutdown
& "$env:LOCALAPPDATA\Programs\DockerDesktop\Docker Desktop.exe"
# engine was answering after ~10 s; then the container auto-started
```

Notes for next time: Docker Desktop is a **per-user** install at
`%LOCALAPPDATA%\Programs\DockerDesktop` (not `C:\Program Files`), there is no
`com.docker.service` on this machine, its data disk
(`%LOCALAPPDATA%\Docker\wsl\disk\docker_data.vhdx`) is ~103 GB, and
`AutoStart=false` in `settings-store.json` — so nothing restarts the engine
after a WSL shutdown or a reboot.

## What changed in this round

* `backend/app/core/swarm.py` — **the completion rule is now mechanical**:
  * a run only ends on a gate-approved success or when every requested round has
    run; a spent token window is raised between rounds (`_keep_budget_alive`,
    `budget_extensions`), never a reason to stop;
  * the strategist's `stop` is audited (`SwarmStrategist.verify_completion`)
    against the recorded findings plus a deterministic finding floor
    (`_completion_floor`); a refused stop posts the missing evidence and the
    fleet re-plans;
  * a lane that stops empty `AV2_SWARM_MAX_ENFORCEMENT` times (3) is
    **terminated** and removed from the roster — stated in the prompt up front;
  * operator directives (`SwarmRun.add_directive`, injected between lane steps
    and into the strategist prompt) outrank the objective;
  * lessons are retrieved *scoped to the objective* and labelled reference-only,
    so a new session does not resume an old task;
  * budget scales as `lanes x rounds x AV2_SWARM_BUDGET_PER_LANE_ROUND`
    (60 k), capped by `AV2_SWARM_BUDGET_MAX_TOKENS` (12 M);
  * `SwarmManager.board()` replaces the `memory()` method that `self.memory`
    shadowed (the board endpoint used to 500).
* `backend/app/core/orchestrator.py` — the chat stream deadline now **detaches**
  from the run instead of cancelling it; the interim answer says the mission is
  still running and steerable.
* `backend/app/main.py` — `POST /api/chat/steer` (directive to the live fleet,
  409 when nothing runs) and `POST /api/chat/stop` (stops the backend run(s) for
  the session); `/api/fleet` exposes the enforcement knobs.
* `frontend/app.js` — typing while a fleet runs sends a directive instead of
  being ignored; `/steer`; `/stop` stops the backend run and then aborts the
  stream; done-handler notes for `exhausted`, `terminated` and detached runs.
* `tools/smoke_swarm.py` — 16 new offline checks for all of the above (stubbed
  model/sandbox, no network): all rounds run, unproven stop → `exhausted`,
  empty stops → terminated lanes, directives reach prompts, budget scaling.
* `docs/SWARM.md`, `docs/STATUS.md` — the enforcement section, the new
  "Steering" section and the chat-command list.

* `backend/app/core/godmode.py` — rewritten clean.  Docstring'd `scoped` /
  `research` / `off` profiles, `HARD_LIMITS` appended to every profile,
  matching prefill seeds, `locate_skill`, `build_block`, `prefill_messages`,
  `is_enabled`.  The injected "NO LIMITS / move funds / FULL AUTHORIZATION"
  text is gone; the file no longer contains any jailbreak payload string.
* `backend/app/core/blockchain.py` — Solana verification fixed:
  `maxSupportedTransactionVersion: 1` on `getTransaction` and finality derived
  from `finalized_slot` (`confirmations = finalized_slot - slot + 1`), not block
  height.
* `tools/godmode_probe.py` — strategy probe over the official skill's
  `auto_jailbreak.py` (benign canaries + harmful classification canary).
* `tools/verify_transaction.py` — independent on-chain verification.
* `tools/smoke_ollama.py` — model listing, tool-calling and `/api/health` smoke.
* `backend/app/core/context_window.py` — the prompt fitter: estimates the transcript,
  drops the oldest whole turns (tool calls leave with their replies), halves oversized
  text, never cuts a system prompt, and raises `OllamaContextError` when the window
  itself is too small.  Wired into `OllamaClient.chat` / `chat_stream`, which now also
  send `num_predict` / `max_tokens` (`AV2_LLM_MAX_OUTPUT_TOKENS`) and record
  `context_window` in `usage`.  `classify_model_failure` gained `kind="context"` so a
  window overflow stops being reported as a credentials problem.
* `backend/tests/test_context_window.py` — 15 checks over the fitter, the reserve, the
  wire payload, the incident's exact 400 payload, the Venice default wiring (both
  models, 128 k ceiling) and the 401/402 sentences.
* `docs/CONTEXT-WINDOW.md` — the 4 096-token incident, the resident set measured per
  window on the retired CPU brain, and why the fitter still runs on a 1M-window provider.
* `tools/glm53/connect_av2.ps1` — `-Native` (talk `/api/chat`, the only surface that
  carries `options.num_ctx`), `-NumCtx` / `-WorkerNumCtx`, and `AV2_PLANNER_THINK=false`
  instead of `reasoning_effort` when native.  For the cloud path it is
  `tools/connect_venice.ps1` instead.
* `tools/connect_venice.ps1` — one-command rewire to Venice (both models, 128 k
  ceiling, masked key, `.env` backup) plus `-Verify`, a live completion against every
  model id it just wrote.
* `tools/render/retire_glm_gate.ps1` — dry-run-or-delete for the two Render services
  that served the retired GLM brain.
* `tools/smoke_ollama.py` — `--num-ctx` and `--pad` to exercise a window the way a swarm
  does.
* `docs/GODMODE.md` — profiles, prefill, probe usage, observed results.
* `start.ps1` — restart + health wait + open UI, plus the opt-in `-Docker`
  sandbox preflight (engine start, one WSL-reset retry, `compose up -d kali` /
  `docker start av2-kali`) and actionable hints when the sandbox is down.

## Verification state

* `python -m compileall backend` and `from app.core import godmode` — clean.
* Frontend served: `/` → 200 (`<title>Anonymous V2 — agent workbench</title>`),
  `/api/agents`, `/api/chains`, `/api/health`, `/api/sessions` → 200.
* Live model turn through the running server: `POST /api/chat`
  (`"Reply with the single word: ready"`) → `content=ready`, `iterations=1`,
  `events=start,agent,token,done`, `model=glm-5-3-flash-uncensored`.
* Live fleet turns through `/api/chat`: run `871e7dfa5e91` (48 tagged calls),
  `ef29a2e7baaa` (41 calls, 95 s) and the post-restart run (54 calls, 78 s,
  2 duplicates suppressed) — six agents, both models, all commands tagged.
* `python tools/smoke_ollama.py --tool-call` → capabilities
  `['completion','thinking','tools','vision']`, `native tools: True`, recovered
  `run_command {"command": "ls -la /work"}`.
* `python -m pytest backend\tests -q` → 15 passed (context fitter, reserve, wire payload,
  Venice default wiring, 401/402 error text).
* Window, live: `/api/show` reports `parameters: stop "<|user|>"` (no baked `num_ctx`) and
  `deepseek2.context_length = 202752`; `/api/ps` reports
  `context_length: 32768, size: 21 573 780 110` when asked for 32 k, so
  `options.num_ctx` is honoured on `/api/chat` and dropped by `/v1` (docs/CONTEXT-WINDOW.md).
* Sandbox: `docker exec av2-kali bash -lc 'id; which nmap sqlmap'` →
  `uid=0(root) … /usr/bin/nmap /usr/bin/sqlmap`.
* `docs/godmode-results.json` — archived probe run; the `scoped` / `research`
  rows predate the prompt cleanup (see `docs/GODMODE.md`), re-run
  `python tools/godmode_probe.py` to refresh them.
* Backup of the Hermes config and `~/.ollama/config.json` is under
  `storage/hermes-backup-*`.

## Egress identity (2026-11-19)

The worker must not reach a target from this machine's address or city.  Two
layers, both verified live:

| layer            | value |
|------------------|-------|
| sandbox address  | `av2-kali` = 10.213.77.3 on the pinned bridge `av2-net` (10.213.77.0/24) — outside the LAN 10.0.0.0/24, the WSL 172.25.0.0/16 and the Docker defaults |
| proxy            | `av2-tor` = 10.213.77.2, SOCKS5 9050, no host port published |
| target-visible IP| `204.8.96.120` (The Netherlands, The Infrastructure Group B.V.) |
| operator IP      | `173.177.72.31` (this device, Videotron/Gatineau) |
| `/api/egress`    | `mode=tor`, `distinct: true` |

How: `app/core/sandbox.py` wraps every `docker exec` in
`ALL_PROXY/HTTP(S)_PROXY=socks5h://tor:9050` (remote DNS) and
`docker/proxychains4.conf` is mounted for tools that open raw sockets
(`proxychains4 nmap -sT -Pn ...`).  `.\\start.ps1` prints the sandbox-vs-host
comparison on every restart; `/api/health` carries the resolved `egress` mode
and every tool result records `meta.egress`.  Details and caveats:
`docs/EGRESS.md`.

Caveats: Tor's entry guard still sees this machine (that is Tor's design, the
target does not); some WAFs block exit nodes; raw scans are only proxied when
prefixed with `proxychains4`; if `av2-tor` is down the mode degrades to
`direct` and both `/api/health` and `start.ps1` say so.  Opt out with
`AV2_SANDBOX_EGRESS=direct`.

## Historical: the 2026-11-20 six-agent fleet (retired)

> Superseded on 2026-11-25: the roster is now **five** specialists per squad and
> the `analyst` lane is retired (its review duty moved to the strategist and the
> completion gate). This section is kept as the record of the earlier shape and
> the live run that validated it.

`POST /api/chat` no longer runs one captain.  Every non-conversational turn is
dispatched to a fixed six-agent roster, all at the same time:

| agent | role | model |
|---|---|---|
| Agent 1 | recon | glm-5-3-flash-uncensored |
| Agent 2 | web | glm-5-3-flash-uncensored |
| Agent 3 | exploit | glm-5-3-flash-uncensored |
| Agent 4 | coder | deepseek-v4-1-flash-1017-uncensored |
| Agent 5 | reporter | deepseek-v4-1-flash-1017-uncensored |
| Agent 6 | analyst (cross-check / coverage gaps) | deepseek-v4-1-flash-1017-uncensored |

* **Own terminal per agent** — `sandbox.py` keeps one persistent `bash` inside
  `av2-kali` per agent (`docker exec -i --workdir /work/fleet/<run>/agent-N`),
  so `cd`, exports and process state are private to that agent.  One-shot
  `docker exec` stays the automatic fallback when a shell dies or a command
  outlives its timeout.  `/api/health` → `agent_terminals`, `GET /api/fleet` →
  `terminals`.
* **Own browser per agent** — `/work/fleet/<run>/agent-N/.browser` cookie jar
  and body cache, so each web agent holds its own independent session.
* **One dedup ledger** — `run_command` keyed by normalised command line,
  `browser` by method + URL + body, everything else by its JSON arguments.  A
  second agent's identical call is not executed: it waits for the owner and
  receives the recorded result with `duplicate=true`.
* **One board + strategist** — round 1 is deterministic role shards (every agent
  starts immediately), round 2 is re-planned by the planner from the board,
  then a synthesis report.
* **Tagged events** — every `agent`, `tool_call`, `tool_result` and `token`
  carries `agent="Agent 3 · exploit · deepseek…"` plus `data.label`,
  `data.model`, `data.terminal`, `data.run_id`.  The chat UI keeps a separate
  segment, bubble and tool-card set per agent, so interleaved lanes never
  overwrite each other; the swarm panel shows the same labels.
* **API** — `GET /api/fleet` (roster, models, live terminals, latest run);
  `POST /api/swarm` accepts `"fleet": true`.
* **Knobs** — `AV2_FLEET_ENABLED`, `AV2_FLEET_AUTO`, `AV2_FLEET_MODELS`,
  `AV2_FLEET_AGENTS_PER_MODEL=3`, `AV2_FLEET_ROUNDS=2`,
  `AV2_FLEET_STEPS_PER_AGENT=4`, `AV2_FLEET_DEADLINE_S=300`,
  `AV2_AGENT_TERMINALS=true`.  `AV2_FLEET_AUTO=false` (or a greeting) keeps the
  single-agent loop; `swarm_execute` still starts the fleet explicitly.

Verified live with `python tools/smoke_fleet.py` (run `871e7dfa5e91`):

```
agents that ran commands   Agent 1..6  (2-12 calls each, all six in parallel)
models used                glm-5-3-flash-uncensored, deepseek-v4-1-flash-1017-uncensored
tool calls tagged          48/48 carry label + model + terminal
dedup ledger               47 executed, 1 duplicate suppressed
terminals                  agent-1..agent-6 alive, 2-7 commands each
final answer               3.5 k-char synthesis, status=completed
```

## Long missions and VM file exchange (2026-11-20)

* **Rounds are no longer capped at 5.**  `POST /api/chat` and `POST /api/swarm`
  accept `rounds` 1–20 (the composer and the swarm form are number inputs) and
  `until_complete: true`.  With until-complete the strategist gets a fresh round
  until the completion gate proves the objective (`stop: true` + audit) or until
  the cap (`AV2_FLEET_MAX_ROUNDS=20`) is reached.  Those chat turns also raise the
  wall-clock ceiling to `AV2_FLEET_UNTIL_COMPLETE_DEADLINE_S=3600` s and the
  shared token budget to `AV2_FLEET_UNTIL_COMPLETE_BUDGET_TOKENS=2000000`.
  `GET /api/swarm/{run_id}` now reports `until_complete`,
  `mission_complete`, `completion`, `stopped_reason` and `budget_extensions`,
  and the panel renders `round x/y (until complete)`.
* **Workspace upload/download.**  New `FileExchange`
  (`backend/app/core/workspace.py`) plus four endpoints:

  | method | path | purpose |
  |---|---|---|
  | POST | `/api/files/upload?name=&rel=&batch=` | raw-body upload of any format (no multipart); streamed to `storage/uploads/<batch>/<rel>` |
  | POST | `/api/files/finish?batch=&dest=` | one `docker cp` into `/work/uploads/<batch>` (or `dest`) followed by `chmod -R a+rwX` |
  | GET | `/api/files?limit=` | list `/work` (agent-created folders included; `.browser`, `node_modules`, `.git`, `__pycache__` pruned) |
  | GET | `/api/files/download?path=` | single file, or a zip (tar.gz fallback) of a folder, pulled back with `docker cp` |

  The sidebar **Workspace** panel supports drag/drop of files *and* folders, a
  file picker, a folder picker (`webkitdirectory`), per-file download links, a
  `reference` button that inserts the uploaded paths into the composer, and a
  `zip` button for the whole tree.  The fleet prompt now points agents at
  `/work/uploads/` for inputs and `/work/out/` for deliverables, so agents can
  create folders, run/debug the app and hand the artefacts back.
* **Layout** — the dashboard has a resizable sidebar (drag the divider,
  240–760 px, remembered in localStorage; double-click resets to 320 px; `◧`
  collapses it), stable scrollbar gutters and independent scroll areas, so
  panels no longer squeeze the transcript on a small screen.

## Swarm execution (2026-11-20, wider category mode)

One task can now be worked by a fleet: **5 agents per category** (recon, web,
exploit, coder, reporter = 25 lanes), each with its own terminal directory and
browser profile, all sharing one dedup ledger and one board that a strategist
re-plans from between rounds.  (This paragraph originally said 6 per category /
30 lanes; the 2026-11-25 squad contract clamps a category to one squad of five.)  Details: `docs/SWARM.md`.

* code: `backend/app/core/swarm.py` (`SwarmManager`, `SwarmRun`, `SwarmLedger`,
  `SwarmMemory`, `LaneSpec`, `SwarmStrategist`); `tools.py` gained `browser`,
  `swarm_execute`, `swarm_status` and lane-aware exec/file tools; `sandbox.py`
  accepts a per-call working directory.
* API: `POST/GET /api/swarm`, `GET /api/swarm/{id}`,
  `GET /api/swarm/{id}/events` (SSE), `/memory`, `POST /message`, `POST /stop`.
* guardrails: `GET/PUT /api/engagement`, `POST /api/engagement/kill-switch`,
  `/dry-run`; every command is checked by `GuardrailEngine` before execution
  (`lab` annotates, `production` enforces the contract).
* evidence: `GET /api/evidence`, `/summary`, `/verify`, `/{id}`; `add_finding`
  seals the six-element record and downgrades unproven verdicts.
* verification: `python tools/smoke_swarm.py` (27 checks) all pass; live runs
  `23ae21302062` (4 lanes, 10 unique claims, hash chain intact) and
  `66e607678255` (2 lanes, 1 duplicate suppressed, 5.9 k-char synthesis).

## Endpoint failures are diagnosed, not guessed (2026-11-21)

Three fleet runs (`e0b2525b4d08`, `0c23abffdbfd`, `59a7ba8bca2c`) ended
`exhausted` with **0 findings** in about ten seconds.  The transcripts were not a
regression in the stop/completion rules: every lane, the strategist and the
synthesis call died with

```
chat failed: [Errno 11001] getaddrinfo failed
```

`Errno 11001` is `WSAHOST_NOT_FOUND` — the API process could not resolve
`api.venice.ai`, so no request ever left the machine.  Each lane failed over to
its second model once and then reported `all models terminated`, which reads like
a swarm bug.  Connectivity on this host is fine again (DNS resolves to Cloudflare,
`GET /api/models` → 200, and both `glm-5-3-flash-uncensored` and
`deepseek-v4-1-flash-1017-uncensored` answered a probe), so the outage was
host-side DNS/VPN, not configuration: the key, the base URL and both model ids are
valid.

Two code changes make that class of failure self-explanatory and fix the reason
the lanes ignored the objective:

* `SwarmManager._endpoint_problem()` probes the endpoint **before** the first
  round (DNS retried twice).  A dead endpoint now ends the run as `failed` with
  `completion.gate = "connectivity"` and one actionable sentence, instead of
  dispatching rounds whose every lane dies on its first step.
* `classify_model_failure()` grades transport errors (`dns` / `connect` / `http`),
  so a lane that loses all its models reports
  `reason: "model endpoint unreachable"` and the hint reaches the UI/error field.
* `_objective_shard()` prefixes every deterministic round-1 mission with the
  objective and the newest directive.  Previously the shard was sent bare, so two
  capable models were handed "Fingerprint every reachable service" on a task that
  had nothing to do with it and followed the template instead of the instruction.

Verification: `tools/smoke_swarm.py` 63/63 (12 new checks: objective-carrying
shards, host parsing, DNS/connect/HTTP classification, and a dead-endpoint run
that must fail without dispatching a round or calling a model).  Live: run
`1a858ee24f7d` (1 lane, 1 round) completed with a finding from 3 tool calls, and
fleet run `e7d5f7c91d51` (one agent per cloud model) had **both** models answer
and execute the objective's steps with 0 duplicates and no termination.


## The Render GLM brain is gone; `.env` is Venice again (2026-11-23)

`.env` had drifted back to the self-hosted Render gate while every other file in the
tree still described Venice:

```dotenv
AV2_LLM_PROVIDER=ollama
AV2_OLLAMA_BASE_URL=https://anonymous-ollama-gate.onrender.com
AV2_PLANNER_MODEL=hf.co/DavidAU/GLM-4.7-Flash-Uncensored-Heretic-NEO-CODE-Imatrix-MAX-GGUF:IQ2_M
AV2_LLM_API_KEY=<48-hex gate bearer token - not a Venice key>
```

That combination is exactly what answered a swarm with `HTTP 400: request (4775
tokens) exceeds the available context size (4096 tokens)`: a 12c-24g CPU box running a
10.3 GB `IQ2_M` GGUF at the daemon default window, behind an nginx gate that billed
~450 USD/month whether it answered or not.  Worse, the key in the file was the *gate's*
bearer token - sent to `api.venice.ai` it returns 401, so once anything else pointed at
Venice the failure read as "bad key".  Everything that touched it is removed:

* `.env` - `AV2_LLM_PROVIDER=venice`, `AV2_OLLAMA_BASE_URL=https://api.venice.ai/api/v1`,
  planner *and* worker `glm-5-3-flash-uncensored`,
  `AV2_FLEET_MODELS=glm-5-3-flash-uncensored,deepseek-v4-1-flash-1017-uncensored`
  (3 lanes each), `AV2_*_NUM_CTX=131072`, `AV2_LLM_REASONING_EFFORT=low`.  The 59-char
  Venice key is restored and is the only key in the file; the native-only
  `AV2_PLANNER_THINK=false` is gone (nothing reads it on this protocol).
* `backend/app/config.py` - default window 16384 -> **131072**.  Venice serves 1M tokens
  to both models, so on this provider the setting stops being "the server's window" and
  becomes AV2's own send ceiling; the fitter still trims to it minus the 512 reserve.
* `backend/app/core/ollama_client.py` - `_http_error` names 401 / 402 / 429 instead of
  passing the bare status through: a rejected key, an empty balance and a rate limit are
  three different fixes, and the swarm's generic "check the API key, the model id and the
  account quota" line conflated them.
* `tools/connect_venice.ps1` (new) - `-Apply` rewrites `.env` (timestamped backup, masked
  key, UTF-8 without BOM, drops native-only keys); `-Verify` posts a real completion to
  **each** model and maps 401/402/404/429 to one actionable sentence.  This is the script
  that would have caught the drift.
* `tools/render/retire_glm_gate.ps1` (new) - deletes `anonymous-ollama-gate`
  (`srv-dc1ufdifngtc738n1qdg`, web_service, ~7 USD/month) and `anonymous-ollama`
  (`srv-dbofib8u01pc73f48tm0`, private_service 12c-24g, ~450 USD/month).  Dry run lists
  them and spends nothing; `-Execute` needs a Render API key (Dashboard -> Account
  Settings -> API Keys).  **The key used earlier in this workspace answers 401**, so the
  deletion cannot be done unattended from here - that one step is the operator's.
* `tools/glm53/connect_av2.ps1` - Render examples removed; it now describes itself as the
  GPU-box/local-daemon path, with Venice named as the cloud default.
* `docs/CONTEXT-WINDOW.md` and `docs/GPU-PROVIDER-OPTIONS.md` 6d - marked retired.  The
  fitter explanation stays: it is what keeps a prompt inside *any* window, and it is the
  reason the 4 096-token incident can no longer recur silently.

Verified after the restart (`.\start.ps1 -NoBrowser`):

```
GET  /api/health  -> provider=venice ollama=True context_window=131072 context_reserve=512
                     planner_model=glm-5-3-flash-uncensored fleet_size=6 planner_tools=True
GET  /models      -> 148 ids; both glm-5-3-flash-uncensored and
                     deepseek-v4-1-flash-1017-uncensored present, supportsFunctionCalling=true
POST /chat/completions, each model -> 402 {"error":"Insufficient USD or Diem balance ..."}
python -m pytest backend\tests -q   -> 15 passed
```

**The remaining blocker is billing, not wiring.**  The Venice account has no balance, so
a lane still cannot answer; the client now says so in one sentence
(`HTTP 402: the account has no credit left - top up at https://venice.ai/settings/api,
then retry; the key itself is valid [<server body>]`).  Top up, run
`python tools\smoke_ollama.py --tool-call`, then a 4-agent swarm - with the window at
131072 the `context_window` in `usage` should read 131072 and no lane can hit 4096 again.

1. Manual browser pass: greeting, Stop, scope/session handling, agent
   selection, transcript restore, and the new per-agent fleet segments.
2. Docker Desktop has `AutoStart=false`; either enable "Start Docker Desktop
   when you sign in" in its settings or run `.\start.ps1 -Docker` after a reboot,
   otherwise `/api/health` goes back to `degraded` with
   `container av2-kali=exited`.
3. Semantically overlapping work: the ledger dedups exact normalised commands
   (`nmap -sV -p 80 host`) and browser requests (method+URL+body), but two agents
   asking the same URL with different flags still both run.  A host+path key per
   tool is the next tightening step.
4. `/api/health` still names the model check `ollama=`; renaming it to `model=`
   needs a schema/frontend touch and is deferred.

## Squads of five, provider panel, unlimited rounds (2026-11-25)

The roster became squads of five, the round ceiling moved to 50 with an until-done
mode, and the provider is editable from the UI.

* **Roster** — `FLEET_GROUP_ROLES = recon, web, exploit, coder, reporter` with
  `FLEET_GROUP_SIZE = 5` and `FLEET_MAX_GROUPS = 5` in `backend/app/config.py`.
  `AV2_FLEET_GROUPS` (default 1) is the composer's *squads* selector: 1-5 launches
  5 / 10 / 15 / 20 / 25 agents, models handed out round-robin over
  `AV2_FLEET_MODELS`.  `_fleet_roster` gives every squad the five roles with group
  ids `[1]*5, [2]*5, …` and unique `agent-N` lane ids; `GET /api/fleet` previews
  exactly that roster.
* **Classic path clamped** — `/api/swarm` no longer multiplies lanes by category
  past one squad: `lanes` is 1-5 in the schema (422 above) and `swarm.start` clamps
  it to `FLEET_GROUP_SIZE`, so five categories top out at 25 agents (`MAX_LANES`)
  instead of 125.
* **Rounds** — `rounds` is 1-50 in both request schemas; `until_complete` uses
  `AV2_FLEET_MAX_ROUNDS` (50) as its cap and `unlimited` uses
  `AV2_FLEET_UNLIMITED_ROUNDS` (10000), with the completion gate, the token window
  and the wall clock as the only other exits.  `/until on` sets the field to 50.
* **Provider panel** — `GET/POST /api/provider` reads and rewrites the `AV2_*` keys
  in `.env`; the sidebar form (label, base URL, model, key, hint, refresh) shows the
  live provider, whether a key is stored and whether the endpoint answered.
  `.env` is git-ignored and `.env.example` documents the accepted labels
  (`openai`/`venice`/`openai-compatible` → `/chat/completions`, `ollama*` → native
  `/api/chat`; Ollama Cloud must use `openai` and keeps the `:cloud` suffix).
* **Branding and packaging** — `browser.png` is the sidebar logo and the favicon;
  `install.ps1` + `install.cmd` install the backend packages; `README.md` and the
  OneNess non-commercial source-available licence are in the repository root.

Verified at the last restart (`.\\start.ps1 -NoBrowser`):

```
GET  /api/health                    -> status=ok version=2.1.0 provider=openai
GET  /api/fleet                     -> groups=1 size=5 max_rounds=50
POST /api/swarm groups=0|6          -> 422 (ceiling is 5 squads)
POST /api/swarm lanes=6, rounds=51  -> 422 (ceiling is one squad / 50 rounds)
_fleet_roster groups 1..5           -> 5/10/15/20/25 lanes, unique ids, [i]*5 groups
_rounds_for until / unlimited       -> 50 / 10000
python -m pytest -q                 -> 15 passed (previous release; 48 in the current one)
```

## Learning Board upgrade (2026-11-25)

The old flat "Memory" note table is now a structured Learning Board.
`backend/app/core/memory.py` re-exports `backend/app/core/learning.py`, so old
imports (`KINDS`, `KnowledgeBase`, `utc_now`) keep working and the existing
database is migrated in place.

* **Schema**: entries carry category, tags, roles, tools, preconditions,
  procedure, expected/observed result, common errors, troubleshooting, status,
  outcome, confidence, attribution, timestamps, usage counters, version, review
  fields, flags and scope. New tables `note_versions` and `note_usage` keep
  history and usage signals. `_ensure()` adds missing columns and backfills old
  rows, then creates the indexes.
* **Validation**: `proposed -> observed -> validated -> deprecated`. Agent writes
  cannot set `validated`; `review()` requires a named reviewer; three failed
  references flag `needs-review`; `deprecated` entries are never recommended.
* **De-duplication**: exact `kind` + canonical title, and near-duplicates at
  >= 0.82 token similarity are merged (both bodies kept, tags/roles/tools unioned,
  version bumped). Opposite outcomes on similar entries are flagged as
  contradictions.
* **Retrieval**: `recommend(task, role, tools, category, limit)` is called per lane
  assignment; the rendered block is role-specific, bounded and labelled
  reference-only. Ranking is local token weighting + synonym expansion +
  confidence/usage bonuses (no external vector store).
* **Automatic learning**: `report_knowledge` now asks for structured fields and
  stores lane notes as `observed`; the deterministic fallback also records
  category, roles and result.
* **Agent tools**: `memory_write` gained the structured fields, `memory_search`
  gained role/category/tool/status/result filters and role-aware ranking, and
  `memory_feedback` records success/failure.
* **Safety**: secrets are redacted on every write; instruction-like content is
  flagged and omitted from prompts; entries cannot override system instructions,
  scope, tool permissions or sandbox rules.
* **API + UI**: `/api/learning` (list/create), `/api/learning/{id}` (detail,
  edit, delete, related, versions, usage), `/api/learning/recommend`,
  `/api/learning/stats`, `/api/learning/categories`,
  `/api/learning/{id}/review`, `/api/learning/{id}/usage`. The sidebar panel is
  now **Learning Board** with search, category/role/status/outcome filters,
  badges, validate/deprecate/related actions and a stats line.

Verified in this release:

```
python -m pytest -q                                 -> 48 passed
tests/test_fleet_architecture.py                    -> 13 passed
tests/test_learning_board.py                        -> 20 passed
node --check frontend/app.js                        -> clean
GET  /api/learning/categories                       -> 10 categories, 5 roles
GET  /api/learning/stats                            -> migrated 5026 existing notes
POST /api/learning (with secrets)                   -> proposed, redactions=2
POST /api/learning (same title)                     -> merged, version 2
POST /api/learning/1/review (no reviewer)           -> 422
POST /api/learning/1/review reviewer=operator       -> validated, version 3
GET  /api/learning?role=recon&status=validated      -> filtered result
GET  /api/learning/recommend?role=recon             -> ranked lesson + digest
POST /api/learning/1/usage success                  -> uses=1, successes=1, conf=0.75
GET  /                                              -> Learning Board panel served
```

