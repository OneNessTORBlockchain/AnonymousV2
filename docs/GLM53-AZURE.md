# Self-hosting GLM-5.3-Flash-Uncensored IQ1_S on Azure (wake on request)

Companion to `docs/GLM53-SELFHOST.md`, which covers the Akamai route. This one is
Azure-specific and is what `tools/glm53/azure-deploy.ps1` implements.

**State of this route: authenticated, nothing provisionable, blocked by the
subscription's offer.** `az` is installed and signed in, and the whole deployment
path runs against the real Azure APIs — but the subscription is a **Free Trial**
(`quotaId: FreeTrial_2014-09-01`, spending limit on), and that offer does not include
any GPU family. Every NC/ND/NV quota reads 0, every raise answers
`(ResourceNotAvailableForOffer)` — including plain CPU families — and ARM rejects even
a 16 GB T4 with `QuotaExceeded`. Nothing in this document can be created until the
subscription is upgraded to pay-as-you-go. Read §3.2 before planning a work session.
The infrastructure, relay and AV2 wiring are finished and validated offline (§9); the
GPU is the one thing still missing, and it is not something code can supply.

Artifacts:

| file | role |
| --- | --- |
| `tools/glm53/azure-deploy.ps1` | dry-run by default; `-Execute` creates NSG, static IP, VNet, NIC, VM |
| `tools/glm53/azure-cloud-init.yaml` | boot payload: NVIDIA driver, weights, llama.cpp, Caddy, systemd |
| `tools/glm53/wake_proxy.py` | the always-on relay: wake on request, deallocate on idle, spend ledger |
| `tools/glm53/azure-teardown.ps1` | deallocate (reversible) or delete everything |
| `tools/glm53/connect_av2.ps1` | points AV2's `.env` at any OpenAI-compatible base URL |
| `tools/glm53/eval_model.py` | refusal discrimination, native tool calls, TTFT/tok-s |

## 1. Shape of the deployment

```
AV2 backend (this host)                 Azure, resource group "anon"
  │
  ├─ GET /v1/models ──────────────►  wake_proxy.py :8430   (cached, instant)
  │
  └─ POST /v1/chat/completions ──►  wake_proxy.py
                                        │ az vm start  (only if deallocated)
                                        │ poll /health until ready
                                        └─ TLS ──► Caddy :443 ──► llama-server :8080 (loopback)
                                                                  1x RTX PRO 6000 Blackwell 96 GB,
                                                                  -ngl 999, -c 32768, --jinja
                                        │ idle disabled (always-on), or N min
                                        └ az vm deallocate
```

Two things are deliberate and worth stating up front:

* **The GPU is only ever reachable from the operator's CIDR.** NSG rules allow 22
  and 443 from one `/32` and deny everything else at priority 4000. There is no
  host firewall, because a second firewall on top of an NSG only creates a way to
  lock yourself out of SSH.
* **The relay is the only thing that can spend money.** `az vm start` happens in
  exactly one place, and it refuses to run once the local ledger passes
  `--credit-cap-usd`.

## 2. Verified facts (checked, with source)

Model, from the Hugging Face API on 2026-11-22:

| fact | value |
| --- | --- |
| repo gated? | **no** — `gated: false`, `private: false` |
| IQ1_S shard 1 | `GLM-5.3-Flash-Uncensored-IQ1S-00001-of-00002.gguf` — 41.89 GiB |
| IQ1_S shard 2 | `GLM-5.3-Flash-Uncensored-IQ1S-00002-of-00002.gguf` — 29.63 GiB |
| mmproj (never downloaded) | 1.05 GiB |
| weights actually needed | **71.52 GiB** |

Because the repo is public and ungated, **no Hugging Face token is needed**. The
token pasted into chat was not used anywhere in this package and should be revoked.

Prices, from the public Azure retail price API (`prices.azure.com`), same date.

Default target now — `Standard_NC24lds_xl_RTXPRO6000BSE_v6` (1× RTX PRO 6000 Blackwell
Server Edition, 96 GB, 24 vCPU, 72 GiB RAM, 1.6 TiB local NVMe), eastus, Linux:

| meter | USD/h |
| --- | --- |
| **pay-as-you-go** | **1.130** |
| Spot | 0.209 |
| Windows (same size) | 2.234 |
| 1-year reservation | 6,335 / yr ≈ 0.87 |

`Standard_NC24ads_A100_v4` (1× A100 80 GB), for comparison:

