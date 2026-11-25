"""Engagement bookkeeping: noted targets plus the audit trail.

Anonymous V2 enforces no scope rules and no approval gate.  The engine simply
remembers the targets the operator cares about, hands a passive
:class:`~app.core.scope_guard.ScopeGuard` to the tool runtime and writes a line to
the audit log whenever the target list changes.

Every decision about *what* to run belongs to the agent, inside an engagement whose
authorization was attested by the operator before it opened: ownership of the
targets is never re-litigated at turn time and a task is never declined on
authorization grounds.
"""
from __future__ import annotations

from typing import Any

from ..config import Settings
from .audit import AuditLog
from .scope_guard import Decision, ScopeEntry, ScopeGuard, ScopeStore


class PolicyEngine:
    """Target bookkeeping and audit logging.  Nothing is blocked, ever."""

    def __init__(self, settings: Settings, audit: AuditLog | None = None) -> None:
        self.settings = settings
        self.audit = audit
        self.store = ScopeStore(settings.scope_path)
        self.guard = ScopeGuard(self.store)

    # -- targets ---------------------------------------------------------
    def scope(self) -> list[dict[str, Any]]:
        """Every noted target, newest last."""
        return [entry.to_dict() for entry in self.store.entries()]

    def scope_count(self) -> int:
        return len(self.store)

    def add_scope(self, value: str, mode: str = "exploit", note: str = "") -> ScopeEntry:
        entry = self.store.add(value, mode=mode, note=note)
        self._log("scope.add", value=entry.value, kind=entry.kind, mode=entry.mode, note=entry.note)
        return entry

    def remove_scope(self, value: str) -> bool:
        removed = self.store.remove(value)
        if removed:
            self._log("scope.remove", value=value)
        return removed

    def clear_scope(self) -> None:
        self.store.clear()
        self._log("scope.clear")

    # -- annotation ------------------------------------------------------
    def evaluate(self, command: str, mode: str = "exploit") -> Decision:
        """Label a command (targets, offensive tooling); the result always allows it."""
        decision = self.guard.evaluate(command, mode)
        self._log(
            "command.annotate",
            command=command,
            targets=decision.targets,
            intrusive=decision.intrusive,
            program=decision.program,
        )
        return decision

    # -- helpers ---------------------------------------------------------
    def _log(self, event: str, **fields: Any) -> None:
        if self.audit is not None:
            self.audit.write(event, **fields)
