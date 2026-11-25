# Self-hosting GLM-5.3-Flash-Uncensored IQ1_S on Akamai Cloud

> Status: **not deployed on Akamai. The blocker is Akamai's own account gate, not a
> credential.**
>
> 1. **The token is valid.** `LINODE_API_TOKEN` authenticates against the live API:
>    `GET /v4/account` → 200, account `oneness`, active since 2026-11-17, billing
>    source `paypal`, balance `$0.00`, `$2.03` uninvoiced, `GET /v4/linode/instances`
>    → 200 (one unrelated `g6-standard-1`). Verified 2026-11-22.
> 2. **Akamai refuses the GPU plan for this account.**
>    `POST /v4/linode/instances {type=g1-gpu-rtx6000-4}` → 400 `A limit on your
>    account is preventing the deployment of the selected Linode plan. To request
>    access to the plan, please contact Support and provide the Linode plan name.`
>    Nothing was created and **$0.00 was charged** (verified twice, `exit 1`). The gate
>    is blanket, not plan-size: `g2-gpu-rtx4000a4-m` ($3.57/h) and even
>    `g2-gpu-rtx4000a1-s` ($0.52/h) return the identical 400, so a smaller card is not
>    a workaround. Only Akamai Support can lift it; `provision_akamai.ps1` cannot, and
>    now reports it as an actionable message (support-ticket text + the Azure fallback)
>    instead of raw localised JSON.
> 3. **Money (decided).** `g1-gpu-rtx6000-4` (4x RTX 6000 Ada = 96 GB VRAM + 128 GB
>    RAM) at **$6.00/h**, i.e. **$96 for 16 h**; the live type object confirms
>    `hourly: 6.0`, `memory: 131072`, `gpus: 4`. Akamai bills the account on file;
>    the $200 Azure credit cannot pay it.
> 4. **No Hugging Face token is needed.** The model repo is public: `HEAD` on
>    `.../resolve/main/GLM-5.3-Flash-Uncensored-IQ1S-00001-of-00002.gguf` → 200,
>    `Content-Length: 44984374336` (41.9 GiB; 71.5 GiB across both shards). The
>    `SHA256SUMS.txt` fetch also works anonymously, so the previously pasted
>    `hf_...` token has no bearing on the build - revoke it, nothing needs it.
>
> **AV2 does not have to wait for that box.** §11 records a verified GLM-5.3
> endpoint that AV2 is running on today, and the one command that moves it to the
> 4-card instance unchanged once the account is allowed to deploy it.
>
> The Azure route is blocked separately: that subscription is a Free Trial offer
> with zero GPU quota in six regions (see `docs/GLM53-AZURE.md` §3.2).

**Status: no Akamai instance exists. AV2 is functional on a real GLM-5.3 endpoint.**

This page records what was verified, the arithmetic that blocks the plan, and the
three routes that do fit a budget. Everything checked on 2026-11-22 against the
public Hugging Face and Linode/Akamai APIs. Prices are the live v4 API values,
not a blog post.

## 1. What the model actually needs

From `https://huggingface.co/api/models/BoldingBuilds/GLM-5.3-Flash-Uncensored-GGUF`
and the repo's own model card:

| field | value |
|---|---|
| base model | `zai-org/GLM-5.3-Flash` (MoE, arch `glm5-next`) |
| total parameters | 320,759,404,382 (~320.8 B) |
| quant | **IQ1_S, 71.5 GiB, 2 shards** |
| repo download | 76.79 GB (71.5 GiB weights + 1.13 GB `mmproj` + card) |
| author's stated fit | **"fits on a single 96 GB card"** |
| runtime floor | **mainline llama.cpp `b11279`+** (PR #27773 merged 2026-09-30) |
| tool calling | needs `--jinja`; template emits `<tool_call>{name}<arg_key>…` |
| text-only switch | simply **do not pass `--mmproj`** — the card says "Text-only use does not need that file" |

The author is explicit that the edit is 666 of 12,384 routed experts (1.74 % of
parameters), that 37 of 120 harmful-set responses hit the output cap, and that
they measured **no** tool use, **no** agentic evaluation and **no** long context
(everything ran at 16 k). So the refusal numbers in that card tell you nothing
about tool-call accuracy, which is the thing AV2 depends on.

## 2. What Akamai Cloud actually sells (live API, 2026-11-22)

`GET https://api.linode.com/v4/linode/types` — every GPU plan is **hourly only**
(`monthly: null`), so a month is 730 h of billing:

| plan | GPUs | VRAM | RAM | $/h | $/day | $/730 h | GLM IQ1_S (71.5 GiB) |
|---|---|---:|---:|---:|---:|---:|---|
| `g2-gpu-rtx4000a1-s` | 1× RTX 4000 Ada | 20 GB | 16 GB | 0.52 | 12.48 | **380** | no |
| `g2-gpu-rtx4000a2-m` | 2× RTX 4000 Ada | 40 GB | 64 GB | 1.34 | 32.16 | 978 | only with ~43.5 GB of experts in RAM |
| `g2-gpu-rtx4000a4-s` | 4× RTX 4000 Ada | 80 GB | 128 GB | 2.96 | 71.04 | 2,161 | not resident: ~3.5 GB spills |
| `g2-gpu-rtx4000a4-m` | 4× RTX 4000 Ada | 80 GB | 196 GB | 3.57 | 85.68 | **2,606** | not resident: ~3.5 GB spills |
| `g1-gpu-rtx6000-1` | 1× RTX 6000 Ada | 24 GB | 32 GB | 1.50 | 36.00 | 1,095 | no |
| **`g1-gpu-rtx6000-2`** | **2× RTX 6000 Ada** | **48 GB** | **64 GB** | **3.00** | **72.00** | **2,190** | **not in VRAM: ~35.5 GB of experts spill to RAM** |
| `g1-gpu-rtx6000-3` | 3× RTX 6000 Ada | 72 GB | 96 GB | 4.50 | 108.00 | 3,285 | not resident: ~11.5 GB spills |
| `g1-gpu-rtx6000-4` | 4× RTX 6000 Ada | **96 GB** | 128 GB | 6.00 | 144.00 | **4,380** | yes — the only resident shape, 84 GB usable after reserve |

**There is no RTX PRO 6000 on Akamai.** The catalog was re-read on 2026-11-22
(`/v4/linode/types`, 13 GPU plans): every "6000" plan is `g1-gpu-rtx6000-*` = the
**RTX 6000 Ada, 24 GB per card**, 32/64/96/128 GB system RAM, hourly-billed
(`price.monthly` is `null`). "RTX PRO 6000 Blackwell" is a different, 96 GB-per-card
part; if a deployment plan assumes 2 × 96 GB = 192 GB, that is 4× what this
provider sells per plan. Confirm the card in the plan's own label before budgeting.

Run `.\tools\glm53\provision_akamai.ps1 -List` for the same table from the live API,
including the per-plan fit column for both models.

### 2.1 Why 2 cards is not 2 × the answer

The IQ1_S weights are **71.52 GiB** (41.89 + 29.63, measured with `?blobs=true`).
Two 24 GB cards give **48 GB**, so `-ngl 999` cannot work: roughly **35.5 GB** of
the MoE experts have to live in the 64 GB of system RAM. That is a real
configuration, not an error — llama.cpp supports it with `--n-cpu-moe N`, and the
cloud-init now derives `N` from the GGUF's own `block_count` instead of hardcoding
a layer number:

```bash
# /usr/local/sbin/av2-glm53-serve on the box (simplified)
moe_layers=$(awk -v v=48 -v w=71.5 -v n="$block_count" 'BEGIN {
  usable = v - 12; off = 1 - usable / w; printf "%d", int(off * n + 0.999) }')
llama-server ... --n-cpu-moe "$moe_layers" -ngl 999
```

What it costs you: decode speed. Experts are read over PCIe instead of VRAM, so
expect a multiple slower than the same model fully resident, and a 16 k context
eats into the 12 GB per-card reserve. The honest statement is **"serves, slower"**,
not "fits". Nothing on this host has measured it yet — see §6.

The second model does not fit at all on 2 cards:

| model | size | `g1-gpu-rtx6000-2` (48+64) | `g1-gpu-rtx6000-4` (96+128) |
|---|---:|---|---|
| GLM-5.3-Flash-Uncensored IQ1_S | 71.5 GiB | ~35.5 GB of experts in RAM | fully resident |
| DeepSeek-V4.1-Flash-1017 IQ1_M | 112.5 GiB | **impossible** (total RAM+VRAM is 112 GB) | ~28.5 GB of experts in RAM |

So "GLM and DeepSeek on the 2-GPU box" is not a tuning problem, and the
provisioner refuses it with that sentence rather than provisioning a box that
cannot load the model.

So "GLM and DeepSeek on the 2-GPU box" is not a tuning problem, and the
provisioner refuses it with that sentence rather than provisioning a box that
cannot load the model.

Non-GPU escape hatch: CPU-only needs ≥ 96 GB RAM for a 71.5 GiB file.
`g7-highmem-8` (150 GB / 8 vCPU) is **$480/mo** and would run at single-digit
tokens per second at 1.92 bpw.

## 3. The arithmetic

* Cheapest GPU on the account: **$380/mo** — 7.6× the cap, and 20 GB of VRAM, so it
  cannot load the model at all.
* Cheapest shape that *serves* it (with ~43.5 GB of experts in RAM): **$978/mo**
  (`g2-gpu-rtx4000a2-m`, 2× 20 GB).
* Cheapest shape that holds it **entirely in VRAM**: **$4,380/mo**
  (`g1-gpu-rtx6000-4`, 96 GB).
* **The requested 2× card box** (`g1-gpu-rtx6000-2`) is **$3.00/h = $72.00/day =
  $2,190/mo**, and it spills ~35.5 GB to RAM.
* **$50 buys:** 16 h on the 2-card box, 14 h on the 80 GB box, or **8.3 h** on the
  96 GB box. **$200 buys 66.7 h** on the 2-card box — 2.8 days, not a month.

So "$X/month, 24/7, this model" is not a tuning problem — it is off by one to two
orders of magnitude at every budget mentioned so far. I did not provision anything
to find this out, because the price list answers it.

## 4. Routes that fit

**A — stay hosted (recommended, $0 new spend).** The stack already runs
`glm-5-3-flash-uncensored` from `https://api.venice.ai/api/v1`
(`backend/app/config.py:38`). It is the same GLM-5.3-Flash family, already wired
into `Settings.openai_style`, the fleet and the GODMODE profile. Self-hosting
buys you weight custody and nothing else at this budget.

**B — time-boxed benchmark burst, fits in $50.** `g1-gpu-rtx6000-4` at $6.00/h
for **8 h ≈ $48**, or `g2-gpu-rtx4000a4-m` at $3.57/h for **14 h ≈ $50** plus an
80 GB block volume (~$8/mo, deletable). Purpose: run `tools/glm53/eval_model.py`
once, publish the numbers, delete the instance. This is the only honest way to
answer "does the uncensored edit still call tools" without a monthly commitment.
`tools/glm53/provision_akamai.ps1` exists to do exactly this and **enforces the
cap**: it refuses any plan whose `hourly × 730` exceeds `-MonthlyBudgetUsd`
unless you pass `-BurstHours`, in which case it checks `hourly × hours ≤ budget`.

**C — own hardware.** 96 GB VRAM (or ~96 GB RAM for CPU/MoE offload) on a
machine you already own. Free at the margin, but you inherit the build.

**D — smaller model.** Every rung below IQ1_S does not exist in this repo
("More rungs to follow"), and the card's own sibling data says a 2-bit build is
97 GB — worse, not better.

I did not pick one for you; that is the approval step. Nothing has been created,
and my instructions were explicitly not to touch your existing server.

## 5. Wiring it into Anonymous/Hermes (text-only, no added filters)

The model is served exactly as the card's quickstart, minus the projector and
minus any wrapper prompt. The command line is assembled by
`/usr/local/sbin/av2-glm53-serve` from `tools/glm53/cloud-init.yaml`, because the
offload flags depend on how much VRAM the plan actually has:

```
llama-server \
  -m GLM-5.3-Flash-Uncensored-IQ1S-00001-of-00002.gguf \
  --alias glm-5.3-flash-uncensored-iq1s \
  -c 16384 --jinja --reasoning-budget 1024 \
  --n-cpu-moe <derived> -ngl 999          # 96 GB plan: --n-cpu-moe is omitted, plain -ngl 999
  --parallel 4 --metrics \
  --host 127.0.0.1 --port 8080 --api-key ... 
```

`<derived>` = `ceil((1 - (VRAM - 12) / 71.5) * block_count)`, read from the GGUF
header with `llama-gguf` at service start; set `GPU_LAYERS=<n>` in
`/etc/av2-glm53/env` to pin it by hand instead. The wrapper prints the numbers it
chose before exec'ing, so the journal says *why* it split where it did.

* **Text-only, structurally:** `mmproj-GLM-5.3-Flash-F16.gguf` is never
  downloaded and `--mmproj` is never passed, so the vision tower cannot load.
* **No added refusals:** the system prompt is whatever AV2 already sends; the
  deploy adds no persona, no refusal rules, no output filter. AV2's config keys
  (`AV2_LLM_PROVIDER`, `AV2_OLLAMA_BASE_URL`, `AV2_LLM_API_KEY`,
  `AV2_PLANNER_MODEL`, verified in `backend/app/config.py:68-93,318-337`) are set
  by `tools/glm53/connect_av2.ps1`; no Python changes are needed because
  `openai_style` is already true for `AV2_LLM_PROVIDER=openai`.
* **TLS:** `llama-server` binds loopback only; Caddy terminates TLS (ACME if you
  give it a domain, otherwise `tls internal`, in which case the Windows side
  needs `SSL_CERT_FILE` pointing at Caddy's root CA because `httpx` verifies by
  default).
* **Firewall:** ufw default-deny, `443` open only to `-OperatorCidr`, plus the
  bearer token — two independent controls, because a llama.cpp port on the open
  internet is an open proxy with a GPU attached.

## 6. What is *not* verified (do not treat as tested)

1. **CUDA build path on the Akamai image.** `cloud-init.yaml` installs
   `nvidia-cuda-toolkit` if `nvcc` is missing, then builds with
   `-DGGML_CUDA=ON`. I could not run this: no instance was created. Treat the
   first boot as a supervised run, not a fire-and-forget.
2. **Native tool-call parsing for `glm5-next`.** llama.cpp's tool parser is
   template-driven; GLM's `<tool_call>name<arg_key>…` shape is the same family as
   GLM-4.5's, but this architecture is weeks old. AV2 degrades gracefully —
   `tool_protocol.extract_text_calls` recovers fenced JSON — so measure *native
   vs recovered*, which is what the harness reports.
3. **MoE-offload throughput on 2 cards, and any multi-GPU tensor split.** 71.5 GiB
   across 2× 24 GB leaves ~35.5 GB of experts in RAM; across 4× 24 GB it is close to
   the ceiling with little left for a 16 k KV cache. Both are unmeasured — §2.1
   *derives* the split, it does not prove the speed.
4. **MTP is not in mainline** (PR #27917 pending); the `blk.45` block is skipped.
5. **Every measurement on the card was taken on a fork**, not mainline — the
   author reports KL 0.017–0.020 drift mainline-vs-fork, so headline numbers may
   move slightly.

## 7. Security actions, do these first

1. **Rotate the Akamai token pasted into chat.** It was sent in plaintext and
   must be treated as public. Akamai Cloud Manager → *My Profile* → *API Tokens*
   → revoke, then mint a new one with **read/write only on Linodes + Firewalls +
   Volumes** and a 7-day expiry. `provision_akamai.ps1` reads
   `$env:LINODE_API_TOKEN` and never stores a token.
2. **Rotate the Render key** mentioned earlier in this workspace the same way
   (Render → Account Settings → API Keys).
3. Keep the deployment token out of Git: `.env` is local-only here, and
   `connect_av2.ps1 -Apply` writes a timestamped backup outside it.
4. The `ssh-ed25519` key you supplied is a **public** key — correct to share, but
   confirm its private half is passphrase-protected before it becomes the only
   door into a $4 k/mo GPU box.

## 8. One thing I noticed in your own logs

`storage/swarm/*/board.jsonl` and `ledger.jsonl` contain agent output touching
live exchange domains (`coinbase.com`, `kraken.com`, `bit2c.co.il`), an
`exec_transfers.py`, and a `dest_balance_before.json` for "100 SOL / 50 ETH /
5 BTC". Part of that run is clearly the loopback mock at `10.0.0.252:8083`
("local simulation · mock hardware"), but the exchange hostnames are not mock.
If those were reachability probes against production, they are outside anything
I will help extend. `tools/glm53/eval_model.py` only prompts the model and scores
text; it executes no tool, and its "unauthorized" cases point at `*.example`
reserved names, never a real third party.


## 9. The 2-GPU / two-model request (2026-11-22)

Asked for: two RTX PRO 6000 cards serving GLM-5.3-Flash-Uncensored, plus
`ollama run hf.co/ngeng/DeepSeek-V4.1-Flash-1017-Uncensored-GSQ-RCO-GGUF:IQ1_M`,
with AV2 promoted to production mode.

What the evidence supports, all of it re-checked against live APIs today:

| claim | evidence |
|---|---|
| both GGUF repos exist, public, **not gated** | `/api/models/...` → `gated=false private=false`, 6 and 16 siblings |
| GLM IQ1_S = 71.52 GiB in 2 shards | `?blobs=true`: 41.89 + 29.63 GiB |
| DeepSeek IQ1_M = 112.44 GiB in 4 shards | `?blobs=true`: 36.66 + 36.49 + 37.25 + 2.04 GiB |
| Akamai has **no** RTX PRO 6000 plan | 13 GPU plans returned, all `g1-gpu-rtx6000-*` (Ada 24 GB) or `g2-gpu-rtx4000a*` (Ada 20 GB) |
| 2× RTX 6000 Ada = 48 GB, $3.00/h | `g1-gpu-rtx6000-2`, `memory 65536`, `price.monthly null` |
| nothing was created | `-Execute` exits 1 at the token check; no `LINODE_API_TOKEN` on this machine |

So, concretely:

* **GLM on 2 cards: possible, slower.** 48 GB VRAM + 64 GB RAM, ~35.5 GB of experts
  in RAM via `--n-cpu-moe`. This is what `cloud-init.yaml` now does automatically.
* **DeepSeek on the same 2 cards: impossible.** 112.5 GiB into 48 + 64 GB leaves
  nothing for the OS, the KV cache or Ollama's own copy.
* **Both models at once, on any Akamai plan: no.** `g1-gpu-rtx6000-4` (96 + 128 GB)
  is the smallest shape that holds GLM resident and DeepSeek with ~28.5 GB spilled,
  and that is $6.00/h with Ollama holding a second 112 GiB copy on disk.
* **AV2 can only talk to one endpoint.** `Settings` has a single
  `AV2_OLLAMA_BASE_URL`; the fleet's two model ids are routed to the same base URL.
  Ollama is therefore loopback-only (11434) on the box, for direct use, not wired
  into AV2 unless you front both runtimes with one router that dispatches on the
  model name.

### What "production mode" means in this repo, and why I did not flip it

`AV2_ENVIRONMENT=production` (`backend/app/config.py:219,287-291`, accepts
`production` / `prod` / `live`) switches `GuardrailEngine` from annotate to enforce
(`backend/app/core/production.py:366-419`). With **no engagement contract** it
refuses *every* external command:

```
decision.allowed = False
decision.reasons.append(f"no engagement contract at {self.path}")
```

So flipping the flag without a contract does not produce a production system; it
produces a system that answers questions and executes nothing. The order is:

1. `PUT /api/engagement` with `owner`, `authorization_ref`, `allowed_targets`
   (and, only if the engagement really authorises them, `allowed_actions`).
2. Then set `AV2_ENVIRONMENT=production` in `.env` and `.\start.ps1`.
3. Then `GET /api/health` should show `godmode`/`scope_count` and `/api/engagement`
   shows `mode=production`, `contract_problems=[]`.

Note what production mode refuses by default: `fund-drain` is a refused action class
(`production.py:44`). That is deliberate and I am not going to help route around it.

### The exact commands, once the two blockers clear

```powershell
# 1. token (never paste it into chat; set it in your own shell)
$env:LINODE_API_TOKEN = '<new token, read/write on Linodes only, 7-day expiry>'

# 2. see the plans and the fit, from the live API - no charge
.\tools\glm53\provision_akamai.ps1 -List

# 3. GLM on the 2-card box, burst-sized to the budget, and prove it serves
.\tools\glm53\provision_akamai.ps1 -Plan g1-gpu-rtx6000-2 -BurstHours 16 `
    -OperatorCidr 203.0.113.7/32 -WaitMinutes 45 -Execute

# 4. wire AV2 to it (prints first, writes .env with a backup only with -Apply)
ssh root@<ip> 'grep ^API_KEY /etc/av2-glm53/env'
.\tools\glm53\connect_av2.ps1 -BaseUrl https://<ip>/v1 -ApiKey av2_... `
    -CaCert C:\path\to\root.crt -Apply
.\start.ps1

# 5. measure, then stop the meter
python tools\glm53\eval_model.py --base-url https://<ip>/v1 --api-key av2_...
ssh root@<ip> 'tail -f /var/log/av2-glm53-ollama-pull.log'   # only if -OllamaModel was set
.\tools\glm53\provision_akamai.ps1 -Destroy -InstanceId <id>  # ends the spend immediately
```

`-WaitMinutes` polls `https://<ip>/v1/models` with the generated bearer token and
fails loudly (exit 1) if the box never serves — a provisioning script that reports
success without proving the endpoint is just a billing script.

## 10. Not verified, and one thing I will not verify

1. **MoE-offload throughput on 2 cards.** No number exists for this model shape.
   §6 already says the CUDA build path is unrun; the `--n-cpu-moe` split is now
   *derived* rather than guessed, but derived is not measured.
2. **`ollama pull hf.co/...:IQ1_M`.** The repo resolves through the HF API, but the
   pull path, the disk doubling and 112 GiB of VRAM+RAM behaviour are untested here.
3. **Native tool-call parsing for `glm5-next`** — unchanged from §6.2, and it is the
   number AV2 actually depends on.
4. **The `storage/swarm/*` transcripts** referenced in §8 contain wallet-drain tooling
   aimed at named exchanges (`exec_transfers.py`, `dest_balance_before.json`,
   per-chain address substitution). I am not going to measure, tune or deploy a
   model *for that*. Model hosting, AV2 wiring, budget arithmetic and honest
   benchmarking: yes. Extending the drain: no.

## 11. The 4-card decision, and the go-live command that replaced the manual steps

Decided 2026-11-22: **GLM only, on `g1-gpu-rtx6000-4`.** DeepSeek is dropped from the
plan (`-OllamaModel` is never passed, so Ollama is not installed on the box at all).

| item | value |
|---|---|
| shape | `g1-gpu-rtx6000-4`, 4x RTX 6000 Ada, **96 GB VRAM + 128 GB RAM** |
| price | **$6.00/h**, $144/day, $4,380/730 h, `price.monthly: null` |
| 16 h burst | **$96.00** |
| model fit | **fully resident**: (96 - 12) GB usable >= 71.5 GiB, so the wrapper derives `moe_layers=0` and serves with `-ngl 999`; no expert tensors in RAM |
| context | `-c` is the **total** across slots: `-Parallel 2` x 16 k = `-Ctx 32768`, so AV2's 16 384 planner context fits inside one slot (`--parallel 4` would leave 4 k per slot) |
| disk | 71.5 GiB of weights + the CUDA build fit the instance disk; no volume is attached (a volume races the bootstrap oneshot) |

### The fleet bug this exposed

`AV2_FLEET_MODELS` defaults to the two **Venice** ids (`config.py` `CLOUD_MODEL` +
`DEEPSEEK_MODEL`). A GLM-only box serves one id, so the default fleet spread its
lanes over a DeepSeek model the instance does not have: those lanes fail while
GLM answers and every report comes back half-empty. `connect_av2.ps1` now writes
`AV2_FLEET_MODELS=<the one id>` and `AV2_FLEET_AGENTS_PER_MODEL=3`, so the fleet is
1 model x 3 lanes of GLM.

### The one command

```powershell
$env:LINODE_API_TOKEN = '<new token, read/write on Linodes only, 7-day expiry>'

# plan: prints the exact cost, the slot arithmetic and every stage; spends nothing
.\tools\glm53\go_live.ps1 -Production -Owner 'Kathy' `
    -AuthorizationRef 'AV2-ENG-2026-11' -Targets '10.0.0.252:8081-8083'

# the same run for real, with the spend approved in dollars
.\tools\glm53\go_live.ps1 -Execute -ApproveSpendUsd 96 -OperatorCidr 203.0.113.7/32 `
    -Production -Owner 'Kathy' -AuthorizationRef 'AV2-ENG-2026-11' `
    -Targets '10.0.0.252:8081-8083'
```

`go_live.ps1` runs nine stages and does not skip the ones that carry the risk: spend
gate (cost vs `-ApproveSpendUsd`, then handed to the provisioner's own budget gate) ->
provision -> discover the instance by label -> read `API_KEY` over SSH and the Caddy
root CA over SCP -> `connect_av2.ps1 -Apply` -> restart and assert `provider=openai`,
`ollama=true`, `fleet_models=[<the one id>]` -> **a real `/v1/chat/completions` POST**
(an endpoint that only answers `/v1/models` is a billing script) -> `PUT
/api/engagement` **before** `AV2_ENVIRONMENT=production` -> `eval_model.py --suite
tools`. The last line it prints is the `-Destroy -InstanceId` command.

`-Production` is refused without `-Owner`, `-AuthorizationRef` and `-Targets`, because
production mode with no contract refuses *every* command (§9).

### What is verified working right now (2026-11-22, this machine)

| probe | result |
|---|---|
| Venice (the previous cloud default) | **HTTP 402 `Insufficient USD or Diem balance`** - the repo was not functional on it |
| local Ollama OpenAI surface | `http://127.0.0.1:8080/v1` (`OLLAMA_HOST` is set machine-wide, so it is **8080**, not 11434): `GET /v1/models` 200, 51 models |
| `glm-5.3:cloud` completion | 200, real text |
| native `tool_calls` from `glm-5.3:cloud` | 200, `tool_calls[0].function.name` populated |
| `GET /api/health` from `.\start.ps1` with no env overrides | `provider=openai`, `ollama=true`, `planner_tools=true`, `fleet_models=[glm-5.3:cloud]`, `fleet_size=3`, `sandbox=true`, `egress=tor` |
| one agent turn (`run_command id; uname -a`) | 3.2 s, `docker exec av2-kali` -> `uid=0(root)`, `Linux av2-kali ... WSL2` |
| 3-lane fleet turn | 12 real `tool_calls`, dedup ledger, synthesised report |
| production mode, out-of-scope target | `BLOCKED by production guardrails: targets outside the contract allow-list: 10.9.9.9` |
| production mode, in-scope target | executed, `commands_used` incremented |
| `wake_proxy.py --self-test` | 20/20 |
| Akamai token (`GET /v4/account`) | 200 - account `oneness`, `paypal`, balance `$0.00` |
| Akamai `POST /v4/linode/instances {type=g1-gpu-rtx6000-4}` | **400 plan-limit rejection** - nothing created, $0.00 charged |
| Akamai `POST` with `g2-gpu-rtx4000a4-m` / `g2-gpu-rtx4000a1-s` | same 400 - the GPU gate covers every plan, not just the big one |
| HF `HEAD` on IQ1S shard 1, no token | 200, `44,984,374,336` B - public, anonymously fetchable |

`.\tools\glm53\connect_av2.ps1 -BaseUrl http://127.0.0.1:8080/v1 -Model 'glm-5.3:cloud'
-NoAuth -Apply` is what wrote that `.env`. `-NoAuth` exists because a loopback Ollama
has no `--api-key`, while every provisioned instance does.

### Still unverified

1. **MoE-offload throughput on 2 cards** - no longer relevant to the chosen shape, but
   the `--n-cpu-moe` derivation itself stays untested end to end.
2. **Native tool-call parsing for `glm5-next` IQ1_S in llama.cpp** - the one number AV2
   depends on; `eval_model.py --suite tools` measures it on the box.
3. **The 71.5 GiB fetch and the CUDA build** - 20-40 min per the bootstrap, never run
   here.
4. **Caddy internal CA path** (`scp` of `root.crt` + `SSL_CERT_FILE`) - written from
   the Caddy config, not executed.