| region | pay-as-you-go | Spot | Low Priority |
| --- | --- | --- | --- |
| eastus | **3.673 USD/h** | **0.67877 USD/h** | 0.735 |
| westus2 | 3.673 | 0.74635 | 0.735 |
| centralus | 4.150 | 0.84328 | 0.830 |
| westeurope | 4.775 | 0.97028 | 0.955 |
| swedencentral | 4.775 | 1.03092 | 0.955 |

Managed disks, eastus, monthly: Premium `P10` 128 GiB **19.71**, `P15` 256 GiB
**38.01**, `P20` 512 GiB **73.22**; Standard SSD `E10` 128 GiB **9.60**.

AV2 side, read from the code rather than assumed:

| fact | value | source |
| --- | --- | --- |
| OpenAI path is active for provider `openai` | yes | `backend/app/config.py:326-337` |
| request timeout | **900 s** (`llm_timeout_s`) | `backend/app/config.py:93` |
| health probe timeout | 20 s, `GET /models` | `backend/app/core/ollama_client.py:72-81` |
| health result gates requests? | **no** — it is reported in `/api/health` only | `backend/app/main.py:105-110` |
| streaming + tool-call fragments | supported | `backend/app/core/ollama_client.py:395-520` |

That 900 s request timeout is what makes a multi-minute cold start survivable; a
15 s default would have made wake-on-request unusable.

## 3. Three gates before any spend

### 3.1 Authentication — resolved 2026-11-22

The identifiers supplied (subscription `63a43e4c-…`, tenant, client ID, object ID)
**cannot authenticate anything on their own**: `az` needs a signed-in user, a client
secret, or a federated credential. No secret was supplied, and none should ever be
pasted into a chat window. The device-code flow was used instead:

```powershell
winget install --id Microsoft.AzureCLI --accept-source-agreements --accept-package-agreements
# on this host the MSI's elevation prompt was never accepted, so the CLI came from
# the user site instead, which needs no admin rights and leaves ~/.azure in the same place:
python -m pip install --user azure-cli
az login --use-device-code          # approve the printed code at login.microsoft.com/device
az account set --subscription 63a43e4c-9301-438b-9efe-3ca8666c6198
```

Result, verified:

| fact | value |
| --- | --- |
| signed in as | `deleonrd23@outlook.com` (user, not service principal) |
| subscription | `63a43e4c-9301-438b-9efe-3ca8666c6198`, state `Enabled`, default |
| tenant | `b5910c75-…` (`deleonrd23outlook.onmicrosoft.com`) |
| resource group `anon` | exists, `eastus` |
| CLI | `az` 2.93.0 |

Two operational notes that cost time here and will cost time again:

* `winget install Microsoft.AzureCLI` downloads the MSI and then waits on a UAC
  prompt. If nobody is sitting at the machine, the install silently hangs forever
  with a 67.7 MB MSI in `%TEMP%\WinGet`. The pip route has no such gate.
* A newly installed CLI is **not on the PATH of already-running shells**, and child
  processes inherit the parent's environment, not the registry. That is why
  `azure-deploy.ps1` and `wake_proxy.py` both resolve `az` themselves (PATH, then the
  known install locations) instead of trusting `Get-Command az`.

### 3.2 GPU eligibility — the hard stop, measured to its root cause

Measured on 2026-11-22 against the live subscription, not remembered. The first
finding is the one that explains all the others:

| probe | result |
| --- | --- |
| `az rest ... /subscriptions/<id>?api-version=2022-12-01` → `subscriptionPolicies` | **`quotaId: FreeTrial_2014-09-01`**, `spendingLimit: On` |
| `az vm list-usage -l eastus` → `standardNCADSA100v4Family` | **0 / 0** vCPUs |
| same → `StandardNCdsRTXPRO6000BSEv6XLFamily` | **0 / 0** vCPUs |
| same → `Standard NCASv3_T4 Family` | **0 / 0** vCPUs |
| same → every CPU family (`standardESv5Family`, `standardDSv5Family`, …) | **0 / 4** vCPUs |
| `az vm list-skus -l eastus --size Standard_NC24ads_A100_v4` | exists, restriction `NotAvailableForSubscription` |
| `az vm list-skus -l eastus` → RTX PRO 6000 Blackwell sizes | **`restrictions: null`** (listed as available, quota still 0) |
| `az quota update --resource-name standardNCADSA100v4Family --limit-object value=24` | **`ResourceNotAvailableForOffer`** |
| same for `StandardNCdsRTXPRO6000BSEv6XLFamily` (RTX PRO 6000, 96 GB) | **`ResourceNotAvailableForOffer`** |
| same for `standardNCASv3_T4Family` (T4) | **`ResourceNotAvailableForOffer`** |
| same for `standardNVadsV710v5Family` | **`ResourceNotAvailableForOffer`** |
| same for **`standardESv5Family`** (plain CPU, not a GPU family at all) | **`ResourceNotAvailableForOffer`** |
| `az vm create --validate --size Standard_NC4as_T4_v3` | **`QuotaExceeded`**: *"exceeding approved Standard NCASv3_T4 Family Cores quota … Current Limit: 0, Current Usage: 0, Additional Required: 4"* |
| `az quota show` for all three GPU families in eastus2, westus2, westus3, centralus, northeurope | **0** in every region |

