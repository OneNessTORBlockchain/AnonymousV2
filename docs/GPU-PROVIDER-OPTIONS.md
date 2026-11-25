# Cloud GPU options for OrcaSAQ-2-Cyber-27B — billing terms, free credit, and the NVIDIA trial question

**Date:** 2026-11-22 · **Prices verified:** 22 Nov 2026 (Runpod's own table is stamped "Updated November 12, 2026")
**Question asked:** *find a cloud GPU provider with no upfront fees that bills in 14–30 days, or that gives
free credit to start; and check whether the NVIDIA vGPU free trial could work*

**Requirement recap:** one box with **≥ 48 GB VRAM** to hold the 15.68 GB GGUF plus a 32 k context and
4 Ollama slots; must expose an HTTP port AV2 can reach; hourly/per-minute billing so the meter can be
stopped; budget $250.

---

## 0. Three sentences of conclusion

1. **No mainstream provider extends true net-14/net-30 credit to a new individual account.** What exists is
   *postpaid* billing with a card on file, invoiced weekly or monthly (Lambda, Scaleway, DigitalOcean, the
   hyperscalers) — no deposit, but no grace period either: the invoice still lands in 7–30 days and the
   card is already on the account.
2. **The cheapest way to stand this stack up today, with nothing upfront beyond a card on file, is
   Thunder Compute: RTX A6000 48 GB at $0.35/h, per-minute, auto-pay, with free auto-HTTPS port
   forwarding** — ~$24.50 for the 70-hour pattern and ~$49 for 140 hours, in *real money*, not Vultr credit.
   It does **not** satisfy "first bill in ≥ 7 days" (it charges the card at usage thresholds), so the
   deployment in `docs/ORCASAQ-LAMBDA.md` uses Lambda instead — the only postpaid-weekly option here.
3. **The NVIDIA vGPU free trial cannot work.** It is a *software licence* trial (vGPU / vWS / vPC / vApps),
   not compute. It licenses *your* vGPU-capable GPUs behind *your* hypervisor. It provides no GPU, no
   server and no cloud account, and it is licensed for non-production evaluation only.

> **Update, 22 Nov 2026 — the unnamed "$0.25/h RTX 5090" option now has a name:** it is **SaladCloud**
> (see **§6b**). It is cheaper per hour than everything in §2 and its prepaid minimum is **$5**, not
> Runpod's $10 and not Lambda's $10 card authorisation — but it is a *container* platform with no SSH
> and no persistent disk, so it needs a different bootstrap than `tools/lambda/bootstrap.sh`.

---

## 1. The reframe that matters

The `$250` is a **Vultr credit**. It is not cash — it cannot be spent at Runpod, Lambda, Scaleway or
anywhere else. So there are two separate decisions:

| Track | Money at risk | What you get |
|---|---|---|
| **A. Vultr** (if Support grants Cloud GPU access) | **$0 cash** — 149.6 h of L40S already paid for | Best value by a mile; still blocked on the ticket |
| **B. Any other provider** | **new, real money** | Works today; keep the Vultr credit for plain `vc2` CPU boxes or let it lapse |

Everything below is priced in *new cash*, so a $0.35/h card beats a $1.09/h card, and "free credit" only
wins if it can actually produce a 48 GB GPU (it usually cannot).

---

## 2. Billing-model comparison (what "no upfront fees" really means)

| Provider | Upfront? | When you actually pay | 48 GB GPU offered | $/h for it |
|---|---|---|---|---|
| **Thunder Compute** | none — save a card, or preload credit | **auto-pay: card charged as usage crosses thresholds** (postpaid, no deposit) | RTX A6000 48 GB | **$0.35** |
| **Lambda** (`lambda.ai`) | none | **weekly invoice for the previous week** (postpaid, per-minute metering) | RTX A6000 48 GB | $1.09 |
| **DigitalOcean** | none | **monthly invoice** (per-second, monthly cap, postpaid) | *no* single-GPU 48 GB NVIDIA droplet | — |
| **Scaleway** | none | **monthly invoice**, per-minute metering | L40S 48 GB | only in their JS calculator |
| **Runpod** | **prepaid credits** | before you run — needs ≥ 1 h of credits to launch | L40S 48 GB, RTX A6000 48 GB, A40 48 GB | $1.09 / $0.69 / $0.59 |
| **Hyperstack** (NexGen) | **prepaid balance** on-demand; reservations invoiced monthly | per-minute against the balance | RTX A6000 48 GB, L40 48 GB | $0.50 / $1.00 |
| **Vast.ai** | **prepaid credits** | per-second against credit | A6000 / L40 / A100 (marketplace) | dynamic, ~$0.30–0.60 on-demand |
| **Vultr** (current plan) | none — credit already on the account | hourly, deducted from credit | L40S 48 GB | $1.671 |

**Read the Runpod wording literally**, because it is the trap hidden in "no upfront fees":
> "You must have at least one hour's worth of credits for your selected configuration to deploy an
> on-demand instance." … "Billing runs every 5 minutes … When your balance reaches $0, Runpod
> automatically stops all of your running Pods."

— `docs.runpod.io/pods/pricing` and `docs.runpod.io/accounts-billing/billing`

So Runpod is prepaid, though the deposit can be as small as **$10** ("you can start with as little as
$10"); business invoicing (ACH/wire) only starts at **$5,000** per transaction.

**Thunder Compute is the one that behaves like "no upfront fee, billed later"**:
> "**Option 1: Auto-Pay.** Set up auto-pay by saving a credit card … **Option 2: Preload Credit** …
> Threshold charges: as your usage grows, we'll bill your card at preset checkpoints (which rise over
> time) to prevent runaway bills."

— `docs.thundercompute.com/docs/billing`

---

## 3. What $250 of *real* money buys on a 48 GB card

Vultr's $1.671/h is the most expensive 48 GB option here — the credit is only attractive because it is
free. For reference, the three workload patterns already agreed in `ORCASAQ-VULTR.md` §1
(70 h / 140 h / 240 h):

| Provider · GPU | $/h | Max hours for $250 | 70 h | 140 h | 240 h |
|---|---|---|---|---|---|
| **Thunder · A6000 48 GB** | **0.35** | **714 h** | **$24.50** | **$49.00** | **$84.00** |
| Hyperstack · A6000 48 GB | 0.50 | 500 h | $35.00 | $70.00 | $120.00 |
| Runpod · A40 48 GB | 0.59 | 424 h | $41.30 | $82.60 | $141.60 |
| Runpod · A6000 48 GB | 0.69 | 362 h | $48.30 | $96.60 | $165.60 |
| Thunder · L40 48 GB | 0.79 | 316 h | $55.30 | $110.60 | $189.60 |
| Hyperstack · L40 48 GB | 1.00 | 250 h | $70.00 | $140.00 | $240.00 |
| Lambda · A6000 48 GB | 1.09 | 229 h | $76.30 | $152.60 | $261.60 ✗ |
| Runpod · L40S 48 GB | 1.09 | 229 h | $76.30 | $152.60 | $261.60 ✗ |
| **Vultr · L40S 48 GB** | 1.671 | **149.6 h** | $116.97 | $233.94 *(= credit)* | ✗ |

Plus storage: Thunder includes **100 GB persistent disk per GPU** (extra ≈ $0.219/GB-month, **no egress
fees**); Runpod container/volume disk is $0.10–0.20/GB-month and is **deleted when a Pod stops** unless a
network volume is attached.

**Throughput note.** Both cards are memory-bandwidth bound on a 15.7 GB Q4 27B at batch 1 (A6000
768 GB/s vs L40S 864 GB/s), so single-stream speed is the same order of magnitude — A6000 roughly 10 %
behind L40S, at a third of the price. For AV2's 3-lane fleet that is a throughput trade, not a
correctness risk (see risk 9 in `ORCASAQ-VULTR.md`).

---

## 4. Free credit: what is real and what is a mirage

| Program | Credit | Why it does / doesn't help here |
|---|---|---|
| Google Cloud Free Trial | **$300 / 90 days** | Their own docs: *"Access to products may be limited to prevent abuse"*; GPUs are **excluded** from the always-free tier, and trial billing accounts typically come with GPU quota at 0 pending an upgrade to a paid billing account. Not a path to a 48 GB card. |
| Azure free account | $200 / 30 days | Same shape: NC/ND-series GPU quota is typically 0 on free subscriptions. |
| Oracle OCI | $300 / 30 days | GPU shape limits typically need a service-limit increase, which is routinely refused for new/trial tenants. |
| AWS Activate | $1k–$100k | Application-based (startup criteria); no GPUs on the free tier. |
| DigitalOcean | **$5**, expires in 60–90 days | $5 buys ~6.5 h of a 20 GB RTX 4000 Ada. Irrelevant. |
| Paperspace free tier | free *notebook* GPUs | Notebook sessions only, auto-shutdown, not a persistent API endpoint. |

**Conclusion:** free credit large enough to host a 48 GB GPU for a multi-day window does not exist on
terms a new individual account can meet. Free credit is an onboarding tool, not a hosting plan.

---

## 5. The NVIDIA vGPU "30-day free trial" — it cannot work, and here is why

What is actually on offer is a **90-day evaluation of NVIDIA vGPU *software*** (vWS / vPC / vApps), and the
licensing guide makes the subject unambiguous: it "describes NVIDIA Virtual GPU licensed products and how
to configure licensing for them **on supported hardware**" — i.e. hardware you already own and operate,
under a supported hypervisor (vSphere, KVM, XenServer, RHEL/Ubuntu-KVM, Windows Server, Nutanix AHV).

| Question | Answer |
|---|---|
| Does the trial include GPUs? | **No.** It issues licence keys for the vGPU Manager + guest driver stack you install on your own hypervisor. |
| Can it be used as cloud compute? | **No.** You would still pay a provider for a hypervisor-capable GPU host. |
| Would it cut a cloud bill? | **No.** vGPU-enabled clouds bundle the licence into the hourly rate; a trial key does not discount their compute. |
| Is it production-legal? | **No** — evaluation only, non-production. |
| Does this workload even need vGPU? | **No.** OrcaSAQ needs a *passthrough* GPU for CUDA. vGPU slices one physical GPU into virtual workstations/VMs; Ollama + nginx on a single-tenant box is the textbook case that does **not** need it. |

Corroborating detail from NVIDIA's AI Enterprise landing page: its evaluation/contact flow asks
*"Compatible System — Yes, I have a compatible system / I have ordered a compatible system /
I don't have a compatible system"*. The software trial presumes **you** bring the server.

**NVIDIA LaunchPad** (the other "free GPU" rumour): `nvidia.com/en-us/launchpad` no longer presents as a
free GPU sandbox — it now resolves to `build.nvidia.com` (free *hosted NIM endpoints*, playbooks,
blueprints). Historically LaunchPad labs were time-boxed, root-less, application-specific environments
with no persistent endpoint — unusable for a long-running token-gated Ollama proxy, and they would not run
`OrcaSAQ-2-Cyber-27B-Uncensored` in any case.

**Verdict: vGPU trial = software licensing, no hardware, wrong use case. Dead end for this project.**

---

## 6. Providers evaluated and rejected

| Provider | Why rejected |
|---|---|
| **DigitalOcean GPU Droplets** | Postpaid and cheap-ish, but the single-GPU NVIDIA line stops at **RTX 4000 Ada 20 GB ($0.76/h)**; the only 48 GB+ options are HGX H100/H200/B300 at $4.41–$7.94/h. The 15.68 GB GGUF plus context does not fit in 20 GB. |
| **Paperspace (now DigitalOcean)** | Still catalogues A6000 48 GB, but the platform is being folded into DigitalOcean GPU Droplets; hourly postpaid, free tier is notebook-only. Legacy risk. |
| **Vast.ai** | Cheapest headline numbers, but marketplace hosts are heterogeneous, interruptible instances get reclaimed, and pricing is dynamic (live table, no stable scrape-able number). Reasonable *second* choice, not what you hand AV2 as a production endpoint. |
| **Hyperstack** | Good A6000 price ($0.50/h) with per-minute metering, but on-demand is explicitly a **prepaid balance** product; balance at $0 → resources scheduled for termination. |
| **Runpod** | Excellent L40S/A6000 availability, per-second billing, $10 minimum — but **prepaid credits**, and Pods are terminated (data lost) if the balance empties without a network volume. |
| **Scaleway** | Genuine monthly invoicing with L40S 48 GB and per-minute metering, EU data residency — but pricing is rendered by a client-side calculator, and non-EU individual verification can stall. Worth a look only if EU residency matters. |
| **OVHcloud** | Monthly invoice, but the earlier ordering track already failed for this account; do not reopen without a reason. |
| **Hyperscalers (AWS/Azure/GCP)** | Postpaid and effectively net-30, but $3.67–$12.29/h for 40–80 GB class GPUs plus quota friction on new accounts. 3–10× Thunder's cost. |
| **Render** | **CPU-only — there is no GPU tier at any price.** The plan grid is **wider than the legacy six** (`render.com/docs/compute-plans` + `/pricing`, verified 22 Nov 2026): `4c-32g $350`, `8c-16g $300`, `8c-32g $450`, `8c-64g $1,000`, `12c-24g $450`, `12c-48g $800`, `12c-96g $1,500` per month — so a **4 vCPU / 32 GB** shape does exist, and the web-service ceiling is **12 CPU / 96 GB** (`16c-*` is Postgres-only). There is however no `12c-32g`: $450 buys either `8c-32g` or `12c-24g`. Compute is prorated **to the second** and stops when the service stops, so `12c-24g` ≈ **$0.616/h while running only**. Still never the primary inference host — the arithmetic is in §6c. |

---

## 6b. SaladCloud Community Cloud — RTX 5090 **32 GB** at $0.250/h *(verified 22 Nov 2026)*

Provider identified: the "$0.25/h RTX 5090" listing is **SaladCloud** (`salad.com/pricing`,
`docs.salad.com`, portal `portal.salad.com`). Note the shape change — Salad's largest class is
**32 GB**, so it can host OrcaSAQ but never a 48 GB box, and **GLM-5.3 (IQ1_S = 71.52 GiB) is out of
reach at any price**.

### 6b.1 Prices — USD per GPU-hour, vCPU and RAM included

| GPU class | VRAM | High | Medium | Low | **Lowest (Batch)** |
|---|---|---|---|---|---|
| **RTX 5090** | **32 GB** | 0.600 | 0.483 | 0.367 | **0.250** |
| RTX 4090 | 24 GB | 0.440 | 0.347 | 0.253 | 0.160 |
| RTX 5090 *Laptop* | 24 GB | 0.235 | 0.190 | 0.145 | 0.100 |
| RTX 3090 | 24 GB | 0.200 | 0.163 | 0.127 | 0.090 |

Do not confuse **RTX 5090** (32 GB) with **RTX 5090 Laptop** (24 GB) — both appear in the published
table, and 24 GB does not hold the 15.68 GB GGUF plus context comfortably.

| Workload pattern | Lowest 0.250 | Low 0.367 | Medium 0.483 | High 0.600 |
|---|---|---|---|---|
| 70 h | **$17.50** | $25.69 | $33.81 | $42.00 |
| 140 h | **$35.00** | $51.38 | $67.62 | $84.00 |

Even at *Medium* priority Salad is **2.3× cheaper than Lambda's A6000** ($0.483 vs $1.09/h, §3) and
should be ~1.5–2× faster on a 15.7 GB Q4 at batch 1 (GDDR7 ≈ 1792 GB/s vs 768 GB/s). The trade is that
the money is **prepaid, non-refundable and expires in 12 months** instead of a card on file.

### 6b.2 Billing model — prepaid, per organization

All of this is from `docs.salad.com/general/explanation/billing`:

* **Prepaid credits**, scoped to the **organization**, not to the user account. There is no
  "deposit funds" workflow and no postpaid invoice for normal use.
* **Manual recharge: $5 – $10,000** per purchase, via **Add Credits** on the **Billing & Usage** page.
* **Auto recharge**: a threshold ("when my balance falls below…") plus a recharge amount, charged to a
  saved card. Salad recommends threshold ≈ 10 % of the recharge amount.
* Accepted payment methods: **credit and debit cards**, plus **crypto on Solana** (USDC / USDT /
  RENDER through a Solflare wallet, processed via Coinbase x402; Phantom is currently rejected).
  No ACH/wire below $10,000.
* Credit purchases are **final and non-transferable**, and **expire 12 months** after purchase.
* Balance at **$0.00 → usage is shut down**; any usage that slips through is invoiced at the start of
  the next month.
* **Per-second** metering, billed only while an instance is in the **running** state. Allocation,
  **image download** and container start are **free** — cold-boot time is never billed.
* **No free trial**, no minimum commitment, no reserved capacity below a sales contract.

**Portal location (the question that started this):** billing is *not* on `portal.salad.com/account`.
Path is account menu → **organization** → **Billing & Usage**, whose tabs are **Billing Information**
(add/replace card, **Add Credits**, auto-recharge settings, billing contact), **Invoice**, **Usage** and
**Credits**. The API key lives separately under the account dropdown → **API Access**.

### 6b.3 Interruption model — this is the real cost driver

`docs.salad.com/container-engine/explanation/billing-pricing/priority-pricing`:

| Tier | Preempted by other workloads? | Notes |
|---|---|---|
| **High** | never *by other workloads* | still **reallocated** if the node disconnects, the container exits, or you request it |
| **Medium** / **Low** | yes, by higher-priority groups | "minimum run before reallocation is roughly 30 minutes" |
| **Lowest (Batch)** | most interruptible | all **CPU-only** groups are forced to this tier |

Nodes are consumer PCs and "can disconnect at any time"; SCE then **reallocates the instance to a new
node automatically**. Because local state is **ephemeral**, a reallocation re-downloads the 15.68 GB
GGUF *inside a running (billed) container*. Mitigation: bake the GGUF into the image, since image
download is explicitly unbilled — verify registry/image size limits first. Priority can be raised on a
running group at any time; lowering it requires 60 minutes at the current tier, or stop → change → start.

### 6b.4 Integration deltas vs `tools/lambda/bootstrap.sh`

| `bootstrap.sh` assumption | Salad reality | Change needed |
|---|---|---|
| cloud-init + `systemd` + `ubuntu@` SSH | **No VM, no SSH.** A container group runs a **docker image** (Compose supported) | Whole script becomes a **Dockerfile + entrypoint**; no unit files |
| nginx + 48-hex bearer on `:8080` | keep both **inside the image** | Container-group auth uses `Salad-Api-Key` — the caller's *account* key, never a client credential |
| inbound `:8080` on a public IP | **Container Gateway** hostname per exposed port; changes on reallocation | `connect_av2.ps1 -BaseUrl` must be re-run after every reallocation |
| `ufw allow from <operator>/32` | no host firewall; exposure = which ports the group publishes | drop the ufw block |
| `provision_orcasaq.ps1` (Vultr API) | `https://api.salad.com/api/public`, header **`Salad-Api-Key`** | new driver; `GET /organizations/{org}/gpu-classes` returns names + `prices[]` per priority (`high`/`medium`/`low`/`batch`), `GET …/quotas` returns `container_replicas_quota` / `container_replicas_used`; the org name **is** the billing boundary |
| — | 240 requests/min per API key; errors may be HTML | handle non-JSON bodies |

**Not verified, check before committing money:** whether any GPU class exposes **more than one GPU per
instance** (the `gpu_count` field allows up to 512 but Community Cloud devices are consumer PCs), and
Docker image size limits for a model-baked image. Blackwell/`sm_120` is a low risk: `bootstrap.sh`
line 100 installs Ollama from `https://ollama.com/install.sh` with **no version pin**, so a fresh box
gets whatever is current — still confirm with `nvidia-smi` plus a real `POST /api/generate` rather than
assuming. Sizing on 32 GB must change too: the defaults are `NUM_CTX=32768`, `PARALLEL=4`
(`bootstrap.sh` lines 52–53, and line 202 sizes the model as `NUM_CTX × PARALLEL`), i.e. ≈ 131 k tokens
and ≈ 16–17 GB of `q8_0` KV cache — that plus 15.68 GB of weights exceeds 32 GB. Use
`NUM_CTX=16384 PARALLEL=2` (32 k total, ≈ 4.2 GB KV) via environment, no script edit.

---

## 6c. Render as a **CPU** inference host — the plan grid, and what 12 vCPU actually buys *(verified 22 Nov 2026)*

Sources: `render.com/docs/compute-plans`, `render.com/pricing` and `render.com/docs/disks`, all fetched
22 Nov 2026. An earlier revision of §6 asserted that RAM was "tied to cores at ~4 GB each" and that no
4 vCPU / 32 GB shape existed. **Both statements were wrong** — the grid below supersedes them.

| Plan | CPU | RAM | $/month | ≈ $/h while running (monthly ÷ 730) |
|---|---|---|---|---|
| `4c-16g` | 4 | 16 GB | 225 | 0.31 |
| `8c-16g` | 8 | 16 GB | 300 | 0.41 |
| `4c-32g` | 4 | 32 GB | 350 | 0.48 |
| `8c-32g` | 8 | 32 GB | 450 | **0.62** |
| `12c-24g` | 12 | 24 GB | 450 | **0.62** |
| `8c-64g` | 8 | 64 GB | 1,000 | 1.37 |
| `12c-48g` | 12 | 48 GB | 800 | 1.10 |
| `12c-96g` | 12 | 96 GB | 1,500 | 2.05 |

Render publishes no GPU row anywhere in the compute table: **CPU-only at every price.** Billing is
prorated **to the second**, and an *off* service costs nothing — so these are per-hour figures you can
switch off, not a standing charge.

### The only choice that matters at $450: `12c-24g` vs `8c-32g`

There is **no `12c-32g`**. For `GLM-4.7-Flash-Uncensored-Heretic-NEO-CODE-Imatrix-MAX` at **IQ2_M =
10,314,638,272 B (9.61 GiB)** — HF API, `gated=false`, repo `DavidAU/…-MAX-GGUF` — take **`12c-24g`**:
10.31 GB of weights in 24 GB leaves ≈ 13 GB for KV cache and page cache, so **RAM is not the binding
constraint** and the extra 4 cores pay for themselves in prefill. `12c-48g` at $800 is only for Q6_K
(23.27 GiB) / Q8_0 (29.93 GiB) or very long context; it is money burned for a 10 GB quant.

### What it costs per token

`12c-24g` ≈ **$0.616/h**. At an estimated 20 tok/s that is 72 k tok/h ⇒ **≈ $8.6 per million tokens**.
The GPU path in §6b/§3 is $0.37–$1.09/h at 60–120 tok/s ⇒ **≈ $1.3–$2.5 per million**. So CPU on Render
is roughly **3–7× more expensive per token** than the 5090 already specced. It is bought for
*availability, no GPU quota, a stable HTTPS hostname and per-second billing* — **not** for $/throughput
and **not** for latency.

> **Measured 23 Nov 2026 (§6d), and the estimate was optimistic.** 7.75 tok/s single-stream ⇒
> **≈ $22 per million tokens**; three concurrent lanes at 5.39 tok/s each ⇒ 16.2 tok/s aggregate
> ⇒ **≈ $10.6 per million**. Call it **$11–22/Mtok against the 5090's $1.3–2.5**, i.e. 4–17× worse
> rather than 3–7×. The verdict does not change — this tier is still bought for uptime — but nobody
> should quote $8.6 again.

### What 12 vCPU realistically delivers

Estimates, not measurements — confirm on the live box before writing them anywhere else: **gen
~15–25 tok/s**, **prefill ~50–150 tok/s**. A 1–3 k-token reasoning model spends 1–3 k thinking tokens
per reply, so an answer lands in ≈ 1–3 min. Usable for batch and background agent work; not "lightning
fast", and never going to beat a 32 GB 5090 at 60–120 tok/s.

> **Measured 23 Nov 2026 (§6d): the low end of both ranges, not the middle.** Gen **4.8–11.8 tok/s**
> depending on context length, prefill **~50–65 tok/s**, and **7.75 tok/s** on a realistic
> 443-prompt/249-answer AV2 turn in **32.6 s**. A 1 k-token answer costs ≈ 2 min of billed time.

The levers that *do* move it, in order of effect:

1. **`OLLAMA_NUM_PARALLEL=3–4`** — a single MoE generation stream cannot saturate 12 cores. Parallel
   requests at `NUM_CTX=8192` convert idle cores into aggregate throughput. This is the only lever that
   makes a CPU inference box feel quick, and it is wasted effort to tune anything else first.
2. **Quant choice.** At 24 GB the IQ2_M squeeze is unnecessary: from the same repo, `IQ4_XS` = 15.28 GiB
   and `Q4_K_S` = 16.25 GiB both fit and are far better for code. i-quant unpacking is CPU-expensive, so
   a larger K-quant can beat IQ2_M on tok/s *and* on quality — measure both, do not assume the smaller
   file is faster.
3. **Prefill dominates AV2 latency** because prompts are long. Trim history / summarise rather than
   raising `NUM_CTX`.
4. `OLLAMA_FLASH_ATTENTION=0`, `OLLAMA_KEEP_ALIVE=-1` so weights stay resident (a cold reload re-reads
   the whole file from SSD, which is billed compute time).

### Disk

$0.25/GB/month; a 10.31 GB model needs a **12 GB** disk ⇒ ≈ **$3/month**. Two hard limits: the disk is
**not mounted during the build**, so the GGUF must be pulled at first boot by the service itself; and
attaching a disk pins the service to **one instance** and disables zero-downtime deploys.

**Verdict:** `12c-24g` is a legitimate always-on CPU endpoint for this model and the best CPU shape
Render sells for it — but it is not fast and not cheap per token. Keep it as the fallback/overflow path
behind the 5090-on-Salad primary, never as the reason to abandon the GPU plan.

## 6d. What was actually deployed on Render *(23 Nov 2026)*

> **Retired the same day.**  Both services below were deleted: the 12c-24g brain could not hold
> AV2's ~4.7 k-token lane prompt at a usable speed (every lane died on `HTTP 400 … exceeds the
> available context size (4096 tokens)`), and it billed ~450 USD/month while it did.  AV2 runs on
> Venice AI instead (`tools/connect_venice.ps1`, `docs/STATUS.md`), and
> `tools/render/retire_glm_gate.ps1` is the delete command.  The measurements stay as the record
> of why CPU inference on Render is not the answer.

Section 6c reasoned about plan shapes. The deploy turned out not to need a new inference service at
all: the workspace (`tea-da93b7hf2nfc73e599sg`, *My Workspace*) already ran one, and the job was to
**put a door in front of it and move it up the plan grid**.

### The finding that changed the shape of the work

`anonymous-ollama` (`srv-dbofib8u01pc73f48tm0`) already existed: `runtime: docker`, built from
`github.com/crocrodil666/AnonymousWebApp` at `./deploy/ollama/Dockerfile`, `plan: 4c-16g`,
`region: virginia`, with a **40 GB disk** (`dsk-dbofib8u01pc73f48tvg`) mounted at `/root/.ollama`, and
three small local GGUFs registered as `anonymous-captain:v1`, `anonymous-agents:v1`,
`anonymous-scout:v1`. Its most important property is `type: private_service`: **Render assigns it no
`onrender.com` subdomain at all**. It is reachable only from other services in the same workspace and
region, over the private network, at `http://anonymous-ollama:10000`.

That is the right design for the webapp and the API, and it is useless for AV2, which runs on this
laptop and therefore has no seat on that private network. A private service cannot be called from
outside, and nothing in the Render API changes a service's `type` after creation; a disk also cannot
be moved between services. So the honest options were: expose a **second** public Ollama with its own
disk (re-download 10 GB, pay twice, two boxes to keep alive) or **keep one brain and give it a public
door**. The second was chosen.

### Topology now live

```
AV2 (laptop)  --https + bearer-->  anonymous-ollama-gate   web_service, 0.5c-512mb, virginia
                                     nginx, no model
                                   --http, private net-->
                                   anonymous-ollama        private_service, 12c-24g, virginia
                                     ollama + 40 GB disk at /root/.ollama
```

| Resource | Id | Shape | Cost/month |
|---|---|---|---|
| `anonymous-ollama-gate` | `srv-dc1ufdifngtc738n1qdg` | `web_service`, `0.5c-512mb`, virginia | ~7 |
| `anonymous-ollama` | `srv-dbofib8u01pc73f48tm0` | `private_service`, **`12c-24g`** (was `4c-16g`) | 450 |
| `anonymous-models` disk | `dsk-dbofib8u01pc73f48tvg` | 40 GB at `/root/.ollama` | ~10 |

Billed to the second; the gate is cheap because **no model runs in it** - its plan only has to carry
one TLS-terminated token stream per concurrent reply, while the 10 GB resident set lives on the
`12c-24g` service behind it. The gate is deployed from branch **`av2-gate`** of the same repo
(`deploy/ollama-gate/`) with `autoDeployTrigger: off`, deliberately not from `main`: neither existing
service was rebuilt or restarted to ship it.



### Measured on the live box *(23 Nov 2026, `12c-24g`, IQ2_M, `num_ctx` 8192)*

All of these are wall-clock measurements through the public gate, taken with `/api/generate`
(Ollama's own timings) or `/v1/chat/completions`:

| What | Result |
|---|---|
| Model load from disk (cold, first request after a deploy) | **9.3 s** for 10.31 GB |
| Prompt evaluation (prefill), 1 380-token prompt | 1 380 tok in 27.5 s = **50.1 tok/s** |
| Generation, short context (128 tok in, 25-token prompt) | **11.75 tok/s** |
| Generation, 1.4 k-token context | 4.76–6.81 tok/s |
| **Realistic AV2 turn** (443 prompt + 249 answer, reasoning off) | **32.6 s** ⇒ 7.75 tok/s |
| 3 concurrent lanes (`OLLAMA_NUM_PARALLEL=3`) | 5.39 tok/s each ⇒ **16.2 tok/s aggregate** |

Three findings that matter more than the tok/s:

1. **Prefill is fast and generation is slow.** 50–65 tok/s of prefill against 5–12 tok/s of decode
   means prompt trimming buys far less than it looks like it should. Cutting `NUM_CTX` will not save
   the turn; only emitting fewer tokens will.
2. **`NUM_PARALLEL=3` gives ~1.4× aggregate, not 3×.** The box batches the lanes, so each lane runs at
   ~45% of single-stream speed while total throughput rises. For AV2's 3-agent fleet that is the right
   trade (the fleet finishes sooner), but per-lane latency is worse — do not turn it up expecting
   linear scaling. It is a throughput lever, not a latency lever.
3. **The model is a thinking model, and that was the real blocker.** With thinking on, a
   `max_tokens: 24` call spent all 24 tokens inside `message.reasoning` and returned
   `content: ""` with `finish_reason: "length"` — an empty reply, which AV2 reads as no answer at all.
   Two things fix it, and only one of them works through the OpenAI surface:
   * `/api/chat` + `"think": false` → answers in **0.6 s**;
   * `/v1/chat/completions` + `"think": false` → **ignored**, still empty content;
   * `/v1/chat/completions` + `"reasoning_effort": "none"` → **`AV2 LINK OK` in 0.6 s, `finish: stop`**.

   So AV2 gained `Settings.llm_reasoning_effort` (`AV2_LLM_REASONING_EFFORT`), sent as
   `reasoning_effort` on the OpenAI payload, and `connect_av2.ps1 -ReasoningEffort none` writes it.
   Without that one field the endpoint looks broken: it answers 200, streams tokens, bills for
   minutes, and hands back nothing.

### The gate bug that cost two deploys *(keep this one)*

The first gate proxied to `http://anonymous-ollama:10000` with a literal `proxy_pass`. That worked
until the brain was redeployed, at which point `/healthz` kept answering `200` while every proxied
call returned **504 after exactly 30 s** (`proxy_connect_timeout`). nginx resolves a literal
`proxy_pass` hostname **once, at config load**: the private service kept its name but got a new
instance IP, and the gate was dialling an address that no longer existed. Nothing appears in the
Ollama logs, because nothing arrives.

The textbook fix — `resolver` + a variable `proxy_pass` — returned **502** instead, i.e. nginx's own
DNS client could not resolve a name nginx had resolved moments earlier at load time. Platform name
lookup and a raw DNS query are not the same path here. The shipped version therefore resolves at load
(which demonstrably works), probes the real proxied path every 30 s via `curl` through itself, and
issues a graceful `nginx -s reload` (which re-resolves) after two consecutive failures. `GET /debugz`
(keyed, same bearer) reports what it is pointed at and how many reloads it has done, because Render's
API exposes **no logs endpoint at all** — `GET /v1/services/{id}/logs` is a 404, so a gate that cannot
describe itself is undebuggable.

### AV2 side, verified end to end

```
provider=openai  openai_style=True  effort='none'  key=48 chars
health       : True  https://anonymous-ollama-gate.onrender.com/v1 ok
content      : 'AV2 LINK OK'   thinking_len=0   done_reason=stop   wall=1.51 s
```

That is `OllamaClient` from `backend/app/core/ollama_client.py` — the real code path, not curl — driven
by the `.env` that `connect_av2.ps1 -Apply` wrote. Backup: `.env.bak-20261123T073714Z`.



---

## 7. Recommended plan, in order

**Decision taken:** the requirement is *start today, first bill no sooner than a week*. Only **Tier 3
(Lambda)** satisfies that; Tier 2 is 3× cheaper per hour but charges the card at usage thresholds. Tiers
below are kept in the order they should be tried if Tier 3's card check fails.

> **Addendum, 22 Nov 2026 - a 16 GB `vcg-a16-*` plan is now offered and is NOT a shortcut.** The
> live deploy UI lists `vcg-a16-6c-60g-16vram` (one 16 GB NVIDIA A16 slice, ~200 GB/s, sm_86) in
> US-New York at **$0.47/h**. It is capacity-available but the wrong shape: 14.60 GiB of weights
> in 16 GB leaves no room for KV cache at the configured `NUM_CTX` x `PARALLEL`, and the
> bandwidth-limited ceiling is ~13 tok/s. Full arithmetic in `ORCASAQ-VULTR.md` section 1. Tier 1
> is still the **48 GB L40S** if Support lifts the gate.

### Tier 1 — Try Vultr once more (free money)
Nothing changes from `ORCASAQ-VULTR.md` §9: the $1.671/h L40S is the best 48 GB deal *for you*, because it
is already paid for. Sending the ticket costs nothing.

### Tier 2 — Stand the stack up **today** on Thunder Compute (≈ $25–$50)
```powershell
# 1. install the CLI (Windows x64 .msi, or the Mac .pkg / Linux install.sh from the same docs page)
#    https://storage.googleapis.com/thunder-cli/tnr/releases/latest/windows/tnr_windows_amd64.msi
# 2. authenticate (opens a browser) and add a card at console.thundercompute.com/settings/billing
tnr login
# 3. create the box: pick the RTX A6000 (48 GB) in the interactive prompt, 100 GB disk included
tnr create
tnr status
tnr connect 0
```

Then run the same stack as `tools/vultr/bootstrap.sh`, with these deltas:

| Vultr assumption | Thunder reality | Change needed |
|---|---|---|
| `systemctl disable/restart ollama` | Container-style instances; `systemctl` is **not documented** | Start Ollama with `nohup ollama serve &` (or `start-ollama` from their Ollama template) instead of a unit |
| `ufw allow from 173.177.72.31/32` | No host firewall needed; no inbound except forwarded HTTP | Drop the ufw block entirely |
| Plain HTTP `:8080` (accepted limitation) | **`https://<instance-uuid>-8080.thundercompute.net`, automatic TLS + DDoS protection** | Keep nginx + 48-hex bearer, then `tnr ports forward <id> --add 8080`; base_url becomes that HTTPS URL |
| `GET /v2/instances` in `provision_orcasaq.ps1` | Different API (`api.thundercompute.com:8443`) + `tnr` CLI | The PowerShell provisioner is Vultr-specific — drive Thunder by CLI/console, or add a second adapter |
| Region choice (`atl`) | North America only, **dynamic IP** | Nothing to choose |

Hard constraints to respect: forwarded ports are **HTTP/gRPC only** (port 22 reserved for SSH);
**crypto mining is banned** (account ban + full-usage billing); service is **not available** in
Brazil, Mexico, China, Hong Kong, Indonesia, Malaysia, Kenya, Nigeria, Russia, Saudi Arabia, Vietnam,
Uruguay, plus CU/IR/KP/SD/SY/BY; in 27 listed countries (e.g. India, Türkiye, Switzerland, UAE, Qatar,
South Korea, Ukraine) it is **B2B only and needs a VAT ID**. A new org gets a GPU quota — one GPU is
fine, more may need verification.

Cost of a full 10 d × 14 h pattern: **$49**. Cost of the 14 d × 5 h pattern: **$24.50**.

### Tier 3 — **CHOSEN**: Lambda, the only true "start now, pay next week" path
Lambda is genuinely **no-deposit, postpaid, weekly-invoiced**: a card on file, a **$10 refundable
pre-authorisation**, then *"weekly invoices for the previous week's usage"* — the first real bill lands
~day 7. A6000 48 GB at **$1.09/h** → 70 h ≈ **$76.30** + tax, 140 h ≈ **$152.60**. You get 7 days of float,
not 30, and 3× Thunder's hourly rate — that is the price of not paying up front.

Fully scripted and ready: **`docs/ORCASAQ-LAMBDA.md`** (billing quotes, cost table, runbook,
`tools/lambda/bootstrap.sh`, `tools/lambda/provision_orcasaq.ps1` with
`Types / Create / Wait / Status / List / Logs / Firewall / Destroy`). Lambda sells only to AU, CA, CL,
EU (−LT), IS, IL, JP, NZ, NO, PR, SG, ZA, KR, CH, TW, TR, AE, UK, US — the billing country is checked at
the card step, not at launch.

---

## 8. Honest limitations of this research

* Scaleway's L40S price could not be extracted (client-side calculator) — no figure quoted rather than a
  guessed one.
* Vast.ai prices are live/dynamic by design; the range quoted is indicative, not contractual.
* Thunder's port-forwarding docs confirm HTTPS + HTTP/gRPC only, but **do not** confirm whether
  `systemctl` exists inside an instance. Verify on the first box before assuming `bootstrap.sh` runs
  unmodified — that is the single real integration risk of Tier 2.
* Runpod figures are Secure Cloud list prices from their 12 Nov 2026 table; Community Cloud is cheaper but
  runs on third-party hosts.
* Nothing here removes the two blockers in `ORCASAQ-VULTR.md`: Vultr GPU access (Support ticket) and the
  Hugging Face licence + read token for the canonical OrcaSAQ repo.
* Sources: `runpod.io/pricing`, `docs.runpod.io/pods/pricing`, `docs.runpod.io/accounts-billing/billing`,
  `lambda.ai/pricing`, `docs.lambda.ai/public-cloud/billing`, `thundercompute.com/pricing`,
  `docs.thundercompute.com/docs/{billing,quotas,restrictions,technical-specs}` and
  `/docs/cli/operations/port-forwarding`, `hyperstack.cloud/gpu-pricing`,
  `digitalocean.com/pricing/gpu-droplets`, `scaleway.com/en/pricing/gpu`, `vast.ai/pricing`,
  `cloud.google.com/free/docs/free-cloud-features`, `docs.nvidia.com/vgpu/latest/grid-licensing-user-guide`,
  `nvidia.com/en-us/data-center/products/ai-enterprise`, `nvidia.com/en-us/launchpad`.
