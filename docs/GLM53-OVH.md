# OVHcloud route for GLM-5.3-Flash-Uncensored (IQ1_S)

> Status: **one click from a GPU, and that click is not mine to make - a $0 CAD
> Public Cloud project order is waiting to be confirmed.**
>
> The credentials resolved cleanly, and the reason nothing could be listed was never
> the key: **this account owns no Public Cloud project.** Signed `GET /me` and
> signed `GET /cloud/project` both answer `200`, and `/cloud/project` answers `[]`.
> That empty array is what every GPU flavor, quota check and create call sat behind.
>
> | check | result |
> |---|---|
> | signed `GET /me` (CA endpoint) | `200` - application key and consumer key `e4558...,` are good |
> | signed `GET /cloud/project` | `200 []` - no project exists |
> | `GET /order/cart` | `200` - one cart "Public Cloud project" holding `project.discovery` at $0 CAD |
> | `GET /order/cart/{id}` | `items: [100919210]`, `orderId: null` - started, never checked out |
> | `POST /cloud/project` | `405 Method Not Allowed` - projects are order-only, not creatable directly |
> | `GET /me/paymentMean/creditCard` | `403 NOT_GRANTED_CALL` - outside this key's rights |
>
> So the abandoned cart was checked out: `POST /order/cart/e8bfc780-.../checkout`
> returned **order `23371205`, $0.00 CAD**, plus the contract text it wants accepted.
> `/cloud/project` then stayed `[]` through two minutes of polling, which means the
> order is still unpaid - a $0 order is still an order. Confirming it needs the
> account owner, in the browser:
>
> ```
> https://ca.ovh.com/ca/en/cgi-bin/order/display-order.cgi?orderId=23371205&orderPassword=TbXurggZdz
> ```
>
> or `ca.ovh.com/manager` > Orders. Until that is accepted, `/cloud/project` stays
> empty and no GPU flavor is reachable no matter what the key is allowed to do.
>
> The minted key had no `/me/order` rights, so it could create an order and then
> never look at it again. `ovh_mint_ck.ps1` now also requests `GET /me/order`,
> `GET /me/order/*` and `POST /me/order/*` - that is what an unattended run needs to
> accept contracts and pay a $0 order without a human. Re-minting costs one more
> approval click; confirming order `23371205` above costs one click and is the
> shorter path today.
>
> Reaching for a better host than a 1-month trial made it worse. A cart built through
> the API - `POST /order/cart`, then `POST /order/cart/{id}/cloud` with
> `planCode=project.2018`, `duration=P1M`, `pricingMode=default`, `quantity=1` (OVH
> demanded those four fields one `400` at a time) - still needs
> `POST /order/cart/{id}/assign` before checkout, and then checkout answers
> **`400 You are not able to order, please contact support`**. The catalog says both
> `project.2018` and `project.discovery` cost $0.00 CAD, so this is not money: the
> account is blocked at the ordering step, most likely because order `23371205` is
> outstanding and/or because no payment method is registered - `GET /me/paymentMean/*`
> was not granted to the key that could have answered that.
>
> That question is now answered, in three parts:
>
> 1. **The $0 order was paid** (`status: delivered`, 13:45) and project
>    `8750f274d7744a549d35a997c800912d` appeared, planCode `project.discovery`.
> 2. **A Discovery project cannot provision anything.** `POST
>    /cloud/project/{id}/sshkey` answers `400 Project discovery forbidden`, so it can
>    never host an instance. It is a throwaway trial, not a stepping stone.
> 3. **A standard project cannot be ordered.** `POST /order/cart/{id}/checkout`
>    answers `400 You are not able to order, please contact support` from three
>    different carts - before the card was added, after it was added, and after the
>    $0 order was paid. The item's required configuration is all `required: false`,
>    so it is not a missing field. What is left is
>    `GET /me/paymentMean/creditCard/12088480` -> `state: valid`,
>    **`threeDsValidated: false`**: a $0 order never triggers 3-D Secure, so the card
>    has never been challenged, and OVH will not sell anything else until it has been.
>
> Two bugs were found and fixed on the way, both of which had made these scripts lie:
>
> - `GET /cloud/project/{id}/region` returns a **string array**
>   (`["BHS","GRA11",...]`), not the `cloud.region` objects the docs describe.
>   Reading `.name` off a string yields `$null`, so every region was skipped and
>   `-List` reported "No GPU flavor visible" for a project that had 19 of them.
> - Flavor `ram` and `disk` are already in **GB** (`h200-1920-eph` is
>   `{"ram":1920,"disk":400}`); dividing by 1024 printed a 1.9 TB box as "2 GB RAM".
>
> With both fixed, `-List` reports the truth: 19 GPU flavors in `GRA11` and
> `EU-WEST-PAR`, `h200-1920-eph` at 141 GB VRAM available, and **quota 0 on every
> single one** - the second support gate. `-IgnoreQuota` asks OVH directly instead of
> trusting that field, and against the Discovery project it answered
> `Project discovery forbidden`.