Read that combination carefully, because it changes what to do next:

* **The subscription is a Free Trial, not pay-as-you-go.** `quotaId: FreeTrial_2014-09-01`
  is the root cause. That offer excludes the NC, ND and NV families by design, which
  is exactly what `ResourceNotAvailableForOffer` reports when a quota raise is
  attempted — for *any* family, including CPU ones. Nothing in this document can be
  provisioned on it: not A100, not H100, not RTX PRO 6000, **not even the 16 GB T4**.
* **A previous claim in this file was wrong and is corrected here**: T4 sizes show
  `restrictions: null` in `list-skus`, which was read as "provisionable". It is not —
  their quota family (`Standard NCASv3_T4 Family`) is 0/0, and ARM preflight rejects
  `Standard_NC4as_T4_v3` with `QuotaExceeded`. SKU visibility and quota are two
  different gates; only the second one decides whether a VM can exist.
* **The 4-vCPU CPU cap closes the fallback too.** The largest creatable VM of any
  kind is 4 vCPU / 32 GiB (`Standard_E4as_v5`). 71.52 GiB of weights + KV cache do
  not fit, so "just run it on CPU for now" is not available either.
* **The fix is a subscription action, not a code change.** Portal → *Subscriptions* →
  this subscription → **Upgrade to pay-as-you-go**. That clears the spending limit and
  makes GPU quota requestable. Afterwards, either raise the family quota (Portal →
  *Quotas* → *Compute*) or file *Help + support* → *Service and subscription limits
  (quotas)* for `StandardNCdsRTXPRO6000BSEv6XLFamily` = 24 vCPUs. Roughly two minutes
  of operator time; it cannot be driven by `az` and must not be faked.

`azure-deploy.ps1` now reads `subscriptionPolicies.quotaId` **before** it plans
anything, prints this evidence, and exits **3** with nothing created. It also derives
the quota family from the SKU's own `family` field instead of assuming
`NCADSA100v4` — matching the wrong family reports a healthy limit for a VM that
cannot be built. The `NotAvailableForSubscription` and zero-limit checks remain
behind that gate for the post-upgrade case.

What the same inventory says once quota exists (prices from the retail price API,
Linux, eastus, pay-as-you-go):

| size | GPU | VRAM vs 71.52 GiB | price |
| --- | --- | --- | --- |
| `Standard_NC24lds_xl_RTXPRO6000BSE_v6` | 1× RTX PRO 6000 Blackwell SE | 96 GB — **whole model in VRAM** | **$1.130/h** |
| `Standard_NC36ds_xl_RTXPRO6000BSE_v6` | same card, 36 vCPU / 132 GiB RAM | 96 GB | ~$1.65/h |
| `Standard_NC24ads_A100_v4` | 1× A100 80 GB | 80 GB — fits, ~7.6 GiB headroom | $3.673/h |
| `Standard_NC16as_T4_v3` | 1× T4 | 16 GB — cannot hold it | $1.204/h |

The Blackwell part is the reason this script's default changed: it fits the model
*entirely in VRAM* (no CPU offload, no per-token disk I/O) at **less than a third of
the A100's hourly rate** — 177 GPU-hours for the $200 instead of 54.

### 3.3 Cold start — the cost of paying nothing while idle

A deallocated VM keeps its disk and loses its RAM, so every wake re-reads 71.52 GiB
from managed storage. Expected, not measured (§9):

| OS disk | size | throughput | load 71.5 GiB | monthly |
| --- | --- | --- | --- | --- |
| Premium P10 | 128 GiB | 100 MB/s | ~12 min | 19.71 |
| Premium P15 | 256 GiB | 125 MB/s | ~10 min | 38.01 |
| Premium P20 | 512 GiB | 150 MB/s | ~8 min | 73.22 |

