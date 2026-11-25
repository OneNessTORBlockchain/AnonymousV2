# Anonymous V2 — OrcaSAQ-2-Cyber-27B-Uncensored on Lambda Cloud (the pick)

**Date:** 2026-11-22 · **Decision:** deploy on **Lambda Cloud, `gpu_1x_a6000` (RTX A6000 48 GB), $1.09/h**
**Question answered:** *pick one we can start now and get the first bill in a week minimum*

---

## 0. Why this one, in four lines

1. **It is the only provider here that is genuinely postpaid with no deposit.** Lambda's own billing page:
   *"You receive weekly invoices for the previous week's usage."* You start the box today; the first
   real invoice covers days 1–7 and lands around day 7.
2. **No prepayment, no credits, no approval queue.** A card on file plus a **$10 pre-authorisation**
   (*"Lambda makes a $10 pre-authorization charge to make sure the card is valid. The charge will be
   refunded in a few days."*) — a card hold, not a bill, and it is released.
3. **The money fits.** A6000 48 GB at **$1.09/h** → 70 h = **$76.30**, 140 h = **$152.60**, 240 h = $261.60
   (over). You asked for ≥10 days; 10 d × 14 h = 140 h = $152.60, still **$97 under** the $250 ceiling.
4. **It is self-serve and it is available in Canada.** Lambda only sells to AU, CA, CL, EU (−LT), IS, IL,
   JP, NZ, NO, PR, SG, ZA, KR, CH, TW, TR, AE, UK, US. This operator's IP is in Gatineau, QC — inside the
   list. (Thunder Compute, the cheaper option, is fine here too, but it auto-charges the card at usage
   thresholds, so money leaves within hours, not next week.)

**What you give up by picking Lambda over Thunder:** 3× the hourly rate ($1.09 vs $0.35) and no automatic
HTTPS. What you get: a real VM (systemd, `/opt` persists, no container quirks), weekly invoicing, and a
provider whose live capacity the API reports up front, so a failed launch costs nothing.

---

## 1. The billing mechanics, quoted

| Question | Answer (source: `docs.lambda.com/public-cloud/{billing,manage-billing}`) |
|---|---|
| Deposit / prepay? | **None.** Only a card on file. |
| Card check | **$10 pre-authorisation**, *"refunded in a few days"*. Credit cards only — no debit, no prepaid cards. |
| When am I billed? | *"ODC prices instances by hourly usage and bills in one-minute increments… You receive **weekly invoices for the previous week's usage**."* |
| Meter starts | When the instance passes health checks. |
| Meter stops | **Only** when the instance is terminated (`POST /instance-operations/terminate`). |
| Storage after terminate | Root volume dies with the instance — nothing left billing. No filesystem is attached. |
| New-account limits | *"New accounts have a limit on the number of instances you can launch… increases automatically as you pay your invoices."* One instance is exactly what this needs. |

**Cash timeline:** day 0 card hold $10 (released) → day ~7 invoice for week 1 (~$76 at 14 h/day, ~$38 at
5 h/day) → day ~14 invoice for week 2. That is the "first bill in a week" requirement, from a provider
that is actually self-serve.

### The card screen, in plain words

It shows wording close to *"You are authorizing a payment to LAMBDA CLOUD for $10.00 USD … with your
card ****<last4>"*. That is **an authorisation, not a sale**:

* Nothing is captured. It reserves $10 of your available balance and the reservation is released —
  Lambda's wording is *"refunded in a few days"*; card issuers usually drop a pre-auth within a few
  business days, sometimes not until the next statement cycle. It is not a charge you have to dispute.
* It is unavoidable across every provider here in some form (Vultr wanted a $250 minimum, Thunder wants a
  card it can debit); Lambda's is the smallest and the only one that is returned.
* The screen appearing at all is **good news**: it means the country check and the credit-card check
  passed. Debit and prepaid cards are rejected before this point.
* If it is later declined / reversed instead, the account is not usable and no instance will launch —
  `-Action Types` will 401 or the launch will 400. Nothing is half-billed in that state.

---

## 2. Cost, against the three workload patterns

| Pattern | Hours | Cost at $1.09/h | Verdict |
|---|---|---|---|
| 14 d × 5 h (as originally asked) | 70 | **$76.30** | cheapest that meets the requirement |
| 10 d × 10 h | 100 | $109.00 | comfortable |
| **10 d × 14 h** | **140** | **$152.60** | best use of the budget, 62 % of $250 |
| 14 d × 14 h | 196 | $213.64 | fits, weekly invoice ≈ $107 |
| continuous 10 days | 240 | $261.60 | **over budget** |

Cold start (boot → driver up → model loaded → serving) is ~8 min ≈ **$0.15**, so daily start/stop is
still the cheapest way to run this: destroy at the end of a session, `-Action Create` the next day, pay
only for the hours used. The 15.7 GB re-download is free (Lambda charges no egress) and takes ~4 min.

Budget guard: `-Action Status` prints elapsed healthy hours × the live hourly rate.

---

## 3. What was built

| File | Purpose |
|---|---|
| `tools/lambda/bootstrap.sh` | The Vultr first-boot build, adapted: driver check → Ollama on loopback → 15.68 GB GGUF with `-C -` resume and exact-size + `GGUF` magic verification → `ollama create orcasaq-cyber:27b` → nginx bearer gate on `:8080` → smoke test → `/opt/av2/READY`. Falls back to a CUDA `llama-server` build if Ollama's runner rejects the `qwen3_5` architecture. **No ufw** (see §4). |
| `tools/lambda/provision_orcasaq.ps1` | Lambda Cloud API driver — `Types / Create / Wait / Status / List / Logs / Firewall / Destroy`. Resolves the A6000 type from `/instance-types`, picks a region that **reports capacity right now**, uploads the SSH key, sets the edge firewall rule, and launches with `bootstrap.sh` base64'd into cloud-init `user_data`. |
| `tools/lambda/smoke_provision.ps1` | 23 offline checks (no account, no network): cloud-init user-data round-trip, CIDR substitution, LF-only payload, HF overrides landing in the root-only runner and **not** in the bootstrap copy, edge-rule merge/idempotency (SSH + ICMP preserved, 8080 pinned to the operator CIDR), instance lookup by name, instance-type resolution + cents→dollars, invariant cost formatting. `powershell -File .\smoke_provision.ps1` → `all offline checks passed`, exit 0. |
| `docs/ORCASAQ-LAMBDA.md` | This runbook. |

Design decisions:

* **Capacity is checked before anything is created.** `POST /instance-operations/launch` costs nothing
  when it fails, but a needless failure still costs a minute; `/instance-types` carries
  `regions_with_capacity_available`, so `Create` picks a region that exists *at that moment* (or honours
  `-Region` when you name one).
* **Cloud-init, not a second hop.** `bootstrap.sh` is base64'd into a `write_files` entry and executed by
  a `0700` runner — quoting can never break the script, and the box converges with nobody logged in.
* **The proxy stays private without ufw.** On Lambda an instance has no security group of its own;
  inbound is filtered at the edge by the account's **global firewall ruleset** (default: `tcp/22` from
  anywhere). `Create` PATCHes that ruleset to add **`tcp/8080` from your CIDR only**, carrying every
  pre-existing rule over verbatim. Enabling ufw inside the VM was rejected: one IP change would lock SSH
  out permanently and Lambda has no out-of-band console to undo it.
