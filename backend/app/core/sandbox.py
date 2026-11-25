"""Docker-backed execution sandbox for the Kali worker container.

The host backend never runs target-facing commands itself: it shells out to
``docker exec`` against a dedicated, isolated Kali container that has no host
mounts and no Docker socket.  Only the shared ``/work`` artefact volume is
writable, and that volume is declared by compose.
"""
from __future__ import annotations

import asyncio
import json
import shlex
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings


@dataclass(slots=True)
class SandboxResult:
    ok: bool
    command: str
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    truncated: bool = False
    denied: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "truncated": self.truncated,
            "denied": self.denied,
            "meta": self.meta,
        }

    def for_model(self) -> str:
        """Compact, token-frugal rendering handed back to the LLM."""
        parts = [f"$ {self.command}", f"exit={self.exit_code}"]
        if self.denied:
            parts.append("DENIED: " + self.stderr)
            return "\n".join(parts)
        if self.stdout:
            parts.append("--- stdout ---\n" + self.stdout)
        if self.stderr:
            parts.append("--- stderr ---\n" + self.stderr)
        if self.truncated:
            parts.append("[output truncated]")
        return "\n".join(parts)


@dataclass(slots=True)
class _AgentShell:
    """A persistent ``bash`` process inside the Kali container for one agent."""

    terminal: str
    workdir: str
    proc: asyncio.subprocess.Process
    lock: asyncio.Lock
    alive: bool = True
    commands: int = 0