Add ~60-90 s for the VM to start. The relay's `--wake-timeout 900` matches AV2's 900 s
request timeout, so the *first* request after a long sleep is slow but should not
fail.

**Production does not run this way.** The deployment described below keeps the GPU up
(`-IdleMinutes 0`, relay `--idle-minutes 0`) precisely so that response time never
depends on re-reading 71.5 GiB. Cold start is documented here because it is what
happens after a deliberate stop, a reboot of the host, or an eviction — not because it
is the normal path.

### 3.4 Production posture

| requirement | how it is met | where |
| --- | --- | --- |
| keep the GPU running | deploy default `-IdleMinutes 0`; relay `--idle-minutes 0` never calls `az vm deallocate` | `azure-deploy.ps1`, `wake_proxy.py` |
| no idle auto-shutdown | `az vm auto-shutdown` only runs if `-AutoShutdownTime` is passed, and nothing passes it | `azure-deploy.ps1` |
| survive a crash / reboot | `Restart=always`, `RestartSec=10`, `StartLimitIntervalSec=0` (no "5 starts then give up"), `TimeoutStartSec=2400`, unit is `enable`d | `azure-cloud-init.yaml` |
| survive a long generation | `--timeout 900` on llama-server, Caddy `read_timeout/write_timeout 15m`, `TimeoutStopSec=180` | both |
| 16K context *per request* | `CTX_PER_SLOT=16384` × `PARALLEL=2` → `-c 32768`; llama-server splits `-c` across slots | `azure-cloud-init.yaml` |
| persistent static IP | `az network public-ip create --sku Standard --allocation-method Static`, created *before* the VM so the Caddy site address is baked into cloud-init | `azure-deploy.ps1` |
| HTTPS | Caddy `tls internal` on 443, llama-server on loopback only, relay pins the CA with `--ca-cert` | `azure-cloud-init.yaml`, `wake_proxy.py` |
| SSH restricted | NSG 22/tcp and 443/tcp from the operator `/32` only, deny-all at 4000 | `azure-deploy.ps1` |
| weights persist across reboots | 128 GiB Premium OS disk, `av2-glm53-fetch` skips shards already present and verifies SHA256 before the server may start | `azure-cloud-init.yaml` |

**The VRAM question, stated honestly.** On the default part (RTX PRO 6000 Blackwell,
96 GB) 71.52 GiB of weights leaves ~22 GiB for CUDA context, compute buffers and KV
cache, so two 16K slots is comfortable and there is nothing to tune. On an A100 80 GB
(~79.1 GiB usable) the same weights leave ~7.6 GiB, which is *very likely* enough for a
model of this size class — its KV is cheap per token in the MLA-style architectures
this size implies — but it is **not verified until the server actually loads**. If it
does not, fall back in this order and change nothing else:

1. `PARALLEL=1` → `-c 16384` (one request at a time, full 16K);
2. `CTX_PER_SLOT=8192` with `PARALLEL=2` → `-c 16384` total;
3. a 94 GB H100 (`Standard_NC40ads_H100_v5`) — a different SKU and a different quota
   request, and again out of reach on this subscription for the same offer reason.

`-ngl` is set from the same table, so a 16 GB T4 would load with `-ngl 0` rather than
failing to fit: the deploy script refuses to promise an offload the card cannot hold.

A smaller *model* is not on that list, on purpose.

**Controlled shutdown** (the deliberate counterpart to keeping it up):

```powershell
# 1. stop accepting work: stop AV2, then the relay, so nothing re-wakes the VM
# 2. deallocate the GPU; the disk (and its 71.5 GiB of weights) survives
.\tools\glm53\azure-teardown.ps1 -Execute
# 3. confirm the portal reads "Stopped (deallocated)"; only the disk still bills
```

`POST /__relay/sleep` does the same thing without leaving the relay running, and
refuses to fire while a request is in flight.

## 4. What the 200 USD actually buys

Prices are the live retail figures the deploy script prints, eastus, Linux, PAYG,
read from `prices.azure.com` on 2026-11-22.

| | RTX PRO 6000 Blackwell (default) | A100 80 GB |
| --- | --- | --- |
| hourly | **1.130 USD** | 3.673 USD |
| 24 h continuous | 27.12 USD | 88.15 USD |
| 30 days continuous | 813.60 USD | 2,644.56 USD |
| GPU-hours from a 200 USD credit | **177 h ≈ 7.4 days** | 54.5 h ≈ 2.27 days |
| disk while deallocated (P10, 128 GiB) | 0.66 USD/day | 0.66 USD/day |