* **Three gates on the model.** nginx exact-matches `Authorization: Bearer <48 hex>`, Ollama only listens
  on `127.0.0.1:11434`, and the edge rule admits only the operator IP.
* **`sudo shutdown` is not a stop button.** Lambda docs warn it leaves the instance `unhealthy`
  **and still billing**. `-Action Destroy` is the only teardown path in this tooling, and it polls until
  the instance reports `terminated`.

---

## 4. Deltas from the Vultr deployment (`docs/ORCASAQ-VULTR.md`)

| Vultr | Lambda | Why |
|---|---|---|
| `root@<ip>` | `ubuntu@<ip>` with `sudo` | Lambda images ship no root SSH login |
| ufw default-deny + operator CIDR | **no ufw**; edge rule for `tcp/8080` from the CIDR | no console to recover from a wrong ufw rule |
| L40S 48 GB @ $1.671/h | A6000 48 GB @ **$1.09/h** | A6000 is ~10 % slower single-stream (768 vs 864 GB/s) at 65 % of the price |
| hourly against prepaid credit | **per-minute, weekly invoice** | the requirement |
| `GET/POST /v2/instances` | `POST /instance-operations/launch` + `Bearer` auth | different API; the Vultr script cannot be reused |
| 16 vCPU / 180 GB RAM | 14 vCPU / 100 GiB RAM / 512 GiB SSD | plenty for a 15.7 GB Q4 model |
| plain HTTP `:8080` + CIDR | plain HTTP `:8080` + CIDR (unchanged) | Lambda offers no auto-TLS; see §7 |


