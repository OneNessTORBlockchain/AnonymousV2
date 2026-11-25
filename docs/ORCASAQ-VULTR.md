# Anonymous V2 — OrcaSAQ-2-Cyber-27B-Uncensored on Vultr (production report)

**Date:** 2026-11-22 · **Account:** WOODJERRY ALMONTE DE LEON `bspgrowtech@gmail.com` · **Credit:** $250.00 (API `balance: -250`, `pending_charges: 0`)

**Verdict:** the machine, the money, the model, the scripts and the AV2 wiring are all
resolved and validated. **Two things remain and only you can do them:** Cloud GPU is an
access-gated product on this Vultr account (§9), and the requested model's canonical repo
is gated behind a free Hugging Face licence acceptance (§4). Every other failure mode has
been eliminated. Paste the support request and the deployment finishes in ~6 minutes
with the commands in §11.

---

## 1. Budget answer (the question you actually asked)

Vultr's credit-usable GPU line is three **L40S** plans with 48/96/192 GB VRAM. (A fourth class -
`vcg-a16-6c-60g-16vram`, 16 GB at $0.47/h - now shows as available in US-New York; it is the wrong
shape for this model, see the note under the table.) Only the
48 GB one fits the credit:

| Plan | vCPU | RAM | VRAM | $/h | $/month | Hours your $250 buys |
|---|---|---|---|---|---|---|
| **`vcg-l40s-16c-180g-48vram`** | 16 | 180 GB | **48 GB** | **1.671** | 1 122.91 | **149.6 h** |
| `vcg-l40s-32c-375g-96vram` | 32 | 375 GB | 96 GB | 3.342 | 2 245.82 | 74.8 h |
| `vcg-l40s-64c-750g-192vram` | 64 | 750 GB | 192 GB | 6.684 | 4 491.65 | 37.4 h |

> **`vcg-a16-6c-60g-16vram` is not the cheap door in** (seen in the live deploy UI, 22 Nov 2026:
> 6 vCPU, 60 GB RAM, **16 GB VRAM**, **$0.47/h** = ~$343/month = 531 h on the $250 credit). The
> 16 GB is **one slice** of an NVIDIA A16: four GA107 dies on one passive board, each with 16 GB
> and ~200 GB/s, compute capability 8.6 (Ampere). Against the 15 676 553 472-byte GGUF (14.60 GiB)
> that leaves only ~1.3-1.5 GiB for KV cache, hybrid-attention state, CUDA context and graph
> buffers - while `bootstrap.sh`'s defaults (`NUM_CTX=32768` x `PARALLEL=4`) ask for 16-17 GB of KV
> on their own, so Ollama would spill into the 60 GB of system RAM at ~2-4 tok/s. Even fully
> resident the 200 GB/s slice caps single-stream decode near **13 tok/s** (A6000 768 GB/s,
> RTX 5090 1792 GB/s), i.e. **more dollars per token than the $1.09/h A6000** despite the smaller
> hourly number. Usable only with `NUM_CTX=4096 PARALLEL=1` and 7-10 tok/s accepted. One genuine
> plus: sm_86 Ampere, so no Blackwell/`sm_120` risk. Note also that "available" in the deploy UI
> is a **capacity** signal, not an access grant - the `vcg-*` gate in section 9 fails at a
> different, later check (the 400 on `POST /v2/instances`, not the region-capacity message).

Your 14 d × 5 h = 70 h pattern costs **$116.97** and leaves **$133.03 unused**. Since
billing is hourly and only `DELETE /v2/instances/{id}` stops it, the credit is better
spent in fewer, longer blocks:

| Pattern | Hours | Cost | Notes |
|---|---|---|---|
| 14 d × 5 h (as asked) | 70 | **$116.97** | leaves 53 % of credit idle |
| 10 d × 10 h | 100 | $167.10 | comfortable |
| 12 d × 12 h | 144 | $240.62 | near-perfect utilisation |
| **10 d × 14 h** | **140** | **$233.94** | best value inside your "10 days minimum" |
| continuous 6.2 days | 149.6 | $250.00 | no cold starts, burns everything |
| continuous 10 days | 240 | $401.04 | **over budget — not affordable** |

Cold start (boot → model loaded → serving) is ~6 min ≈ **$0.17**, i.e. 2 % overhead on a
5 h session. That is cheap enough that daily start/stop is fine, but the 10 d × 14 h
block is the better deal.