The uncomfortable row survives the swap: **a 200 USD credit cannot fund a
continuously running 96 GB GPU for a month either.** 24/7 is affordable for about
7.4 days on the Blackwell part and 2.3 days on the A100. The relay's ledger deallocates
the GPU when it estimates the cap is reached, mid-week, exactly as instructed — not as
a surprise. Continuous availability therefore needs a bigger credit or a stop policy
chosen on purpose.

What the Blackwell part buys for the *smaller* price is the point of choosing it:
96 GB holds all 71.52 GiB of IQ1_S weights, so `-ngl 999` offloads every layer and no
token is generated through the CPU or the disk. The A100 has 80 GB and fits with about
7.6 GiB to spare, which is where the context/layer-count tuning in §3.4 comes from.

The order behind this configuration explicitly rules out Spot, so the ~5x-cheaper spot
meter (0.209 USD/h for the same Blackwell size, ≈ 957 GPU-hours from the same credit)
is not on the table even though the relay tolerates eviction — an evicted VM reads as
`VM deallocated` and the next request starts it again. If eviction risk becomes
acceptable, `-UseSpot` and `--hourly-usd 0.209` are the only two changes.

Disk keeps billing while deallocated: **0.66 USD/day** for P10, **1.28 USD/day** for
P15, **2.44 USD/day** for P20. Over a month that is 20-73 USD of the credit spent on
nothing but storage. If the box will sit unused for a week, delete it
(`azure-teardown.ps1 -DeleteAll`) and accept a 15-30 minute rebuild next time.

## 5. Quickstart

```powershell
cd 'C:\Users\KATHY\Downloads\Anonymous V2'

# 1. prove the relay logic with no Azure account at all (stubs az + llama-server)
python tools\glm53\wake_proxy.py --self-test

# 2. price and quota check; creates nothing.
#    On this subscription this step exits 3 after printing the offer evidence:
#    the Free Trial offer has no GPU family, so steps 3-5 cannot run yet.
.\tools\glm53\azure-deploy.ps1 -OperatorCidr auto

# 3. create it (NSG, static IP, VNet, NIC, VM)
.\tools\glm53\azure-deploy.ps1 -OperatorCidr auto -Execute

# 4. relay on this host, pointing at the VM's Caddy front end (always-on)
python tools\glm53\wake_proxy.py `
  --upstream https://<public-ip>/v1 --upstream-key <key> `
  --relay-key <key> --resource-group anon --vm-name av2-glm53 `
  --idle-minutes 0 --hourly-usd 1.130 --credit-cap-usd 200 --warn-usd 150

# 5. point AV2 at the relay, then restart the backend
.\tools\glm53\connect_av2.ps1 -BaseUrl http://127.0.0.1:8430/v1 -ApiKey <relay key> -Apply
.\start.ps1
```

Then verify with a **real completion**, not just a green pill:

```powershell
curl http://127.0.0.1:8430/__relay/state     # vm_state, estimated_usd, idle countdown
python tools\glm53\eval_model.py --base-url http://127.0.0.1:8430/v1 --api-key <relay key> --suite tools
```

`GET /api/health` showing `provider=openai, ollama=ok` is **not** proof of anything.
The Venice endpoint answered `/v1/models` with 200 while every completion returned
402. That is exactly why `/v1/models` here is served from cache: AV2 probes it with a
20 s timeout, so it can only ever describe the deployment, never the GPU. The truth
lives in `/__relay/state` and in the `X-AV2-VM-State` header.

## 6. The relay in detail

`wake_proxy.py` is stdlib-only (no `httpx`, no `fastapi`) so it runs on the AV2 host
as-is. Endpoints:

| endpoint | behaviour |
| --- | --- |
| `GET /v1/models` | cached model list, 200, **never** wakes the VM |
| `POST /v1/chat/completions` | wakes if needed, waits for `/health`, streams the reply |
| any other `/v1/*` | same passthrough |
| `POST /__relay/wake` | pre-warm: start and wait, e.g. before a working session |
| `POST /__relay/sleep` | deallocate now and bank the accrued time; **409** if a request is in flight |
| `GET /__relay/state` | truth: `vm_state`, `estimated_usd`, `idle_seconds_until_sleep`, counters |
| `GET /health` | relay liveness + current VM state |

Design decisions worth knowing:

