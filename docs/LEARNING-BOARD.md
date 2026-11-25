# The Learning Board

The Learning Board is AnonymousV2's durable, structured, searchable institutional
memory. It is what lets a fleet of five specialists improve across runs, restarts
and model swaps instead of starting from zero every time.

- Implementation: `backend/app/core/learning.py` (storage + retrieval).
- Compatibility shim: `backend/app/core/memory.py` re-exports the same objects for
  older imports (`KINDS`, `KnowledgeBase`, `utc_now`).
- Database: `storage/memory/agent_memory.sqlite3` (SQLite, WAL, stdlib only).
- API: `/api/learning/...`; `/api/memory/...` remains a legacy alias.
- UI: **Learning Board** panel in the operator console.

Nothing is kept only in process memory. Every lesson, version and usage signal is
committed to SQLite, so a restart does not lose what the fleet learned.

## 1. What an entry contains

| field | meaning |
| --- | --- |
| `id` | unique entry id |
| `title` | short specific title (natural key for de-duplication) |
| `body` | description / details |
| `category` | one of the ten knowledge categories |
| `tags` | searchable tags (`recon`, `api`, `timeout`, `false-positive`, ...) |
| `roles` | which specialists it applies to (`recon`, `web`, `exploit`, `coder`, `reporter`) |
| `tools` | tools involved (`ffuf`, `nuclei`, `curl`, ...) |
| `preconditions` | what must be true before the procedure applies |
| `procedure` | the general procedure another agent can follow |
| `expected` | expected result |
| `observed` | what actually happened |
| `common_errors` | known errors of the approach |
| `troubleshooting` | how to get past them |
| `status` | `proposed` / `observed` / `validated` / `deprecated` |
| `result` | `success` / `failure` / `unknown` |
| `confidence` | 0.0 - 1.0, adjusted by usage feedback |
| `source`, `agent`, `model`, `session_id`, `run_id`, `task`, `outcome` | attribution |
| `created_at`, `updated_at` | timestamps |
| `uses`, `successes`, `failures` | reference counters |
| `version` | increments on every edit or merge |
| `reviewed_by`, `reviewed_at` | who validated it, and when |
| `flags` | `instruction-like`, `near-duplicate-merged`, `contradiction[:id]`, `needs-review` |
| `redactions` | how many secrets were removed before storage |
| `scope` | the authorised engagement scope the lesson applies to |

Legacy columns (`kind`, `helpful`) are kept so old notes and external callers keep
working. `kind` is still one of `technique`, `tool`, `skill`, `error`, `strategy`,
`lesson`.

## 2. Categories

| category | purpose |
| --- | --- |
| `reconnaissance` | general discovery techniques: DNS, ports, services, directories |
| `web-security` | web and API assessment: authentication, injection, access control |
| `vulnerability-validation` | safe validation lessons, controlled proof, blast radius |
| `tool-usage` | tool configuration, flags, installation and invocation |
| `troubleshooting` | errors, timeouts and the fix that resolved them |
| `environment` | sandbox, container, proxy and execution configuration |
| `planning` | task decomposition and prioritisation strategies |
| `agent-coordination` | collaboration, hand-off and duplicate-avoidance lessons |
| `reporting` | evidence quality, findings and report writing |
| `general-lessons` | reusable knowledge that fits no narrower category |

`GET /api/learning/categories` returns the categories with their purpose, plus the
five roles, four statuses, three results and six legacy kinds.

## 3. Validation lifecycle

```
proposed -> observed -> validated -> deprecated
     \          \           \
      `----------`-----------`-> deprecated
