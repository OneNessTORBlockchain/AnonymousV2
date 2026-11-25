# Context window: why a swarm died at 4 096 tokens, and what to run at

*Measured 23 Nov 2026 against the Render gate, Ollama 0.40.17.*

> **Retired the same day.**  The gate and the `anonymous-ollama` brain behind it were
> deleted, and AV2 now runs on Venice AI, where both models serve a 1M-token window
> (`tools/connect_venice.ps1`, `docs/STATUS.md`).  This note is kept for two reasons: the
> fitter it describes is still what keeps a prompt inside *any* window, and the
> 4 096-token failure is why the fitter exists.  Anything below that names
> `onrender.com`, a 48-hex token or `/api/ps` is history, not wiring.

## The symptom

Every lane of a five-agent run failed on its **first** call, and the UI reported it as
a credentials problem:

```
model error: HTTP 400: {"error":{"message":"request (4775 tokens) exceeds the
available context size (4096 tokens), try increasing it", ...}}
 - ... Check the API key, the model id and the account quota; this is not a network problem.
```

Nothing was wrong with the key, the model id, the quota or the network. The prompt was
simply 700 tokens larger than the window the inference server had opened.

## Why it happened - three facts, each verified on the live box

1. **Nothing bakes a window into the model.** `POST /api/show` reports
   `parameters: stop "<|user|>"` and nothing else. No `PARAMETER num_ctx`, so the
   runner takes the daemon default - `OLLAMA_CONTEXT_LENGTH`, **4096** unless it is
   set. The model itself is far bigger than that: `deepseek2.context_length = 202752`.
2. **AV2 was talking to `/v1`, and `/v1` throws the window away.** `options.num_ctx`
   only exists on the native `POST /api/chat`; the OpenAI shim has no field for it, so
   `AV2_PLANNER_NUM_CTX=16384` was decoration. (Same trap as `docs/ORCASAQ-VULTR.md` §7.)
3. **One AV2 lane prompt is already ~4.7 k tokens** - lane system prompt, objective,
   guardrails block and tool specs. On a 4 096-token daemon *every* request failed
   before a single tool ran.

## What the box can actually hold

The live brain is `anonymous-ollama` (`srv-dbofib8u01pc73f48tm0`), a **12c-24g CPU**
private service (no GPU, `size_vram: 0`), serving the 10.31 GB `IQ2_M` GGUF. `GET /api/ps`
prints the window and the resident set that are actually in force - the only trustworthy
source, since config only says what was asked for:

| window | resident (`/api/ps`) | what happened |
|---|---|---|
| 4 096 (daemon default) | ~11 GiB | the window that broke the swarm; a warm turn was 41 s |
| **16 384** | ~15 GiB (extrapolated from the two measured rows) | **operating point**: holds a 4.7 k prompt with room to grow, leaves ~9 GiB of page cache |
| 32 768 | **21 573 780 110 B = 20.1 GiB** | loads and reports `context_length: 32768`, but a 4.8 k-token prompt never completed |
| 65 536 | ~30 GiB | does not fit at all |

### Why 32 k "fits" but is not usable

The KV cache and compute buffers, not the weights, are what a bigger window buys: going
from 4 096 to 32 768 costs ≈ 317 KB *per token* (≈ 10 GiB), which leaves a 24 GiB box with
only ~4 GiB of page cache to hold 10.3 GB of weights. The result is not a slow answer, it
is a stalled one:

| request | result |
|---|---|
| 4 096 window, 4.8 k-token prompt | HTTP 400 in milliseconds - *the bug* |
| 32 768 window, 4.8 k-token prompt | no completion in 900 s, then more than 45 min still emitting ≈ 1 token / 25 s |
| 16 384 window, 11-token prompt | 200 in 347 s, of which **`load_duration` was 346 s** - the reload itself |
| 4 096 window, 443-token prompt | 200 in 41 s (measured earlier, warm) |

Three lessons that cost more than the window did: **a change of `num_ctx` reloads the
runner** (40 s with a warm disk, ~6 min without); **a 20 GiB runner has no page cache
left**, so it thrashes instead of answering; and **a second request that asks for a
different window while the first is still running** wedges the box - with 20.1 GiB
resident there is no room for the new runner beside the old one, and the old one keeps
the CPU. Pick the window once, keep the planner and worker tiers on the same value, and
serialise anything that changes it.

## The fix, in order of preference

### 1. Native protocol + `options.num_ctx` (client-side, no box change)

Ollama's own protocol carries the window with each request, so a self-hosted daemon can
be driven at 16 k without touching the box:

```dotenv
AV2_LLM_PROVIDER=ollama                     # -> openai_style False
AV2_OLLAMA_BASE_URL=http://gpu-box:11434    # no /v1
AV2_PLANNER_NUM_CTX=16384
AV2_WORKER_NUM_CTX=16384                    # equal, see below
AV2_PLANNER_THINK=false                     # native spelling of reasoning_effort=none
```

or, with the wiring script:

