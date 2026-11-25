#!/usr/bin/env python3
"""Wake-on-request relay in front of the Azure GLM-5.3 GPU VM.

Why this exists
---------------
A deallocated Azure VM has no running process, so the VM cannot wake itself: the
"wake on API request" half of the design has to live somewhere that is always on.
That somewhere is this process, running on the same host as the AV2 backend.

    AV2 backend  ->  http://127.0.0.1:8430/v1    (this relay, loopback, no TLS)
                 ->  https://<vm-public-ip>/v1  (Caddy -> llama-server, TLS)

What it does
------------
* serves a cached ``/v1/models`` so AV2's 20 s health probe is instant;
* on the first inference request, starts the VM (``az vm start``) and blocks until
  llama-server answers ``/health``, then streams the request through untouched;
* deallocates the VM after ``--idle-minutes`` with no requests -- and **never** when
  ``--idle-minutes 0``, which is the production setting: the GPU stays up so that
  response times do not depend on a 8-12 minute weight reload;
* refuses to start the VM once the local spend ledger passes ``--credit-cap-usd``;
* enforces that same cap continuously in the watchdog.  A 24/7 GPU crosses the cap
  without any new request arriving, so a start-time-only check would never fire.
  The emergency stop refuses to deallocate while a generation is in flight: an
  abandoned 900 s completion costs pennies, killing it corrupts the run.

What it deliberately does not do
--------------------------------
* It does not pretend the GPU is up.  ``/__relay/state`` and the ``X-AV2-VM-State``
  header report the truth, because a green health check that hides a dead backend
  is exactly how the Venice endpoint looked healthy while returning 402.
* It does not re-implement the OpenAI API.  Request and response bytes are
  forwarded as-is; only ``Authorization`` is rewritten.

The spend ledger counts GPU-hours this relay itself started (``az vm start``) times
``--hourly-usd``.  It is an estimate, not a billing feed: hours started outside the
relay are invisible to it, so treat it as a tripwire, not an invoice.

Standard library only, so it runs on the AV2 host as-is.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_MODEL = "glm-5.3-flash-uncensored-iq1s"
READY_STATES = {"VM running"}


def resolve_az(value: str) -> str:
    """Find the Azure CLI when PATH has not caught up with a fresh install.

    ``shutil.which`` first, then the places Windows installs land.  An explicit
    ``--az`` path is never second-guessed; a missing CLI is still reported loudly
    by ``VmController._az`` rather than silently ignored.
    """
    if value != "az":
        return value
    found = shutil.which("az")
    if found:
        return found
    for candidate in (
        r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd",
        os.path.join(os.environ.get("APPDATA", ""), r"Python\Python311\Scripts\az.bat"),
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    return value


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


# --------------------------------------------------------------------------- state
@dataclass
class Ledger:
    """GPU time this relay started, persisted across restarts."""

    path: Path
    hourly_usd: float
    billed_seconds: float = 0.0
    started_at: float | None = None
    last_request_at: float | None = None
    wake_count: int = 0
    sleep_count: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def load(cls, path: Path, hourly_usd: float) -> "Ledger":
        ledger = cls(path=path, hourly_usd=hourly_usd)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ledger
        ledger.billed_seconds = float(raw.get("billed_seconds") or 0.0)
        ledger.last_request_at = raw.get("last_request_at")
        ledger.wake_count = int(raw.get("wake_count") or 0)
        ledger.sleep_count = int(raw.get("sleep_count") or 0)
        # A start recorded before a crash still burned money up to that moment.
        started = raw.get("started_at")
        if isinstance(started, (int, float)):
            ledger.billed_seconds += max(0.0, time.time() - float(started))
        return ledger

    def save(self) -> None:
        payload = {
            "billed_seconds": round(self.billed_seconds, 1),
            "started_at": self.started_at,
            "last_request_at": self.last_request_at,
            "wake_count": self.wake_count,
            "sleep_count": self.sleep_count,
            "hourly_usd": self.hourly_usd,
        }
        try:
            self.path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        except OSError as exc:  # disk full / permissions
            log(f"ledger write failed: {exc}")

    def usd(self, now: float | None = None) -> float:
        seconds = self.billed_seconds
        if self.started_at is not None:
            seconds += max(0.0, (now or time.time()) - self.started_at)
        return seconds / 3600.0 * self.hourly_usd

    def touch(self) -> None:
        with self.lock:
            self.last_request_at = time.time()
            self.save()

    def mark_started(self) -> None:
        with self.lock:
            self.started_at = time.time()
            self.wake_count += 1
            self.save()

    def adopt(self) -> None:
        """Take ownership of a VM that is already running (relay restarted).

        Without this the ledger has no start time, ``usd()`` stops growing, and the
        spend cap silently never fires -- the worst possible failure for a budget
        guard.  ``wake_count`` is deliberately left alone: this relay did not start
        that VM.
        """
        with self.lock:
            if self.started_at is None:
                self.started_at = time.time()
                self.save()

    def mark_stopped(self) -> None:
        with self.lock:
            if self.started_at is not None:
                self.billed_seconds += max(0.0, time.time() - self.started_at)
                self.started_at = None
            self.sleep_count += 1
            self.save()


# --------------------------------------------------------------------------- config
@dataclass
class RelayConfig:
    listen_host: str = "127.0.0.1"
    listen_port: int = 8430
    upstream: str = "https://127.0.0.1:8443"
    upstream_key: str = ""
    relay_key: str = ""
    model: str = DEFAULT_MODEL
    resource_group: str = "anon"
    vm_name: str = "av2-glm53"
    az: str = "az"
    # 0 = never deallocate.  Production runs 24/7 on purpose: a warm A100 answers in
    # ~1 s, a deallocated one costs 8-12 min of weight reload before the first token.
    idle_minutes: float = 0.0
    wake_timeout_s: float = 900.0
    poll_seconds: float = 5.0
    ca_cert: str = ""
    # Pay-as-you-go eastus, verified against the retail price API.  Spot is 0.679 but
    # is evicted with 30 s notice, which is incompatible with unattended production.
    hourly_usd: float = 3.673
    credit_cap_usd: float = 200.0
    warn_usd: float = 150.0
    state_file: str = ""
    autostart: bool = True
    # How often the watchdog looks at the ledger.  Lowered only by the self-test.
    watchdog_seconds: float = 10.0


class RelayError(RuntimeError):
    """Carries the HTTP status the client should see."""

    def __init__(self, status: int, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


# --------------------------------------------------------------------------- VM control
class VmController:
    """Thin wrapper over the ``az`` CLI; the only part that needs Azure."""

    def __init__(self, config: RelayConfig, ledger: Ledger) -> None:
        self.config = config
        self.ledger = ledger
        self._lock = threading.Lock()
        self._ready = threading.Event()

    def _az(self, *args: str, timeout: float = 180.0) -> subprocess.CompletedProcess[str]:
        cmd = [self.config.az, *args, "--only-show-errors", "-o", "json"]
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        except FileNotFoundError as exc:
            raise RelayError(500, f"'{self.config.az}' not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise RelayError(504, f"az {args[0]} {args[1]} timed out after {timeout:.0f}s") from exc

    def power_state(self) -> str:
        result = self._az(
            "vm", "show", "-d",
            "-g", self.config.resource_group,
            "-n", self.config.vm_name,
            "--query", "powerState",
            timeout=90.0,
        )
        if result.returncode != 0:
            raise RelayError(503, f"cannot read VM power state: {result.stderr.strip()[:300]}")
        return result.stdout.strip().strip('"') or "unknown"

    def start(self) -> None:
        log(f"az vm start {self.config.resource_group}/{self.config.vm_name}")
        result = self._az(
            "vm", "start", "-g", self.config.resource_group, "-n", self.config.vm_name,
            timeout=1200.0,
        )
        if result.returncode != 0:
            raise RelayError(503, f"az vm start failed: {result.stderr.strip()[:300]}")

    def deallocate(self) -> None:
        log(f"az vm deallocate {self.config.resource_group}/{self.config.vm_name}")
        result = self._az(
            "vm", "deallocate", "-g", self.config.resource_group, "-n", self.config.vm_name,
            timeout=900.0,
        )
        if result.returncode != 0:
            raise RelayError(503, f"az vm deallocate failed: {result.stderr.strip()[:300]}")

    # -- upstream readiness -------------------------------------------------
    def auth_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.config.upstream_key:
            headers["Authorization"] = f"Bearer {self.config.upstream_key}"
        return headers

    def ssl_context(self) -> ssl.SSLContext | None:
        if not self.config.upstream.startswith("https"):
            return None
        if self.config.ca_cert:
            return ssl.create_default_context(cafile=self.config.ca_cert)
        return ssl.create_default_context()

    def health_ok(self) -> bool:
        url = f"{self.config.upstream.rstrip('/')}/health"
        request = urllib.request.Request(url, headers=self.auth_headers())
        try:
            with urllib.request.urlopen(request, timeout=10, context=self.ssl_context()) as response:
                return response.status == 200
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def ensure_ready(self) -> None:
        """Block until llama-server answers /health, starting the VM if needed."""
        if self._ready.is_set() and self.health_ok():
            return
        with self._lock:
            if self._ready.is_set() and self.health_ok():
                return
            state = self.power_state() if self.config.autostart else "VM running"
            if state not in READY_STATES:
                if not self.config.autostart:
                    raise RelayError(503, f"VM is {state} and --no-autostart is set")
                spent = self.ledger.usd()
                if spent >= self.config.credit_cap_usd:
                    raise RelayError(
                        403,
                        f"spend cap reached: ${spent:.2f} of ${self.config.credit_cap_usd:.0f} "
                        "already burned; refusing to start the GPU again",
                    )
                if spent >= self.config.warn_usd:
                    log(f"WARNING: ${spent:.2f} of ${self.config.credit_cap_usd:.0f} spent")
                self.start()
                self.ledger.mark_started()
            deadline = time.time() + self.config.wake_timeout_s
            log(f"waiting for {self.config.upstream}/health (budget {self.config.wake_timeout_s:.0f}s)")
            while time.time() < deadline:
                if self.health_ok():
                    log("llama-server is healthy")
                    self._ready.set()
                    return
                time.sleep(self.config.poll_seconds)
            raise RelayError(
                504,
                f"llama-server did not become healthy within {self.config.wake_timeout_s:.0f}s",
                retry_after=60,
            )

    def mark_unready(self) -> None:
        self._ready.clear()

    def reconcile(self) -> str:
        """Adopt a VM this relay finds already running after a restart.

        Returns the observed state so the caller can report it instead of the
        placeholder ``unknown``.  Best effort: an unreachable Azure is logged, not
        fatal, because the relay is still useful as a plain proxy.
        """
        if not self.config.autostart:
            return "unmanaged"
        try:
            state = self.power_state()
        except RelayError as exc:
            log(f"cannot read VM state at startup: {exc}")
            return "unknown"
        if state in READY_STATES:
            self.ledger.adopt()
            log(f"VM was already {state}; ledger now counts from now "
                f"(${self.ledger.usd():.2f} of ${self.config.credit_cap_usd:.0f})")
        return state


# --------------------------------------------------------------------------- HTTP
class RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "av2-glm53-relay/1.0"

    # injected by build_server()
    config: RelayConfig
    vm: VmController
    ledger: Ledger
    flight: FlightCounter
    last_power_state: str

    # -- helpers -----------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # quieter than BaseHTTPRequestHandler
        log(f"{self.address_string()} {fmt % args}")

    def _send_json(self, status: int, payload: dict, retry_after: int | None = None) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-AV2-VM-State", self.last_power_state)
        if retry_after:
            self.send_header("Retry-After", str(retry_after))
        self.end_headers()
        self.wfile.write(raw)

    def _authorised(self) -> bool:
        if not self.config.relay_key:
            return True
        header = self.headers.get("Authorization") or ""
        return header == f"Bearer {self.config.relay_key}"

    def _refresh_power_state(self) -> str:
        try:
            self.last_power_state = self.vm.power_state()
        except RelayError as exc:
            self.last_power_state = f"unknown ({str(exc)[:60]})"
        return self.last_power_state

    # -- GET ---------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = self.path.split("?", 1)[0]
        if path in ("/health", "/__relay/health"):
            self._send_json(200, {"relay": "ok", "vm_state": self._refresh_power_state()})
            return
        if path == "/__relay/state":
            self._send_json(200, self._state_payload())
            return
        if path == "/v1/models":
            # Served from cache on purpose: AV2 probes this with a hard 20 s timeout
            # and only reads data[].id.  The truth about the GPU is in the
            # X-AV2-VM-State header and /__relay/state, not here.
            if not self._authorised():
                self._send_json(401, {"error": {"message": "bad relay key", "code": "unauthorised"}})
                return
            self._send_json(200, {"object": "list", "data": [{"id": self.config.model, "object": "model",
                                                            "owned_by": "av2-selfhost"}]})
            return
        if path.startswith("/v1/"):
            self._proxy("GET")
            return
        self._send_json(404, {"error": {"message": f"no route {path}", "code": "not_found"}})

    # -- POST --------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = self.path.split("?", 1)[0]
        if path == "/__relay/wake":
            try:
                self.vm.ensure_ready()
            except RelayError as exc:
                self._send_json(exc.status, {"error": {"message": str(exc), "code": "gpu_unavailable"}},
                                exc.retry_after)
                return
            self._send_json(200, self._state_payload())
            return
        if path == "/__relay/sleep":
            self._sleep_now()
            return
        if path.startswith("/v1/"):
            self._proxy("POST")
            return
        self._send_json(404, {"error": {"message": f"no route {path}", "code": "not_found"}})

    def _state_payload(self) -> dict:
        # Ask Azure rather than echoing the last value we happened to see: a state
        # endpoint that lies is worse than no state endpoint.
        self._refresh_power_state()
        idle_left = None
        if self.ledger.last_request_at:
            idle_left = max(0.0, self.config.idle_minutes * 60 - (time.time() - self.ledger.last_request_at))
        return {
            "vm_state": self.last_power_state,
            "model": self.config.model,
            "upstream": self.config.upstream,
            "in_flight": self.flight.value,
            "idle_seconds_until_sleep": idle_left,
            "idle_minutes": self.config.idle_minutes,
            "billed_seconds": round(self.ledger.billed_seconds, 1),
            "estimated_usd": round(self.ledger.usd(), 2),
            "credit_cap_usd": self.config.credit_cap_usd,
            "wake_count": self.ledger.wake_count,
            "sleep_count": self.ledger.sleep_count,
            "hourly_usd": self.config.hourly_usd,
        }

    def _sleep_now(self) -> None:
        busy = self.flight.value
        if busy:
            # The same rule as the watchdog: a paid-for generation is never worth
            # less than the minutes of GPU time saved by killing it.
            self._send_json(409, {"error": {
                "message": f"{busy} request(s) in flight; refusing to deallocate mid-generation",
                "code": "busy"}})
            return
        try:
            state = self.vm.power_state()
            if state == "VM running":
                self.vm.deallocate()
                self.vm.mark_unready()
                self.ledger.mark_stopped()
            self.last_power_state = state
            self._send_json(200, self._state_payload())
        except RelayError as exc:
            self._send_json(exc.status, {"error": {"message": str(exc), "code": "vm_error"}})

    # -- passthrough -------------------------------------------------------
    def _proxy(self, method: str) -> None:
        if not self._authorised():
            self._send_json(401, {"error": {"message": "bad relay key", "code": "unauthorised"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        try:
            self.vm.ensure_ready()
        except RelayError as exc:
            self._send_json(exc.status, {"error": {"message": str(exc), "code": "gpu_unavailable"}},
                            exc.retry_after)
            return

        self.ledger.touch()
        self.flight.enter()
        try:
            self._stream_upstream(method, body)
        finally:
            self.flight.leave()
            # A long generation is activity too; without this the watchdog would
            # deallocate the GPU in the middle of a stream.
            self.ledger.touch()

    def _stream_upstream(self, method: str, body: bytes | None) -> None:
        url = self.config.upstream.rstrip("/") + self.path
        headers = {"Accept": self.headers.get("Accept") or "application/json"}
        if self.headers.get("Content-Type"):
            headers["Content-Type"] = self.headers["Content-Type"]
        if self.config.upstream_key:
            headers["Authorization"] = f"Bearer {self.config.upstream_key}"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)

        try:
            upstream = urllib.request.urlopen(
                request,
                timeout=max(60.0, self.config.wake_timeout_s),
                context=self.vm.ssl_context(),
            )
        except urllib.error.HTTPError as exc:
            # Upstream answered with a real status: forward it verbatim so AV2 sees
            # llama.cpp's own error instead of a relay-shaped one.
            payload = exc.read()
            if exc.code >= 500:
                self.vm.mark_unready()
            self.send_response(exc.code)
            self.send_header("Content-Type", exc.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-AV2-VM-State", self.last_power_state)
            self.end_headers()
            self.wfile.write(payload)
            return
        except (urllib.error.URLError, OSError) as exc:
            self.vm.mark_unready()
            self._send_json(502, {"error": {"message": f"upstream unreachable: {exc}", "code": "upstream"}})
            return

        self.send_response(upstream.status)
        self.send_header("Content-Type", upstream.headers.get("Content-Type", "application/json"))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-AV2-VM-State", self.last_power_state)
        # Upstream streams SSE; chunked framing lets us forward bytes the moment
        # they arrive instead of buffering a whole completion in the relay.
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        try:
            while True:
                chunk = upstream.read(4096)
                if not chunk:
                    break
                self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii") + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # The client hung up (AV2 cancelled the turn).  Stop copying; the
            # upstream connection is closed by the context below.
            log("client disconnected mid-stream")
        finally:
            upstream.close()


# --------------------------------------------------------------------------- watchdog
def watchdog_loop(config: RelayConfig, vm: VmController, ledger: Ledger,
                  handler_cls: type[RelayHandler], stop: threading.Event) -> None:
    """Stop the GPU on the policy this relay was given, and never while it is busy.

    Two reasons to deallocate, in priority order:

    1. the spend cap was crossed -- this is the independent kill switch that a
       budget *alert* cannot provide, and it has to work on a 24/7 GPU where no
       further request will ever arrive to trigger the start-time check;
    2. the idle window elapsed -- off in production (``--idle-minutes 0``).

    A request in flight always wins.  Deallocating under a streaming completion
    turns a paid-for answer into a 500, so the watchdog reports and waits instead.
    """
    interval = max(0.1, config.watchdog_seconds)
    idle_limit = config.idle_minutes * 60
    cap_warned = False
    while not stop.wait(interval):
        try:
            busy = handler_cls.flight.value
            if ledger.started_at is None:
                # Nothing this relay believes it is paying for: nothing to stop.
                continue
            spent = ledger.usd()
            reason = ""
            if config.credit_cap_usd > 0 and spent >= config.credit_cap_usd:
                if busy:
                    if not cap_warned:
                        log(f"EMERGENCY STOP armed: ${spent:.2f} >= "
                            f"${config.credit_cap_usd:.0f}; holding off for {busy} "
                            "in-flight request(s)")
                        cap_warned = True
                    continue
                reason = f"spend cap reached (${spent:.2f} >= ${config.credit_cap_usd:.0f})"
            elif (idle_limit > 0 and not busy and ledger.last_request_at is not None
                  and time.time() - ledger.last_request_at >= idle_limit):
                reason = f"idle for {config.idle_minutes:g} min with no requests"
            if not reason:
                continue
            state = vm.power_state()
            handler_cls.last_power_state = state
            if state not in READY_STATES:
                # Already down (external teardown or a failed boot): bank the time
                # and stop counting instead of polling Azure every interval.
                log(f"{reason}, but the VM is {state}; ledger closed")
                ledger.mark_stopped()
                vm.mark_unready()
                continue
            log(f"{reason}; deallocating")
            vm.deallocate()
            vm.mark_unready()
            ledger.mark_stopped()
        except RelayError as exc:
            log(f"watchdog: {exc}")
        except Exception as exc:  # noqa: BLE001 - the watchdog must never die
            log(f"watchdog error: {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- plumbing
class FlightCounter:
    """Requests currently streaming; keeps the watchdog off a busy GPU."""

    def __init__(self) -> None:
        self._count = 0
        self._lock = threading.Lock()

    def enter(self) -> None:
        with self._lock:
            self._count += 1

    def leave(self) -> None:
        with self._lock:
            self._count = max(0, self._count - 1)

    @property
    def value(self) -> int:
        with self._lock:
            return self._count


def build_server(config: RelayConfig):
    state_path = Path(config.state_file) if config.state_file else Path(__file__).with_name(
        "wake_proxy.state.json")
    ledger = Ledger.load(state_path, config.hourly_usd)
    vm = VmController(config, ledger)
    handler = type(
        "BoundRelayHandler",
        (RelayHandler,),
        {
            "config": config,
            "vm": vm,
            "ledger": ledger,
            "flight": FlightCounter(),
            "last_power_state": "unknown",
        },
    )
    server = ThreadingHTTPServer((config.listen_host, config.listen_port), handler)
    server.daemon_threads = True
    return server, handler, vm, ledger


def parse_args(argv: list[str] | None = None) -> tuple[RelayConfig, bool]:
    parser = argparse.ArgumentParser(
        description="Wake-on-request relay for the Azure GLM-5.3 GPU VM.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--listen", default="127.0.0.1:8430", help="host:port to bind")
    parser.add_argument("--upstream", default="https://127.0.0.1:8443",
                        help="base URL of the VM's Caddy front end")
    parser.add_argument("--upstream-key", default="", help="llama-server --api-key value")
    parser.add_argument("--relay-key", default="", help="bearer AV2 must present (recommended)")
    parser.add_argument("--ca-cert", default="", help="pin Caddy's local root.crt")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--resource-group", default="anon")
    parser.add_argument("--vm-name", default="av2-glm53")
    parser.add_argument("--az", default="az", help="az CLI executable")
    parser.add_argument("--idle-minutes", type=float, default=0.0,
                        help="deallocate after this many quiet minutes; 0 keeps the GPU "
                             "up until the spend cap or teardown stops it")
    parser.add_argument("--wake-timeout", type=float, default=900.0)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--hourly-usd", type=float, default=3.673,
                        help="pay-as-you-go price per hour (Spot is 0.679 and evictable)")
    parser.add_argument("--credit-cap-usd", type=float, default=200.0)
    parser.add_argument("--warn-usd", type=float, default=150.0)
    parser.add_argument("--state-file", default="", help="ledger path (default: beside this script)")
    parser.add_argument("--no-autostart", action="store_true",
                        help="proxy only; never call az vm start")
    parser.add_argument("--self-test", action="store_true",
                        help="exercise wake/proxy/idle paths against stubs and exit")
    args = parser.parse_args(argv)

    host, _, port = args.listen.rpartition(":")
    config = RelayConfig(
        listen_host=host or "127.0.0.1",
        listen_port=int(port or 8430),
        upstream=args.upstream.rstrip("/"),
        upstream_key=args.upstream_key,
        relay_key=args.relay_key,
        model=args.model,
        resource_group=args.resource_group,
        vm_name=args.vm_name,
        az=resolve_az(args.az),
        idle_minutes=args.idle_minutes,
        wake_timeout_s=args.wake_timeout,
        poll_seconds=args.poll_seconds,
        ca_cert=args.ca_cert,
        hourly_usd=args.hourly_usd,
        credit_cap_usd=args.credit_cap_usd,
        warn_usd=args.warn_usd,
        state_file=args.state_file,
        autostart=not args.no_autostart,
    )
    return config, args.self_test


def main(argv: list[str] | None = None) -> int:
    config, run_self_test = parse_args(argv)
    if run_self_test:
        return self_test()

    server, handler, vm, ledger = build_server(config)
    stop = threading.Event()
    thread = threading.Thread(
        target=watchdog_loop, args=(config, vm, ledger, handler, stop),
        name="idle-watchdog", daemon=True,
    )
    thread.start()

    log(f"relay listening on http://{config.listen_host}:{config.listen_port}")
    log(f"upstream {config.upstream}  vm {config.resource_group}/{config.vm_name}")
    if config.idle_minutes > 0:
        log(f"idle deallocate after {config.idle_minutes:g} min  "
            f"budget ${ledger.usd():.2f}/${config.credit_cap_usd:.0f} "
            f"at ${config.hourly_usd:.3f}/h")
    else:
        log(f"idle deallocate DISABLED (always-on)  budget "
            f"${ledger.usd():.2f}/${config.credit_cap_usd:.0f} "
            f"at ${config.hourly_usd:.3f}/h = ${config.hourly_usd * 24:.2f}/day")
    handler.last_power_state = vm.reconcile()
    if not config.relay_key and config.listen_host not in ("127.0.0.1", "localhost"):
        log("WARNING: no --relay-key on a non-loopback bind; anyone who can reach the "
            "port can spend your GPU hours")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
    finally:
        stop.set()
        server.server_close()
    return 0


# --------------------------------------------------------------------------- self test
FAKE_AZ = r'''
import json, os, sys
from pathlib import Path
d = Path(os.environ["GLM53_STUB_DIR"])
state = d / "vm_state.txt"
if not state.exists():
    state.write_text("VM deallocated")
args = sys.argv[1:]
with (d / "az.log").open("a", encoding="utf-8") as fh:
    fh.write(" ".join(args) + "\n")
cur = state.read_text().strip()
verb = args[:2]
if verb == ["vm", "show"]:
    print(json.dumps(cur)); sys.exit(0)
if verb == ["vm", "start"]:
    state.write_text("VM running")
    (d / "health_polls.txt").unlink(missing_ok=True)
    print(json.dumps("VM running")); sys.exit(0)
if verb == ["vm", "deallocate"]:
    state.write_text("VM deallocated")
    print(json.dumps("VM deallocated")); sys.exit(0)
sys.exit(0)
'''

STUB_UPSTREAM = r'''
import json, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
d = Path(os.environ["GLM53_STUB_DIR"])
polls = d / "health_polls.txt"

class Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a):
        pass
    def _json(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    def do_GET(self):
        if self.path == "/health":
            n = (int(polls.read_text()) if polls.exists() else 0) + 1
            polls.write_text(str(n))
            if n < 3:
                self._json(503, {"status": "loading_model"}); return
            self._json(200, {"status": "ok"}); return
        if self.path == "/v1/models":
            self._json(200, {"object": "list", "data": [{"id": "glm-5.3-flash-uncensored-iq1s"}]}); return
        self._json(404, {})
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        body = b""
        for piece in ("he", "llo"):
            body += b"data: " + json.dumps({"choices": [{"delta": {"content": piece}}]}).encode() + b"\n\n"
        body += b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

ThreadingHTTPServer(("127.0.0.1", int(os.environ["GLM53_STUB_PORT"])), Stub).serve_forever()
'''


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def self_test() -> int:
    """Prove the wake / proxy / idle-deallocate paths without touching Azure.

    A fake ``az`` and a fake llama-server stand in for the two external
    dependencies, so this exercises exactly the code that would run for real.
    """
    import shutil
    import tempfile

    failures: list[str] = []
    total = 0

    def check(name: str, condition: bool, detail: str = "") -> None:
        nonlocal total
        total += 1
        verdict = "PASS" if condition else "FAIL"
        print(f"  [{verdict}] {name}" + (f" - {detail}" if detail and not condition else ""))
        if not condition:
            failures.append(name)

    work = Path(tempfile.mkdtemp(prefix="glm53-relay-"))
    upstream_proc = None
    server = None
    try:
        (work / "fake_az.py").write_text(FAKE_AZ, encoding="utf-8")
        (work / "stub_upstream.py").write_text(STUB_UPSTREAM, encoding="utf-8")
        az_cmd = work / "az.cmd"
        az_cmd.write_text(f'@echo off\r\n"{sys.executable}" "{work / "fake_az.py"}" %*\r\n',
                          encoding="utf-8")
        os.environ["GLM53_STUB_DIR"] = str(work)

        upstream_port = _free_port()
        os.environ["GLM53_STUB_PORT"] = str(upstream_port)
        upstream_proc = subprocess.Popen(
            [sys.executable, str(work / "stub_upstream.py")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        config = RelayConfig(
            listen_host="127.0.0.1",
            listen_port=_free_port(),
            upstream=f"http://127.0.0.1:{upstream_port}",
            upstream_key="upstream-secret",
            relay_key="relay-secret",
            az=str(az_cmd),
            # 3 s window, 0.25 s sweep, so the watchdog fires inside the test.  The
            # production setting is 0 (never), which phase 2 below checks.
            idle_minutes=0.05,
            wake_timeout_s=30.0,
            poll_seconds=0.25,
            watchdog_seconds=0.25,
            hourly_usd=3.673,
            credit_cap_usd=200.0,
            warn_usd=150.0,
            state_file=str(work / "ledger.json"),
        )

        for _ in range(80):
            with contextlib.suppress(OSError, urllib.error.URLError):
                urllib.request.urlopen(f"http://127.0.0.1:{upstream_port}/v1/models", timeout=1).close()
                break
            time.sleep(0.1)

        server, handler, vm, ledger = build_server(config)
        stop = threading.Event()
        watchdog = threading.Thread(target=watchdog_loop,
                                    args=(config, vm, ledger, handler, stop), daemon=True)
        watchdog.start()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{config.listen_port}"

        def call(path: str, *, method: str = "GET", key: str | None = "relay-secret",
                 payload: dict | None = None):
            data = json.dumps(payload).encode() if payload else None
            headers = {"Content-Type": "application/json"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=40) as response:
                    return response.status, response.read().decode("utf-8", "replace"), dict(response.headers)
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read().decode("utf-8", "replace"), dict(exc.headers)

        print("relay self-test")

        status, _, _ = call("/v1/models", key=None)
        check("models rejects a missing relay key", status == 401, f"status {status}")
        status, body, headers = call("/v1/models")
        check("models answers from cache with 200", status == 200 and "glm-5.3" in body, body[:120])
        check("models does not start the VM", not (work / "az.log").exists())
        check("VM state header is present", "X-AV2-VM-State" in headers)

        status, body, _ = call("/__relay/state")
        check("state reports the VM deallocated before any request",
              status == 200 and "VM deallocated" in body, body[:160])

        status, body, _ = call("/v1/chat/completions", method="POST",
                               payload={"model": "glm-5.3-flash-uncensored-iq1s",
                                        "messages": [{"role": "user", "content": "ping"}],
                                        "stream": True})
        check("inference is proxied after waking the VM",
              status == 200 and "he" in body and "llo" in body and "[DONE]" in body,
              f"status {status} body {body[:160]}")
        az_log = (work / "az.log").read_text(encoding="utf-8") if (work / "az.log").exists() else ""
        check("az vm start called exactly once", az_log.count("vm start") == 1, az_log)
        polls = (work / "health_polls.txt").read_text() if (work / "health_polls.txt").exists() else "0"
        check("boot wait really polled /health", int(polls) >= 3, polls)

        status, body, _ = call("/__relay/state")
        check("state flips to running", "VM running" in body, body[:160])
        check("ledger persisted to disk", (work / "ledger.json").exists())

        deadline = time.time() + 30
        while time.time() < deadline and "vm deallocate" not in az_log:
            time.sleep(1)
            az_log = (work / "az.log").read_text(encoding="utf-8")
        check("watchdog deallocates after the idle window", "vm deallocate" in az_log, az_log.strip())

        status, body, _ = call("/__relay/state")
        check("state flips back to deallocated", "VM deallocated" in body, body[:160])
        banked = json.loads((work / "ledger.json").read_text(encoding="utf-8"))
        check("billed seconds banked after sleep", banked["billed_seconds"] > 0, str(banked))
        check("sleep counter recorded", banked["sleep_count"] == 1, str(banked))

        # -- phase 2: production policy, --idle-minutes 0 -----------------------
        from dataclasses import replace

        stop.set()
        watchdog.join(timeout=10)
        prod = replace(config, idle_minutes=0.0, credit_cap_usd=200.0)
        prod_stop = threading.Event()
        threading.Thread(target=watchdog_loop, args=(prod, vm, ledger, handler, prod_stop),
                         daemon=True).start()

        status, body, _ = call("/v1/chat/completions", method="POST",
                               payload={"model": "glm-5.3-flash-uncensored-iq1s",
                                        "messages": [{"role": "user", "content": "again"}],
                                        "stream": True})
        check("always-on relay still serves after the VM was deallocated",
              status == 200 and "[DONE]" in body, f"status {status} body {body[:160]}")
        az_log = (work / "az.log").read_text(encoding="utf-8")
        sleep_count = az_log.count("vm deallocate")
        time.sleep(2.0)  # 8 watchdog sweeps, 28x the idle window used in phase 1
        az_log = (work / "az.log").read_text(encoding="utf-8")
        check("--idle-minutes 0 never deallocates an idle GPU",
              az_log.count("vm deallocate") == sleep_count, az_log.strip())

        # -- phase 3: the spend cap is a real kill switch, but never mid-stream --
        handler.flight.enter()                      # a generation is in flight
        ledger.started_at = time.time() - 3600      # one hour of a 3.673 USD/h GPU
        prod.credit_cap_usd = 1.0
        time.sleep(1.5)
        az_log = (work / "az.log").read_text(encoding="utf-8")
        check("spend cap does NOT deallocate under an in-flight request",
              az_log.count("vm deallocate") == sleep_count, az_log.strip())

        status, body, _ = call("/__relay/sleep", method="POST", payload={})
        az_log = (work / "az.log").read_text(encoding="utf-8")
        check("/__relay/sleep refuses while a generation is in flight",
              status == 409 and az_log.count("vm deallocate") == sleep_count,
              f"status {status} body {body[:120]}")

        handler.flight.leave()                      # the generation finished
        deadline = time.time() + 15
        while time.time() < deadline:
            az_log = (work / "az.log").read_text(encoding="utf-8")
            if az_log.count("vm deallocate") > sleep_count:
                break
            time.sleep(0.25)
        check("spend cap deallocates the GPU once it goes quiet",
              az_log.count("vm deallocate") == sleep_count + 1, az_log.strip())
        prod_stop.set()

        # -- phase 4: a VM left running across a relay restart must be adopted --
        vm.start()                                 # fake az now reports "VM running"
        fresh = Ledger.load(work / "fresh-ledger.json", 3.673)
        adopted = VmController(prod, fresh).reconcile()
        check("reconcile adopts an already-running VM (cap can then still fire)",
              adopted == "VM running" and fresh.started_at is not None
              and fresh.wake_count == 0 and fresh.usd() >= 0.0,
              f"state={adopted} started_at={fresh.started_at} wakes={fresh.wake_count}")
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if upstream_proc is not None:
            upstream_proc.terminate()
            with contextlib.suppress(Exception):
                upstream_proc.wait(timeout=10)
        shutil.rmtree(work, ignore_errors=True)

    print()
    if failures:
        print(f"SELF-TEST FAILED: {len(failures)} of {total} checks: {', '.join(failures)}")
        return 1
    print(f"SELF-TEST PASSED: {total}/{total} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
