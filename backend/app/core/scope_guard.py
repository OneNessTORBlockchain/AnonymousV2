"""Target bookkeeping for commands that leave the sandbox.

Anonymous V2 applies **no** scope restrictions.  This module is a *recorder*, not
a gatekeeper: :func:`extract_targets` pulls the host / IP / URL literals out of a
command line and :class:`ScopeGuard` annotates the command so the audit trail and
the UI can show what a step was aimed at.  Nothing is ever blocked and nothing
needs a human signature — each agent is free to decide what to run against the
operator's attested targets.

The program sets below only drive the ``intrusive`` label on events; they do not
restrict anything.  The operator-supplied target list in ``storage/scope.json`` is
context that is handed to the model, never a whitelist.
"""
from __future__ import annotations

import ipaddress
import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

#: Programs that are labelled as hands-on / offensive tooling (informational).
INTRUSIVE_PROGRAMS = {
    "sqlmap", "hydra", "medusa", "john", "hashcat", "msfconsole", "msfvenom", "metasploit",
    "crackmapexec", "nxc", "impacket-smbexec", "impacket-psexec", "impacket-wmiexec",
    "impacket-secretsdump", "responder", "bettercap", "ettercap", "aircrack-ng", "reaver",
    "commix", "xsser", "ysoserial", "beef-xss", "patator", "wfuzz", "cadaver", "weevely",
}

#: Programs that are labelled as discovery tooling (informational).
RECON_PROGRAMS = {
    "nmap", "masscan", "whois", "dig", "host", "nslookup", "traceroute", "tracert", "ping",
    "curl", "wget", "whatweb", "wafw00f", "dnsenum", "dnsrecon", "fierce", "theharvester",
    "amass", "subfinder", "assetfinder", "gobuster", "ffuf", "feroxbuster", "dirb",
    "dirsearch", "nuclei", "nikto", "wpscan", "enum4linux", "smbclient", "snmpwalk",
    "ldapsearch", "openssl", "nc", "ncat", "netcat", "socat", "ssh-keyscan", "testssl",
    "sslscan", "gospider", "katana", "httpx", "subjack", "dalfox", "arjun", "paramspider",
    "waybackurls", "gau", "jsluice", "searchsploit",
}

_SEPARATORS = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")
_URL_RE = re.compile(r"\b[a-zA-Z][\w+.-]*://([^/\s:'\"`]+)")
_IPV4_RE = re.compile(r"(?<![\w.])((?:\d{1,3}\.){3}\d{1,3})(?:/(\d{1,2}))?(?![\w.])")
_HOST_RE = re.compile(r"(?<![\w.@-])((?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,24})(?![\w.-])")
_FLAGISH = re.compile(r"^[-./]")
_LOOPBACK = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
_LOCAL_SUFFIXES = (".local", ".internal", ".test", ".lan", ".lab", ".example")


@dataclass(slots=True)
class ScopeEntry:
    value: str
    kind: str = "host"
    mode: str = "exploit"
    note: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, str]:
        return {
            "value": self.value,
            "kind": self.kind,
            "mode": self.mode,
            "note": self.note,
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class Decision:
    """Annotation attached to one command line.  ``allowed`` is always ``True``."""

    allowed: bool
    reason: str
    targets: list[str] = field(default_factory=list)
    intrusive: bool = False
    program: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "targets": self.targets,
            "intrusive": self.intrusive,
            "program": self.program,
        }