---

## 5. Runbook — from zero to a serving endpoint

### 5.1 One-time, in the browser (~4 minutes)

1. Create the account: <https://cloud.lambda.com> → **Sign up**. The billing country must be one of
   AU, CA, CL, EU (−LT), IS, IL, JP, NZ, NO, PR, SG, ZA, KR, CH, TW, TR, AE, UK, US. (Canada is fine.)
2. **Billing → add a credit card.** Lambda makes the $10 pre-authorisation and the screen says it is
   "authorizing a payment" — that is the hold described in §1, not a bill; continue. Debit and prepaid
   cards are rejected, and a VPN can get the charge flagged as suspicious.
3. **API keys → Generate**, copy the key.
4. **SSH key**: nothing to do — the script uploads `~/.ssh/av2-lambda-orcasaq.pub`, generating the pair if
   it does not exist.

### 5.2 Then, here (~10 minutes, unattended)

```powershell
cd 'C:\Users\KATHY\Downloads\Anonymous V2\tools\lambda'
$env:LAMBDA_API_KEY = '<key from step 3>'

.\provision_orcasaq.ps1 -Action Types          # A6000 price + which regions have capacity NOW
.\provision_orcasaq.ps1 -Action Create -OperatorCidr 173.177.72.31/32
.\provision_orcasaq.ps1 -Action Wait           # prints the IP, then /opt/av2/READY with base_url + token
```

`Wait` ends with the ready block, for example:

```text
backend=ollama
model_id=orcasaq-cyber:27b
base_url=http://203.0.113.7:8080/v1
api_token=<48 hex>
```

### 5.3 Wire AV2 (no Python changes — same contract as `ORCASAQ-VULTR.md` §7)

```powershell
cd 'C:\Users\KATHY\Downloads\Anonymous V2\tools\glm53'
.\connect_av2.ps1 -BaseUrl 'http://<IP>:8080/v1' -ApiKey '<token>' -Model 'orcasaq-cyber:27b' -Apply
cd '..\..'
.\start.ps1
# then: GET http://127.0.0.1:8420/api/health -> provider=openai, ollama=ok
```

`connect_av2.ps1` backs `.env` up first and pins `AV2_FLEET_MODELS` to the single id this box serves —
without that pin, half the six-agent fleet 404s against a Venice model the box does not have.

### 5.4 Stop paying / keep paying correctly

```powershell
.\provision_orcasaq.ps1 -Action Destroy        # terminate + wait for status=terminated
```

Nothing else stops the meter. Restarting tomorrow is `-Action Create` + `-Action Wait` again: ~8 minutes,
~$0.15, and the edge rule is already in place.