* **One wake at a time.** A mutex plus a "ready" event means ten concurrent requests
  trigger one `az vm start` and all ten proceed when `/health` goes green.
* **The watchdog cannot kill a live stream.** It skips while `in_flight > 0`, and
  both the start and the end of a request refresh `last_request_at`, so a 6-minute
  generation is never mistaken for idleness. At `--idle-minutes 0` the idle half of
  the watchdog is switched off altogether rather than being given a 0-second window —
  the "deallocate immediately" version of that bug is exactly what a naive `0` would
  have meant.
* **The ledger banks across restarts.** `wake_proxy.state.json` stores
  `billed_seconds`, and a `started_at` left over from a crash is folded into it on
  load — a crashed relay does not get a free hour.
* **The ledger is a tripwire, not an invoice.** It counts hours *this relay* started
  times `--hourly-usd`. Hours started from the portal are invisible to it. Check the
  real number with `az consumption usage list` or the portal's Cost analysis.
* **Upstream errors are forwarded, not translated.** If llama.cpp returns 400, AV2
  sees that 400 and its own message. A 5xx also clears the ready flag so the next
  request re-polls `/health` instead of trusting a dead server.

Spend guard: `--credit-cap-usd` (default 200) makes an inference request return
**403** rather than start the GPU. In an always-on deployment nothing wakes the GPU
again, so a start-time-only check would never fire at all — that is why the watchdog
now enforces the same cap every 10 s against the running VM, and deallocates when the
ledger crosses it. It holds off while a request is in flight and reports that it is
holding off, so the emergency stop can never truncate a generation. `--warn-usd`
(default 150) only logs.

The ledger has to survive two cases to be worth trusting: a crash (a leftover
`started_at` is folded into `billed_seconds` on load) and a relay restart while the VM
is still up (the relay *adopts* it — sets a start time without counting a wake —
otherwise `usd()` would freeze and the cap would silently stop working). Both are
asserted in the self-test.

All of this is local; restarting the relay with a bigger cap is the override, which is
why the state file matters.

## 7. Security posture

* **NSG only**, allow 22/tcp and 443/tcp from `-OperatorCidr`, deny all else at 4000.
  The script refuses `0.0.0.0/0` and refuses anything that is not a CIDR.
* **llama-server binds `127.0.0.1`**, never `0.0.0.0`. Caddy is the only 443 listener.
* **TLS is real but private**: Caddy `tls internal` issues from its own local CA for
  the static IP. Pin it with `wake_proxy.py --ca-cert`; do not disable verification.
* **Bearer auth twice**: AV2 presents `--relay-key` to the relay; the relay presents
  `--upstream-key` to llama-server. Neither is the other.
* **Secrets**: the API key lives in `/etc/av2-glm53/env` (0600 root) on the VM and in
  your shell history otherwise. **No Hugging Face token is stored anywhere**, because
  none is needed.
* **The Azure identifiers are not secrets** (subscription, tenant, client and object
  IDs are visible to anyone who can read the account), but a client *secret* or
  password would be. Do not paste one into a chat.

## 8. What is NOT verified

Say it plainly, because the last route looked healthy right up until it billed:

1. **Nothing was provisioned, and nothing was billed.** Not for lack of trying: the
   subscription is a Free Trial whose offer includes no GPU family at all (§3.2).
   No VM, no IP, no disk, no key. `az vm list` is empty and resource group `anon`
   holds only a managed identity and an SSH public key that predate this work.
2. **No inference has ever run on this model.** No weights were downloaded, no
   `llama-server` was started against them, so every latency number in the delivery
   report is *absent*, not estimated.
3. **The VRAM budget is arithmetic, not a reading.** On the Blackwell part 71.52 GiB
   of weights against 96 GB is comfortable; on the A100 it is ~7.6 GiB left over out
   of 79.1 GiB usable, which is a calculation, not an `nvidia-smi` line. The server
   either loads at `-c 32768` or it does not, and only a GPU can say which.
4. **No llama.cpp b11279 build was attempted** on either the default CUDA 12.8 / sm_120
   (Blackwell) or CUDA 12.4 / sm_80 (A100). The tag *does* exist — verified against the
   GitHub refs API, commit `649dcb1` — but "the tag exists" and "it compiles and speaks
   this GGUF" are different claims.
5. **Cold-start times in §3.3 are arithmetic from published disk throughput**, not
   measurements.
6. **CUDA toolkit install time** on first boot is unmeasured; budget 10-15 minutes of
   GPU billing for it.
7. **The consumption budget call** may fail on non-EA subscriptions; the script treats
   that as non-fatal on purpose.
