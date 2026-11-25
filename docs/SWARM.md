# Swarm — squads of five, one ledger, one board

`backend/app/core/swarm.py` turns one objective into a **synchronised fleet**:

> **The default path is a squad of five.**  `POST /api/chat` fans a task out to
> `recon, web, exploit, coder, reporter` — one agent per role, models handed out
> round-robin over `AV2_FLEET_MODELS` — each with its own **persistent shell**
> inside `av2-kali`, its own workdir and browser profile, one dedup ledger and
> one board.  `groups: 2..5` repeats that squad 2-5 times (10/15/20/25 agents).
> The category-per-lane mode below is what `POST /api/swarm` starts when you want
> a wide sweep without squads.

| knob | default | meaning |
|---|---|---|
| fleet (default chat path) | 1 squad = 5 | recon, web, exploit, coder, reporter, spread over the fleet models |
| groups | 1 (1-5) | squads per run: 5 / 10 / 15 / 20 / 25 agents |
| categories | `recon, web, exploit, coder, reporter` | classic mode forms lanes per category |
| lanes per category | **5** | clamped to one squad, so 5 categories × 5 = the 25-agent ceiling |
| rounds | 2 (max 50) | strategist re-plans after each barrier; `until_complete: true` runs to `AV2_FLEET_MAX_ROUNDS` (50) and `unlimited: true` to `AV2_FLEET_UNLIMITED_ROUNDS` (10000) |
| steps per lane | 3 | model turns per lane per round |
| calls per step | 2 | tool calls executed from one model turn |
| concurrency | 30 | asyncio semaphore, so all 25 lanes really do run together |
| token budget | 400 000 (until-complete 2 000 000) | shared by the whole run, scaled by lanes × rounds |
| per-lane wall clock | 900 s | a hung lane is cut, the rest continue |

Override with `AV2_SWARM_*` / `AV2_FLEET_*` (see `backend/app/config.py`), e.g.
`AV2_FLEET_GROUPS=3` for three squads, `AV2_SWARM_CATEGORIES=recon,web,exploit`.

The dashboard always renders all five role chips (recon, web, exploit, coder,
reporter) and starts with every one enabled.  `AV2_SWARM_CATEGORIES` only sets
the default for API callers that omit `categories`; it never hides a role from
the swarm form.

### Squad contract (single source of truth)

`FLEET_GROUP_ROLES` in `backend/app/config.py` is the only definition of the
active roster: `recon, web, exploit, coder, reporter`. `FLEET_GROUP_SIZE` derives
from it, `FLEET_ROLE_ORDER` in `swarm.py` is an alias of it, and the classic
`lanes` value is clamped to `FLEET_GROUP_SIZE`. The legacy `agents` field
(`POST /api/chat`, `POST /api/swarm`) is accepted only as **1-5** agents per
model and can never create a sixth specialist.

There is no `analyst` lane. The cross-check/review work it used to do is now a
coordination duty: the strategist audits the board each round and
`verify_completion` audits the recorded findings before a stop is accepted. The
squad is five, the maximum is five squads (25 agents), and every schema
(`ChatRequest`, `SwarmStartIn`) enforces that.

## The four shared structures

1. **Terminal per lane** — a *persistent* `bash` inside `av2-kali`, opened as
   `docker exec -i --workdir /work/fleet/<run>/agent-N … bash --noprofile`, so
   `cd`, exports and process state belong to that agent alone.  `run_command`,
   `read_file`, `write_file` and `list_dir` all run there.  If a command hangs
   past its timeout the shell is killed and the next call falls back to a
   one-shot `docker exec`, so one lane can never poison the others.
2. **Browser per lane** — `/work/swarm/<run>/<lane>/.browser/{cookies.txt,last-body.txt}`.
   The `browser` tool keeps a persistent cookie jar per lane, so each web lane
   holds its own independent authenticated sessions.
3. **Dedup ledger** — `storage/swarm/<run>/ledger.jsonl`, one claim per unit of
   work. `run_command` is keyed by the normalised command line, `browser` by
   method + URL + body, every other tool by its JSON arguments. A second lane
   that tries the same thing is **not executed**: it receives the recorded
   result with `duplicate=true`. This is mechanical, not a promise.
4. **Shared board** — `storage/swarm/<run>/board.jsonl`, append-only: plan,
   notes, tool records, results, findings, operator notes, synthesis. Every lane
   prompt contains a digest of the board, and the strategist reads it between
   rounds, so the fleet actually collaborates.