```

- `proposed`: an agent (tool or lane report) suggested it. Nobody verified it.
- `observed`: seen in a real transcript during a run.
- `validated`: confirmed by the operator (or a review pass) and safe to recommend.
- `deprecated`: kept for history, never retrieved as advice.

Rules enforced in code:

- An agent write can never set `validated` or `deprecated`
  (`write(status=..., allow_review=False)` raises).
- `review()` and `update(status=...)` require a non-empty `reviewer`.
- Every status change, edit and merge writes a row to `note_versions`.
- Validation does not hide history: the old version remains queryable.

## 4. De-duplication and contradictions

`write()` uses two layers:

1. **Exact key**: `kind` + canonical title (`canon`, normalised tokens).
2. **Near-duplicate**: Jaccard similarity of title+body tokens against up to 200
   candidates of the same kind. At **0.82** or above the entry is merged into the
   existing row.

Merging keeps both bodies (separated by `---`), unions tags/roles/tools, keeps the
longest structured fields, raises confidence and bumps the version. The returned
record carries `duplicate_of` and `similarity`.

If a similar entry (0.55-0.82) has the opposite `result` (`success` vs `failure`),
the write is flagged `contradiction[:id]` for review instead of silently replacing
the earlier claim. Repeated failed references add `needs-review`.

## 5. Retrieval

`recommend(task, role=..., tools=..., category=..., limit=5)` implements the
retrieval workflow:

1. infer the assignment's categories from its text (`infer_categories`);
2. search with structured filters (category, role, tool, status, result, tag);
3. rank by weighted token overlap (title 3x, metadata 2x, body 1x), synonym
   expansion, confidence and reuse;
4. boost entries that match the specialist's role and its tool set;
5. keep failed methods, marked as warnings;
6. return a handful of entries (default 5), not the whole board.

Role priorities:

| role | first categories |
| --- | --- |
| `recon` | reconnaissance, tool-usage, environment |
| `web` | web-security, vulnerability-validation, tool-usage |
| `exploit` | vulnerability-validation, web-security, tool-usage |
| `coder` | tool-usage, troubleshooting, environment |
| `reporter` | reporting, agent-coordination, general-lessons |

The ranking is deliberately local: token weights + a small synonym table + SQL
filters. It is not an embedding model or an external vector database, and this
document says so rather than pretending otherwise. The interface
(`recommend` / `render`) is the extension point if a semantic backend is added
later.

Before each lane assignment the swarm runner retrieves and renders the relevant
entries into the lane prompt, titled `LEARNING BOARD for <role> (reference only)`.
The block always begins with the untrusted-data banner and never contains
instruction-like lines.

## 6. Automatic learning after a task

After every lane task (`SwarmManager.report_knowledge`, controlled by
`AV2_SWARM_KNOWLEDGE_REPORT`):

1. one small model call converts the transcript into structured notes (kind,
   category, roles, tools, procedure, expected/observed, errors, result);
2. notes are written with `status="observed"` — never `validated`;
3. if the model call fails, a deterministic note is derived from the transcript so
   the task still leaves a record;
4. the written entries are posted to the run board and counted in `learned`.

Agents can also write during a task with `memory_write` (stored as `proposed`) and
read with `memory_search`. `memory_feedback(note_id, action=success|failure)`
records whether a retrieved lesson actually helped.

## 7. Quality control and anti-poisoning

- **Source attribution**: agent, model, session, run, task and outcome are stored.
- **Duplicate detection** and **near-duplicate merging** (section 4).
- **Version history** for every change (`note_versions`).
- **Mandatory reviewer** for validation or deprecation.
- **Confidence** changes with success/failure feedback; three failures flag
  `needs-review`.
- **Expiration/revalidation**: `deprecated` entries are never retrieved; the
  operator can deprecate and re-validate at any time.
- **Contradiction flags** when similar entries claim opposite outcomes.
- **Instruction-like payloads** (e.g. "ignore previous instructions") are flagged
  and are never rendered into a prompt. The board treats web pages, files, tool
  output and imported text as untrusted data.
- **Secrets**: `sanitize_secrets()` redacts PEM private keys, `0x`+64-hex private
  keys, context-confirmed 64-hex keys/seeds, seed phrases, `sk-`/`pk-` API keys,
  AWS keys, GitHub/Slack tokens, `Bearer` tokens and `password=`/`token=` values
  before anything is stored. Only sanitised descriptions of an exposure are kept.

Learning Board entries cannot override system instructions, server-side
authorization, engagement scope, tool permissions, sandbox restrictions or
security policy. They are reference data, labelled as such in every prompt.


## 8. API

All endpoints accept JSON unless noted.

| method | path | body / query | purpose |
| --- | --- | --- | --- |
| `GET` | `/api/learning` | `query`, `kind`, `category`, `tag`, `role`, `tool`, `status`, `result`, `sort=relevance\|recent`, `limit` | search + filter + rank |
| `POST` | `/api/learning` | entry fields (`title` required) + `review=true` to validate as operator | create or merge |
| `GET` | `/api/learning/{id}` | - | entry + related + versions + usage |
| `PUT` | `/api/learning/{id}` | any editable field | operator edit (versioned) |
| `DELETE` | `/api/learning/{id}` | - | delete entry, versions and usage |
| `POST` | `/api/learning/{id}/review` | `{"status":"validated","reviewer":"operator"}` | validate / observe / deprecate |
| `POST` | `/api/learning/{id}/usage` | `{"action":"success\|failure\|reference"}` | learning signal |
| `GET` | `/api/learning/{id}/related` | `limit` | related lessons |
| `GET` | `/api/learning/{id}/versions` | `limit` | version history |
| `GET` | `/api/learning/recommend` | `task`, `role`, `tools`, `category`, `limit` | role-aware retrieval + rendered digest |
| `GET` | `/api/learning/stats` | - | counts, pending reviews, reuse, flags |
| `GET` | `/api/learning/categories` | - | category/role/status metadata |

Examples:

```bash
curl "http://127.0.0.1:8420/api/learning/recommend?task=ffuf+wordlist+behind+tor&role=recon&tools=ffuf&limit=3"