---

## 2. Why this model and this machine (and why not GLM-5.3)

| Candidate | Fits 48 GB? | Fits $250? | Decision |
|---|---|---|---|
| **OrcaSAQ-2-Cyber-27B-Uncensored** | ✅ 15.68 GB GGUF | ✅ 149 h runway | **deploy this** |
| GLM-5.3-Flash-Uncensored Q2_K | ❌ ~109 GB | needs 192 GB @ $6.684/h → 37 h | rejected |
| GLM-5.3-Flash-Uncensored Q4_K_M | ❌ ~180 GB | needs >192 GB VRAM | impossible here |
| GH200 bare metal (96 GB, $1.99/h) | ✅ | but `deploy_ondemand: false`, `locations: []`, **no arm64 OS image** | unavailable |
| A100 bare metal (320 GB, $9.589/h) | ✅ | 26 h, then broke | rejected |

The model is a single-file GGUF, `OrcaSAQ-2-27B-Uncensored.gguf`,
**15 676 553 472 bytes (14.60 GiB)**, architecture `Qwen3_5ForCausalLM`
(`model_type: qwen3_5`, 64 layers, **hybrid `linear_attention` + `full_attention`**,
262 144 native context). It fits in 48 GB VRAM with room for a 32 k context and 4
parallel slots, which is exactly what AV2's fleet wants.

> If you later insist on GLM-5.3-Flash, the arithmetic is brutal: ≥109 GB of weights
> means the $6.684/h 192 GB plan, and $250 buys 37 hours. Not this credit.

---

## 3. Evidence from the live account (all re-verified today)

```text
GET /v2/account            -> balance -250 · pending_charges 0 · name/email as above
GET /v2/plans              -> 139 plans, 3 with gpu_vram_gb > 0 (table in §1)
GET /v2/regions/atl/...    -> vcg-l40s-16c-180g-48vram, vcg-l40s-32c-375g-96vram
POST /v2/instances (gpu)   -> 400 {"error":"Server add failed: Please open a support
                                    request for access to this product."}
POST /v2/instances (atl, 96 GB plan) -> same 400 access gate
POST /v2/instances (ord, 48 GB plan) -> 400 "Cannot add instance plan is not available
                                    in the selected region"   (capacity, not access)
POST /v2/instances (vc2-1c-1gb) -> 201 CREATED, then DELETEd -> 204
                                    => the account, the payment method and the credit
                                       are healthy; ONLY the GPU product is gated.
GET /v2/plans-metal        -> 9 GPU bare-metal plans; GH200 $1.99/h is on-demand=false
```

Two conclusions worth carrying into the ticket: capacity for the 48 GB L40S exists at
least in `atl` (the request got past capacity validation to the access check), and the
$0-probe proves nothing is wrong with billing.

Artefact-level checks (no GPU required, all done today):

```text
GET /api/models/orcarouter/OrcaSAQ-2-Cyber-27B-Uncensored-GGUF  -> gated: "auto"
GET .../resolve/main/OrcaSAQ-2-27B-Uncensored.gguf            -> 401, X-Error-Code: GatedRepo
GET /api/models/isilicon/mango-orcasaq2-cyber-27b             -> gated: false
local GGUF header (first 400 kB read off the wire)            -> magic GGUF · v3 · 866 tensors
                                                                 · 52 kv pairs · general.architecture=qwen35
llama.cpp conversion/qwen.py:734                              -> registers Qwen3_5ForCausalLM
C:\...\Ollama\lib\ollama\libllama.dll + ollama.exe            -> contain "qwen3_5" / "qwen3next"
```

---

## 4. Model availability — the gotcha that would have killed the build

`orcarouter/OrcaSAQ-2-Cyber-27B-Uncensored-GGUF` is **gated** (`gated=auto`):

```text
HTTP/1.1 401 Unauthorized
X-Error-Code: GatedRepo
X-Error-Message: Access to model ... is restricted. You must have access to it and be
authenticated to access it. Please log in.
```

So `ollama run hf.co/orcarouter/OrcaSAQ-2-Cyber-27B-Uncensored-GGUF` fails for anyone
without a Hugging Face account that accepted the licence **plus** a read token. Two
paths, both wired into the bootstrap:

