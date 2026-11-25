"""Operational guardrails for live engagements.

``AV2_ENVIRONMENT=lab`` (the default) keeps the free-agency behaviour: commands
are annotated for the audit trail and nothing is refused.  ``production`` turns
the same annotations into hard, mechanical limits that do not depend on the
model's judgement:

* an engagement contract must exist (owner, authorization reference, window);
* every target literal in a command must match the contract allow-list;
* a kill switch refuses everything immediately;
* destructive classes (disk wipe, database drop, service shutdown, fund drain,
  mass encryption, credential dumping, denial of service) are refused unless the
  class is explicitly enabled in ``allowed_actions``;
* a token bucket caps command rate and the per-command timeout is clamped.

The guardrail is a *bookkeeping* boundary, not a moral one: it refuses nothing
the operator has not already written down, and the refusal message names the
exact missing authorization and the API call that grants it.
"""
from __future__ import annotations

import ipaddress
import json
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..config import Settings
from .scope_guard import _LOOPBACK, INTRUSIVE_PROGRAMS, canonical_target, extract_targets, split_program

ENVIRONMENTS: tuple[str, ...] = ("lab", "production")

#: (tag, pattern, plain-language description).  A tag must be listed in
#: ``allowed_actions`` before a matching command is allowed in production.
DESTRUCTIVE_RULES: tuple[tuple[str, str, str], ...] = (
    ("disk-wipe", r"\bmkfs(\.[a-z0-9]+)?\b|\bdd\b[^\n]*\bof=/dev/(sd|nvme|vd|xvd|hd|mmcblk)", "filesystem or raw block-device overwrite"),
    ("host-destruction", r"\b(?:rm\s+-(?:[a-z]*[rf][a-z]*\s+)+(?:/|/\*|~|\$HOME)\b|shutdown\b|poweroff\b|halt\b|reboot\b)", "host shutdown or recursive delete of a system/home path"),
    ("database-drop", r"\b(?:DROP\s+(?:DATABASE|SCHEMA)|TRUNCATE\s+TABLE)\b", "irreversible database deletion"),
    ("fund-drain", r"(?:--drain\b|drain[-_ ]?(?:funds?|account|wallet)\b|sweep[-_ ]?(?:all|funds)\b|transfer[-_ ]?all\b)", "moving or draining funds"),
    ("mass-encryption", r"\b(?:encrypt[-_ ]?(?:all|files|disk)|ransom(?:ware)?|crypto[-_ ]?locker)\b", "mass file encryption"),
    ("credential-dump", r"\b(?:secretsdump|mimikatz|lsass\.dmp|procdump[^\n]*lsass|ntdsutil)\b", "credential dumping"),
    ("denial-of-service", r"\b(?:hping3\b[^\n]*--flood|slowloris|--flood\b|siege\b[^\n]*-c\s*\d{2,})", "sustained load or flood"),
)

_ALLOWED_SEVERITIES = ("info", "low", "medium", "high", "critical")

_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
_HOSTPORT = re.compile(r"^(?P<host>\[[^\]]+\]|[^:/\s]+)(?::(?P<ports>\d+(?:-\d+)?))?$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_ports(spec: str) -> tuple[int, int] | None:
    spec = (spec or "").strip()
    if not spec:
        return None
    if "-" in spec:
        low, _, high = spec.partition("-")
    else:
        low = high = spec
    try:
        first, last = int(low), int(high)
    except ValueError:
        return None
    if not (0 < first <= 65535 and 0 < last <= 65535):
        return None
    return (min(first, last), max(first, last))


@dataclass(slots=True)
class TargetRule:
    """One allow-list entry: host, CIDR or domain, optionally with a port range."""

    raw: str
    host: str
    kind: str
    network: Any = None
    ports: tuple[int, int] | None = None

    def covers(self, host: str, port: int | None = None) -> bool:
        if self.ports and port is not None and not (self.ports[0] <= port <= self.ports[1]):
            return False
        if self.network is not None:
            try:
                return ipaddress.ip_address(host) in self.network
            except ValueError:
                return False
        host = host.lower().rstrip(".")
        if self.kind == "host":
            return host == self.host or host.endswith("." + self.host)
        return host == self.host

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "host": self.host,
            "kind": self.kind,
            "ports": f"{self.ports[0]}-{self.ports[1]}" if self.ports else "",
        }


