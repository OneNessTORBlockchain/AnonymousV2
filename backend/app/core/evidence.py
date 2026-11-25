"""Evidence standard, tamper-evident store and replay comparison.

Anonymous V2 never reports a claim that cannot be audited.  Every operation that
produces a finding carries the six mandatory elements below; the store hashes
them, chains each record to the previous one and labels the claim ``confirmed``
only when nothing is missing.

1. ``command``       - the exact command line that was executed.
2. ``output``        - the raw result, byte-for-byte as the tool returned it.
3. ``vulnerability`` - the weakness it demonstrates (CVE, CWE or precise name).
4. ``reproduction``  - the steps another operator needs to reproduce it.
5. ``impact``        - what an attacker gains, in business terms.
6. ``verdict``       - ``confirmed``, ``unconfirmed`` or ``refuted``.

A blockchain claim additionally needs the real ``tx_hash``, the ``chain``, the
receipt ``receipt_status``, the ``confirmations`` depth and the explorer URL.
Submission is never confirmation: an unconfirmed transaction is reported as
unconfirmed, with the missing fields named in ``proof_gaps``.

The store is append-only from the application's point of view: every record
embeds ``prev_digest`` and its own ``digest``, so :meth:`EvidenceStore.verify_chain`
can prove that no line was edited after the fact.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

#: The six mandatory elements, in report order.
EVIDENCE_FIELDS: tuple[str, ...] = (
    "command",
    "output",
    "vulnerability",
    "reproduction",
    "impact",
    "verdict",
)

#: One-line reminder per field, reused by the tool description and the prompt.
FIELD_GUIDE: dict[str, str] = {
    "command": "exact command line executed inside the sandbox",
    "output": "raw stdout/stderr exactly as returned, no paraphrase",
    "vulnerability": "CVE / CWE / precise weakness name",
    "reproduction": "steps that reproduce it from a clean sandbox",
    "impact": "what an attacker gains (data, privilege, funds, availability)",
    "verdict": "confirmed | unconfirmed | refuted",
}

VERDICTS: tuple[str, ...] = ("confirmed", "unconfirmed", "refuted")

#: Required blockchain fields when ``blockchain`` is present.
BLOCKCHAIN_FIELDS: tuple[str, ...] = (
    "tx_hash",
    "chain",
    "receipt_status",
    "confirmations",
    "explorer_url",
)

#: Raw output shorter than this cannot prove anything.
MIN_OUTPUT_CHARS = 8

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalise_output(text: str) -> str:
    """Strip ANSI colour and trailing whitespace so digests are stable."""
    cleaned = _ANSI.sub("", str(text or ""))
    lines = [line.rstrip() for line in cleaned.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def digest_text(text: str) -> str:
    return hashlib.sha256(normalise_output(text).encode("utf-8", "replace")).hexdigest()


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_session(session_id: str) -> str:
    return _SAFE.sub("_", session_id or "session")[:96] or "session"


@dataclass(slots=True)
class Evidence:
    """One auditable claim and everything needed to re-check it."""

    title: str
    target: str
    vulnerability: str = ""
    command: str = ""
    output: str = ""
    reproduction: str = ""
    impact: str = ""
    verdict: str = "unconfirmed"
    severity: str = "info"
    session_id: str = ""
    agent: str = ""
    evidence_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    tags: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    blockchain: dict[str, Any] = field(default_factory=dict)
    captured_at: str = field(default_factory=utc_now)
    status: str = "unconfirmed"
    proof_gaps: list[str] = field(default_factory=list)
    output_sha256: str = ""
    command_sha256: str = ""
    prev_digest: str = ""
    digest: str = ""


    # -- derived ---------------------------------------------------------
    def blockchain_gaps(self) -> list[str]:
        if not self.blockchain:
            return []
        gaps: list[str] = []
        for name in BLOCKCHAIN_FIELDS:
            value = self.blockchain.get(name)
            if value in (None, "", [], {}):
                gaps.append(f"blockchain.{name} missing")
        status = str(self.blockchain.get("receipt_status") or "").lower()
        if status and status not in {"success", "0x1", "confirmed", "finalized"}:
            gaps.append(f"blockchain.receipt_status={status} (not a successful execution)")
        try:
            confirmations = int(self.blockchain.get("confirmations") or 0)
        except (TypeError, ValueError):
            confirmations = 0
        if confirmations <= 0:
            gaps.append("blockchain.confirmations=0 (not confirmed on-chain)")
        if self.blockchain.get("verified") is False:
            gaps.append("blockchain.verified=false")
        return gaps

    def compute_gaps(self) -> list[str]:
        gaps: list[str] = []
        if not self.command.strip():
            gaps.append("command missing")
        if len(normalise_output(self.output)) < MIN_OUTPUT_CHARS:
            gaps.append("output missing or too short to prove anything")
        if not self.vulnerability.strip():
            gaps.append("vulnerability missing (CVE/CWE or precise name)")
        if not self.reproduction.strip():
            gaps.append("reproduction missing")
        if not self.impact.strip():
            gaps.append("impact missing")
        verdict = (self.verdict or "").strip().lower()
        if verdict not in VERDICTS:
            gaps.append(f"verdict '{self.verdict}' is not one of {', '.join(VERDICTS)}")
        gaps.extend(self.blockchain_gaps())
        return gaps

    def seal(self) -> "Evidence":
        """Recompute hashes, downgrade unproven confirmations, keep the verdict."""
        self.verdict = (self.verdict or "unconfirmed").strip().lower()
        if self.verdict not in VERDICTS:
            self.verdict = "unconfirmed"
        self.output = normalise_output(self.output)
        self.output_sha256 = digest_text(self.output) if self.output else ""
        self.command_sha256 = digest_text(self.command) if self.command else ""
        self.proof_gaps = self.compute_gaps()
        if self.proof_gaps and self.verdict == "confirmed":
            self.status = "unconfirmed"
        else:
            self.status = self.verdict
        self.digest = self.compute_digest()
        return self

    def compute_digest(self) -> str:
        payload = self.to_dict()
        payload.pop("digest", None)
        return hashlib.sha256(_canonical(payload).encode("utf-8", "replace")).hexdigest()

    # -- serialisation ---------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "title": self.title,
            "target": self.target,
            "vulnerability": self.vulnerability,
            "command": self.command,
            "output": self.output,
            "reproduction": self.reproduction,
            "impact": self.impact,
            "verdict": self.verdict,
            "status": self.status,
            "severity": self.severity,
            "session_id": self.session_id,
            "agent": self.agent,
            "tags": list(self.tags),
            "artifacts": list(self.artifacts),
            "blockchain": dict(self.blockchain),
            "captured_at": self.captured_at,
            "proof_gaps": list(self.proof_gaps),
            "output_sha256": self.output_sha256,
            "command_sha256": self.command_sha256,
            "prev_digest": self.prev_digest,
            "digest": self.digest,
        }

    def for_model(self) -> str:
        lines = [
            f"evidence_id={self.evidence_id} status={self.status} verdict={self.verdict} severity={self.severity}",
            f"target={self.target} vulnerability={self.vulnerability or '(none)'}",
            f"command: {self.command[:600] or '(none)'}",
            f"output_sha256: {self.output_sha256 or '(none)'} (raw length {len(self.output)})",
        ]
        if self.blockchain:
            lines.append(
                "blockchain: "
                + ", ".join(f"{key}={self.blockchain.get(key)}" for key in BLOCKCHAIN_FIELDS)
            )
        if self.proof_gaps:
            lines.append("proof gaps: " + "; ".join(self.proof_gaps))
        else:
            lines.append("proof: complete (6/6 elements, output hashed)")
        lines.append(f"chain digest: {self.digest[:16]}... (prev {self.prev_digest[:16] or 'genesis'})")
        return "\n".join(lines)


class EvidenceStore:
    """Append-only JSONL evidence store, hash-chained per session."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()
        self._last: dict[str, str] = {}

    def path(self, session_id: str) -> Path:
        return self.root / f"{_safe_session(session_id)}.jsonl"

    # -- writes ----------------------------------------------------------
    def append(self, record: Evidence) -> Evidence:
        with self._lock:
            path = self.path(record.session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            prev = self._last.get(path.stem)
            if prev is None:
                prev = self._tail(path)
            record.prev_digest = prev
            record.seal()
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
            self._last[path.stem] = record.digest
            return record

    def _tail(self, path: Path) -> str:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                return str(json.loads(line).get("digest") or "")
            except ValueError:
                return ""
        return ""

    # -- reads -----------------------------------------------------------
    def read(self, session_id: str) -> list[dict[str, Any]]:
        return _read_jsonl(self.path(session_id))

    def all(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        records: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*.jsonl")):
            for record in _read_jsonl(path):
                record.setdefault("session_id", path.stem)
                records.append(record)
        records.sort(key=lambda item: str(item.get("captured_at", "")), reverse=True)
        return records

    def get(self, evidence_id: str) -> dict[str, Any] | None:
        wanted = (evidence_id or "").strip()
        if not wanted:
            return None
        for record in self.all():
            if record.get("evidence_id") == wanted:
                return record
        return None

    # -- integrity -------------------------------------------------------
    def verify_chain(self, session_id: str) -> dict[str, Any]:
        records = self.read(session_id)
        prev = ""
        for index, record in enumerate(records):
            payload = dict(record)
            stored = str(payload.pop("digest", ""))
            prev_seen = str(payload.get("prev_digest") or "")
            expected = hashlib.sha256(_canonical(payload).encode("utf-8", "replace")).hexdigest()
            if prev_seen != prev:
                return {"ok": False, "records": len(records), "broken_at": index, "reason": "prev_digest mismatch"}
            if stored != expected:
                return {"ok": False, "records": len(records), "broken_at": index, "reason": "digest mismatch"}
            prev = stored
        return {"ok": True, "records": len(records), "broken_at": None, "reason": "chain intact"}

    def summary(self, session_id: str = "") -> dict[str, Any]:
        records = self.read(session_id) if session_id else self.all()
        by_status: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        with_gaps = 0
        for record in records:
            status = str(record.get("status") or "unconfirmed")
            by_status[status] = by_status.get(status, 0) + 1
            severity = str(record.get("severity") or "info")
            by_severity[severity] = by_severity.get(severity, 0) + 1
            if record.get("proof_gaps"):
                with_gaps += 1
        chain = self.verify_chain(session_id) if session_id else {"ok": None}
        return {
            "records": len(records),
            "confirmed": by_status.get("confirmed", 0),
            "unconfirmed": by_status.get("unconfirmed", 0),
            "refuted": by_status.get("refuted", 0),
            "with_gaps": with_gaps,
            "by_status": by_status,
            "by_severity": by_severity,
            "chain": chain,
        }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records

def compare_outputs(expected_digest: str, output: str) -> dict[str, Any]:
    """Compare a replayed command's output with the digest recorded earlier."""
    actual = digest_text(output)
    expected = (expected_digest or "").strip().lower()
    if not expected:
        return {"match": False, "reason": "no recorded digest", "expected": "", "actual": actual, "similarity": 0.0}
    return {
        "match": actual == expected,
        "reason": "identical normalised output" if actual == expected else "output differs from the recorded run",
        "expected": expected,
        "actual": actual,
        "similarity": 1.0 if actual == expected else 0.0,
    }


def build_evidence(
    *,
    title: str,
    target: str,
    vulnerability: str = "",
    command: str = "",
    output: str = "",
    reproduction: str = "",
    impact: str = "",
    verdict: str = "unconfirmed",
    severity: str = "info",
    session_id: str = "",
    agent: str = "",
    tags: Iterable[str] = (),
    artifacts: Iterable[str] = (),
    blockchain: dict[str, Any] | None = None,
    evidence_id: str = "",
) -> Evidence:
    """Assemble and seal one evidence record (hashes, gaps, effective status)."""
    record = Evidence(
        title=(title or "").strip()[:300],
        target=(target or "").strip()[:300],
        vulnerability=(vulnerability or "").strip()[:300],
        command=(command or "").strip()[:4_000],
        output=str(output or ""),
        reproduction=(reproduction or "").strip()[:4_000],
        impact=(impact or "").strip()[:4_000],
        verdict=(verdict or "unconfirmed").strip().lower(),
        severity=(severity or "info").strip().lower(),
        session_id=session_id,
        agent=agent,
        tags=[str(tag)[:60] for tag in tags if str(tag).strip()],
        artifacts=[str(item)[:300] for item in artifacts if str(item).strip()],
        blockchain=dict(blockchain or {}),
        evidence_id=evidence_id or uuid.uuid4().hex[:16],
    )
    return record.seal()


__all__ = [
    "BLOCKCHAIN_FIELDS",
    "EVIDENCE_FIELDS",
    "FIELD_GUIDE",
    "MIN_OUTPUT_CHARS",
    "VERDICTS",
    "Evidence",
    "EvidenceStore",
    "build_evidence",
    "compare_outputs",
    "digest_text",
    "normalise_output",
    "utc_now",
]