curl -X POST http://127.0.0.1:8420/api/learning \
  -H "Content-Type: application/json" \
  -d '{"title":"ffuf needs -timeout behind the proxy","body":"ffuf -w words.txt -u https://target/FUZZ -timeout 20","kind":"technique","category":"tool-usage","roles":["recon"],"tools":"ffuf","result":"success"}'

curl -X POST http://127.0.0.1:8420/api/learning/12/review \
  -H "Content-Type: application/json" \
  -d '{"status":"validated","reviewer":"operator"}'
```

Error behaviour: `400` for invalid enum/field values, `403` when a status change
has no reviewer, `404` for an unknown id.

## 9. Operator interface

The sidebar panel provides:

- a search box plus category, role, validation-status and outcome filters;
- a create form with kind, category, role, tools, title, body and a
  **validated (operator review)** checkbox;
- per-entry badges for status, category and outcome, plus confidence, reuse count,
  timestamps and relevance score;
- actions: `edit`, `validate`, `deprecate`, `related`, `delete`;
- a stats line: total lessons, validated, pending review, failed approaches,
  flagged entries and reference count.

`/memory [query]` in chat searches the same board, and `/memory forget <id>`
deletes an entry.

## 10. Tests

`backend/tests/test_learning_board.py` covers:

1. an agent retrieves a relevant lesson;
2. a lesson persists after a restart;
3. duplicate and near-duplicate lessons are merged;
4. failed methods are retrieved as warnings;
5. validated lessons rank above proposed ones;
6. agents contribute lessons, but cannot validate them;
7. review without a reviewer is rejected;
8. secrets are redacted before storage;
9. search filters and synonym expansion work;
10. instruction-like text is flagged and never rendered;
11. version history, usage signals, related lessons and statistics;
12. an old note table is migrated in place without losing notes.

Run them with:

```powershell
cd backend
python -m pytest tests/test_learning_board.py -q
```

## 11. What is measured, and what is not

Measured and tested: retrieval relevance ordering, persistence, deduplication,
validation rules, secret redaction, filter behaviour, versioning, migration.

**Not** yet measured: whether retrieval *improves task outcomes* in a controlled
experiment. No such benchmark has been run for this repository, so no claim of
automatic improvement is made. A credible benchmark would compare the same fleet,
model and objectives with retrieval enabled and disabled, measuring repeated
errors, duplicate tool executions, time to completion, successful reuse of prior
lessons, token consumption and verified outcomes. Until that exists, the board is
documented as a mechanism, not as a proven performance multiplier.

`AV2_LEARNING_RETRIEVAL=false` disables the retrieval injection (writes still
happen unless `AV2_SWARM_KNOWLEDGE_REPORT=false`), which gives an operator a clean
retrieval-off control arm for such a benchmark.

## 12. Limitations

- Relevance is lexical (plus synonyms), not semantic embeddings.
- The board is a local SQLite file; there is no multi-host replication.
- Contradiction detection is a heuristic on token similarity and outcome.
- Version history stores title/body/status/result, not every structured field.
- Seeded notes from earlier runs default to `proposed`; operators should review
  before treating them as validated advice.
- `analyst` is not a role on the board or in a squad: cross-checking is a
  strategist/completion-gate duty.