## A round

```
strategist.plan(board, ledger, lanes)  -> one mission per lane (JSON)
        |
        +-- 30 lanes in parallel, each: model turn -> claim -> execute -> post
        |
barrier (asyncio.gather)
        |
strategist.plan(...) again  (hypotheses merged, dead ends dropped)
        |
synthesis (markdown report, unconfirmed claims labelled)
```

The strategist is the configured planner model asked for strict JSON.  Fleet
round 1 skips it and uses the deterministic role table so every agent in every
squad moves immediately; round 2 re-plans from the board.  If the planner is
unreachable, the same table is used and the fleet still runs.

The deterministic shard is never sent on its own: every fallback mission opens
with `Objective (your whole task): <objective>.` (plus the newest operator
directive) and only then names the lane's slice.  Without that prefix a capable
model is handed a concrete instruction - "fingerprint every reachable service" -
that contradicts the objective, and it works the template instead of the task.

### When the provider cannot be reached

A swarm does not fail once when the model endpoint is down: every lane spends its
first step on the same dead socket, so a multi-round fleet can "finish" in ten
seconds with zero findings.  Before any lane is dispatched, the run probes the
endpoint (`GET /models`, or `/api/tags` for Ollama), retrying twice for DNS and
with a couple of seconds between attempts, and a failed probe ends the run as
`failed` with one sentence instead of N `terminated` agents:

```
model endpoint unreachable - DNS lookup failed for api.venice.ai: this host cannot
resolve the model provider, so no request ever left the machine. Fix DNS, the
VPN/proxy or the base URL (AV2_LLM_BASE_URL), then restart the API.
```

`swarm.classify_model_failure()` splits transport failures into `dns`,
`connect`, `http` and unknown, so a provider that answers `401` is never blamed on
DNS, and the lane that loses every model reports
`reason: "model endpoint unreachable"` rather than a generic termination.

## API

| method | path | purpose |
|---|---|---|
| GET | `/api/fleet` | default roster, models, live agent terminals, latest run |
| POST | `/api/swarm` | start a run; body `{objective, categories, lanes, rounds, session_id, model, fleet, until_complete}` (`fleet: true` = 3+3 model fleet; `rounds` 1–20, or the cap when `until_complete`) |
| GET | `/api/swarm` | recent runs + defaults |
| GET | `/api/swarm/{run_id}` | status: lane states, ledger counters, budget, board counts, synthesis |
| GET | `/api/swarm/{run_id}/events?after=` | SSE stream (same event vocabulary as `/api/chat`) |
| GET | `/api/swarm/{run_id}/memory?limit=` | raw shared-board records |
| POST | `/api/swarm/{run_id}/message` | operator note injected into the board for the next round |
| POST | `/api/swarm/{run_id}/stop?cancel=` | graceful stop (or immediate cancel) |

Round-loop semantics:

* `rounds` accepts 1–20.  With `until_complete: true` it is the **hard cap**:
  the run stops early only when the strategist sets `"stop": true` *and* the
  completion gate proves the objective, or when the operator stops it.  A run
  never stops because a round produced nothing or because a fixed token window
  ran out: the window scales with lanes x rounds and is raised between rounds.
  `GET /api/swarm/{run_id}` reports `until_complete`, `mission_complete`,
  `completion`, `stopped_reason`, `budget_extensions` and `rounds_remaining`.
* `POST /api/chat` takes the same `rounds` / `until_complete` fields, so the
  composer controls the default fleet without opening the swarm panel.  An
  until-complete turn gets `AV2_FLEET_UNTIL_COMPLETE_DEADLINE_S` (1 h) and
  `AV2_FLEET_UNTIL_COMPLETE_BUDGET_TOKENS` (2 M) as a floor; the scaled budget
  above is used when it is larger.  When the *stream* deadline is reached the
  turn detaches and returns an interim report while the run keeps working.
* File exchange is distinct from the swarm: `POST /api/files/upload` +
  `POST /api/files/finish` copy any format into `/work/uploads/<batch>`,
  `GET /api/files` lists the tree and `GET /api/files/download?path=…` returns a
  file or a zip of a folder.  Agents are told to put deliverables in
  `/work/out/` so they are easy to download.