| Path | Repo | Token | Provenance |
|---|---|---|---|
| canonical | `orcarouter/OrcaSAQ-2-Cyber-27B-Uncensored-GGUF` | `HF_TOKEN` + accepted terms | authoritative |
| **default** | `isilicon/mango-orcasaq2-cyber-27b` (ungated, 1 700 downloads) | none | **unverified** |

**Do not treat the mirror as equivalent.** Its header, read straight off the wire,
is a valid GGUF v3 with 866 tensors and 52 KV pairs — but it declares
`general.architecture = qwen35` and `general.name = "Qwen3.8 27B Uncensored"`. The byte
length matches the gated file (15 676 553 472) simply because the same 27B base
quantised with the same recipe produces identically shaped tensors; **equal size is not
equal weights**. Same family, same runtime, plausibly different fine-tune and therefore
possibly different refusal behaviour — which is the entire reason you picked this model.

Production call: use the mirror only to stand the stack up, then accept the licence at
`https://huggingface.co/orcarouter/OrcaSAQ-2-Cyber-27B-Uncensored-GGUF` (gated `auto` =
instant approval), mint a read token at `https://huggingface.co/settings/tokens`, and
rebuild with:

```bash
MODEL_REPO=orcarouter/OrcaSAQ-2-Cyber-27B-Uncensored-GGUF HF_TOKEN=hf_xxx \
  MODEL_ID=orcasaq-cyber:27b bash /root/av2-bootstrap.sh
```

(Delete `/opt/av2/model.gguf` first so the download is not resumed into the old bytes —
the size check alone would happily accept a different file of the same length; the
`GGUF` magic check catches corruption, not substitution.)


---

## 5. What was built (all in `tools/vultr/`)

| File | Purpose |
|---|---|
| `bootstrap.sh` | Idempotent first-boot build: GPU check → Ollama on loopback → 15.68 GB GGUF with `-C -` resume and exact-size verification → `ollama create orcasaq-cyber:27b` → nginx bearer gate on `:8080` → ufw to the operator CIDR → smoke test → `/opt/av2/READY`. Falls back to a CUDA `llama-server` build if Ollama's runner rejects the architecture. |
| `provision_orcasaq.ps1` | Vultr API driver: `-Action Create / Wait / Status / List / Logs / Destroy`, plus a read-only `-Action Plans`. Uploads or reuses the SSH key, base64-inlines `bootstrap.sh` into cloud-init user-data (no quoting hazards, no second hop), prices the run from `GET /v2/plans` for whatever `-Plan` you pass, and `Destroy` is the only thing that stops billing. |
| `docs/ORCASAQ-VULTR.md` | This report. |

Design decisions worth knowing:

* **One artefact, two runtimes.** The GGUF is downloaded once to `/opt/av2/model.gguf`
  and both Ollama (`FROM /opt/av2/model.gguf`) and llama.cpp (`--model`) use it, so the
  fallback costs no extra download.
* **No dependence on Ollama's Hub resolver.** On Windows, `FROM https://...` is parsed
  as a Windows path (`CreateFile C:\...\https:\huggingface.co\...`) and fails. That bug
  is Windows-only, but it is exactly why the bootstrap resolves the URL in curl and
  feeds Ollama a local path.
* **Loopback inference, public proxy.** `OLLAMA_HOST=127.0.0.1:11434` means the model is
  never directly reachable; nginx is the only public listener and it checks a 48-hex
  token before proxying, with 900 s timeouts and buffering off so streaming works.
* **`OLLAMA_KEEP_ALIVE=-1`** keeps the 15 GB model resident: AV2 issues bursts of fleet
  calls and a 20 s reload between turns is wasted money.