def parse_rule(value: str) -> TargetRule | None:
    """Parse ``10.0.0.252``, ``10.0.0.0/24``, ``atm.example:8081`` or ``host:1-1024``."""
    raw = str(value or "").strip()
    if not raw:
        return None
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    raw = raw.split("?", 1)[0].split("#", 1)[0]
    # A CIDR keeps its slash; anything else loses a trailing path.
    try:
        network = ipaddress.ip_network(raw, strict=False)
        return TargetRule(raw=raw, host=str(network), kind="cidr", network=network, ports=None)
    except ValueError:
        raw = raw.split("/", 1)[0]
    match = _HOSTPORT.match(raw)
    if not match:
        return None
    host = match.group("host").strip("[]").lower().rstrip(".")
    ports = _parse_ports(match.group("ports") or "")
    try:
        network = ipaddress.ip_network(f"{host}/32", strict=False)
        kind = "ip"
    except ValueError:
        network = None
        kind = "host"
    return TargetRule(raw=raw, host=host, kind=kind, network=network, ports=ports)


def parse_rules(values: Iterable[str]) -> list[TargetRule]:
    rules: list[TargetRule] = []
    for value in values:
        rule = parse_rule(value)
        if rule is not None:
            rules.append(rule)
    return rules


#: Actions a production engagement allows unless the operator says otherwise.
DEFAULT_ACTIONS: tuple[str, ...] = (
    "recon",
    "vulnerability-scan",
    "exploit-validation",
    "auth-testing",
    "file-read",
)