```powershell
$body = @{ objective = 'Map 10.0.0.252:8081-8083 and prove the riskiest finding';
           categories = @('recon','web','exploit'); lanes = 5; rounds = 2 } | ConvertTo-Json
$run = Invoke-RestMethod http://127.0.0.1:8420/api/swarm -Method Post -ContentType application/json -Body $body
Invoke-RestMethod "http://127.0.0.1:8420/api/swarm/$($run.run_id)" | ConvertTo-Json -Depth 5
```

Chat turns start the fleet automatically; the captain can also start one with the
`swarm_execute` tool (read it back with `swarm_status`), and the system prompt
carries a `FLEET` block explaining the roster.  Lanes cannot delegate or start
nested swarms.

## Guardrails and evidence

* Every lane command passes through `GuardrailEngine.evaluate()` before the
  sandbox. In `production` an out-of-contract target, an unauthorized action tag,
  the kill switch, dry-run, expiry and the rate/budget limits all refuse with the
  exact reason; in `lab` the same check only annotates the audit trail.
* `add_finding` seals a record in the hash-chained evidence store. The six
  elements (`command, output, vulnerability, reproduction, impact, verdict`) are
  scored on write: anything missing downgrades the verdict to `unconfirmed` and
  is listed in `proof_gaps`. Lanes are told this in their system prompt.
* `GET /api/evidence/summary?session_id=...` and
  `GET /api/evidence/verify?session_id=...` expose the store and re-check the
  chain.

## Enforcement: a task is only done when it is proven done

The runner no longer accepts a friendly "done". A lane may end its task only in
three cases:

1. **success** - it recorded at least `AV2_SWARM_MIN_FINDINGS_PER_TASK` findings
   with `add_finding` (default 1), or
2. **the round barrier** - its step budget or the operator's stop ended the
   round, or
3. **termination** - *every* model available to that lane failed. A single model
   failure now triggers a failover to the other cloud model instead of ending
   the lane, so a task only dies when GLM *and* DeepSeek are down.

Anything else is rejected: the lane receives an ENFORCEMENT prompt with the
fresh board digest, the durable lessons and an instruction to change technique,
and it keeps working until the barrier. `LaneResult.completed`,
`LaneResult.terminated`, `LaneResult.enforced` and `LaneResult.reason` make that
visible in `/api/swarm/<run>` and in the UI.

**Termination is the penalty, not a success path.** A lane that keeps answering
with prose is warned twice; the third empty stop kills it
(`AV2_SWARM_MAX_ENFORCEMENT`, default 3). The lane is removed from the roster,
its slice is handed to a live lane on the next plan, and the operator sees
`AGENT TERMINATED` in the stream. The prompt tells every agent this rule up
front, so the consequence is never a surprise.

**Only success or the round cap ends a run.** The strategist's `stop` is a
*claim*: a completion gate (`AV2_SWARM_VERIFY_COMPLETION=true`) audits the
recorded findings against the objective and the per-lane success criteria, and a
deterministic floor requires at least a third of the live lanes (minimum one) to
have produced a finding. A refused stop writes the missing evidence to the board
and the fleet keeps re-planning. Lanes that ended without a finding are added to
`unresolved` and get a CONTINUATION mission next round (same lane, different
technique). `AV2_SWARM_ENFORCE_ROUNDS=true` (default) also removes the old
"two stalls and give up" shortcut.

**A spent budget is not an ending.** The token window is
`lanes x rounds x AV2_SWARM_BUDGET_PER_LANE_ROUND` (60 k by default, never below
the base budget, capped by `AV2_SWARM_BUDGET_MAX_TOKENS`), and if it is reached
between rounds the manager raises it (`budget_extensions` in the status) instead
of stopping. The hard cap is the only mechanical limit that can still end a run
early, and when it does the status is **`exhausted`** with an explicit "mission
NOT complete" notice. When all rounds finish without a gate-approved answer the
status is `exhausted` too - never `completed`.

Disable with `AV2_SWARM_ENFORCE_FINDINGS=false` / `AV2_SWARM_ENFORCE_ROUNDS=false`
if a free-form run should be allowed to end on prose alone.

## Steering: stop, redirect, and start clean

* **Type while the fleet is running** and the message is delivered as an
  `OPERATOR DIRECTIVE` (`POST /api/chat/steer`): it lands on the shared board,
  leads the next strategist plan and is injected into every running lane between
  steps. The directive outranks the objective and the current missions, so
  "stop, do this instead" is obeyed at the agent's very next call instead of
  being queued behind the old task. `/steer <text>` does the same explicitly.