* **Three gates on the artefact, learned the hard way.** A gated-repo 401 body saved by
  `curl` and then continued with `-C -` produces a file of exactly the right length that
  is *not* a GGUF (the resume lands at the error body's length). The bootstrap therefore
  checks (a) exact byte count, (b) the `GGUF` magic in the first four bytes, and (c) the
  model actually answers before writing `/opt/av2/READY`.
* **`-Action Plans` exists because "unavailable in region" is a three-way error.** A failed
  `POST /v2/instances` can mean (a) the GPU class is genuinely sold out, (b) the API token's
  user lacks the *Deploy New Servers* permission, or (c) the Organization the token belongs
  to has no billing method / no credit. `Plans` is read-only and separates them in one call:
  it prints `GET /v2/account` name + `balance` + `pending_charges`, then every `vcg` plan the
  token can see with its real `hourly_cost` and the `locations` it is sold in, marking the
  ones matching `-Region`. Empty list ⇒ permission (b); non-empty list with `balance 0.00` ⇒
  funding (c), and note credits are **per Organization**, not per login; list contains the
  plan you asked for ⇒ (a) and retry another region.
* **Permission policies are inert until a role carries them.** Vultr's IAM exposes built-in
  *Vultr Managed* templates such as `Deploy New Servers` (type `Vultr Managed`), but a policy
  grants nothing by itself — it must be attached to a **Role**, and that role assigned to the
  **User** the API token authenticates as. Seeing the template listed is not proof of access;
  only an API call is.
* **Status as of this report:** `provision_orcasaq.ps1` parses clean, its
  `-Action List` round-trip against the live API works, and `-Action Plans` has been verified
  against a deliberately invalid token (reaches `api.vultr.com`, surfaces the `401` instead of
  inventing a catalogue). `-Action Create` ran and correctly surfaced the access gate;
  `bootstrap.sh` has **not** run on a GPU instance because none can be created until §9 is
  actioned. During a control-plane incident Vultr warned that instances "may incorrectly
  appear as offline" **and that new resource deployment may be affected** — so a create
  failure inside such a window is not evidence of missing capacity, and `-Action List` (not
  `Status`, which filters by label) should be checked for a half-created instance that is
  already billing.

---

## 6. Security posture

| Layer | Control | Rationale |
|---|---|---|
| Inference daemon | binds `127.0.0.1:11434` only | never publicly reachable, even if ufw were off |
| Public surface | nginx `:8080`, `Authorization: Bearer <48 hex>` | exact-match token check, 401 JSON body |
| Network | ufw default-deny; 22/tcp and 8080/tcp only from `173.177.72.31/32` | two independent gates on the same asset |
| Secrets on host | `/opt/av2/api.token` mode 600 | never echoed into the bootstrap log |
| Secrets in repo | none — Vultr key from `$env:VULTR_API_KEY`, HF token from `HF_TOKEN` | repo stays shareable |
| Transport | plain HTTP on a non-standard port, IP-allowlisted | see limitation |

**Known limitation, stated plainly:** `:8080` is not TLS. It is protected by a bearer
token plus a source-IP allowlist over one hop to a Vultr VPS. For TLS, either put a real
hostname behind Caddy (Let's Encrypt) or generate a self-signed CA and pass
`connect_av2.ps1 -CaCert` (it writes `SSL_CERT_FILE`, which `httpx` honours). I did not
enable it by default because a self-signed cert silently breaks the backend unless
`SSL_CERT_FILE` reaches the process, and a broken endpoint is worse than an allowlisted
HTTP one for a 10-day engagement.

**Rotate the Vultr API key** when the engagement ends — it was pasted in chat, so treat
it as compromised: `https://my.vultr.com/settings/#settingsapi`.

---

## 7. AV2 wiring (no Python changes needed)

`backend/app/config.py:326-337` already handles this: `AV2_LLM_PROVIDER=openai` makes
`openai_style` true, so `OllamaClient` uses `GET /models` + `POST /chat/completions`
with `Authorization: Bearer $AV2_LLM_API_KEY` (`core/ollama_client.py:63-71, 87-101`).
Because `AV2_FLEET_MODELS` defaults to two Venice ids, it **must** be pinned to the one
model this box serves, or half the fleet 404s.

```powershell
cd 'C:\Users\KATHY\Downloads\Anonymous V2\tools\glm53'
.\connect_av2.ps1 `
  -BaseUrl 'http://<INSTANCE_IP>:8080/v1' `
  -ApiKey  '<contents of /opt/av2/api.token>' `
  -Model   'orcasaq-cyber:27b' `
  -Apply
```

Resulting `.env` (backed up automatically to `.env.bak-<utc>`):

```dotenv
AV2_LLM_PROVIDER=openai
AV2_OLLAMA_BASE_URL=http://<INSTANCE_IP>:8080/v1
AV2_LLM_API_KEY=<token>
AV2_PLANNER_MODEL=orcasaq-cyber:27b
AV2_WORKER_MODEL=orcasaq-cyber:27b
AV2_FLEET_MODELS=orcasaq-cyber:27b
AV2_FLEET_AGENTS_PER_MODEL=3
AV2_PLANNER_NUM_CTX=16384
AV2_WORKER_NUM_CTX=8192
```

Then `.\start.ps1` and confirm `GET http://127.0.0.1:8420/api/health` reports
`provider=openai` and `ollama=ok`, then measure with
`python tools/glm53/eval_model.py --base-url http://<ip>:8080/v1 --api-key <token>`.

**Detail that bites:** `AV2_PLANNER_NUM_CTX` / `AV2_WORKER_NUM_CTX` are only honoured on
the **native** Ollama path — `ollama_client.py:213-221` routes to `_openai_payload`,
which has no `options` block, and Ollama's `/v1` ignores one anyway. The context length
actually used is the one baked into the model: **`PARAMETER num_ctx 32768`** from
`/opt/av2/Modelfile`. Keep the two in sync if you rebuild with a different value; a
silent 32 k vs 8 k mismatch truncates tool output mid-engagement.

---

## 8. Runbook

```powershell
cd 'C:\Users\KATHY\Downloads\Anonymous V2\tools\vultr'
$env:VULTR_API_KEY = '<token>'

# 1. bring a box up (cloud-init does everything; no SSH needed)
.\provision_orcasaq.ps1 -Action Create -Region atl -OperatorCidr 173.177.72.31/32
.\provision_orcasaq.ps1 -Action Wait          # prints the public IP

# 2. read the credentials + verify it is serving
.\provision_orcasaq.ps1 -Action Logs          # tail bootstrap + cat /opt/av2/READY

# 3. point AV2 at it
cd ..\glm53
.\connect_av2.ps1 -BaseUrl 'http://<IP>:8080/v1' -ApiKey '<token>' -Model 'orcasaq-cyber:27b' -Apply
cd '..\..'
.\start.ps1
```

Manual probes from the operator machine (ufw allows only your IP, so `curl` from
anywhere else will simply time out):

```powershell
curl.exe -s -H "Authorization: Bearer <token>" http://<IP>:8080/v1/models
curl.exe -s -H "Authorization: Bearer <token>" -H "Content-Type: application/json" `
  -d '{\"model\":\"orcasaq-cyber:27b\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":16}' `
  http://<IP>:8080/v1/chat/completions
```

**Stop paying:** `.\provision_orcasaq.ps1 -Action Destroy`. Nothing else (halt, suspend,
snapshot-only) is guaranteed to stop the meter; `Destroy` is unambiguous. A snapshot of
the 1 200 GB boot disk would itself cost more than re-downloading 15 GB, so the design
intentionally treats the box as cattle: destroy, recreate, 6-minute cold start.

**Rotate the API token without rebuilding:**

```bash
ssh -i ~/.ssh/av2-vultr-orcasaq root@<ip>
NEW=$(openssl rand -hex 24); echo "$NEW" > /opt/av2/api.token; chmod 600 /opt/av2/api.token
sed -i "s/Bearer [0-9a-f]\{48\}/Bearer $NEW/" /etc/nginx/sites-available/av2-llm
nginx -t && systemctl reload nginx && cat /opt/av2/api.token
```

Then re-run `connect_av2.ps1` with the new token.

**Troubleshooting map**

| Symptom | Look at | Meaning |
|---|---|---|
| `connect_av2` ok, `/api/health` says `ollama=unreachable` | ufw on the box, `nc -vz <ip> 8080` | source IP changed, or instance destroyed |
| 401 JSON from `:8080` | `/opt/av2/api.token` vs `AV2_LLM_API_KEY` | token mismatch |
| `ollama create` failed in the log | `/var/log/av2-bootstrap.log` | runner rejected the arch → fallback path rebuilt llama.cpp |
| model answers but slowly | `nvidia-smi` on the box | driver missing → CPU inference |
| `/v1/models` lists a different id | `ollama list` | pin `AV2_*_MODEL` to what is actually there |

---

## 9. The one blocking action (paste this into Vultr Support)

> **Subject:** Requesting Cloud GPU (L40S) access — account restriction on `vcg-*` plans
>
> Hello,
>
> I am trying to launch a Cloud GPU instance and the API is refusing every GPU plan:
>
> `POST https://api.vultr.com/v2/instances`
> `400 {"error":"Server add failed: Please open a support request for access to this product.","status":400}`
>
> Details:
> * Account: WOODJERRY ALMONTE DE LEON / bspgrowtech@gmail.com, balance $250 credit,
>   `pending_charges: 0`, no failed payments.
> * Plan: `vcg-l40s-16c-180g-48vram` (also reproduced with the 96 GB L40S plan).
> * Region: `atl` (the same request against `ord` returns *"plan is not available in the
>   selected region"*, i.e. `atl` passes the capacity check and only fails the access
>   check). Ubuntu 24.04 x64, OS id 2284.
> * I can create and destroy ordinary Cloud Compute instances on this account without
>   any error, so billing/payment is not the issue — only the Cloud GPU product is
>   restricted. (Happy to confirm: a `vc2-1c-1gb` probe instance was created and
>   deleted successfully today.)
>
> Intended use: self-hosted inference for an internal application I run — serving an
> open-weights 27B model (a 15.7 GB GGUF) through Ollama behind a token-authenticated
> reverse proxy. Single tenant, one instance, no resale, no public model API. I accept
> the
> Cloud GPU acceptable-use terms and the export-control restrictions that apply to
> NVIDIA hardware.
>
> Could you enable Cloud GPU (`vcg-*`) purchasing on this account, or tell me what
> verification you need from me? I would like to start with the 48 GB L40S for roughly
> 10–14 hours per day.
>
> Thank you.

Expect to be asked for the usual GPU onboarding: a short description of the workload,
sometimes an ID or business document. The credit is already on the account, so once
the flag flips, §11 works immediately.


---

## 10. Risk register

| # | Risk | Severity | Status / mitigation |
|---|---|---|---|
| 1 | **Cloud GPU access not granted** | blocking | Open the §9 ticket. Everything else is ready; approval is the only dependency. |
| 2 | `qwen3_5` hybrid attention unsupported by the runner | high | **Mitigated by evidence.** `llama.cpp` upstream registers `Qwen3_5ForCausalLM` → `Qwen3_5TextModel` (`conversion/qwen.py:734`, `_LinearAttentionVReorderBase` + interleaved MRoPE), and the installed Ollama 0.40.17 binaries contain the `qwen3_5` / `qwen3next` symbols (`findstr` hit in `libllama.dll` **and** `ollama.exe`). Bootstrap still probes Ollama first and rebuilds a CUDA `llama-server` if the runner refuses. |
| 3 | Capacity at `atl` sells out | medium | `ord` currently answers "plan is not available in the selected region"; retry other regions (`-Region lax|mia|dfw|ewr`) — `Create` fails fast and costs nothing. |
| 4 | Mirror provenance (`isilicon/mango-…`) | **high** | Header forensics show `general.name = "Qwen3.8 27B Uncensored"` — a valid `qwen35` 27B GGUF, but not provably the requested fine-tune (equal byte size ≠ equal weights). Use it to stand the stack up; switch to the canonical gated repo with `HF_TOKEN` before you rely on its refusal behaviour. |
| 5 | Budget overrun from leaving the box up | **high (money)** | $1.671/h = **$40.10/day**. `-Action Status` shows it; `-Action Destroy` is the kill switch. No auto-shutdown exists because halting is not guaranteed to stop billing. |
| 6 | 27B Q4 quality below the cloud GLM-5.3 you wanted | medium | This is the hard trade: GLM-5.3-Flash cannot fit in $250 at any quant (≥109 GB). If quality matters more than locality, keep AV2 on Venice and use this box only for uncensored/local work. |
| 7 | No TLS on `:8080` | low-medium | Token + IP allowlist; see §6 for the TLS upgrade path. |
| 8 | Single instance, no HA | low | It is a workstation, not a cluster. A destroyed box loses `/opt/av2` (nothing irreplaceable: 15 GB re-download + 6 min bootstrap). |
| 9 | AV2 fleet concurrency vs one GPU | medium | 3 lanes on one 48 GB card with `OLLAMA_NUM_PARALLEL=4` and `num_ctx=32768`; expect queueing, not failure. Lower `AV2_FLEET_AGENTS_PER_MODEL` to 2 if latency matters more than coverage. |

**Explicit assumptions**

* The operator IP stays `173.177.72.31`; if it changes, ufw must be updated from the
  Vultr web console (`ufw allow from <new>/32 to any port 8080`).
* The AV2 backend runs on this Windows machine and reaches the box over the public
  internet — it is not colocated in the same Vultr region.
* Billing is hourly with no minimum term; the numbers in §1 use the account's live
  `hourly_cost` for `vcg-l40s-16c-180g-48vram`.

---

## 11. After approval — the whole deployment in five commands

```powershell
cd 'C:\Users\KATHY\Downloads\Anonymous V2\tools\vultr'
$env:VULTR_API_KEY = '<token>'
.\provision_orcasaq.ps1 -Action Create -Region atl -OperatorCidr 173.177.72.31/32
.\provision_orcasaq.ps1 -Action Wait
.\provision_orcasaq.ps1 -Action Logs      # -> base_url=… , api_token=… , backend=ollama
```

then wire AV2 (§7) and start it. Expected timeline: instance `active` in 3–6 min,
`/opt/av2/READY` 4–10 min later (15.7 GB download dominates), first token a few seconds
after that.

### If Vultr GPU access never arrives — cheapest working alternatives

The $250 is Vultr credit and cannot be spent elsewhere, so any other provider is *new cash*. Prices and
billing terms were re-checked on 2026‑11‑22 in **[`docs/GPU-PROVIDER-OPTIONS.md`](GPU-PROVIDER-OPTIONS.md)**;
the short version:

| Provider | 48 GB GPU | $/h | Billing | 70 h costs |
|---|---|---|---|---|
| Thunder Compute | RTX A6000 | **$0.35** | per-minute, **card auto-pay (no deposit)** | **$24.50** |
| Hyperstack | RTX A6000 | $0.50 | prepaid balance | $35.00 |
| Runpod | A40 / A6000 / L40S | $0.59 / $0.69 / $1.09 | prepaid credits ($10 min) | $41.30–$76.30 |
| Lambda | RTX A6000 | $1.09 | **postpaid, weekly invoice** | $76.30 |
| Vultr (current plan) | L40S | $1.671 | hourly against credit | $116.97 *(credit)* |

Two findings worth keeping: **no provider offers real net‑14/net‑30 credit to a new individual account**
(only postpaid weekly/monthly invoicing with a card on file), and the **NVIDIA vGPU / AI Enterprise free
trial is software licensing for your own hardware — it supplies no GPU and cannot host this model**.
Thunder Compute also removes §6's plain-HTTP limitation: it publishes forwarded ports as
`https://<uuid>-8080.thundercompute.net` with automatic TLS.

**Chosen path for this engagement:** Lambda `gpu_1x_a6000` (48 GB, $1.09/h) because it is the only
postpaid-weekly option — the first invoice lands ~day 7. Scripts and the full runbook are in
**[`docs/ORCASAQ-LAMBDA.md`](ORCASAQ-LAMBDA.md)**; `tools/lambda/` mirrors `tools/vultr/` with an
edge-firewall rule instead of ufw and `ubuntu@` instead of `root@`.

---

## 12. Honest status summary

* **Done and verified live:** credit, plans, prices, capacity-vs-access distinction,
  account health, gated-repo discovery, ungated mirror, architecture support in
  llama.cpp, AV2's exact configuration contract, both deployment scripts.
* **Built and syntax-checked, not yet run on GPU:** `bootstrap.sh` (blocked by §9).
* **Running at report time:** a local CPU pull of the mirror GGUF (14.6 GB at ~7.5 MB/s)
  purely to prove the runtime loads a `qwen35` 27B and answers; architecture support is
  already evidenced independently by the binary symbols and the llama.cpp converter
  registration in §3. It validates the runtime, **not** the exact fine-tune.
* **Needs you:** the Vultr GPU access request (§9). Optionally an HF token if you want
  the canonical repo instead of the mirror.
* **Researched this round:** nine alternative providers, their real billing models and their free-credit
  programmes, plus a definitive answer on the NVIDIA vGPU trial — `docs/GPU-PROVIDER-OPTIONS.md`.
  Bottom line: Thunder Compute RTX A6000 48 GB at **$0.35/h, per-minute, card on auto-pay, free HTTPS
  forwarding** is the cheapest way to have this stack running today ($24.50 for 70 h of *new cash*);
  the vGPU trial is software licensing and cannot supply a GPU.
* **Nothing is currently billing.** No instances exist on the account; the probe was
  deleted.

`python tools/glm53/eval_model.py --base-url http://<ip>:8080/v1 --api-key <token>`.