@dataclass(slots=True)
class EngagementContract:
    """The operator's written authorization, in machine-checkable form."""

    engagement: str = ""
    owner: str = ""
    authorization_ref: str = ""
    environment: str = "production"
    opened_at: str = ""
    expires_at: str = ""
    allowed_targets: list[str] = field(default_factory=list)
    allowed_actions: list[str] = field(default_factory=list)
    excluded_targets: list[str] = field(default_factory=list)
    rate_limit_per_minute: int = 60
    max_command_timeout_s: int = 300
    max_commands: int = 0
    dry_run: bool = False
    kill_switch: bool = False
    notes: str = ""
    updated_at: str = ""

    # -- construction ----------------------------------------------------
    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "EngagementContract":
        data = payload if isinstance(payload, dict) else {}
        contract = cls(
            engagement=str(data.get("engagement") or ""),
            owner=str(data.get("owner") or ""),
            authorization_ref=str(data.get("authorization_ref") or ""),
            environment=str(data.get("environment") or "production").lower(),
            opened_at=str(data.get("opened_at") or ""),
            expires_at=str(data.get("expires_at") or ""),
            allowed_targets=[str(v) for v in data.get("allowed_targets") or [] if str(v).strip()],
            allowed_actions=[str(v).strip().lower() for v in data.get("allowed_actions") or [] if str(v).strip()],
            excluded_targets=[str(v) for v in data.get("excluded_targets") or [] if str(v).strip()],
            rate_limit_per_minute=max(1, int(data.get("rate_limit_per_minute") or 60)),
            max_command_timeout_s=max(5, int(data.get("max_command_timeout_s") or 300)),
            max_commands=max(0, int(data.get("max_commands") or 0)),
            dry_run=bool(data.get("dry_run", False)),
            kill_switch=bool(data.get("kill_switch", False)),
            notes=str(data.get("notes") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )
        if not contract.allowed_actions:
            contract.allowed_actions = list(DEFAULT_ACTIONS)
        if contract.environment not in ENVIRONMENTS:
            contract.environment = "production"
        return contract

    def to_dict(self) -> dict[str, Any]:
        return {
            "engagement": self.engagement,
            "owner": self.owner,
            "authorization_ref": self.authorization_ref,
            "environment": self.environment,
            "opened_at": self.opened_at,
            "expires_at": self.expires_at,
            "allowed_targets": list(self.allowed_targets),
            "allowed_actions": list(self.allowed_actions),
            "excluded_targets": list(self.excluded_targets),
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "max_command_timeout_s": self.max_command_timeout_s,
            "max_commands": self.max_commands,
            "dry_run": self.dry_run,
            "kill_switch": self.kill_switch,
            "notes": self.notes,
            "updated_at": self.updated_at,
        }

    # -- checks ----------------------------------------------------------
    def problems(self) -> list[str]:
        missing: list[str] = []
        if not self.owner.strip():
            missing.append("owner missing")
        if not self.authorization_ref.strip():
            missing.append("authorization_ref missing")
        if not self.allowed_targets:
            missing.append("allowed_targets empty")
        return missing

    def expired(self, now: datetime | None = None) -> bool:
        if not self.expires_at.strip():
            return False
        stamp = self.expires_at.strip().replace("Z", "+00:00")
        try:
            deadline = datetime.fromisoformat(stamp)
        except ValueError:
            return False
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return (now or datetime.now(timezone.utc)) > deadline

    def rules(self) -> list[TargetRule]:
        return parse_rules(self.allowed_targets)

    def exclusions(self) -> list[TargetRule]:
        return parse_rules(self.excluded_targets)

    def allows(self, host: str, port: int | None = None) -> bool:
        if any(rule.covers(host, port) for rule in self.exclusions()):
            return False
        return any(rule.covers(host, port) for rule in self.rules())

    def action_allowed(self, tag: str) -> bool:
        return tag.strip().lower() in {item for item in self.allowed_actions}


@dataclass(slots=True)
class GuardDecision:
    """Outcome of one guardrail evaluation."""

    allowed: bool
    mode: str
    reasons: list[str] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    intrusive: bool = False
    program: str = ""
    timeout_s: int = 180

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "mode": self.mode,
            "reasons": list(self.reasons),
            "targets": list(self.targets),
            "out_of_scope": list(self.out_of_scope),
            "actions": list(self.actions),
            "intrusive": self.intrusive,
            "program": self.program,
            "timeout_s": self.timeout_s,
        }

    def refusal(self) -> str:
        """Operator-facing message: what was refused and what would allow it."""
        detail = "; ".join(self.reasons) if self.reasons else "no reason recorded"
        return (
            f"BLOCKED by production guardrails: {detail}. "
            f"Program: {self.program or 'unknown'}; target: "
            f"{', '.join(self.out_of_scope or self.targets) or 'no external target'}. "
            "Nothing was executed. Fix the engagement contract (PUT /api/engagement) or, if the "
            "action is genuinely required, add its tag to allowed_actions under the owner's written "
            "authorization, then retry the same command."
        )