* **`/stop`** calls `POST /api/chat/stop`, which stops the backend run (all live
  runs for the session, or one `run_id`) and then aborts the chat stream. A
  stream abort alone would leave the fleet working - the old "stop" button only
  closed the browser's connection.
* **A new session never resumes an old task.** The objective is the newest
  operator message, durable lessons are retrieved *scoped to that objective* and
  labelled reference-only, and the lane prompt states that boards, board records
  and `/work/fleet/<other-run>` files from earlier runs are never a task to
  resume. Old session history and workspace artefacts are context, not a queue.
* **The chat stream can detach without killing the mission.** When the stream's
  own time budget is reached the turn returns an interim report and the run keeps
  going in the background (visible in the swarm panel, steerable with a message)
  until success or the round cap.


## The Learning Board: what the fleet learns survives the run

`backend/app/core/learning.py` is the durable, structured knowledge base at
`storage/memory/agent_memory.sqlite3` (SQLite, WAL). `backend/app/core/memory.py`
is a compatibility shim that re-exports it. Full reference:
[docs/LEARNING-BOARD.md](LEARNING-BOARD.md).

An entry carries category, tags, specialist roles, tools, preconditions,
procedure, expected and observed result, common errors, troubleshooting, status,
outcome, confidence, source/agent/model, timestamps, usage counters, version and
review fields. The legacy `kind` (`technique|tool|skill|error|strategy|lesson`)
is retained for older callers.

* **Categories**: `reconnaissance`, `web-security`, `vulnerability-validation`,
  `tool-usage`, `troubleshooting`, `environment`, `planning`,
  `agent-coordination`, `reporting`, `general-lessons`.
* **Validation**: `proposed` (agent suggestion) -> `observed` (seen in a run) ->
  `validated` (operator review) -> `deprecated` (history only). An agent write can
  never set `validated`, and `review()` requires a named reviewer.
* **Role-aware retrieval**: before each assignment the runner infers the task's
  categories, searches the board and injects a small block titled
  `LEARNING BOARD for <role> (reference only)` into that lane's prompt. Recon gets
  reconnaissance/tooling lessons, web gets web/API lessons, exploit gets
  validation lessons, coder gets tooling/troubleshooting, reporter gets
  evidence/reporting. Failed methods are included as warnings.
* **Automatic learning**: after every lane task the runner asks the model for
  structured notes (`AV2_SWARM_KNOWLEDGE_REPORT=true`). They are stored as
  `observed`, never `validated`; if the call fails, a deterministic note is derived
  from the transcript so the task still leaves a record.
* **Agent tools**: `memory_search` (ranked, role-aware filters), `memory_write`
  (structured, stored as `proposed`), `memory_feedback` (success/failure signal
  that adjusts confidence and flags stale advice).
* **De-duplication**: `kind` + canonical title is the exact key; a near-duplicate
  (>= 0.82 token similarity) is merged into the existing entry, keeping both
  bodies, unioning tags/roles/tools and recording a version.
* **Safety**: secrets are redacted before storage; instruction-like text is
  flagged and never rendered into a prompt; entries are labelled untrusted
  reference data so they cannot override system instructions, scope, tool
  permissions or sandbox rules.
* **Operator controls**: the **Learning Board** panel and
  `GET/POST/PUT/DELETE /api/learning`, `GET /api/learning/{id}` (with related
  lessons, versions and usage), `POST /api/learning/{id}/review`,
  `POST /api/learning/{id}/usage`, `GET /api/learning/recommend`,
  `GET /api/learning/stats`, `GET /api/learning/categories`. The
  `/api/memory...` paths remain as aliases.

## Agent-authored skills

`backend/app/core/skills.py` keeps a registry at `storage/skills/<slug>/`
(`skill.json` + one source file). Any agent can turn working code into a tool:

* `skill_create` - save python / bash / node source, name it, describe it;
* `skill_run` - install into `<kali_workdir>/skills/<slug>/` (first run) and
  execute with arguments, returning stdout/stderr as tool output;
* `skill_list` / `skill_delete`; the operator gets the same controls in the
  **Skills** panel (`/api/skills`, `POST /api/skills/<name>/run`).

Skills are available to every model in the fleet, and creating one also writes a
`skill` note to the knowledge base.

## Workspace: upload, download and delete

* The chat box has **attach** / **folder** buttons and accepts drag-and-drop:
  files are streamed to the host, published into `/work/uploads/<batch>`, and
  their sandbox paths are written into the message.