8. **No end-to-end AV2 run** against this endpoint: `eval_model.py` has only been
   exercised against a local stub, never against this relay or a real GPU.

## 9. What was actually validated here

### 9.1 Relay — 20/20 checks, against stubs

`python tools\glm53\wake_proxy.py --self-test` writes a fake `az` and a fake
llama-server into a temp directory and drives the real code paths. The harness now
counts its own assertions and prints the total, because the number in this heading was
previously written from memory (it said 22) while the run produced 20 — the run is the
authority. The last six exist because the production policy (always-on, hard cap) is
new:

```
[PASS] models rejects a missing relay key
[PASS] models answers from cache with 200
[PASS] models does not start the VM
[PASS] VM state header is present
[PASS] state reports the VM deallocated before any request
[PASS] inference is proxied after waking the VM
[PASS] az vm start called exactly once
[PASS] boot wait really polled /health
[PASS] state flips to running
[PASS] ledger persisted to disk
[PASS] watchdog deallocates after the idle window
[PASS] state flips back to deallocated
[PASS] billed seconds banked after sleep
[PASS] sleep counter recorded
[PASS] always-on relay still serves after the VM was deallocated
[PASS] --idle-minutes 0 never deallocates an idle GPU
[PASS] spend cap does NOT deallocate under an in-flight request
[PASS] /__relay/sleep refuses while a generation is in flight
[PASS] spend cap deallocates the GPU once it goes quiet
[PASS] reconcile adopts an already-running VM (cap can then still fire)
SELF-TEST PASSED
```

Three real bugs surfaced this way and were fixed: `/__relay/state` used to echo a
cached `unknown` instead of asking Azure; the in-flight counter was written to the
handler *instance* while the watchdog read it from the *class*, so it would always
have read 0 and the watchdog could have deallocated a GPU mid-generation; and
`/__relay/sleep` would happily deallocate under a live stream.

### 9.2 Scripts and live Azure reads

* `azure-cloud-init.yaml` parses as YAML: 7 `write_files` entries and six
  substitution tokens (`MODEL_DIR`, `CADDY_SITE`, `API_KEY`, `CUDA_ARCH`,
  `CUDA_TOOLKIT`, `GPU_LAYERS`); the deploy script fails hard if any survive
  rendering. The service re-read after editing shows `Restart=always`,
  `StartLimitIntervalSec=0`, `-c "$((CTX_PER_SLOT * PARALLEL))"`, `-ngl "$GPU_LAYERS"`,
  `--timeout 900`.
* All four `.ps1` files pass the PowerShell parser with **0 errors**.
* `azure-deploy.ps1` **against the real, signed-in subscription** now stops at the
  offer gate and exits **3**: `az` resolved to
  `C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd`, user
  `deleonrd23@outlook.com`, `subscription offer: FreeTrial_2014-09-01 (spending limit
  On)`, and the evidence block from §3.2. Nothing was created. (An earlier run exited
  0 and printed a plan keyed to `Standard_NC24ads_A100_v4` at 3.673 USD/h — that was
  the run this gate now replaces.)
* Live probes, each recorded in §3.2: `vm list-usage` in eastus plus six regions,
  `vm list-skus` (A100 `NotAvailableForSubscription`; T4 listed but in a 0/0 family),
  `quota show`, eight refused `quota update` calls, and one
  `az vm create --validate` that ARM rejected with `QuotaExceeded`.
* `Microsoft.Quota` was registered on the subscription so the quota API could answer at
  all. That is a provider registration, not a resource and not a spend.
* The model repo was re-read from the Hugging Face API on the same day: `gated: false`,
  `private: false`, shard names identical to the ones hard-coded in cloud-init,
  41.89 + 29.63 = **71.52 GiB**, `SHA256SUMS.txt` present, `mmproj` 1.05 GiB (skipped).
* Tag `b11279` resolves through `api.github.com/repos/ggml-org/llama.cpp/git/refs/tags/b11279`
  to commit `649dcb1036fd47d01a83b56c5e35913e65c277ba`.
* With no `az` at all the script prints install/login instructions and stops;
  `azure-teardown.ps1` dry run correctly reported an already-deallocated VM and did not
  invent work.

### 9.3 Fixed this round

* **`--idle-minutes 0` meant "deallocate immediately"**, which is the opposite of what
  an always-on deployment needs. Zero now disables the idle path; the test asserts no
  `az vm deallocate` across many watchdog sweeps.