def canonical_target(token: str) -> str:
    """Normalise a URL/host/ip literal to a comparable lowercase host string.

    IP prefixes such as ``10.0.0.0/24`` are preserved (and normalised through
    :mod:`ipaddress`); only URL paths are stripped.
    """
    token = token.strip().strip("\"'`,;()[]{}<>")
    if "://" in token:
        token = token.split("://", 1)[1]
    candidate = token.split("?", 1)[0].split("#", 1)[0]
    if "@" in candidate:
        candidate = candidate.rsplit("@", 1)[1]
    if "/" in candidate:
        try:
            return str(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            pass
    token = token.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in token:
        token = token.rsplit("@", 1)[1]
    if token.count(":") == 1 and not token.endswith(":"):
        token = token.split(":", 1)[0]
    return token.lower().rstrip(".")


def classify_target(value: str) -> str:
    value = value.strip()
    try:
        ipaddress.ip_network(value, strict=False)
        return "cidr" if "/" in value else "ip"
    except ValueError:
        pass
    return "url" if "://" in value else "host"


def extract_targets(command: str) -> list[str]:
    """Return every external target literal referenced by a command line."""
    found: list[str] = []
    for raw in _URL_RE.findall(command):
        found.append(canonical_target(raw))
    stripped = _URL_RE.sub(" ", command)
    for host, _suffix in _IPV4_RE.findall(stripped):
        found.append(host.lower())
    for host in _HOST_RE.findall(stripped):
        lowered = host.lower()
        if lowered.endswith(_LOCAL_SUFFIXES):
            continue
        found.append(lowered)
    seen: set[str] = set()
    ordered: list[str] = []
    for item in found:
        item = item.strip(".")
        if not item or item in _LOOPBACK or _FLAGISH.match(item) or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


class ScopeStore:
    """JSON-backed target list with a coarse-grained lock."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._entries: list[ScopeEntry] = []
        self.load()

    # -- persistence -----------------------------------------------------
    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                self._entries = []
                return
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8") or "[]")
            except (ValueError, OSError):
                self._entries = []
                return
            entries: list[ScopeEntry] = []
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for item in payload if isinstance(payload, list) else []:
                if isinstance(item, str) and item.strip():
                    entries.append(ScopeEntry(value=canonical_target(item), kind=classify_target(item), created_at=now))
                elif isinstance(item, dict) and item.get("value"):
                    raw = str(item["value"])
                    entries.append(
                        ScopeEntry(
                            value=canonical_target(raw),
                            kind=str(item.get("kind") or classify_target(raw)),
                            mode=str(item.get("mode") or "exploit"),
                            note=str(item.get("note") or ""),
                            created_at=str(item.get("created_at") or now),
                        )
                    )
            self._entries = entries

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([e.to_dict() for e in self._entries], indent=2), encoding="utf-8")

    # -- mutation --------------------------------------------------------
    def add(self, value: str, mode: str = "exploit", note: str = "") -> ScopeEntry:
        entry = ScopeEntry(
            value=canonical_target(value),
            kind=classify_target(value),
            mode=mode if mode in {"exploit", "intrusive", "recon"} else "exploit",
            note=note,
        )
        with self._lock:
            self._entries = [e for e in self._entries if e.value != entry.value]
            self._entries.append(entry)
            self._flush()
        return entry

    def remove(self, value: str) -> bool:
        target = canonical_target(value)
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if e.value != target]
            if len(self._entries) != before:
                self._flush()
                return True
        return False

    def clear(self) -> None:
        with self._lock:
            self._entries = []
            self._flush()

    # -- reads -----------------------------------------------------------
    def entries(self) -> list[ScopeEntry]:
        with self._lock:
            return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def match(self, target: str) -> ScopeEntry | None:
        """Return the noted entry covering ``target``, or ``None`` (lookup only)."""
        host = canonical_target(target)
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        for entry in self.entries():
            if entry.kind == "cidr" and address is not None:
                try:
                    if address in ipaddress.ip_network(entry.value, strict=False):
                        return entry
                except ValueError:
                    continue
            elif host == entry.value or host.endswith("." + entry.value):
                return entry
        return None


def split_program(segment: str) -> tuple[str, list[str]]:
    """Return ``(program_basename, args)`` for one simple command."""
    tokens = [t for t in segment.strip().split() if t]
    while tokens and tokens[0] in {"sudo", "env", "time", "nice", "command", "doas", "nohup"}:
        tokens.pop(0)
    if not tokens:
        return "", []
    return tokens[0].rsplit("/", 1)[-1], tokens[1:]


class ScopeGuard:
    """Passive command annotator wired to a :class:`ScopeStore`.

    ``evaluate`` never denies anything: it records the targets a command line
    references and whether it uses offensive tooling, so the audit trail and the
    UI stay informative while the agents keep full freedom of action.
    """

    def __init__(self, store: ScopeStore) -> None:
        self.store = store

    def evaluate(self, command: str, mode: str = "exploit") -> Decision:
        # ``mode`` is kept for API compatibility and has no effect.
        command = (command or "").strip()
        if not command:
            return Decision(True, "empty command", [], False, "")

        targets: list[str] = []
        intrusive = False
        last_program = ""
        for segment in _SEPARATORS.split(command):
            if not segment.strip():
                continue
            program, _args = split_program(segment)
            if not program:
                continue
            last_program = program
            targets.extend(extract_targets(segment))
            if program in INTRUSIVE_PROGRAMS:
                intrusive = True

        ordered: list[str] = []
        for target in targets:
            if target not in ordered:
                ordered.append(target)
        label = "offensive tooling" if intrusive else "discovery or local"
        return Decision(True, f"free agency: {label} command recorded", ordered, intrusive, last_program)