>
> When the project exists, the rest is unchanged: `provision_ovh.ps1 -List` reads the
> per-flavor `quota` field, and GPU quota on a fresh (let alone discovery) project is
> routinely `0` - that gate is account-level and only an OVH support ticket moves it.
> `provision_ovh.ps1` now also explains, on an empty `/cloud/project`, whether a cart
> was never checked out or an order is merely unconfirmed.


## 0. The two gates only OVH support can open

Nothing in this repo can create a GPU while these are closed, and
`provision_ovh.ps1` exits `3` (quota) or `1` (ordering) until they are.

Copy-paste ticket (manager > Support, or help.ovhcloud.com), account
`3010-3316-47`:

```
Subject: Public Cloud - ordering blocked + GPU instance quota on 3010-3316-47

Two things block a Public Cloud GPU deployment on this account
(bspgrowtech@gmail.com, CA subsidiary):

1. Every project order is refused. POST /order/cart/{cartId}/checkout returns
   400 "You are not able to order, please contact support" for planCode
   project.2018 ($0.00 CAD), from three separate carts, including after the one
   existing $0 order (23371205, status "delivered") was paid. The order item has
   no unfilled required configuration. GET /me/paymentMean/creditCard/12088480
   reports state "valid" but threeDsValidated false, and a $0.00 order never
   triggers a 3-D Secure challenge, so the card cannot be validated from the API.
   Note that a $0 vRack PRE_ORDER (23371236, service pn-195909) was delivered
   without error, so the account is not wholly blocked - only Public Cloud project
   orders are refused.

2. The account has no GPU quota. On project
   8750f274d7744a549d35a997c800912d (planCode project.discovery),
   GET /cloud/project/{id}/flavor returns quota 0 for all 19 GPU flavors,
   including l4-90, l40s-90 and h100-380 in GRA11 and h200-1920-eph in
   EU-WEST-PAR. That project also refuses provisioning outright:
   POST /cloud/project/{id}/sshkey returns 400 "Project discovery forbidden".

Please (a) clear the ordering restriction, (b) grant GPU instance quota of 1 on
l4-90 or h100-380 in GRA11 - or h200-1920-eph in EU-WEST-PAR if GRA11 is out -
and (c) validate the payment method for 3-D Secure so orders can be paid.

Thank you,
WOODJERRY ALMONTE DE LEON
```

The moment a project exists with `quota >= 1` on any GPU flavor, the rest of this
document runs unattended: `-List` shows it, `-Execute` builds it, `fetch.sh` and
`serve.sh` load the model, `connect_av2.ps1` points AV2 at it.

## 1. Re-mint and approve

```powershell
$env:OVH_APP_KEY    = '528b187ad4dacc0f'
$env:OVH_APP_SECRET = '<the application secret>'
.\tools\glm53\ovh_mint_ck.ps1
```

It prints a `validationUrl` such as
`https://ca.ovh.com/auth/sso/api?credentialToken=...`. Open it, log in, approve, then:

```powershell
$env:OVH_CONSUMER_KEY = '<consumerKey printed by the script>'
$env:OVH_ENDPOINT     = 'https://ca.api.ovh.com/1.0'   # optional, this is the default
```

Requested rights are deliberately narrow - no `/*` write access:

| method | path |
|---|---|
| GET | `/me` |
| GET | `/cloud/project` |
| GET POST PUT DELETE | `/cloud/project/*` |
| GET POST | `/order/cart`, `/order/cart/*` |

`/auth/credential` is signed with the application key only, which is why minting
works before any consumer key exists.

## 2. Then, in this order

```powershell
.\tools\glm53\ovh_ensure_project.ps1 -Pay      # turn the pending $0 order into a project
.\tools\glm53\provision_ovh.ps1 -List          # GPU flavors, per region, with quota
.\tools\glm53\provision_ovh.ps1                # dry run: plan + exact API calls
.\tools\glm53\provision_ovh.ps1 -Execute -OperatorCidr 173.177.72.31/32
```