If your public IP changes (Videotron shuffles them), re-run:

```powershell
.\provision_orcasaq.ps1 -Action Firewall -OperatorCidr <new-ip>/32
```

SSH stays open on 22 by Lambda default, so this is always fixable remotely — that is the whole reason
ufw is not used in the guest.

---

## 6. Model availability — unchanged from the Vultr report

The canonical repo `orcarouter/OrcaSAQ-2-Cyber-27B-Uncensored-GGUF` is **gated** (`401 GatedRepo`), so the
bootstrap defaults to the ungated mirror `isilicon/mango-orcasaq2-cyber-27b` — same byte length
(15 676 553 472), valid GGUF v3, but its metadata says `general.name = "Qwen3.8 27B Uncensored"`.
**Equal size is not equal weights.** For the real artefact, accept the licence at
<https://huggingface.co/orcarouter/OrcaSAQ-2-Cyber-27B-Uncensored-GGUF> (gated `auto` = instant), mint a
read token, and either launch with it:

```powershell
.\provision_orcasaq.ps1 -Action Create `
  -ModelRepo 'orcarouter/OrcaSAQ-2-Cyber-27B-Uncensored-GGUF' `
  -HfToken 'hf_xxx'
```

or rebuild in place on the box:

```bash
sudo rm -f /opt/av2/model.gguf
sudo MODEL_REPO=orcarouter/OrcaSAQ-2-Cyber-27B-Uncensored-GGUF HF_TOKEN=hf_xxx \
  bash /opt/av2/av2-bootstrap.sh
```

`-ModelRepo` / `-HfToken` are exported by the root-only `/opt/av2/run-bootstrap.sh` written into the
instance, so the token never lands in the shell history; delete that file after the first boot if you
want it gone.

---

## 7. Honest limitations

* **No TLS.** `:8080` is plain HTTP, protected by a 48-hex bearer token, an edge firewall rule limited to
  your IP, and loopback-only Ollama. Same trade as `ORCASAQ-VULTR.md` §6, for the same reason: a
  self-signed cert silently breaks `httpx` unless `SSL_CERT_FILE` reaches the process. Real TLS means a
  hostname + Caddy + Let's Encrypt on 443 — a 20-minute change, not made here.
* **A6000 capacity moves by region.** `-Action Types` shows it live; `Create` refuses to launch into a
  region with no reported capacity instead of queueing. If every region is dry, retry in a few minutes.
* **Rate limit** on `/instance-operations/launch`: one request per 12 s. The script launches once.
* **No snapshots, no suspend.** Lambda instances are launch/restart/terminate only, and termination
  destroys the root volume. `/opt/av2` is disposable by design (15.7 GB re-download + 8 min).
* **Billing granularity is one minute**, so a 3-minute smoke test costs ~$0.05 — cheap, not free.
* **The $10 pre-authorisation** is a real card hold for a few days even though it is refunded.
* **Country restriction is hard**: an unsupported billing country fails at the card step, before launch.
* **This script has not been executed against a live Lambda account yet** — no key existed when it was
  written. What *has* been verified: the API schema against Lambda's published OpenAPI document
  (`https://cloud.lambda.com/api/v1/openapi.json`: `POST /instance-operations/launch` requires
  `region_name`, `instance_type_name`, `ssh_key_names` — exactly one key — and accepts `user_data` as
  plain-text cloud-init ≤ 1 MB; `/instances` and `/ssh-keys` both return `data` as arrays; auth is
  `Authorization: Bearer <key>`), the endpoint answers live (a dummy key gets
  `401 global/invalid-api-key` and the script prints exactly that), `smoke_provision.ps1` passes 23/23,
  the PowerShell parses clean (`[Parser]::ParseFile` → 0 errors), and `bootstrap.sh` passes `bash -n`.
  The first `-Action Types` run is what proves the account side.