class GuardrailEngine:
    """Loads the contract and turns command annotations into yes/no answers."""

    def __init__(self, settings: Settings, contract: EngagementContract | None = None) -> None:
        self.settings = settings
        self.path: Path = settings.engagement_path
        self._lock = threading.Lock()
        self._hits: deque[float] = deque()
        self._executed = 0
        self._contract = contract if contract is not None else self.load()

    # -- persistence -----------------------------------------------------
    def load(self) -> EngagementContract | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict) or not payload:
            return None
        return EngagementContract.from_mapping(payload)

    def save(self, contract: EngagementContract) -> EngagementContract:
        with self._lock:
            contract.updated_at = _utc_now()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(contract.to_dict(), indent=2), encoding="utf-8")
            self._contract = contract
            self._hits.clear()
            self._executed = 0
        return contract

    def reload(self) -> EngagementContract | None:
        with self._lock:
            self._contract = self.load()
            return self._contract

    @property
    def contract(self) -> EngagementContract | None:
        return self._contract

    @property
    def mode(self) -> str:
        return "production" if self.settings.environment == "production" else "lab"

    # -- evaluation ------------------------------------------------------
    def evaluate(self, command: str, *, timeout_s: int | None = None) -> GuardDecision:
        command = (command or "").strip()
        contract = self._contract
        targets = _targets_with_ports(command)
        programs = _programs(command)
        program = programs[-1] if programs else ""
        intrusive = any(name in INTRUSIVE_PROGRAMS for name in programs)
        tags = _destructive_tags(command)
        timeout = int(timeout_s or self.settings.exec_timeout_s)
        decision = GuardDecision(
            allowed=True,
            mode=self.mode,
            targets=[_label(host, port) for host, port in targets],
            actions=tags,
            intrusive=intrusive,
            program=program,
            timeout_s=timeout,
        )
        if self.mode != "production":
            decision.reasons.append("lab mode: scope and actions are annotated, not enforced")
            return decision

        if contract is None:
            decision.allowed = False
            decision.reasons.append(f"no engagement contract at {self.path}")
            return decision
        if contract.kill_switch:
            decision.allowed = False
            decision.reasons.append("kill switch is engaged")
            return decision
        missing = contract.problems()
        if missing:
            decision.allowed = False
            decision.reasons.append("contract incomplete: " + ", ".join(missing))
            return decision
        if contract.expired():
            decision.allowed = False
            decision.reasons.append(f"engagement expired at {contract.expires_at}")
            return decision

        out_of_scope = [
            _label(host, port) for host, port in targets if not contract.allows(host, port)
        ]
        if out_of_scope:
            decision.allowed = False
            decision.out_of_scope = out_of_scope
            decision.reasons.append("targets outside the contract allow-list: " + ", ".join(out_of_scope))

        blocked_actions = [tag for tag in tags if not contract.action_allowed(tag)]
        if blocked_actions:
            decision.allowed = False
            decision.reasons.append("destructive action(s) not authorized: " + ", ".join(blocked_actions))

        decision.timeout_s = max(5, min(timeout, contract.max_command_timeout_s))
        if not self._rate_ok(contract.rate_limit_per_minute):
            decision.allowed = False
            decision.reasons.append(
                f"rate limit reached ({contract.rate_limit_per_minute}/minute); wait before retrying"
            )
        if contract.max_commands and self._executed >= contract.max_commands:
            decision.allowed = False
            decision.reasons.append(
                f"command budget exhausted ({self._executed}/{contract.max_commands})"
            )
        if contract.dry_run:
            decision.allowed = False
            decision.reasons.append("contract is in dry_run: commands are reported, never executed")

        if decision.allowed:
            self._record_hit()
            decision.reasons.append("in scope, action authorized, within rate limit")
        return decision

    # -- rate limiting ---------------------------------------------------
    def _rate_ok(self, limit: int) -> bool:
        now = time.monotonic()
        while self._hits and now - self._hits[0] > 60.0:
            self._hits.popleft()
        return len(self._hits) < limit

    def _record_hit(self) -> None:
        self._hits.append(time.monotonic())
        self._executed += 1

    # -- introspection ---------------------------------------------------
    def status(self) -> dict[str, Any]:
        contract = self._contract
        return {
            "mode": self.mode,
            "environment": self.settings.environment,
            "contract_path": str(self.path),
            "contract": contract.to_dict() if contract else None,
            "contract_problems": contract.problems() if contract else ["no contract"],
            "expired": contract.expired() if contract else True,
            "commands_used": self._executed,
            "max_commands": contract.max_commands if contract else 0,
            "recent_commands": len(self._hits),
        }

    def describe(self) -> str:
        """Compact block injected into the system prompt."""
        contract = self._contract
        if self.mode != "production":
            return (
                "GUARDRAILS: lab mode. Scope and destructive tags are recorded in the audit log but not "
                "enforced. Production mode (AV2_ENVIRONMENT=production) enforces the engagement contract."
            )
        if contract is None:
            return (
                "GUARDRAILS: production mode with NO contract loaded. Every external command will be refused. "
                "Ask the operator to PUT /api/engagement with owner, authorization_ref and allowed_targets."
            )
        lines = [
            "GUARDRAILS: production mode (mechanical, not a judgement call).",
            f"- engagement: {contract.engagement or '(unnamed)'} | owner: {contract.owner} | "
            f"authorization: {contract.authorization_ref}",
        ]
        if contract.expires_at:
            lines.append(f"- window closes: {contract.expires_at}{' (EXPIRED)' if contract.expired() else ''}")
        lines.append("- allow-list: " + ", ".join(contract.allowed_targets))
        if contract.excluded_targets:
            lines.append("- exclusions: " + ", ".join(contract.excluded_targets))
        lines.append("- authorized action tags: " + ", ".join(contract.allowed_actions))
        lines.append(
            f"- limits: {contract.rate_limit_per_minute} commands/minute, "
            f"timeout <= {contract.max_command_timeout_s}s"
            + (f", budget {contract.max_commands}" if contract.max_commands else "")
            + (" | kill switch ENGAGED" if contract.kill_switch else " | dry_run" if contract.dry_run else "")
        )
        lines.append(
            "A refused command returns the exact reason and the missing authorization. Never fabricate a "
            "target outside this list and never retry a refusal with a disguised command."
        )
        return "\n".join(lines)