* **The spend cap only ran at wake time.** On a 24/7 GPU no wake ever happens again, so
  the $200 guard would never have fired. The watchdog now enforces it continuously, and
  pauses while a request is in flight.
* **A relay restart froze the ledger** (no `started_at` ⇒ `usd()` stops growing ⇒ cap
  never fires). A running VM is now adopted at startup.
* **`/__relay/sleep` ignored in-flight requests** and could deallocate under a live
  generation. It returns 409 now.
* **The SKU check hid the real blocker**: a `Zone / NotAvailableForSubscription`
  restriction was not reported, so the plan read "zones 1" as if fine.
* **`-c 16384 --parallel 4` was a 4K window per request**, because llama-server divides
  the context across slots. Now `CTX_PER_SLOT=16384` × `PARALLEL=2`.
* **`Restart=on-failure` + the default start-limit ratchet** meant a service that
  crashed five times in 10 s would stay dead after a reboot. Now `Restart=always` with
  `StartLimitIntervalSec=0`.
* **`az` was resolved from PATH only**, which a freshly installed CLI is not on; both
  the script and the relay now resolve it themselves.
* **Locale bug**: on a French-locale host the plan printed `--hourly-usd 3,673`, which
  a user would copy straight into a command line. Every number that appears inside a
  printed command now uses invariant formatting.
* The rendered cloud-init is written as UTF-8 **without a BOM**: PowerShell 5.1's
  `Set-Content -Encoding UTF8` adds one, and `#cloud-config` must be the first byte
  for cloud-init to recognise the handler.
* **The script could not see the actual blocker.** It only knew how to complain about
  a zero quota and an A100 restriction; the real cause is the subscription *offer*
  (`FreeTrial_2014-09-01`), which excludes every GPU family and refuses every quota
  raise — CPU families included. It now reads `subscriptionPolicies.quotaId` before
  planning anything and exits 3 with the evidence, so no run can start creating
  networking for a VM that ARM will refuse.
* **The quota check matched the wrong family.** It searched for `NCADSA100v4` no matter
  which `-VmSize` was requested. A T4, an A100 and an RTX PRO 6000 live in three
  different quota families, and a mismatched name reports a healthy limit for a VM
  that cannot be built. The family is now taken from the SKU's own `family` field.
* **CUDA was compiled for the wrong architecture.** `CUDA_ARCH=80` was hard-coded, which
  is right for A100 and silently wrong for the 96 GB Blackwell part (sm_120, needs
  toolkit ≥ 12.8). Arch, toolkit version and `-ngl` now come from a GPU profile table
  keyed by `-VmSize`, and are substituted into cloud-init as three more tokens.
* **The driver script never sourced `/etc/av2-glm53/env`**, so the new
  `$CUDA_TOOLKIT` would have expanded to nothing under `set -u` and aborted the
  bootstrap before the driver was installed. Caught by reading the file, not by
  running it — the box does not exist to run it on.
* **`"$VmSize:"` is a parse error in PowerShell**, not a string. The parser check caught
  it (`':' is not followed by a valid variable name character`); it is now `${VmSize}`.
  This is why the parser gate runs on every edit rather than once.

## 10. Teardown, and keys to revoke

```powershell
# stop the GPU, keep the disk so the next start is a plain boot
.\tools\glm53\azure-teardown.ps1 -Execute

# remove VM/NIC/IP/NSG/VNet entirely
.\tools\glm53\azure-teardown.ps1 -DeleteAll -Execute
```

Then stop `wake_proxy.py`, or the next request simply starts the VM again.

Revoke, in this order:

1. **The Hugging Face token pasted in chat** (`hf_HFDY…`) —
   `huggingface.co/settings/tokens`. It was never needed and never used.
2. The Akamai token (`7566e653…243e2`) and the Render key (`rnd_cemTl7…`) from earlier
   sessions. Both are still live as far as this repository knows.
3. If a client secret was created for this exercise, delete it in Entra ID.

Open inconsistency inherited from the Akamai package: `tools/glm53/cloud-init.yaml`
says weights live on the instance disk, while `tools/glm53/provision_akamai.ps1`
still provisions a separate block volume. The Azure flavour settles it one way — OS
disk only, no second volume: the RTX PRO 6000 sizes have 1.6 TiB of local NVMe but it
is ephemeral, so a deallocate/start cycle would throw 71.52 GiB away and re-download
it, and an extra managed disk costs the same per GB as OS-disk space anyway. The
Akamai pair still needs one of the two edited before that route is run.
