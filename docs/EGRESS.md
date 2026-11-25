# Sandbox egress identity

The worker (Kali) must never contact a target from the operator's own address.
Two independent things are handled here: the *container address* and the
*public address a target sees*.

## 1. Container address

`docker/docker-compose.yml` pins `av2-net` to **10.213.77.1/24**:

| host               | address       |
|--------------------|---------------|
| bridge gateway     | 10.213.77.1   |
| `av2-tor` (SOCKS5) | 10.213.77.2   |
| `av2-kali` (tools) | 10.213.77.3   |

That range is chosen to sit outside the ranges this machine actually uses
(`10.0.0.0/24` Wi-Fi, `172.25.0.1` WSL) and outside Docker's defaults
(`172.17.0.0/16`, `172.18.0.0/16`).  No port of either container is published
on the host, so there is no inbound path and no LAN-reachable proxy.

Verify:

```powershell
docker inspect av2-kali --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'   # 10.213.77.3
docker inspect av2-tor  --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'   # 10.213.77.2
docker port av2-kali    # empty
docker port av2-tor     # empty
```

## 2. Public address (what a target logs)

`av2-tor` is a `dperson/torproxy` sidecar; `app/core/sandbox.py` prefixes every
`docker exec` with

```
ALL_PROXY / all_proxy / HTTP_PROXY / HTTPS_PROXY / http_proxy / https_proxy = socks5h://tor:9050
NO_PROXY = 127.0.0.1,localhost,::1
```

so curl, wget, sqlmap and anything using `requests`/libcurl exit through a Tor
exit node in another country.  `socks5h` also resolves DNS at the exit node, so
the target never sees the operator's resolver either.

Tools that open raw sockets (nmap, masscan, whatweb) ignore `*_PROXY`; for those
the image has proxychains-ng pre-pointed at the sidecar through
`docker/proxychains4.conf`:

```bash
proxychains4 nmap -sT -Pn -T4 <target>    # -sT: SOCKS has no raw SYN
```

## 3. Checking it

```powershell
Invoke-RestMethod http://127.0.0.1:8420/api/egress | Format-List
```

`start.ps1` calls the same endpoint and prints the summary on every restart:

```
--- egress identity ---
sandbox 204.8.96.120 (; ) mode=tor
host    173.177.72.31
distinct: yes - targets see a Tor exit node, not this device or city
```

`/api/egress` returns `mode`, `proxy`, `ip`, `geo` (city/region/country/org),
`host_ip` and `distinct`.  `/api/health` carries the resolved `egress` mode, and
every tool result's `meta.egress` records which path that command used.

Live check from inside the worker:

```powershell
docker exec av2-kali env ALL_PROXY=socks5h://tor:9050 curl -s https://api.ipify.org
```

## 4. Limits and switches

* Tor's **entry guard** still sees this machine's IP; that is how Tor works.
  The *target* only ever sees the exit node.
* Tor exits are blocked by some WAFs/CDNs and by ipinfo/ipapi (they rate-limit
  them).  Expect 403s on those and use `ifconfig.co` for geo lookups.
* Raw-socket scans are **not** proxied unless you prefix them with
  `proxychains4` — nothing silently pretends otherwise, `meta.egress` only
  describes the HTTP-proxy path.
* If `av2-tor` is stopped, `egress_mode()` resolves to `direct`, `/api/health`
  reports `egress=direct`, `start.ps1` prints `distinct: NO`.  Start it again
  with `docker start av2-tor` (or `.\start.ps1 -Docker`).
* Opt out for one engagement with `AV2_SANDBOX_EGRESS=direct` (set it in the
  environment or `.env` before starting the backend).
* New exit node on demand: `docker restart av2-tor`.