* After every turn the newest files the agents produced are offered as download
  links inside that chat turn; `/download <path>` and `/files [path]` work at any
  time.
* **Right-click** any workspace entry for `download`, `use in chat` and
  `delete` (single file or a whole folder, recursive).
* **wipe** in the Workspace header calls `POST /api/files/clear`, which deletes
  every child of the workspace and resets the persistent agent terminals so no
  shell keeps a `cwd` inside the tree that just disappeared. Deleting the
  workspace root itself is refused by `DELETE /api/files` on purpose.

## Chat commands (always available, even mid-run)

`/help` `/squads 1..5` `/rounds 1..50` `/until on|off` `/stop` `/steer <text>`
`/files [path]` `/download <path>` `/delete <path>` `/wipe` `/memory [query]`
`/memory forget <id>` `/skill` `/skill run <name> [args]` `/skill delete <name>`
`/swarm` `/swarm stop`.

`squads` (composer selector, `/squads N` or `/agents N`) is the number of five-agent
squads in the next task, so 3 means fifteen agents at once; it is passed to
`/api/chat` and `/api/swarm` as `groups` (1-5).  `agents` still exists for the legacy
``models x N`` roster and only applies when no `groups` value is sent.

Typing a plain message while a fleet is live is not queued: it becomes an
operator directive and the agents switch to it at their next step.

## Validation on this host

* `python tools/smoke_swarm.py` — 63 offline checks: command/HTTP dedup keys,
  claim/replay, 30 unique terminals and browser profiles, deterministic planner
  with no duplicate mission and every fallback mission quoting the objective,
  DNS/connect/HTTP failure classification, guardrail refusal matrix, evidence
  chain, tool registration and evidence sealing, **plus the completion rule**:
  every requested round runs when a stop is refused, a run with no proven answer
  ends `exhausted` (never `completed`), empty stops terminate the lane after
  `AV2_SWARM_MAX_ENFORCEMENT` pushes, directives reach the board and both lane
  prompts, the token window scales/extends instead of ending a run, and an
  unreachable endpoint fails the run in one sentence without dispatching a
  single round or calling a single model. All pass.
* `python tools/smoke_fleet.py` — historical live turn `871e7dfa5e91` under the
  previous six-lane roster: Agents 1-6 all ran commands (48 tagged calls), both
  cloud models were used, one duplicate was suppressed, all six persistent
  terminals stayed alive and the run completed with a 3.5 k-char synthesis. The
  current roster is five roles per squad; the same tooling paths are covered by
  `backend/tests/test_fleet_architecture.py`.
* Live run `23ae21302062` (2 recon + 2 web lanes): all four reported `running`
  simultaneously at t+12 s, completed with 10 unique claims and a 5.9 k-char
  synthesis. `recon-1` recorded a confirmed finding; `/api/evidence/verify`
  returned `chain intact`.
* Live run `66e607678255` (2 recon lanes): the ledger suppressed **1 duplicate**
  and the synthesis reported it. A dead endpoint was caught by the swarm itself:
  `/login` answers **404** on 8081, so the login-form objective was refuted
  rather than invented.
* Live run `1a858ee24f7d` (1 recon lane, 1 round): 3 tool calls, 17.9 k tokens,
  `completed` with a recorded finding on the objective's own target.
* Live run `e7d5f7c91d51` (fleet, one agent per cloud model, 1 round): both
  `glm-5-3-flash-uncensored` and `deepseek-v4-1-flash-1017-uncensored` answered,
  ran the objective's steps (3 and 4 calls, 0 duplicates), recorded findings and
  were not terminated - the deterministic round-1 shards carried the objective.

## Known limits

* Agents avoid collisions by reading the board; the ledger is the backstop, so
  a duplicate normally shows up as `duplicates_suppressed > 0`, not as two
  identical executions.
* `browser` is curl-based: no JavaScript execution. Lanes that need a real DOM
  run chromium through `run_command` with their own `--user-data-dir`.
* Runs live in the API process. A restart cancels them (`lifespan` →
  `SwarmManager.shutdown()`); the ledger and board files stay on disk for replay.
* The completion gate and the per-lane lesson report are extra model calls: one
  audit per stop request (and one at the end of a run with findings), plus one
  small report per lane task. Set `AV2_SWARM_VERIFY_COMPLETION=false` or
  `AV2_SWARM_KNOWLEDGE_REPORT=false` to trim that spend.