# ---------------------------------------------------------------------------
# command introspection helpers
# ---------------------------------------------------------------------------
_URL_HOSTPORT = re.compile(
    r"\b[a-zA-Z][\w+.-]*://(?:[^@/\s]*@)?(?P<host>\[[^\]]+\]|[^:/\s'\"`]+)(?::(?P<port>\d{1,5}))?"
)
_IP_PORT = re.compile(
    r"(?<![\w.])(?P<host>(?:\d{1,3}\.){3}\d{1,3})(?::(?P<port>\d{1,5}))?(?![\w.])"
)
_HOST_PORT = re.compile(
    r"(?<![\w.@/-])(?P<host>(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,24})"
    r"(?::(?P<port>\d{1,5}))?(?![\w.-])"
)
_CHAIN = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")


def _programs(command: str) -> list[str]:
    """Every program basename in a possibly chained command line."""
    programs: list[str] = []
    for segment in _CHAIN.split(command or ""):
        program, _args = split_program(segment)
        if program:
            programs.append(program)
    return programs


def _destructive_tags(command: str) -> list[str]:
    """Destructive classes matched by ``command`` (empty when harmless)."""
    text = command or ""
    tags: list[str] = []
    for tag, pattern, _description in DESTRUCTIVE_RULES:
        try:
            if re.search(pattern, text, re.I):
                tags.append(tag)
        except re.error:
            continue
    return tags


def _targets_with_ports(command: str) -> list[tuple[str, int | None]]:
    """Hosts referenced by a command line, with the port when one is written.

    ``extract_targets`` only returns host literals, so URL/IP/host:port pairs are
    pulled out separately here: the contract may allow ``10.0.0.252:8081`` and the
    guardrail has to compare the port, not just the address.
    """
    found: list[tuple[str, int | None]] = []
    seen: set[tuple[str, int | None]] = set()

    def _add(host: str, port: str | int | None) -> None:
        host = canonical_target((host or "").strip())
        if not host or host in _LOOPBACK:
            return
        try:
            number = int(port) if port not in (None, "") else None
        except (TypeError, ValueError):
            number = None
        if number is not None and not (0 < number <= 65535):
            number = None
        key = (host, number)
        if key in seen:
            return
        seen.add(key)
        found.append(key)

    for match in _URL_HOSTPORT.finditer(command or ""):
        _add(match.group("host"), match.group("port"))
    for match in _IP_PORT.finditer(command or ""):
        _add(match.group("host"), match.group("port"))
    for match in _HOST_PORT.finditer(command or ""):
        _add(match.group("host"), match.group("port"))
    for host in extract_targets(command or ""):
        _add(host, None)
    return found


def _label(host: str, port: int | None) -> str:
    return f"{host}:{port}" if port else host


__all__ = [
    "DEFAULT_ACTIONS",
    "DESTRUCTIVE_RULES",
    "ENVIRONMENTS",
    "EngagementContract",
    "GuardDecision",
    "GuardrailEngine",
    "TargetRule",
    "parse_rule",
    "parse_rules",
]