```powershell
.\tools\glm53\connect_av2.ps1 -BaseUrl http://gpu-box:11434 -ApiKey '<token>' `
                              -Model '<tag>' -Native -NumCtx 16384 -Apply
```

Verified on the retired gate: native requests carrying `"options": {"num_ctx": 32768}` and
`16384` both answered 200, and `/api/ps` reported the same `context_length` - the field
really is honoured per request, unlike on `/v1`.  (The window on `/v1` is impossible to
change; that is why the protocol switch, not a new key, was the fix.)

**Venice, which is what AV2 runs on now, sidesteps all of this**: its OpenAI protocol has
no window field because the window is 1 048 576 tokens for `glm-5-3-flash-uncensored` and
1 000 000 for `deepseek-v4-1-flash-1017-uncensored`.  There `AV2_PLANNER_NUM_CTX` is a send
ceiling AV2 imposes on itself (131072 by default), enforced only by the fitter.

**Keep `AV2_PLANNER_NUM_CTX` and `AV2_WORKER_NUM_CTX` equal.** Ollama reloads the
runner whenever `num_ctx` changes, so 16 k for the planner and 8 k for the summariser
pays a model load on every alternation - and on this host it may not be able to hold the
old runner while loading the new one.

### 2. Raise the daemon default (server-side)

Set `OLLAMA_CONTEXT_LENGTH=16384` on the **brain** service (the one running Ollama), not on
a proxy in front of it: nginx has no model in it.  That fixes `/v1` callers too, at the
price of a service restart (≈ 10 GB model reload).  The Render instance this was written
against is retired (banner above), and it also showed why declaring is not applying: the
service carried **0 env vars** while five were declared (`docs/GPU-PROVIDER-OPTIONS.md`
6d).  Whatever the host, read back `/api/ps` - it prints the window actually in force.

### 3. Bake it into a Modelfile

Only worth it for a dedicated model tag: `PARAMETER num_ctx 16384`, then rebuild the tag.
It survives restarts but pins one window for every client of that tag, including the
webapp.

## What AV2 does about it now

`backend/app/core/context_window.py` runs before every request:

* estimates the prompt (4 ASCII chars/token, 1 token per non-ASCII char, +4 per
  message, tool-call arguments included);
* holds back `AV2_LLM_PROMPT_RESERVE_TOKENS` (default 512) so the model has room to
  answer instead of stopping at `finish_reason: length`;
* if over budget, drops the **oldest whole turns** (an assistant `tool_calls` message
  and its `role="tool"` replies always leave together), then halves the bulkiest text
  and marks it `...[trimmed to fit a N-token context window]`;
* never cuts a system prompt: if the system prompt alone cannot fit, it raises
  `OllamaContextError`, which `classify_model_failure` reports as `kind="context"` with
  the sentence naming the setting;
* records the outcome in `ModelReply.usage` (`context_window`, `dropped_messages`,
  `trimmed_chars`), so token counts and window sit next to each other.

`AV2_LLM_MAX_OUTPUT_TOKENS` (0 = unlimited) caps `num_predict` / `max_tokens`. On this
CPU box decode is the slow half of a turn (5-12 tok/s), so a cap is the only lever that
shortens a reply without cutting the prompt.

## Verifying

```powershell
# fitter, wire payload, Venice default wiring and error text (15 checks)
python -m pytest backend\tests -q

# what AV2 is pointed at, and what the account answers - one line per model
.\tools\connect_venice.ps1 -Verify

# a real turn through the same client the swarm uses
python tools\smoke_ollama.py --tool-call

# the incident's shape: ~4.8 k tokens of prompt, bigger than the old 4 096 window
python tools\smoke_ollama.py --pad 11 --stream
```

On Venice there is nothing to poll: the window is the provider's (1M tokens for both
models), `/api/health` shows `context_window=131072` as AV2's own send ceiling, and a
prompt can only fail for billing reasons - which now reads `HTTP 402 … top up …`, never a
400.

For a self-hosted Ollama box the old check still applies: `/api/ps` must end with
`"context_length":16384`.  If it says `4096`, the request went out on `/v1` (or with no
`num_ctx` at all) and a swarm will fail exactly as it did before; if it says `32768`,
something overrode the setting - expect the thrash described above rather than an answer.

| 4 096 (daemon default) | ~11 GiB | the window that broke the swarm |
| **32 768** | **21 573 780 110 B = 20.1 GiB** | fits, ~4 GiB headroom - **this is the max** |
| 65 536 | ~30 GiB extrapolated | does not fit; expect OOM or thrash |

So "maximise the tokens" on this host means **32 768**, not the model's 202 752: the KV
cache and compute buffers, not the weights, are the constraint - every extra 32 k of
window costs ≈ 10 GiB here. Two consequences:

* **`OLLAMA_NUM_PARALLEL` multiplies that cost.** Two parallel 32 k slots want ~40 GiB
  on a 24 GiB box. Leave it at 1 while the window is 32 k, or drop the window to 16 k.
* **`OLLAMA_MAX_LOADED_MODELS > 1` is not affordable at 32 k** either: a second model
  residency cannot fit beside a 20 GiB runner.