class Sandbox:
    """Thin async wrapper around the Docker CLI targeting one container."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._tor_checked_at = 0.0
        self._tor_reachable = False
        #: One long-lived bash per agent id, so ``cd``, exports and shell history
        #: stay private to that agent instead of leaking between lanes.
        self._shells: dict[str, _AgentShell] = {}
        self._shell_lock = asyncio.Lock()

    # -- egress identity ---------------------------------------------------
    async def _tor_alive(self) -> bool:
        """True when the SOCKS5 sidecar answers inside the sandbox network."""
        now = time.monotonic()
        if now - self._tor_checked_at < 20.0:
            return self._tor_reachable
        host = self.settings.tor_socks_host
        port = self.settings.tor_socks_port
        code, out, _err = await self._run_host(
            [
                self.settings.docker_bin,
                "exec",
                self.settings.kali_container,
                "bash",
                "-lc",
                f"timeout 3 bash -c '</dev/tcp/{host}/{port}' && echo OPEN",
            ],
            timeout=12,
        )
        self._tor_checked_at = now
        self._tor_reachable = code == 0 and "OPEN" in out
        return self._tor_reachable

    async def egress_mode(self) -> str:
        """``tor`` when the proxy is wanted *and* up, otherwise ``direct``."""
        if self.settings.sandbox_egress != "tor":
            return "direct"
        return "tor" if await self._tor_alive() else "direct"

    def proxy_env(self) -> list[str]:
        """`env` assignments that put a command's HTTP clients behind Tor."""
        proxy = self.settings.tor_proxy_url
        return [
            "env",
            f"ALL_PROXY={proxy}",
            f"all_proxy={proxy}",
            f"HTTP_PROXY={proxy}",
            f"HTTPS_PROXY={proxy}",
            f"http_proxy={proxy}",
            f"https_proxy={proxy}",
            "NO_PROXY=127.0.0.1,localhost,::1",
            "no_proxy=127.0.0.1,localhost,::1",
        ]

    def proxy_env_pairs(self) -> list[str]:
        """``KEY=VALUE`` assignments (for ``docker exec -e``) behind Tor."""
        prefix = self.proxy_env()
        return prefix[1:] if prefix and prefix[0] == "env" else prefix

    # -- per-agent terminals ---------------------------------------------
    def terminal_state(self) -> list[dict[str, Any]]:
        """Live view of every persistent agent shell (used by /api/fleet)."""
        return [
            {
                "terminal": shell.terminal,
                "workdir": shell.workdir,
                "alive": bool(shell.alive and shell.proc.returncode is None),
                "commands": shell.commands,
            }
            for shell in self._shells.values()
        ]

    async def _spawn_shell(self, terminal: str, workdir: str) -> _AgentShell | None:
        """Open one persistent bash for ``terminal`` inside the sandbox."""
        args = [self.settings.docker_bin, "exec", "-i", "--workdir", workdir]
        if await self.egress_mode() == "tor":
            for pair in self.proxy_env_pairs():
                args.extend(["-e", pair])
        args.extend(
            [
                "-e",
                f"AV2_AGENT={terminal}",
                "-e",
                "TERM=dumb",
                self.settings.kali_container,
                "bash",
                "--noprofile",
                "--norc",
            ]
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                limit=1_048_576,
            )
        except (FileNotFoundError, OSError):
            return None
        shell = _AgentShell(terminal=terminal, workdir=workdir, proc=proc, lock=asyncio.Lock())
        marker = f"__AV2_READY_{uuid.uuid4().hex[:10]}__"
        try:
            text, _code = await self._roundtrip(shell, "printf 'ready'", marker, timeout=20)
        except (
            asyncio.TimeoutError,
            OSError,
            ValueError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            await self._kill_shell(terminal, shell)
            return None
        if "ready" not in text:
            await self._kill_shell(terminal, shell)
            return None
        return shell

    async def _roundtrip(
        self,
        shell: _AgentShell,
        command: str,
        marker: str,
        *,
        timeout: float,
    ) -> tuple[str, int]:
        """Send one command, read until the sentinel, return (output, exit code)."""
        proc = shell.proc
        stdin = proc.stdin
        stdout = proc.stdout
        if stdin is None or stdout is None:
            raise ValueError("shell has no pipes")
        script = (
            command
            + " < /dev/null\n"
            + f"printf '\\n{marker}%s\\n' \"$?\"\n"
        )
        stdin.write(script.encode("utf-8"))
        await stdin.drain()
        raw = await asyncio.wait_for(stdout.readuntil(marker.encode("utf-8")), timeout=timeout)
        code_line = await asyncio.wait_for(stdout.readline(), timeout=10)
        output = raw.decode("utf-8", "replace")
        if marker in output:
            # readuntil() includes the separator; it is framing, not output.
            output = output.split(marker, 1)[0]
        if output.endswith("\n"):
            output = output[:-1]
        try:
            code = int(code_line.decode("utf-8", "replace").strip() or "-1")
        except ValueError:
            code = -1
        return output, code

    async def _terminal_exec(
        self, terminal: str, command: str, *, timeout: int | None = None, cwd: str | None = None
    ) -> SandboxResult | None:
        """Run ``command`` in the agent's own shell; ``None`` means fall back."""
        if "\n" in command or "\r" in command:
            return None
        workdir = cwd or self.settings.kali_workdir
        shell = await self._ensure_shell(terminal, workdir)
        if shell is None:
            return None
        limit_s = float(timeout or self.settings.exec_timeout_s)
        started = time.perf_counter()
        marker = f"__AV2_END_{uuid.uuid4().hex[:12]}__"
        async with shell.lock:
            if shell.proc.returncode is not None:
                await self._kill_shell(terminal, shell)
                return None
            try:
                output, code = await self._roundtrip(shell, command, marker, timeout=limit_s)
            except (
                asyncio.TimeoutError,
                asyncio.IncompleteReadError,
                asyncio.LimitOverrunError,
                ConnectionResetError,
                BrokenPipeError,
                OSError,
                ValueError,
            ):
                # A hung or self-terminating command must not poison the fleet:
                # drop the shell and let the caller use the one-shot exec path.
                await self._kill_shell(terminal, shell)
                return None
        shell.commands += 1
        duration = int((time.perf_counter() - started) * 1000)
        limit = self.settings.exec_max_output_bytes
        truncated = len(output) > limit
        return SandboxResult(
            ok=code == 0,
            command=command,
            exit_code=code,
            stdout=output[:limit],
            stderr="" if code == 0 else output[: max(2000, limit // 4)],
            duration_ms=duration,
            truncated=truncated,
            meta={
                "workdir": workdir,
                "container": self.settings.kali_container,
                "egress": await self.egress_mode(),
                "terminal": terminal,
                "persistent": True,
            },
        )

    async def _ensure_shell(self, terminal: str, workdir: str) -> _AgentShell | None:
        current = self._shells.get(terminal)
        if (
            current is not None
            and current.alive
            and current.proc.returncode is None
            and current.workdir == workdir
        ):
            return current
        if current is not None:
            await self._kill_shell(terminal, current)
        async with self._shell_lock:
            current = self._shells.get(terminal)
            if (
                current is not None
                and current.alive
                and current.proc.returncode is None
                and current.workdir == workdir
            ):
                return current
            shell = await self._spawn_shell(terminal, workdir)
            if shell is not None:
                self._shells[terminal] = shell
            return shell

    async def _kill_shell(self, terminal: str, shell: _AgentShell | None = None) -> None:
        target = shell or self._shells.get(terminal)
        if target is None:
            return
        target.alive = False
        if self._shells.get(terminal) is target:
            self._shells.pop(terminal, None)
        try:
            if target.proc.returncode is None:
                target.proc.kill()
                await asyncio.wait_for(target.proc.wait(), timeout=10)
        except (ProcessLookupError, OSError, asyncio.TimeoutError):
            pass

    async def close(self) -> None:
        """Close every persistent agent shell (API shutdown / test teardown)."""
        for terminal in list(self._shells):
            await self._kill_shell(terminal)

    async def egress_report(self) -> dict[str, Any]:
        """Probe the address a target would actually see.

        The probe runs through the same ``exec`` path as every tool, so when
        the proxy is active it returns the Tor exit node, not the operator's
        public address.  ``ifconfig.co`` answers the address *and* the geo
        record in one response, which keeps ``ip`` and ``geo`` consistent even
        though Tor rotates circuits between destinations.
        """
        mode = await self.egress_mode()
        geo: dict[str, Any] = {}
        detail = ""
        geo_result = await self.exec(
            f"curl -s --max-time 25 {shlex.quote(self.settings.egress_geo_url)}",
            timeout=40,
        )
        if geo_result.ok and geo_result.stdout.strip():
            try:
                # ifconfig.co answers pretty-printed JSON: parse the whole body.
                parsed = json.loads(geo_result.stdout.strip())
            except json.JSONDecodeError:
                parsed = {}
            if isinstance(parsed, dict):
                geo = parsed
        else:
            detail = (geo_result.stderr or geo_result.stdout or "geo probe failed").strip()[:400]

        ip = str(geo.get("ip") or "").strip()
        if not ip:
            ip_result = await self.exec(
                f"curl -s --max-time 25 {shlex.quote(self.settings.egress_probe_url)}",
                timeout=40,
            )
            if ip_result.ok and ip_result.stdout.strip():
                ip = ip_result.stdout.strip().splitlines()[-1].strip()
            else:
                detail = (ip_result.stderr or ip_result.stdout or "probe failed").strip()[:400]

        if geo:
            # Normalise the handful of keys the UI/start-up summary reads; the
            # geo services disagree on names (org vs asn_org, region vs
            # region_name) and any of them may be suppressed for exit nodes.
            geo.setdefault("org", geo.get("asn_org") or geo.get("isp") or "")
            geo.setdefault("city", geo.get("city") or "")
            geo.setdefault("region", geo.get("region") or geo.get("region_name") or "")
            geo.setdefault("country", geo.get("country") or "")
        return {
            "mode": mode,
            "configured": self.settings.sandbox_egress,
            "proxy": self.settings.tor_proxy_url if mode == "tor" else "",
            "ip": ip,
            "geo": geo,
            "detail": detail,
        }

    # -- primitives ------------------------------------------------------
    async def _run_host(self, args: list[str], timeout: float | None = None) -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return 127, "", f"executable not found: {args[0]}"
        except OSError as exc:  # pragma: no cover - host specific
            return 127, "", f"failed to spawn {args[0]}: {exc}"
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(), timeout=timeout or self.settings.exec_timeout_s + 15
            )
        except asyncio.TimeoutError:
            # The child can exit between wait_for() timing out and kill(), in which
            # case Windows raises ProcessLookupError.  That used to escape and 500
            # /api/health, which also makes start.ps1 report "backend did not answer".
            try:
                proc.kill()
            except ProcessLookupError:  # pragma: no cover - host specific race
                pass
            await proc.wait()
            return 124, "", "timeout"
        return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    async def docker_available(self) -> tuple[bool, str]:
        code, out, err = await self._run_host(
            [self.settings.docker_bin, "info", "--format", "{{.ServerVersion}}"], timeout=25
        )
        if code == 0 and out.strip():
            return True, f"docker {out.strip()}"
        return False, (err or out or "docker daemon unreachable").strip()[:400]

    async def container_state(self) -> str:
        code, out, _err = await self._run_host(
            [self.settings.docker_bin, "inspect", "-f", "{{.State.Status}}", self.settings.kali_container],
            timeout=25,
        )
        return out.strip() if code == 0 else "missing"

    async def ensure_container(self) -> tuple[bool, str]:
        """Start the Kali container if it exists; never create one implicitly."""
        state = await self.container_state()
        if state == "running":
            return True, "running"
        if state in {"exited", "created", "paused"}:
            code, _out, err = await self._run_host(
                [self.settings.docker_bin, "start", self.settings.kali_container]
            )
            if code != 0:
                return False, f"failed to start {self.settings.kali_container}: {err.strip()}"
            return True, "started"
        if state == "missing":
            return False, (
                f"container '{self.settings.kali_container}' not found - run "
                "'docker compose -f docker/docker-compose.yml up -d' first"
            )
        return False, f"unexpected container state: {state}"

    # -- command execution ----------------------------------------------
    async def exec(
        self,
        command: str,
        *,
        timeout: int | None = None,
        cwd: str | None = None,
        terminal: str | None = None,
    ) -> SandboxResult:
        if terminal and self.settings.agent_terminals:
            # One persistent shell per agent: cwd, exports and process state are
            # private to that agent, and agents work in parallel
            # without sharing a working directory or racing on ``cd``.
            persistent = await self._terminal_exec(terminal, command, timeout=timeout, cwd=cwd)
            if persistent is not None:
                return persistent
        started = time.perf_counter()
        workdir = cwd or self.settings.kali_workdir
        inner = f"cd {shlex.quote(workdir)} && {command}"
        egress = await self.egress_mode()
        prefix = self.proxy_env() if egress == "tor" else []
        args = [
            self.settings.docker_bin,
            "exec",
            "--workdir",
            workdir,
            self.settings.kali_container,
            *prefix,
            "bash",
            "-lc",
            inner,
        ]
        code, out, err = await self._run_host(args, timeout=timeout or self.settings.exec_timeout_s)
        duration = int((time.perf_counter() - started) * 1000)
        limit = self.settings.exec_max_output_bytes
        truncated = len(out) > limit or len(err) > limit
        return SandboxResult(
            ok=code == 0,
            command=command,
            exit_code=code,
            stdout=out[:limit],
            stderr=err[: max(2000, limit // 4)],
            duration_ms=duration,
            truncated=truncated,
            meta={
                "workdir": workdir,
                "container": self.settings.kali_container,
                "egress": egress,
                "terminal": terminal or "",
                "persistent": False,
            },
        )

    async def read_file(
        self, path: str, *, max_bytes: int | None = None, cwd: str | None = None
    ) -> SandboxResult:
        limit = min(max_bytes or self.settings.exec_max_output_bytes, self.settings.exec_max_output_bytes)
        return await self.exec(f"head -c {limit} {shlex.quote(path)}", cwd=cwd)

    async def write_file(self, path: str, content: str, *, cwd: str | None = None) -> SandboxResult:
        """Write via stdin so arbitrary content never lands in the argv string."""
        started = time.perf_counter()
        quoted = shlex.quote(path)
        inner = f'mkdir -p "$(dirname {quoted})" && cat > {quoted}'
        args = [
            self.settings.docker_bin,
            "exec",
            "-i",
            "--workdir",
            cwd or self.settings.kali_workdir,
            self.settings.kali_container,
            "bash",
            "-lc",
            inner,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as exc:
            return SandboxResult(ok=False, command=f"write {path}", stderr=str(exc), denied=True)
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(content.encode("utf-8")),
                timeout=self.settings.exec_timeout_s,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return SandboxResult(ok=False, command=f"write {path}", exit_code=124, stderr="timeout")
        return SandboxResult(
            ok=(proc.returncode or 0) == 0,
            command=f"write {len(content)}B -> {path}",
            exit_code=proc.returncode or 0,
            stdout=out.decode("utf-8", "replace")[:2000],
            stderr=err.decode("utf-8", "replace")[:2000],
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    async def list_dir(self, path: str, *, cwd: str | None = None) -> SandboxResult:
        return await self.exec(f"ls -lah {shlex.quote(path)}", cwd=cwd)

    # -- operator file exchange -------------------------------------------
    async def copy_in(self, local_path: str, container_path: str, *, timeout: int = 180) -> SandboxResult:
        """Copy a host file or directory into the container (``docker cp``)."""
        code, out, err = await self._run_host(
            [
                self.settings.docker_bin,
                "cp",
                str(local_path),
                f"{self.settings.kali_container}:{container_path}",
            ],
            timeout=timeout,
        )
        return SandboxResult(
            ok=code == 0,
            command=f"docker cp {local_path} -> {container_path}",
            exit_code=code,
            stdout=out[:4_000],
            stderr=err[:4_000],
        )

    async def copy_out(self, container_path: str, local_path: str, *, timeout: int = 180) -> SandboxResult:
        """Copy a container file or directory back to the host (``docker cp``)."""
        code, out, err = await self._run_host(
            [
                self.settings.docker_bin,
                "cp",
                f"{self.settings.kali_container}:{container_path}",
                str(local_path),
            ],
            timeout=timeout,
        )
        return SandboxResult(
            ok=code == 0,
            command=f"docker cp {container_path} -> {local_path}",
            exit_code=code,
            stdout=out[:4_000],
            stderr=err[:4_000],
        )

    async def host_exec(self, args: list[str], *, timeout: float | None = None) -> tuple[int, str, str]:
        """Run a host-side command (used by the file exchange for ``docker cp``)."""
        return await self._run_host(args, timeout=timeout)