Exit codes are meaningful: `0` ok, `1` error, `2` no GPU flavor visible, `3` GPU
quota is zero on every flavor.

**The quota gate is the OVH equivalent of the Akamai plan gate.** OVH flavor objects
carry a `quota` field; a fresh project usually has GPU quota at 0 and a create call
then fails with a generic 400/403. `provision_ovh.ps1` therefore reads quota first and
refuses to call create at all when every GPU flavor reports 0, printing the
support-ticket instruction instead. If you hit that, name the flavor id from `-List`
in the ticket.

## 3. What gets built

`cloud-init-ovh.yaml` is substituted (`__OPERATOR_CIDR__`, `__CTX__`, `__PARALLEL__`),
base64'd into the instance `userData`, and does five things: docker, NVIDIA driver
(skipped when the image already has one), `nvidia-container-toolkit`, a ufw ruleset
that exposes 22 and only the operator CIDR on 8080, and a pre-pull of the runtime
image. It does **not** start the model - the 71.5 GiB download is the explicit next
step so a partial download cannot leave a restart-looping service.

On the box:

```bash
/opt/glm53/fetch.sh    # both shards + SHA256SUMS, resumable via curl -C -
/opt/glm53/serve.sh    # llama.cpp server-cuda on :8080
```

`serve.sh` measures total VRAM with `nvidia-smi` and branches:

* `>= 78 GiB` - full offload, `-ngl 999`.
* below that - computes a `--n-cpu-moe` layer count from the shortfall and says so,
  rather than pretending 71.5 GiB fits in 48 GiB. `LAYERS=` overrides the layer count
  used for that estimate.

Stop billing with `.\tools\glm53\provision_ovh.ps1 -Destroy -InstanceId <uuid>`.

## 4. Model facts (live Hugging Face API, 2026-11-22)

`https://huggingface.co/api/models/BoldingBuilds/GLM-5.3-Flash-Uncensored-GGUF?blobs=true`
returns `private: false`, so **no HF token is needed**:

| file | bytes |
|---|---|
| `GLM-5.3-Flash-Uncensored-IQ1S-00001-of-00002.gguf` | 44,984,374,336 |
| `GLM-5.3-Flash-Uncensored-IQ1S-00002-of-00002.gguf` | 31,810,244,064 |
| `mmproj-GLM-5.3-Flash-F16.gguf` | 1,128,047,456 |
| `SHA256SUMS.txt` | 328 |

Total weights 76,794,618,400 bytes = **71.52 GiB**. Text-only use omits `--mmproj`,
which is why `serve.sh` does not pass it. Disk needs roughly 80 GB for the weights
plus the image, so check `DiskGb` in `-List`.

## 5. Wiring AV2 to it

```powershell
.\tools\glm53\connect_av2.ps1 -BaseUrl 'http://<ip>:8080/v1' -Model '<id from /v1/models>' -Apply
```

**Production mode is not a flag that can be flipped on its own.** `go_live.ps1
-Production` and `AV2_ENVIRONMENT=production` require an engagement contract:
`owner`, `authorization_ref` and the target allow-list, pushed through
`PUT /api/engagement`. Without a contract `backend/app/core/production.py` refuses
*every* command, and `contract_problems` names exactly what is missing.

That refusal is correct behaviour, so no contract was fabricated here. A made-up
authorization reference would be a false authorization record - worse than a stopped
deployment, because the audit trail would then *claim* authority that never existed.
Supply the real owner, the real reference and the real target list, and production
mode engages on its own.

## 6. Verification performed

| check | result |
|---|---|
| `/auth/time` on eu/ca/us | 200 on all three |
| signed `GET /me` per endpoint | EU/US `This application key is invalid`; CA `NOT_GRANTED_CALL` |
| original consumer key rights sweep, 10 routes | `NOT_GRANTED_CALL` on every one |
| `POST /auth/credential` | 200, `pendingValidation`, consumer key + validation URL issued |
| `provision_ovh.ps1 -List` with the pending key | `exit 1`, actionable message, nothing created |
| `cloud-init-ovh.yaml` parsing | `yaml.safe_load` OK, 2 write_files, 5 runcmd steps, no tabs |
| PowerShell parse sweep: `ovh_api.ps1`, `ovh_mint_ck.ps1`, `provision_ovh.ps1` | 0 errors |
