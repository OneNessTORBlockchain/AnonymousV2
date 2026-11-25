"""Squads of five specialist agents working one task without repeating work.

A swarm is not "more agents".  It is *one* objective, N lanes per category, a
single dedup ledger, one shared blackboard and a strategist that re-plans at
every barrier.

Layout of a run
---------------
``categories``
    recon, web, exploit, coder, reporter.
``lanes per category``
    6 by default, so five categories give 30 concurrent agents.
``lane``
    its own terminal workdir (``/work/swarm/<run>/<lane>``), its own browser
    cookie profile, its own model conversation and a slice of the token budget.
``ledger``
    one append-only JSONL claim log per run.  A tool call, command line or HTTP
    request is normalised to a canonical key and may be claimed exactly once.
    A second lane that tries the same thing receives the *recorded result*
    instead of executing it again.  That is what makes "no duplicate commands,
    tools or requests" mechanical instead of a promise.
``blackboard``
    append-only JSONL of plans, lane results, tool records, hypotheses, operator
    notes and the final synthesis.  Every lane reads a digest of it before it
    acts, so the fleet actually collaborates instead of working blind.
``strategist``
    the cloud planner model.  It reads the digest after every barrier, merges
    lane hypotheses, and writes the next assignments (mission, success criteria,
    hypothesis, what to avoid).

Everything is async and bounded: a semaphore caps concurrency, a token budget
caps spend, a wall-clock deadline caps each round, and a stop flag is checked
between every step.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shlex
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..config import FLEET_GROUP_ROLES, FLEET_GROUP_SIZE, FLEET_MAX_GROUPS, Settings
from .agents import AGENT_META
from .audit import AuditLog
from .context_window import looks_like_context_overflow
from .evidence import EvidenceStore
from .ollama_client import ModelReply, OllamaClient, OllamaError
from .production import GuardrailEngine
from .sandbox import Sandbox
from .scope_guard import ScopeGuard
from .tools import ToolContext, ToolOutcome, ToolRuntime, names_for, specs_for

#: Agents per category on the classic swarm path.  ``MAX_LANES`` is a hard
#: ceiling and equals the largest fleet: 5 squads x 5 agents = 25.  A classic
#: run never puts more than one squad's five roles on a category.
DEFAULT_LANES = FLEET_GROUP_SIZE
MAX_LANES = FLEET_MAX_GROUPS * FLEET_GROUP_SIZE
#: Categories that may form lanes; the captain is the orchestrator, not a lane.
SWARM_CATEGORIES: tuple[str, ...] = ("recon", "web", "exploit", "coder", "reporter")
#: Tools a lane may not use: delegation and nested swarms would fork the fleet.
LANE_EXCLUDED_TOOLS = frozenset({"delegate", "swarm_execute", "swarm_status"})
#: Event types stay inside the shared TurnEvent vocabulary so the UI can render
#: swarm traffic with the same components as a normal turn.
_EVENT_TYPES = frozenset(
    {"start", "agent", "token", "thinking", "tool_call", "tool_result", "notice", "error", "done"}
)
#: Rough token estimate when the provider does not report usage.
_CHARS_PER_TOKEN = 4


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def utc_now() -> str:
    """UTC timestamp used by every record in this module."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:32]


def _clean(text: Any, limit: int = 4_000) -> str:
    if not isinstance(text, str):
        return ""
    return text.strip()[:limit]


def _norm(text: str) -> str:
    """Canonical form used for dedup: case-folded, whitespace-collapsed."""
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _tokens(reply: ModelReply) -> int:
    usage = reply.usage if isinstance(reply.usage, dict) else {}
    total = 0
    for key in ("prompt_eval_count", "eval_count", "total_tokens", "completion_tokens", "prompt_tokens"):
        try:
            total += int(usage.get(key) or 0)
        except (TypeError, ValueError):
            continue
    if total:
        return total
    return max(1, (len(reply.content) + len(reply.thinking)) // _CHARS_PER_TOKEN)


# ---------------------------------------------------------------------------
# transport failures - "the model said no" is not "the name did not resolve"
# ---------------------------------------------------------------------------
#: Errors that mean the provider hostname never resolved.  No request left the
#: machine, so nothing about the model, the prompt or the swarm is at fault.
_DNS_FAILURES = (
    "getaddrinfo failed",
    "name or service not known",
    "nodename nor servname provided",
    "temporary failure in name resolution",
    "no address associated with hostname",
    "wsahost_not_found",
    "[errno 11001]",
    "[errno -2]",
    "[errno -3]",
)
#: Errors that mean the socket opened (or tried to) and the endpoint did not
#: answer: egress, firewall, proxy or provider outage rather than DNS.
_CONNECT_FAILURES = (
    "connecterror",
    "connect error",
    "connect timeout",
    "connection refused",
    "connection reset",
    "connection attempts failed",
    "network is unreachable",
    "timed out",
    "timeout",
    "proxy",
    "ssl",
)


def _endpoint_host(base_url: str) -> str:
    """``https://api.venice.ai/api/v1`` -> ``api.venice.ai``."""
    match = re.match(r"^[a-z][a-z0-9+.-]*://([^/?#]+)", (base_url or "").strip(), re.IGNORECASE)
    if not match:
        return ""
    authority = match.group(1).rsplit("@", 1)[-1]
    if authority.startswith("["):  # IPv6 literal
        return authority.split("]")[0].lstrip("[")
    return authority.split(":", 1)[0]


def classify_model_failure(
    exc: BaseException | str, base_url: str = ""
) -> tuple[str, str]:
    """Name a failed model call by its real cause.

    Returns ``(kind, hint)``.  ``kind`` is ``"context"``, ``"dns"``, ``"connect"``,
    ``"http"`` or ``""`` (unknown); ``hint`` is the sentence an operator can act on.

    ``[Errno 11001] getaddrinfo failed`` is a Windows host-lookup failure.  It is
    reported today as ``all models terminated``, which sends the operator looking
    for a swarm bug that does not exist: the request never left the process.

    ``HTTP 400: request (4775 tokens) exceeds the available context size (4096
    tokens)`` used to land in the same bucket and told the operator to check the
    API key.  It is neither a key nor a network problem: the prompt is bigger than
    the window the daemon started with, and only a settings change fixes it.
    """
    text = exc if isinstance(exc, str) else str(exc)
    low = (text or "").lower()
    endpoint = _endpoint_host(base_url) or "the model endpoint"
    if looks_like_context_overflow(low):
        return "context", (
            f"{endpoint} refused the prompt because it does not fit the model's context window "
            f"({text}). Raise the window and keep AV2_PLANNER_NUM_CTX / AV2_WORKER_NUM_CTX inside "
            "it: they are sent as options.num_ctx on the native /api/chat path, and the inference "
            "host must allow it too (OLLAMA_CONTEXT_LENGTH, or PARAMETER num_ctx in the Modelfile). "
            "Ollama's /v1 shim drops options entirely. See docs/CONTEXT-WINDOW.md."
        )
    if any(marker in low for marker in _DNS_FAILURES):
        return "dns", (
            f"DNS lookup failed for {endpoint}: this host cannot resolve the model provider, so "
            "no request ever left the machine. Fix DNS, the VPN/proxy or the base URL "
            "(AV2_LLM_BASE_URL), then restart the API."
        )
    if any(marker in low for marker in _CONNECT_FAILURES):
        return "connect", (
            f"{endpoint} is not reachable ({text}). Check egress, firewall rules and provider "
            "status, then restart the run."
        )
    if low.startswith("http "):
        return "http", (
            f"{endpoint} answered with an error ({text}). Check the API key, the model id and "
            "the account quota; this is not a network problem."
        )
    return "", ""


# ---------------------------------------------------------------------------
# dedup ledger - the "never do the same thing twice" guarantee
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Claim:
    """Outcome of one ledger claim."""

    key: str
    lane: str
    kind: str
    duplicate: bool
    owner: str = ""
    ok: bool = False
    content: str = ""
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "lane": self.lane,
            "kind": self.kind,
            "duplicate": self.duplicate,
            "owner": self.owner,
            "ok": self.ok,
            "content": self.content,
            "created_at": self.created_at,
        }


class SwarmLedger:
    """One canonical key per executed unit of work, shared by every lane.

    ``run_command`` is keyed by the normalised command line, so ``nmap -p- x``
    issued by ``exploit-3`` blocks the identical call from ``exploit-5`` even if
    the timeout differs.  ``browser`` is keyed by method + URL + body, so two
    lanes cannot hammer the same endpoint twice.  Every other tool is keyed by
    its full JSON arguments.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._claims: dict[str, Claim] = {}
        self._executed = 0
        self._duplicates = 0

    # -- keys -------------------------------------------------------------
    def key_for(self, tool: str, args: dict[str, Any]) -> str:
        name = (tool or "").strip().lower()
        payload = args if isinstance(args, dict) else {}
        if name == "run_command":
            return _hash(f"run_command|{_norm(str(payload.get('command') or ''))}")
        if name == "browser":
            return _hash(
                "browser|"
                + "|".join(
                    _norm(str(payload.get(key) or ""))
                    for key in ("method", "url", "data")
                )
            )
        try:
            rendered = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            rendered = repr(payload)
        return _hash(f"{name}|{rendered}")

    # -- claims -----------------------------------------------------------
    def claim(self, key: str, lane: str, kind: str) -> Claim:
        """Claim ``key`` for ``lane``; ``duplicate`` is true when already taken."""
        with self._lock:
            existing = self._claims.get(key)
            if existing is not None:
                self._duplicates += 1
                return Claim(
                    key=key,
                    lane=lane,
                    kind=kind,
                    duplicate=True,
                    owner=existing.lane,
                    ok=existing.ok,
                    content=existing.content,
                    created_at=existing.created_at,
                )
            claim = Claim(key=key, lane=lane, kind=kind, duplicate=False)
            # Reserve immediately so two lanes cannot both "win" while one runs.
            self._claims[key] = claim
            return claim

    def record(self, key: str, lane: str, kind: str, ok: bool, content: str, data: dict[str, Any] | None = None) -> None:
        """Attach the real result to a claim and persist it."""
        excerpt = _clean(content, 8_000)
        with self._lock:
            claim = self._claims.get(key)
            if claim is None:
                claim = Claim(key=key, lane=lane, kind=kind, duplicate=False)
                self._claims[key] = claim
            claim.ok = bool(ok)
            claim.content = excerpt
            claim.created_at = utc_now()
            self._executed += 1
            record = {
                "key": key,
                "lane": lane,
                "kind": kind,
                "ok": bool(ok),
                "excerpt": excerpt[:2_000],
                "at": claim.created_at,
            }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            # The in-memory ledger still prevents duplicates if disk is full.
            pass

    def release(self, key: str) -> None:
        """Drop an unfinished claim (used when a tool crashed before recording)."""
        with self._lock:
            self._claims.pop(key, None)

    # -- reads ------------------------------------------------------------
    def get(self, key: str) -> Claim | None:
        with self._lock:
            return self._claims.get(key)

    def status(self) -> dict[str, Any]:
        with self._lock:
            kinds: dict[str, int] = {}
            for claim in self._claims.values():
                kinds[claim.kind] = kinds.get(claim.kind, 0) + 1
            return {
                "unique_claims": len(self._claims),
                "executed": self._executed,
                "duplicates_suppressed": self._duplicates,
                "by_tool": kinds,
                "rate_saved_pct": round(
                    (self._duplicates / (self._executed + self._duplicates)) * 100, 1
                )
                if (self._executed + self._duplicates)
                else 0.0,
            }


# ---------------------------------------------------------------------------
# shared blackboard - the "same memory, same picture" guarantee
# ---------------------------------------------------------------------------
class SwarmMemory:
    """Append-only board that every lane reads and every lane writes to."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._records: list[dict[str, Any]] = []

    def post(self, kind: str, *, lane: str = "", round_no: int = 0, **payload: Any) -> dict[str, Any]:
        record = {
            "kind": kind,
            "lane": lane,
            "round": round_no,
            "at": utc_now(),
            **(payload or {}),
        }
        with self._lock:
            self._records.append(record)
            self._maybe_write(record)
        return record

    def _maybe_write(self, record: dict[str, Any]) -> None:
        # Called with the lock held; best effort, never fatal.
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def recent(self, kind: str | None = None, limit: int = 40) -> list[dict[str, Any]]:
        with self._lock:
            records = list(self._records)
        if kind:
            records = [record for record in records if record.get("kind") == kind]
        return records[-max(1, limit) :]

    def counts(self) -> dict[str, int]:
        with self._lock:
            counts: dict[str, int] = {}
            for record in self._records:
                key = str(record.get("kind") or "")
                counts[key] = counts.get(key, 0) + 1
        return counts

    def digest(self, limit_chars: int = 6_000) -> str:
        """Compact transcript of the run, small enough to put in every prompt."""
        with self._lock:
            records = list(self._records)
        lines: list[str] = []
        for record in records:
            kind = record.get("kind")
            lane = record.get("lane") or "-"
            if kind == "plan":
                lines.append(f"[plan r{record.get('round')}] {str(record.get('strategy') or '')[:300]}")
            elif kind == "result":
                lines.append(
                    f"[result r{record.get('round')} {lane}] ok={record.get('ok')} "
                    f"tools={record.get('tool_calls')} dup={record.get('duplicates')} :: "
                    f"{str(record.get('summary') or '')[:320]}"
                )
            elif kind == "note":
                lines.append(f"[note r{record.get('round')} {lane}] {str(record.get('text') or '')[:240]}")
            elif kind == "tool":
                lines.append(
                    f"[tool r{record.get('round')} {lane}] {record.get('tool')} "
                    f"dup={record.get('duplicate')} ok={record.get('ok')} :: "
                    f"{str(record.get('excerpt') or '')[:200]}"
                )
            elif kind == "operator":
                lines.append(f"[operator] {str(record.get('text') or '')[:400]}")
            elif kind == "synthesis":
                lines.append(f"[synthesis] {str(record.get('text') or '')[:600]}")
            elif kind == "finding":
                lines.append(f"[finding {lane}] {str(record.get('text') or '')[:300]}")
        text = "\n".join(lines)
        if len(text) <= limit_chars:
            return text
        return "...\n" + text[-limit_chars:]

    def to_list(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._records[-max(1, limit) :])


# ---------------------------------------------------------------------------
# budget - tokens are the shared finite resource
# ---------------------------------------------------------------------------
class SwarmBudget:
    """Token and wall-clock budget for one run."""

    def __init__(self, tokens: int, seconds: int) -> None:
        self.limit = max(10_000, int(tokens))
        self.seconds = max(60, int(seconds))
        self.used = 0
        self.started = time.monotonic()
        self._lock = threading.Lock()

    def charge(self, tokens: int) -> None:
        with self._lock:
            self.used += max(0, int(tokens))

    def extend(self, *, tokens: int = 0, seconds: int = 0) -> dict[str, int]:
        """Raise the ceiling so the remaining rounds are actually paid for.

        A run may only end on success or when every requested round has run;
        hitting a fixed token window in the middle of a 20-round mission used to
        stop the fleet with the task half done.  The driver calls this when the
        window is reached and keeps the mission alive instead.
        """
        with self._lock:
            added_tokens = max(0, int(tokens))
            added_seconds = max(0, int(seconds))
            self.limit += added_tokens
            self.seconds += added_seconds
            return {"tokens": added_tokens, "seconds": added_seconds, "limit": self.limit}

    def exhausted(self) -> bool:
        with self._lock:
            return self.used >= self.limit or (time.monotonic() - self.started) >= self.seconds

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.limit - self.used)

    def status(self) -> dict[str, Any]:
        with self._lock:
            elapsed = int(time.monotonic() - self.started)
            return {
                "tokens_used": self.used,
                "tokens_limit": self.limit,
                "tokens_remaining": max(0, self.limit - self.used),
                "elapsed_s": elapsed,
                "seconds_limit": self.seconds,
            }


# ---------------------------------------------------------------------------
# run objects
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class LaneSpec:
    """One agent: category + index, with a private terminal and browser."""

    lane_id: str
    category: str
    index: int
    workdir: str
    browser_profile: str
    #: Cloud model this specific agent runs on (fleet lanes differ per model).
    model: str = ""
    #: Operator-facing id: "Agent 1" ... "Agent 25".
    label: str = ""
    #: Squad this agent belongs to (1-5); 0 on the legacy per-model roster.
    group: int = 0
    #: Role-aware Learning Board excerpt retrieved for the current assignment.
    learning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane_id": self.lane_id,
            "category": self.category,
            "index": self.index,
            "workdir": self.workdir,
            "browser_profile": self.browser_profile,
            "title": AGENT_META.get(self.category, ("Agent", ""))[0],
            "model": self.model,
            "label": self.label or self.lane_id,
            "group": self.group,
            "terminal": self.lane_id,
        }


#: Roles handed out to fleet agents in order.  One squad is exactly these five
#: roles - ``FLEET_GROUP_ROLES`` is the single source of truth, so the classic
#: roster can never invent a sixth specialist.
FLEET_ROLE_ORDER: tuple[str, ...] = FLEET_GROUP_ROLES


@dataclass(slots=True)
class LaneTask:
    """What the strategist ordered one lane to do this round."""

    mission: str
    success: str = ""
    hypothesis: str = ""
    avoid: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission": self.mission,
            "success": self.success,
            "hypothesis": self.hypothesis,
            "avoid": list(self.avoid),
        }


@dataclass(slots=True)
class LaneResult:
    """What a lane actually delivered."""

    lane_id: str
    category: str
    ok: bool = True
    summary: str = ""
    steps: int = 0
    tool_calls: int = 0
    duplicates: int = 0
    findings: int = 0
    tokens: int = 0
    error: str = ""
    #: True only when the lane proved its mission (a finding) or the round
    #: barrier ended it; an empty report leaves this False and the runner
    #: re-tasks the lane instead of treating "done" as completion.
    completed: bool = True
    #: True when every model available to this lane failed - the one condition
    #: (besides success and the round barrier) that may end a task.
    terminated: bool = False
    #: How many times enforcement pushed the lane back to work this round.
    enforced: int = 0
    reason: str = ""
    models_used: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane_id": self.lane_id,
            "category": self.category,
            "ok": self.ok,
            "summary": self.summary[:4_000],
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "duplicates": self.duplicates,
            "findings": self.findings,
            "tokens": self.tokens,
            "error": self.error,
            "completed": self.completed,
            "terminated": self.terminated,
            "enforced": self.enforced,
            "reason": self.reason,
            "models_used": list(self.models_used),
        }


@dataclass(slots=True)
class SwarmPlan:
    """Strategist output for one round."""

    round: int
    analysis: str = ""
    strategy: str = ""
    stop: bool = False
    tasks: dict[str, LaneTask] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "analysis": self.analysis[:2_000],
            "strategy": self.strategy[:2_000],
            "stop": self.stop,
            "tasks": {lane: task.to_dict() for lane, task in self.tasks.items()},
        }


class SwarmRun:
    """Live state of one swarm: lanes, ledger, board, budget, event stream."""

    def __init__(
        self,
        *,
        run_id: str,
        objective: str,
        session_id: str,
        categories: Iterable[str],
        lanes_per_category: int,
        rounds: int,
        model: str,
        root: Path,
        kali_workdir: str,
        budget_tokens: int,
        budget_seconds: int,
        lanes: Iterable[LaneSpec] | None = None,
        mode: str = "swarm",
        models: Iterable[str] | None = None,
        until_complete: bool = False,
    ) -> None:
        self.run_id = run_id
        self.objective = objective
        self.session_id = session_id
        self.categories = tuple(categories)
        self.lanes_per_category = max(1, min(MAX_LANES, int(lanes_per_category)))
        self.rounds = max(1, int(rounds))
        #: True keeps the strategist re-planning after a round that does not
        #: answer the objective; ``rounds`` is then the hard cap, and the run
        #: stops early only when the strategist sets ``stop`` (mission complete)
        #: or two consecutive rounds produce no new work.
        self.until_complete = bool(until_complete)
        self.mission_complete = False
        self.model = model
        #: ``fleet`` = fixed multi-model roster (default path), ``swarm`` = the
        #: classic N-lanes-per-category run started from /api/swarm.
        self.mode = "fleet" if mode == "fleet" else "swarm"
        self.models = tuple(dict.fromkeys(models or (model,)))
        #: Long-term lessons injected into every lane prompt (techniques, tools,
        #: errors not to repeat, strategies) and the skills the fleet already owns.
        self.lessons: str = ""
        self.skill_manual: str = ""
        #: Lanes whose last round ended without a proven result; the next round
        #: hands them a continuation mission instead of a fresh slice.
        self.unresolved: set[str] = set()
        #: Lanes killed by enforcement (repeatedly stopped without a finding).
        #: A terminated lane never runs again; the other lanes cover its slice.
        self.terminated: set[str] = set()
        #: Operator directives sent while the run was live.  They outrank the
        #: original objective and every mission the strategist already handed out
        #: - this is how "stop and follow this instead" reaches the fleet.
        self.directives: list[str] = []
        #: Verdict of the completion gate: {"approved": bool, "reason": str, ...}.
        self.completion: dict[str, Any] = {}
        #: Set when the run could not honour "all rounds" for a mechanical reason.
        self.stopped_reason: str = ""
        #: How many times the token/wall-clock window had to be raised.
        self.budget_extensions: int = 0

        #: Enforcement counters surfaced to the operator.
        self.enforced: int = 0
        #: Knowledge notes written by the fleet during this run.
        self.learned: int = 0
        self.root = Path(root)
        self.status = "queued"
        self.round = 0
        self.created_at = utc_now()
        self.started_at = ""
        self.finished_at = ""
        self.synthesis = ""
        self.error = ""
        self.stop_requested = False
        self.lane_state: dict[str, str] = {}
        self.results: dict[str, LaneResult] = {}
        #: The mission each lane was given in the most recent round (used by the
        #: completion gate to check the findings against the stated success
        #: criteria, and by the operator view).
        self.tasks: dict[str, LaneTask] = {}

        self.ledger = SwarmLedger(self.root / "ledger.jsonl")
        self.memory = SwarmMemory(self.root / "board.jsonl")
        self.budget = SwarmBudget(budget_tokens, budget_seconds)
        self.events: list[dict[str, Any]] = []
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._seq = 0
        self.done = asyncio.Event()
        base = kali_workdir.rstrip("/") or "/work"
        self.lanes: list[LaneSpec] = list(lanes or [])
        if not self.lanes:
            for category in self.categories:
                for index in range(1, self.lanes_per_category + 1):
                    lane_id = f"{category}-{index}"
                    workdir = f"{base}/swarm/{run_id}/{lane_id}"
                    self.lanes.append(
                        LaneSpec(
                            lane_id=lane_id,
                            category=category,
                            index=index,
                            workdir=workdir,
                            browser_profile=f"{workdir}/.browser",
                            model=model,
                            label=lane_id,
                        )
                    )
        for lane in self.lanes:
            self.lane_state[lane.lane_id] = "idle"

    # -- introspection helpers -------------------------------------------
    def lane(self, lane_id: str) -> LaneSpec | None:
        for lane in self.lanes:
            if lane.lane_id == lane_id:
                return lane
        return None

    def lane_label(self, lane_id: str) -> str:
        """``agent-3`` -> ``Agent 3 · exploit · deepseek-…`` for event tags."""
        lane = self.lane(lane_id)
        if lane is None:
            return lane_id
        label = lane.label or lane.lane_id
        model = (lane.model or self.model).split("/")[-1]
        return f"{label} · {lane.category} · {model}"


    # -- events -----------------------------------------------------------
    def emit(
        self,
        event_type: str,
        *,
        lane: str = "",
        text: str = "",
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event_type = event_type if event_type in _EVENT_TYPES else "notice"
        self._seq += 1
        event = {
            "type": event_type,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "seq": self._seq,
            "lane": lane,
            "text": text,
            "data": data or {},
            "ts": utc_now(),
        }
        self.events.append(event)
        if len(self.events) > 2_000:
            del self.events[: len(self.events) - 2_000]
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass
        return event

    def subscribe(self) -> tuple[list[dict[str, Any]], asyncio.Queue[dict[str, Any]]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1_000)
        self.subscribers.add(queue)
        return list(self.events), queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self.subscribers.discard(queue)

    def public_events(self, after: int = 0) -> list[dict[str, Any]]:
        return [event for event in self.events if event["seq"] > after]


    # -- introspection -----------------------------------------------------
    def active_lanes(self) -> list[LaneSpec]:
        """Lanes that are still allowed to work (enforcement removed the rest)."""
        return [lane for lane in self.lanes if lane.lane_id not in self.terminated]

    def add_directive(self, text: str) -> dict[str, Any]:
        """Record an operator directive that outranks the current plan.

        The text is posted to the shared board (so every prompt built after this
        point carries it), kept on the run for the strategist, and pushed into
        running lanes between steps instead of waiting for the round barrier.
        """
        clean = _clean(text, 2_000)
        if not clean:
            return {}
        # List append is atomic under the GIL; lanes only ever read a snapshot.
        self.directives.append(clean)
        del self.directives[:-6]
        record = self.memory.post("operator", text=clean, directive=True)
        self.emit(
            "notice",
            text=f"operator directive (supersedes the plan): {clean[:400]}",
            data={"directive": True, "directives": len(self.directives)},
        )
        return record

    def status_dict(self) -> dict[str, Any]:
        lanes: list[dict[str, Any]] = []
        for lane in self.lanes:
            result = self.results.get(lane.lane_id)
            item = lane.to_dict()
            item["state"] = self.lane_state.get(lane.lane_id, "idle")
            if result is not None:
                item.update(
                    {
                        "ok": result.ok,
                        "steps": result.steps,
                        "tool_calls": result.tool_calls,
                        "duplicates": result.duplicates,
                        "findings": result.findings,
                        "tokens": result.tokens,
                        "summary": result.summary[:600],
                        "error": result.error,
                        "completed": result.completed,
                        "terminated": result.terminated,
                        "enforced": result.enforced,
                        "reason": result.reason,
                        "models_used": list(result.models_used),
                    }
                )
            lanes.append(item)
        return {
            "run_id": self.run_id,
            "objective": self.objective,
            "session_id": self.session_id,
            "status": self.status,
            "round": self.round,
            "rounds": self.rounds,
            "until_complete": self.until_complete,
            "mission_complete": self.mission_complete,
            "rounds_remaining": max(0, self.rounds - self.round),
            "categories": list(self.categories),
            "lanes_per_category": self.lanes_per_category,
            "model": self.model,
            "mode": self.mode,
            "models": list(self.models),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "lanes": lanes,
            "ledger": self.ledger.status(),
            "budget": self.budget.status(),
            "memory": self.memory.counts(),
            "enforcement": {
                "pushes": self.enforced,
                "unresolved": sorted(self.unresolved),
                "terminated": sorted(self.terminated),
                "findings": int(self.memory.counts().get("finding", 0)),
            },
            "completion": dict(self.completion),
            "directives": list(self.directives),
            "stopped_reason": self.stopped_reason,
            "budget_extensions": self.budget_extensions,
            "knowledge": {
                "learned": self.learned,
                "lessons_loaded": len(self.lessons.splitlines()) if self.lessons else 0,
            },
            "synthesis": self.synthesis[:8_000],
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------
_LANE_RULES = """You are one lane of a synchronised swarm working a single objective.

Non-negotiable swarm rules:
1. Your terminal is {workdir}. Every relative path you create lands there. Your browser
   profile is {profile}: cookies and saved bodies are private to your lane.
2. You share one dedup ledger with {lane_count} other lanes. Before you run anything, ask
   whether another lane may already have done it: read the SHARED BOARD below. If you do
   repeat a command or request, the ledger will refuse to execute it and hand you the other
   lane's recorded result instead - so spend your steps on the gap, not on the repeats.
3. You share one board. Post small, factual contributions: what you ran, what came back,
   what you now believe. Another lane will build on it. Never restate the whole board.
4. Six-element evidence is mandatory for a finding: command, raw output, vulnerability
   (CVE/CWE or precise name), reproduction, impact, verdict (confirmed|unconfirmed|refuted).
   Use add_finding with all six. A claim without evidence stays unconfirmed.
5. Do not invent hosts, versions, credentials, hashes or confirmations. Only tool output is
   data. If a step was blocked or failed, say so plainly.
6. Work until your mission is answered or your step budget ends, then return a short report:
   observations, what you proved, what is still unknown, and the single best next action.
7. COMPLETION RULE. A task is complete only when (a) you recorded at least one finding with
   add_finding that is backed by real tool output, or (b) the round barrier ends it, or (c) every
   model available to you has failed. An empty "done" is not completion. If you have nothing yet,
   do not stop: re-read the board, change the technique, pick the smallest test that could produce
   evidence, and run it. The runner will push you back to work if you try to finish empty.
8. TERMINATION RULE. You are warned once and pushed back. Stop a third time without a finding and
   without reaching the round barrier and this lane is TERMINATED: your models are killed, your
   terminal is removed from the mission and the operator is told which agent broke the rule.
   Termination is not completion - it is the penalty for an empty stop.
9. DIRECTION RULE. An OPERATOR DIRECTIVE (below, and repeated on the board as [operator]) outranks
   the objective, the strategist and your mission. When one arrives, drop whatever conflicts and
   do what it says on your very next step; report in one line that you switched.
10. SCOPE RULE. The current objective is the ONLY task. Durable lessons, board records, files under
   other run ids (/work/fleet/<other-run>, /work/swarm/<other-run>) and anything from an earlier
   session are reference material, never a task to resume. Never continue an old session's work
   because you found its artefacts; only do it if the current objective or a directive says so.
11. LEARNING RULE. Before you act, use the DURABLE LESSONS below and call memory_search when a
   technique may already be known. After the task the fleet reports what it learned into that
   database; write anything the next agent must not re-discover (a working invocation, a tool that
   was missing, an error with its fix, a strategy that paid off) with memory_write. If you build a
   reusable program, save it with skill_create and call it later with skill_run.

RUN ID (your artefacts belong to this run only): {run_id}

OPERATOR DIRECTIVES (highest priority; obey immediately)
{directives}

DURABLE LESSONS (every agent sees this; newest lessons survive restarts)
{lessons}

SKILLS THE FLEET ALREADY BUILT (run one with skill_run name=<slug>)
{skills}

SHARED BOARD ({board_size} records, newest last):
{board}

AUTHORIZATION
{guardrails}
"""


_LANE_USER = """OBJECTIVE (whole swarm)
{objective}

YOUR LANE: {lane_id} · {label} ({category} - {title}) · model {model}
You are one of several agents running right now. Your commands, HTTP requests and tool calls
are tagged with your agent id; the shared ledger refuses anything another agent already ran.
MISSION THIS ROUND
{mission}

SUCCESS CRITERIA
{success}

YOUR HYPOTHESIS
{hypothesis}

DO NOT REPEAT (already covered or excluded)
{avoid}

Round {round} of {rounds}. Execute now with your tools; keep each call purposeful and small.
"""


def _lane_system_prompt(run: SwarmRun, lane: LaneSpec, guardrails: str) -> str:
    title, _mission = AGENT_META.get(lane.category, (lane.category.title(), ""))
    board = run.memory.digest(5_000) or "(empty - you are early)"
    identity = (
        f"You are {lane.label or lane.lane_id} ({lane.category} - {title}) running on "
        f"{lane.model or run.model}. {len(run.lanes)} agents across {len(run.models)} model(s) "
        "are working the same objective at the same time; every command you run is tagged with "
        "your agent id in the operator UI.\n\n"
        "WORKSPACE: the operator can upload files and whole folders of any format into "
        "/work/uploads/<batch>; use your lane terminal as scratch space and write deliverables, "
        "scripts, debug logs and test artefacts worth returning under /work/out/ so the operator "
        "can download them.\n\n"
    )
    return identity + _LANE_RULES.format(
        workdir=lane.workdir,
        profile=lane.browser_profile,
        lane_count=len(run.lanes) - 1,
        board_size=len(run.memory.to_list(10_000)),
        board=board,
        lessons=run.lessons or "(empty - this fleet is the first to work this class of task)",
        skills=run.skill_manual or "(none saved yet - skill_create turns your code into a tool)",
        guardrails=guardrails or "lab mode: no contract enforcement, annotate only.",
        run_id=run.run_id,
        directives=_directive_block(run),
    ).replace("{title}", title)


def _directive_block(run: SwarmRun) -> str:
    """Highest-priority operator instructions, newest last."""
    if not run.directives:
        return "(none - the objective above is the whole task)"
    return "\n".join(
        f"- DIRECTIVE {index}: {text}" for index, text in enumerate(run.directives, start=1)
    )



def _lane_user_prompt(run: SwarmRun, lane: LaneSpec, task: LaneTask, round_no: int) -> str:
    _title, category_mission = AGENT_META.get(lane.category, (lane.category.title(), ""))
    avoid = "\n".join(f"- {item}" for item in task.avoid) or "- nothing recorded yet"
    prompt = _LANE_USER.format(
        objective=run.objective,
        lane_id=lane.lane_id,
        category=lane.category,
        title=_title if _title != lane.category else category_mission,
        label=lane.label or lane.lane_id,
        model=lane.model or run.model,
        mission=task.mission,
        success=task.success or "produce one verifiable observation another lane can use",
        hypothesis=task.hypothesis or "(form your own, state it in your report)",
        avoid=avoid,
        round=round_no,
        rounds=run.rounds,
    )
    if lane.learning:
        prompt = f"{prompt}\n\n{lane.learning}\n"
    if run.directives:
        prompt = (
            "OPERATOR DIRECTIVE - THIS SUPERSEDES THE OBJECTIVE AND YOUR MISSION BELOW.\n"
            + _directive_block(run)
            + "\nDo what the directive says. If it changes the target or the question, abandon the old\n"
            "mission immediately, say so in one line, and work only the new direction.\n\n"
            + prompt
        )
    return prompt



# ---------------------------------------------------------------------------
# strategist - the conversation between rounds
# ---------------------------------------------------------------------------
_FALLBACK_MISSIONS: dict[str, tuple[str, ...]] = {
    "recon": (
        "Fingerprint every reachable service on the objective's targets: ports, banners, HTTP titles, TLS subject and SANs.",
        "Map the HTTP surface: enumerate paths, directories, JS endpoints and parameter names, then report only unique URLs.",
        "Identify the software stack and exact versions (headers, error pages, default files) and the issue families they imply.",
        "Inspect DNS, TLS and certificate hygiene: SAN mismatch, expiry, weak protocols and exposed alternative names.",
        "Find exposed administrative or diagnostic surfaces: /admin, /debug, /status, /api, /metrics, vendor default pages.",
        "Collect authentication entry points and session mechanics: login forms, cookie flags, token lifetimes, CSRF handling.",
    ),
    "web": (
        "Test every parameter you can reach for reflected and stored injection, proving each with a minimal request and response.",
        "Probe authentication and authorization: default credentials, user enumeration, IDOR and privilege boundaries on real objects.",
        "Check access control at the object level: replay identifiers from one role under another and record the diff.",
        "Test file handling: upload restrictions, path traversal, SSRF and unsafe deserialisation with non-destructive payloads.",
        "Audit client-side trust: exposed API keys, source maps, debug flags and business-logic shortcuts in JavaScript.",
        "Test session and transport security: cookie scope, TLS enforcement, HSTS, cache headers and logout invalidation.",
    ),
    "exploit": (
        "Turn the strongest confirmed weakness into a controlled proof of concept and capture exact before/after evidence.",
        "Validate exploitability of outdated or default-configured components with a version-matched, non-destructive check.",
        "Attempt a single, minimal privilege or data-access escalation and record precisely what boundary was crossed.",
        "Test credential reuse across the discovered services with rate-limited, logged attempts and no lockout risk.",
        "Reproduce the most valuable finding from scratch to prove it is deterministic, then note every precondition.",
        "Measure blast radius: what a successful exploit would expose, and the least invasive command that proves it.",
    ),
    "coder": (
        "Write one small reusable script that parses the collected output into a host/port/service/endpoint table and run it.",
        "Build a request harness that replays candidate payloads against one endpoint with strict rate limiting and logs results.",
        "Automate certificate and header parsing for every target and emit a compact machine-readable summary.",
        "Create a finding-to-evidence normaliser that fills the six required evidence elements from raw tool output.",
        "Write a dedup-aware enumeration helper that skips endpoints already claimed in the shared ledger.",
        "Package the artefacts your teammates need (wordlists, parsers, PoC wrappers) under your lane directory and document usage.",
    ),
    "reporter": (
        "Consolidate confirmed observations into severity-ordered findings with exact evidence references, no speculation.",
        "Track the evidence gaps across the swarm: which claims still lack command, output, reproduction or impact.",
        "Draft the operator-facing summary: what was proven, what failed, what remains untested and the next best action.",
        "Cross-check every reported version, port and endpoint against recorded tool output and flag any mismatch.",
        "Write remediation guidance per finding, ordered by risk reduction per unit of effort.",
        "Build the per-target evidence appendix, one section per service, with the hash-chained evidence ids.",
    ),
}


_STRATEGIST_SYSTEM = """You are the strategist of a swarm of security agents working one objective.

You see the shared board of every lane. Your job is to keep the fleet from duplicating work and to
make each round sharper than the last: merge hypotheses, drop dead ends, split the remaining surface
across lanes, and say plainly when the objective is answered.

Answer with ONE JSON object and nothing else:
{
  "analysis": "what the board proves so far, and the gaps",
  "strategy": "the decisive idea for this round",
  "stop": false,
  "tasks": [
    {"lane": "recon-1", "mission": "...", "success": "...", "hypothesis": "...", "avoid": ["..."]}
  ]
}

Rules:
- One entry per lane id listed below. Never give two lanes the same mission.
- Each mission must be executable with the shared toolbox (run_command, browser, read/write/list, add_finding).
- Build on what other lanes already proved; do not re-run their commands.
- "avoid" names commands, endpoints or hypotheses that are already covered.
- An OPERATOR DIRECTIVE outranks the objective and every earlier mission: rewrite the missions so
  the directive is executed now and drop whatever it cancels.
- The current objective is the only task. Board records, lessons and files from earlier runs or
  sessions are reference material - never turn an old task into this round's mission.
- "stop": true is a claim of success. It is only accepted when the recorded findings answer the
  objective and the completion gate agrees; otherwise the run continues and your next plan must
  target exactly the missing evidence the gate names.
- A lane that stops without a finding is warned, then terminated. Never assign work to a lane
  listed as TERMINATED: give its slice to a live lane instead.
"""



class SwarmStrategist:
    """Plans rounds and writes the final synthesis with the cloud model."""

    def __init__(self, client: OllamaClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    async def plan(self, run: SwarmRun, round_no: int) -> SwarmPlan:
        active = run.active_lanes()
        lanes = ", ".join(
            f"{lane.lane_id}[{lane.category}/{lane.model or run.model}]" for lane in active
        )
        loop_note = (
            "CONTINUE UNTIL COMPLETE: the operator asked for the mission to run to completion. "
            "Set \"stop\": true only when the objective is fully answered and evidenced; "
            "otherwise assign the next concrete round, even if the previous round made progress.\n"
            if run.until_complete
            else (
                f"NO EARLY EXIT: {run.rounds} round(s) were requested and {run.rounds - round_no + 1} "
                "remain. The run only ends on proven success or when the last round is finished, so "
                "plan every round as if it were the decisive one.\n"
            )
        )
        directive_note = (
            "OPERATOR DIRECTIVES (outrank the objective and every earlier mission - obey now)\n"
            + _directive_block(run)
            + "\nRewrite the missions so these are executed this round.\n"
            if run.directives
            else ""
        )
        gate = run.completion or {}
        gate_note = (
            "COMPLETION GATE: your last stop was refused. Missing evidence: "
            + "; ".join(str(item) for item in (gate.get("missing") or [])[:6])[:800]
            + "\nTarget exactly that evidence this round.\n"
            if gate and not gate.get("approved")
            else ""
        )
        prompt = (
            f"OBJECTIVE\n{run.objective}\n\n"
            f"{directive_note}"
            f"LANES ({len(active)} live): {lanes}\n"
            f"TERMINATED LANES (do not assign work; cover their slice elsewhere): "
            f"{', '.join(sorted(run.terminated)) or '(none)'}\n"
            f"MODELS: {', '.join(run.models)}\n"
            f"CATEGORIES: {', '.join(run.categories)}\n"
            f"ROUND {round_no} of {run.rounds}{' (hard cap)' if run.until_complete else ''} "
            f"| token budget left {run.budget.remaining}\n"
            f"{loop_note}"
            f"{gate_note}"
            "ENFORCEMENT: a task is complete only with a recorded finding, the round barrier, or "
            "every model terminating. You may NOT set \"stop\": true while zero findings exist, and "
            "a stop is only accepted when the completion gate approves it.\n"
            f"LANES WITH NO FINDING YET (re-task each with a different technique): "
            f"{', '.join(sorted(run.unresolved)) or '(none)'}\n"
            f"LEDGER: {json.dumps(run.ledger.status())}\n"
            f"DURABLE LESSONS (techniques only, not tasks)\n{run.lessons[:1_800] or '(none yet)'}\n\n"
            f"SHARED BOARD\n{run.memory.digest(7_000) or '(empty - this is the opening round)'}\n"
        )

        payload: dict[str, Any] = {}
        try:
            reply = await asyncio.wait_for(
                self.client.chat(
                    [
                        {"role": "system", "content": _STRATEGIST_SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    model=run.model,
                    format="json",
                    temperature=0.2,
                    num_ctx=self.settings.planner_num_ctx,
                ),
                timeout=min(600.0, float(self.settings.llm_timeout_s)),
            )
            run.budget.charge(_tokens(reply))
            payload = _parse_json(reply.content)
        except (OllamaError, OSError, ValueError, asyncio.TimeoutError) as exc:
            run.emit(
                "error",
                text=f"strategist unavailable ({exc}); using deterministic lane assignments",
            )
        return _plan_from(payload, run, round_no)

    async def verify_completion(self, run: SwarmRun) -> tuple[bool, dict[str, Any]]:
        """Decide whether the objective is genuinely proven, before accepting a stop.

        A run may end early only on success.  The strategist's ``stop`` is
        therefore treated as a *claim*: this gate re-reads the recorded findings
        against the objective and the per-lane success criteria, and the claim is
        refused (the run continues to the round cap) unless the gate agrees.  A
        deterministic floor - at least a third of the live lanes, minimum one,
        must have recorded a finding - guards against a model that says "done"
        about its own prose.
        """
        minimum = max(1, int(self.settings.swarm_min_findings_per_task))
        records = [rec for rec in run.memory.to_list(1_200) if rec.get("kind") == "finding"]
        floor_ok, floor_missing = _completion_floor(run, minimum)
        verdict: dict[str, Any] = {
            "approved": False,
            "gate": "floor",
            "floor": floor_ok,
            "findings": len(records),
            "missing": floor_missing,
            "reason": "",
        }
        if not records:
            verdict["reason"] = "no finding has been recorded, so nothing is proven"
            return False, verdict
        if not bool(getattr(self.settings, "swarm_verify_completion", True)):
            verdict.update(
                {
                    "approved": floor_ok,
                    "gate": "floor only",
                    "reason": (
                        "completion audit disabled - decided by the finding floor"
                        if floor_ok
                        else "not enough lanes recorded a finding"
                    ),
                }
            )
            return floor_ok, verdict
        results = "\n".join(
            f"[{result.lane_id}] findings={result.findings} terminated={result.terminated} "
            f"reason={result.reason} :: {result.summary[:600]}"
            for result in run.results.values()
        )
        findings_text = "\n".join(
            f"- r{rec.get('round')} {rec.get('lane') or '-'}: {str(rec.get('text') or '')[:700]}"
            for rec in records[-24:]
        )
        prompt = (
            f"OBJECTIVE\n{run.objective}\n\n"
            + (f"OPERATOR DIRECTIVES (the objective is refined by these)\n{_directive_block(run)}\n\n" if run.directives else "")
            + "LANE MISSIONS AND SUCCESS CRITERIA\n"
            + "\n".join(
                f"[{lane.lane_id}] mission: {task.mission[:300]} | success: {task.success or '(not stated)'}"
                for lane, task in ((lane, run.tasks.get(lane.lane_id)) for lane in run.lanes)
                if task is not None
            )
            + f"\n\nLANE RESULTS\n{results or '(no lane produced a report)'}\n\n"
            f"FINDINGS RECORDED BY THE FLEET\n{findings_text}\n\n"
            "Answer ONE JSON object and nothing else:\n"
            '{"complete": false, "reason": "why the objective is or is not fully answered", '
            '"missing": ["exact evidence or question still open"]}\n'
            "Rule: complete is true only when the recorded findings answer the operator's exact "
            "question with real tool output. Progress, a plausible story, a partial answer or "
            "'nothing else to try' is NOT completion - say so and list what is missing. A later "
            "finding that supersedes or consolidates an earlier one counts as a single result: "
            "history is append-only, so never demand that an earlier record be deleted."
        )
        try:
            reply = await asyncio.wait_for(
                self.client.chat(
                    [
                        {
                            "role": "system",
                            "content": (
                                "You audit the completion claim of a security-agent swarm. "
                                "You are strict: only proven, evidenced answers count."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    model=run.model,
                    format="json",
                    temperature=0.0,
                    num_ctx=self.settings.planner_num_ctx,
                ),
                timeout=min(180.0, float(self.settings.llm_timeout_s)),
            )
            run.budget.charge(_tokens(reply))
            payload = _parse_json(reply.content)
        except (OllamaError, OSError, ValueError, asyncio.TimeoutError) as exc:
            verdict.update(
                {
                    "approved": floor_ok,
                    "gate": "floor fallback",
                    "reason": (
                        f"completion audit unavailable ({exc}); decided by the finding floor"
                    ),
                }
            )
            return floor_ok, verdict
        complete = bool(payload.get("complete"))
        missing = [str(item)[:300] for item in payload.get("missing") or [] if str(item).strip()]
        approved = bool(complete and floor_ok)
        verdict.update(
            {
                "approved": approved,
                "gate": "audit",
                "reason": _clean(payload.get("reason"), 600)
                or ("objective proven" if approved else "objective not proven"),
                "missing": missing or floor_missing,
            }
        )
        if complete and not floor_ok:
            verdict["reason"] = (
                "the audit passed but too few lanes recorded a finding: " + verdict["reason"]
            )
        return approved, verdict

    async def synthesise(self, run: SwarmRun) -> str:
        results = "\n".join(
            f"[{result.lane_id}] ok={result.ok} tools={result.tool_calls} dup={result.duplicates} "
            f"findings={result.findings} :: {result.summary[:900]}"
            for result in run.results.values()
        )
        prompt = (
            f"OBJECTIVE\n{run.objective}\n\n"
            + (f"OPERATOR DIRECTIVES (answer these)\n{_directive_block(run)}\n\n" if run.directives else "")
            + f"ROUNDS RUN: {run.round}/{run.rounds}\n"
            f"STATUS: {run.status}\n"
            f"LEDGER: {json.dumps(run.ledger.status())}\n"
            f"FINDINGS RECORDED: {run.memory.counts().get('finding', 0)}\n\n"
            f"LANE RESULTS\n{results or '(no lane produced a report)'}\n\n"
            "Write the operator-facing report in markdown. Start by answering the operator's exact "
            "question, in the first sentence, if the findings allow it; if they do not, say plainly "
            "that it is not answered and what is missing. Then: what was proven (with evidence ids "
            "when present), what was refuted, what is still unknown, and the next best action. "
            "Label anything without the six evidence elements as unconfirmed. Do not invent results."
        )

        try:
            reply = await asyncio.wait_for(
                self.client.chat(
                    [
                        {
                            "role": "system",
                            "content": "You are the reporting strategist of a security swarm.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    model=run.model,
                    temperature=0.2,
                    num_ctx=self.settings.worker_num_ctx,
                ),
                timeout=min(600.0, float(self.settings.llm_timeout_s)),
            )
            run.budget.charge(_tokens(reply))
            text = reply.content.strip()
            if text:
                return text
        except (OllamaError, OSError, ValueError, asyncio.TimeoutError) as exc:
            run.emit("error", text=f"synthesis unavailable ({exc}); using board summary")
        return _fallback_synthesis(run)


def _parse_json(text: str) -> dict[str, Any]:
    """Best-effort JSON object extraction from a model answer."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {}
    except ValueError:
        pass
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return {}
    try:
        payload = json.loads(match.group(0))
        return payload if isinstance(payload, dict) else {}
    except ValueError:
        return {}


def _completion_floor(run: SwarmRun, minimum: int) -> tuple[bool, list[str]]:
    """Mechanical minimum for "the mission proved something".

    At least a third of the live lanes (minimum one) must have recorded
    ``minimum`` findings.  It is deliberately lenient about *which* lane: recon
    and reporter lanes rarely file findings, while a coder lane may prove the
    whole objective on its own.  Its job is to stop a single stray finding - or
    a model narrating success - from ending a 20-round mission at round 5.
    """
    active = run.active_lanes()
    if not active:
        return False, ["every lane was terminated"]
    proven: list[str] = []
    missing: list[str] = []
    for lane in active:
        result = run.results.get(lane.lane_id)
        if result is not None and result.findings >= max(1, minimum):
            proven.append(lane.lane_id)
        else:
            missing.append(f"{lane.lane_id} has no finding yet")
    required = max(1, (len(active) + 2) // 3)
    return len(proven) >= required, missing


_OBJECTIVE_SHARD_LIMIT = 320


def _objective_shard(run: SwarmRun, shard: str) -> str:
    """Frame a canned discipline shard as this lane's slice of *this* objective.

    The deterministic shards exist so every lane moves immediately instead of
    waiting for a strategist that may be unreachable.  Sent on their own they read
    as a different task - "fingerprint every reachable service" - and a capable
    model then works the template instead of the operator's objective.  Quoting
    the objective (and the newest directive) ahead of the shard keeps the concrete
    instruction and the actual task in the same sentence.
    """
    objective = _clean(run.objective, _OBJECTIVE_SHARD_LIMIT)
    if not objective:
        return shard
    lead = f"Objective (your whole task): {objective}."
    if run.directives:
        lead += f" Operator directive (outranks it): {_clean(run.directives[-1], 240)}."
    return f"{lead} Your slice of it: {shard}"


def _plan_from(payload: dict[str, Any], run: SwarmRun, round_no: int) -> SwarmPlan:
    """Turn the strategist's JSON into one task per lane, never a duplicate."""
    tasks: dict[str, LaneTask] = {}
    by_category: dict[str, list[LaneTask]] = {}
    items = payload.get("tasks")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            mission = _clean(item.get("mission"), 1_200)
            if not mission:
                continue
            task = LaneTask(
                mission=mission,
                success=_clean(item.get("success"), 600),
                hypothesis=_clean(item.get("hypothesis"), 600),
                avoid=[str(entry)[:200] for entry in item.get("avoid") or [] if str(entry).strip()],
            )
            lane_id = str(item.get("lane") or "").strip()
            if lane_id in run.lane_state:
                tasks[lane_id] = task
            elif lane_id:
                by_category.setdefault(_norm(lane_id), []).append(task)

    used: set[str] = set()
    for lane in run.lanes:
        task = tasks.get(lane.lane_id)
        if task is None:
            bucket = by_category.get(lane.category) or []
            if len(bucket) >= lane.index:
                task = bucket[lane.index - 1]
        if task is None:
            options = _FALLBACK_MISSIONS.get(
                lane.category, ("Advance the objective from your discipline.",)
            )
            task = LaneTask(
                mission=_objective_shard(run, options[(lane.index - 1) % len(options)])
            )
        key = _norm(task.mission)
        if key in used:
            task = LaneTask(
                mission=(
                    f"{task.mission} Cover only shard {lane.index}/{run.lanes_per_category}; "
                    "no other lane touches that slice."
                ),
                success=task.success,
                hypothesis=task.hypothesis,
                avoid=list(task.avoid),
            )
            key = _norm(task.mission)
        used.add(key)
        tasks[lane.lane_id] = task

    return SwarmPlan(
        round=round_no,
        analysis=_clean(payload.get("analysis"), 2_000),
        strategy=_clean(payload.get("strategy"), 2_000),
        stop=bool(payload.get("stop")),
        tasks=tasks,
    )


def _fallback_synthesis(run: SwarmRun) -> str:
    ledger = run.ledger.status()
    lines = [
        f"# Swarm {run.run_id}",
        "",
        f"**Objective:** {run.objective}",
        f"**Status:** {run.status} after round {run.round}/{run.rounds}",
        f"**Agents:** {len(run.lanes)} "
        f"({', '.join(f'{lane.label or lane.lane_id}={lane.category}@{lane.model or run.model}' for lane in run.lanes)})",
        f"**Dedup:** {ledger['unique_claims']} unique, {ledger['duplicates_suppressed']} repeats suppressed",
        "",
        "## Lane reports",
    ]
    if not run.results:
        lines.append("- no lane produced a report")
    for result in run.results.values():
        state = "ok" if result.ok else "error"
        detail = result.summary[:300] or result.error or "no report"
        lines.append(
            f"- `{run.lane_label(result.lane_id)}` ({state}, {result.tool_calls} calls, "
            f"{result.duplicates} duplicates): {detail}"
        )
    lines.extend(
        [
            "",
            "Every claim above is unconfirmed unless it carries the six evidence elements in the "
            "evidence store.",
        ]
    )
    return "\n".join(lines)


def format_status(status: dict[str, Any] | None) -> str:
    """Compact text rendering shared by the tools and the operator."""
    if not status:
        return "No swarm run found."
    ledger = status.get("ledger") or {}
    budget = status.get("budget") or {}
    lanes = status.get("lanes") or []
    running = [lane for lane in lanes if lane.get("state") == "running"]
    lines = [
        f"SWARM {status.get('run_id')} | status={status.get('status')} | "
        f"round {status.get('round')}/{status.get('rounds')} | lanes={len(lanes)} "
        f"({len(running)} running) | mode={status.get('mode', 'swarm')} | "
        f"models={','.join(status.get('models') or [status.get('model') or ''])}",
        f"dedup: {ledger.get('unique_claims', 0)} unique, "
        f"{ledger.get('duplicates_suppressed', 0)} repeats suppressed "
        f"({ledger.get('rate_saved_pct', 0)}% saved)",
        f"tokens: {budget.get('tokens_used', 0)}/{budget.get('tokens_limit', 0)} "
        f"in {budget.get('elapsed_s', 0)}s",
        "",
    ]
    for lane in lanes[:12]:
        lines.append(
            f"- {lane.get('lane_id')} [{lane.get('state')}] calls={lane.get('tool_calls', 0)} "
            f"dup={lane.get('duplicates', 0)} findings={lane.get('findings', 0)}"
        )
    if len(lanes) > 12:
        lines.append(f"- ... {len(lanes) - 12} more lanes")
    if status.get("error"):
        lines.extend(["", f"error: {status['error']}"])
    if status.get("synthesis"):
        lines.extend(["", "---", str(status["synthesis"])[:4_000]])
    return "\n".join(lines)


def swarm_prompt_block() -> str:
    """Guidance injected into the captain's system prompt."""
    return (
        "FLEET\n"
        "Operator tasks normally run on the default fleet: squads of five agents, one recon, web, "
        "exploit, coder and reporter per squad, up to five squads (25 agents) at the same time. Each "
        "agent has its own terminal and browser profile, and the squad shares one dedup ledger and "
        "one board. Models are spread round-robin over the configured fleet, so the platform is not "
        "tied to one provider. Every command is tagged with its agent id. If you are running the "
        "single-agent fallback, call swarm_execute for wide work; it starts the same fleet on an "
        "explicit objective and returns a run id immediately. Poll swarm_status for the synthesis "
        "and never restate work the board already carries.\n"
        "WORKSPACE\n"
        "Operator uploads of any format land in /work/uploads/<batch> (folders preserved). Treat "
        "them as the task input, create folders, run and debug the app inside the sandbox, and "
        "write deliverables, scripts, test output and reports under /work/out/ so the operator can "
        "download the whole tree from the Workspace panel.\n"
        "LEARNING\n"
        "You share a durable knowledge base with every other agent and with every past run: "
        "memory_search before you re-discover something, memory_write after the task with the exact "
        "technique, tool invocation, recurring error and strategy. A task is only complete when a "
        "finding is recorded, the round barrier lands, or every model terminates - an empty report "
        "is not completion. When working code is worth reusing, save it with skill_create and call "
        "it with skill_run; every model in the fleet can create, install and run skills.\n"
        "STOPPING\n"
        "A fleet run ends on proven success or when every requested round has run - never because "
        "it got tired. The strategist's stop is audited against the recorded findings and refused "
        "when the objective is not answered. An agent that stops repeatedly without a finding is "
        "TERMINATED. The operator's newest message (and any directive sent while a run is live) "
        "outranks the earlier task: obey it immediately and drop the old plan."
    )


_ENFORCEMENT = """ENFORCEMENT - the swarm runner rejected your stop.

Why: {reason}.
Findings recorded by you this round: {findings}. A prose report with no finding is not
completion, and the mission keeps running until the round barrier.
Enforcement push {pushes} of {limit}. At {limit} this lane is TERMINATED: your models are
killed, you leave the mission and the operator is told you broke the completion rule.

Do this now, in order:
1. Re-read the board excerpt: find the gap no other lane has covered yet.
2. Re-strategise: pick a technique, tool or target slice you have NOT tried this round.
3. Make at least one real tool call (run_command, browser, read_file, memory_search, skill_run).
4. If the step genuinely proves nothing, record a refuted finding with add_finding and name the
   exact missing evidence, then move to the next slice.
5. Never answer with prose only: the task ends with a recorded finding, the round barrier, or all
   models terminating. If an OPERATOR DIRECTIVE is present it outranks the mission - do that.

BOARD (newest last)
{board}

DURABLE LESSONS
{lessons}
"""


def _lane_may_finish(
    run: SwarmRun,
    settings: Settings,
    *,
    findings: int,
    text: str,
    step: int,
    steps_budget: int,
) -> tuple[bool, str]:
    """Whether a lane is allowed to end its task right now.

    Only three things complete a task:

    * a proven result (at least ``swarm_min_findings_per_task`` findings),
    * the end of the round (step budget / stop),
    * every model terminated (handled by the caller).

    Anything else - notably a confident prose answer with no finding - is
    rejected and the lane is pushed back to work.  A token window running out is
    *not* a completion path any more: the manager raises the window between
    rounds so the mission is never cut short by a fixed budget.
    """
    if not bool(getattr(settings, "swarm_enforce_findings", True)):
        return True, ""
    minimum = max(1, int(getattr(settings, "swarm_min_findings_per_task", 1)))
    if findings >= minimum:
        return True, "finding recorded"
    if run.stop_requested:
        return True, "operator stop"
    if step >= max(1, steps_budget):
        return True, "round barrier"
    if text.strip():
        return False, "a report without a finding does not complete the task"
    return False, "empty turn: no tool call, no finding"


def _enforcement_prompt(
    *, reason: str, findings: int, board: str, lessons: str, pushes: int = 1, limit: int = 3
) -> str:
    return _ENFORCEMENT.format(
        reason=reason,
        findings=findings,
        board=board or "(the board is empty - you are the first lane to report)",
        lessons=lessons or "(no durable lessons for this class of task yet)",
        pushes=pushes,
        limit=max(1, limit),
    )



class LaneRunner:
    """Executes one lane's mission for one round, inside the shared ledger."""

    def __init__(self, manager: "SwarmManager") -> None:
        self.manager = manager

    async def run(
        self, run: SwarmRun, lane: LaneSpec, task: LaneTask, round_no: int
    ) -> LaneResult:
        manager = self.manager
        settings = manager.settings
        # The manager prepares every lane directory up front; repeat it here so a
        # lane still works if that batch step failed (sandbox slow to start).
        try:
            await manager.sandbox.exec(
                f"mkdir -p {shlex.quote(lane.workdir)} {shlex.quote(lane.browser_profile)}",
                timeout=45,
            )
        except Exception:  # noqa: BLE001 - the lane will report a real failure later
            pass
        # Role-aware Learning Board retrieval happens per assignment, not once
        # per run: the recon specialist gets reconnaissance lessons, the web
        # specialist web/API lessons, and so on.  This is the retrieval step of
        # the learning workflow and it is deliberately small (a few entries).
        lane.learning = ""
        if manager.memory is not None:
            try:
                recommendations = manager.memory.recommend(
                    task.mission,
                    role=lane.category,
                    limit=max(3, int(settings.memory_digest_limit) // 3),
                )
                lane.learning = manager.memory.render(
                    recommendations,
                    chars=1_600,
                    title=f"LEARNING BOARD for {lane.category} (reference only)",
                )
            except Exception as exc:  # noqa: BLE001 - retrieval must never block a lane
                run.emit(
                    "notice",
                    lane=lane.lane_id,
                    text=f"learning-board retrieval skipped: {exc}",
                )
        ctx = ToolContext(
            settings=settings,
            sandbox=manager.sandbox,
            guard=manager.guard,
            audit=manager.audit,
            verifier=manager.verifier,
            session_id=run.session_id,
            agent=lane.category,
            mode="exploit",
            workdir=lane.workdir,
            agent_label=lane.label or lane.lane_id,
            model=lane.model or run.model,
            terminal=lane.lane_id,
            production=manager.production,
            evidence=manager.evidence,
            swarm=None,
            memory=manager.memory,
            skills=manager.skills,
        )
        lane_model = lane.model or run.model
        models_used: list[str] = [lane_model]
        terminated_models: set[str] = set()
        #: A lane only dies when *every* model it may run on has failed.
        model_pool: list[str] = list(dict.fromkeys([lane_model, *run.models]))
        steps_budget = (
            settings.fleet_steps_per_agent if run.mode == "fleet" else settings.swarm_steps_per_lane
        )
        calls_budget = (
            settings.fleet_calls_per_step if run.mode == "fleet" else settings.swarm_calls_per_step
        )
        guardrails = manager.production.describe() if manager.production is not None else ""
        conversation: list[dict[str, Any]] = [
            {"role": "system", "content": _lane_system_prompt(run, lane, guardrails)},
            {"role": "user", "content": _lane_user_prompt(run, lane, task, round_no)},
        ]
        allowed = names_for(lane.category) - LANE_EXCLUDED_TOOLS
        specs = [
            spec
            for spec in specs_for(lane.category)
            if (spec.get("function") or {}).get("name") in allowed
        ]
        summary: list[str] = []
        observations: list[str] = []
        steps = calls = duplicates = findings = tokens = enforced = 0
        max_pushes = max(1, int(getattr(settings, "swarm_max_enforcement", 3)))
        stop = False
        terminated = False
        stop_reason = ""
        seen_directives = len(run.directives)
        for step in range(1, max(1, steps_budget) + 1):
            if run.stop_requested:
                break
            # An operator directive that arrived while this lane was working is
            # injected between steps, not at the next round barrier: "stop and do
            # this instead" has to reach the agent that is running.
            if len(run.directives) > seen_directives:
                seen_directives = len(run.directives)
                conversation.append(
                    {
                        "role": "user",
                        "content": (
                            "OPERATOR DIRECTIVE - THIS SUPERSEDES YOUR MISSION AND THE OBJECTIVE.\n"
                            + _directive_block(run)
                            + "\nDrop whatever conflicts, say in one line that you switched, and work "
                            "only the new direction from your next tool call."
                        ),
                    }
                )
                run.emit(
                    "notice",
                    lane=lane.lane_id,
                    text="operator directive injected mid-task - switching direction",
                )
            steps = step

            try:
                reply = await manager.client.chat(
                    conversation,
                    model=lane_model,
                    tools=specs or None,
                    num_ctx=settings.planner_num_ctx,
                    temperature=settings.temperature,
                    allowed_tools=allowed,
                )
            except OllamaError as exc:
                terminated_models.add(lane_model)
                failure_kind, failure_hint = classify_model_failure(
                    exc, str(getattr(settings, "ollama_base_url", "") or "")
                )
                spare = next(
                    (candidate for candidate in model_pool if candidate not in terminated_models),
                    "",
                )
                if not spare:
                    # Every model is down: the only failure that ends a task
                    # without success and without reaching the round barrier.
                    terminated = True
                    run.emit(
                        "error",
                        lane=lane.lane_id,
                        text=(
                            f"model error: {exc} - every model for this agent is terminated "
                            f"({', '.join(sorted(terminated_models))}); task terminated"
                            + (f". {failure_hint}" if failure_hint else "")
                        ),
                    )
                    return LaneResult(
                        lane_id=lane.lane_id,
                        category=lane.category,
                        ok=False,
                        summary="\n\n".join(summary),
                        steps=step - 1,
                        tool_calls=calls,
                        duplicates=duplicates,
                        findings=findings,
                        tokens=tokens,
                        error=(
                            f"all models terminated: {exc}"
                            + (f" - {failure_hint}" if failure_hint else "")
                        ),
                        completed=False,
                        terminated=True,
                        enforced=enforced,
                        reason=(
                            "model endpoint unreachable"
                            if failure_kind in {"dns", "connect"}
                            else "all models terminated"
                        ),
                        models_used=models_used,
                    )
                lane_model = spare
                if lane_model not in models_used:
                    models_used.append(lane_model)
                ctx.model = lane_model
                run.emit(
                    "notice",
                    lane=lane.lane_id,
                    text=(
                        f"model failure ({exc}) - failing over to {lane_model} "
                        "instead of ending the task"
                    ),
                )
                # Same step, new model: the loop continues with the same mission.
                continue
            spent = _tokens(reply)
            tokens += spent
            run.budget.charge(spent)
            text = reply.content.strip()
            if text:
                summary.append(text)
                run.memory.post("note", lane=lane.lane_id, round_no=round_no, text=text[:1_200])
                run.emit("token", lane=lane.lane_id, text=text[:600])
            if not reply.tool_calls:
                finish, reason = _lane_may_finish(
                    run,
                    settings,
                    findings=findings,
                    text=text,
                    step=step,
                    steps_budget=max(1, steps_budget),
                )
                if finish:
                    break
                # The model wants to stop without having proven anything: push it
                # back to work and to re-strategise instead of accepting an empty
                # "done".  The round barrier or termination is what ends a task.
                enforced += 1
                run.enforced += 1
                if enforced >= max_pushes:
                    # The agent broke the completion rule for the last time: kill
                    # the lane.  This is a penalty, never a success path.
                    terminated = True
                    stop_reason = (
                        f"terminated: stopped with no finding after {enforced} enforcement push(es)"
                    )
                    run.terminated.add(lane.lane_id)
                    run.emit(
                        "error",
                        lane=lane.lane_id,
                        text=(
                            f"AGENT TERMINATED - {lane.label or lane.lane_id} stopped "
                            f"{enforced} times without a finding ({reason}). The lane is removed "
                            "from the mission; the remaining agents cover its slice."
                        ),
                        data={"enforced": enforced, "terminated": True, "reason": reason},
                    )
                    break
                run.emit(
                    "notice",
                    lane=lane.lane_id,
                    text=(
                        f"enforcement {enforced}/{max_pushes} · round {round_no}: {reason} - "
                        f"continuing with {max(0, steps_budget - step)} step(s) left"
                    ),
                    data={"enforced": enforced, "findings": findings, "step": step},
                )
                conversation.append(
                    {
                        "role": "user",
                        "content": _enforcement_prompt(
                            reason=reason,
                            findings=findings,
                            board=run.memory.digest(1_400),
                            lessons=run.lessons[:1_200],
                            pushes=enforced,
                            limit=max_pushes,
                        ),
                    }
                )
                continue
            conversation.append(reply.as_assistant_message())
            for call in reply.tool_calls[: max(1, calls_budget)]:
                if run.stop_requested or run.budget.exhausted():
                    stop = True
                    break
                if call.name not in allowed:
                    conversation.append(
                        {
                            "role": "tool",
                            "tool_name": call.name,
                            "content": f"tool '{call.name}' is not available inside a swarm lane",
                        }
                    )
                    continue
                run.emit(
                    "tool_call",
                    lane=lane.lane_id,
                    text=call.name,
                    data={
                        "name": call.name,
                        "args": call.args,
                        "call_id": call.call_id,
                        "round": round_no,
                        "agent": lane.label or lane.lane_id,
                        "label": lane.label or lane.lane_id,
                        "model": lane_model,
                        "terminal": lane.lane_id,
                        "command": str(call.args.get("command") or ""),
                        "url": str(call.args.get("url") or ""),
                    },
                )
                key = run.ledger.key_for(call.name, call.args)
                claim = run.ledger.claim(key, lane.lane_id, call.name)
                calls += 1
                if claim.duplicate:
                    duplicates += 1
                    # Wait briefly for the owning agent to record its result so a
                    # duplicate never proceeds on an empty promise: the fleet stays
                    # synchronised instead of racing the same call twice.
                    for _attempt in range(20):
                        current = run.ledger.get(key)
                        if current is not None and current.content:
                            claim = current
                            break
                        await asyncio.sleep(0.5)
                    ok = claim.ok
                    content = (
                        "DUPLICATE SUPPRESSED by the shared ledger: "
                        f"{claim.owner or 'another lane'} already executed this exact {call.name} "
                        "request. Do not repeat it - reuse this recorded result instead:\n"
                        f"{claim.content[:3_000] or '(the other lane is still running that call)'}"
                    )
                else:
                    try:
                        outcome = await manager.tools.execute(ctx, call.name, call.args)
                    except Exception as exc:  # noqa: BLE001 - one lane must not kill the fleet
                        run.ledger.release(key)
                        outcome = ToolOutcome(ok=False, content=f"tool '{call.name}' crashed: {exc}")
                    ok = outcome.ok
                    content = outcome.content
                    run.ledger.record(key, lane.lane_id, call.name, outcome.ok, content, outcome.data)
                    if call.name == "add_finding" and outcome.ok:
                        findings += 1
                        finding = outcome.data.get("finding") or {}
                        run.memory.post(
                            "finding",
                            lane=lane.lane_id,
                            round_no=round_no,
                            text=str(finding.get("title") or "finding"),
                            evidence_id=str(finding.get("evidence_id") or ""),
                        )
                    if outcome.stop:
                        stop = True
                run.emit(
                    "tool_result",
                    lane=lane.lane_id,
                    text=content[:600],
                    data={
                        "name": call.name,
                        "ok": ok,
                        "duplicate": claim.duplicate,
                        "key": key,
                        "owner": claim.owner if claim.duplicate else lane.lane_id,
                        "content": content[:6_000],
                    },
                )
                run.memory.post(
                    "tool",
                    lane=lane.lane_id,
                    round_no=round_no,
                    tool=call.name,
                    ok=ok,
                    duplicate=claim.duplicate,
                    key=key,
                    excerpt=content[:1_000],
                )
                # Keep a compact record of what the tools actually returned.  Even
                # when the lane runs out of steps on a tool call, the board and the
                # final synthesis still carry real output instead of intent.
                observations.append(f"{call.name}: {content[:600]}")
                conversation.append(
                    {"role": "tool", "tool_name": call.name, "content": content[:8_000]}
                )
                if stop:
                    break
            if stop:
                break
        narrative = "\n\n".join(summary)
        if observations:
            narrative = (
                narrative
                + "\n\nTOOL OUTPUTS (verbatim excerpts)\n"
                + "\n".join(observations[-6:])
            )
        minimum = max(0, int(settings.swarm_min_findings_per_task))
        success = findings >= max(1, minimum) if settings.swarm_enforce_findings else True
        barrier = steps >= max(1, steps_budget) or run.stop_requested
        completed = bool(success or (barrier and not terminated))
        if success:
            reason = "finding recorded"
        elif terminated:
            reason = stop_reason or "terminated"
        elif barrier:
            reason = "round barrier reached without a finding - re-tasked next round"
        else:
            reason = "task ended without a finding - re-tasked next round"

        result = LaneResult(
            lane_id=lane.lane_id,
            category=lane.category,
            ok=True,
            summary=narrative[:6_000] or "(no narrative output)",
            steps=steps,
            tool_calls=calls,
            duplicates=duplicates,
            findings=findings,
            tokens=tokens,
            completed=completed,
            terminated=terminated,
            enforced=enforced,
            reason=reason,
            models_used=models_used,
        )
        # Every agent reports to the durable knowledge base after its task.
        await manager.report_knowledge(run, lane, task, result)
        return result


class SwarmManager:
    """Owns every swarm run in this process and their background tasks."""

    def __init__(
        self,
        settings: Settings,
        *,
        sandbox: Sandbox,
        guard: ScopeGuard,
        audit: AuditLog,
        verifier: Any,
        client: OllamaClient,
        tools: ToolRuntime,
        production: GuardrailEngine | None = None,
        evidence: EvidenceStore | None = None,
        memory: Any = None,
        skills: Any = None,
    ) -> None:
        self.settings = settings
        self.sandbox = sandbox
        self.guard = guard
        self.audit = audit
        self.verifier = verifier
        self.client = client
        self.tools = tools
        self.production = production
        self.evidence = evidence
        #: Durable knowledge base every lane reads before and reports after a task.
        self.memory = memory
        #: Agent-authored skills (create / install / run) shared by the fleet.
        self.skills = skills
        self.runs: dict[str, SwarmRun] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._lock = threading.Lock()
        self._runner = LaneRunner(self)

    # -- lifecycle ---------------------------------------------------------
    def _hydrate(self, run: SwarmRun) -> None:
        """Load durable lessons and the skill catalogue into the run.

        Lessons are retrieved **scoped to this objective** first.  The unscoped
        digest is only used as a fallback when nothing matches, because a global
        digest of old tasks is exactly how a new session ends up repeating the
        previous session's work: the models read a stored task and treat it as
        the current one.  Everything below is labelled reference material.
        """
        if self.memory is not None:
            try:
                relevant = self.memory.digest(
                    limit=max(4, self.settings.memory_digest_limit // 2),
                    chars=2_000,
                    query=run.objective[:160],
                )
                general = ""
                if len(relevant.splitlines()) < 3:
                    general = self.memory.digest(
                        limit=self.settings.memory_digest_limit, chars=2_000
                    )
                body = "\n".join(part for part in (relevant, general) if part)[:4_000]
                run.lessons = (
                    "REFERENCE ONLY - techniques and tool facts from earlier runs. They are not "
                    "the current task; never resume one of them unless this objective says so.\n"
                    + body
                    if body
                    else ""
                )
            except Exception as exc:  # noqa: BLE001 - memory must never block a run
                run.lessons = ""
                run.emit("notice", text=f"knowledge base unavailable: {exc}")

        if self.skills is not None:
            try:
                run.skill_manual = self.skills.manual()
            except Exception:  # noqa: BLE001 - a broken skill dir must not stop the fleet
                run.skill_manual = ""

    def _schedule(self, run: SwarmRun) -> SwarmRun:
        """Register a run and start its background driver."""
        self._hydrate(run)
        with self._lock:
            self.runs[run.run_id] = run
        loop = asyncio.get_running_loop()
        self._tasks[run.run_id] = loop.create_task(self._drive(run), name=f"swarm:{run.run_id}")
        return run

    def _fleet_roster(
        self,
        run_id: str,
        agents_per_model: int | None = None,
        groups: int | None = None,
    ) -> list[LaneSpec]:
        """Squads of five, or the legacy N-agents-per-model roster.

        A squad is ``FLEET_GROUP_ROLES`` (recon, web, exploit, coder, reporter),
        so ``groups`` of 1-5 produce 5 / 10 / 15 / 20 / 25 agents.  Models are
        handed out round-robin across the roster, which keeps every squad spread
        over the configured fleet models (with two models, a squad has three
        agents on one and two on the other).  ``agents_per_model`` keeps the old
        ``models x N`` shape alive for callers that still ask for it.
        """
        base = self.settings.kali_workdir.rstrip("/") or "/work"
        models = list(self.settings.fleet_models) or [self.settings.planner_model]
        lanes: list[LaneSpec] = []
        index = 0
        if groups is None and agents_per_model is None:
            groups = self.settings.fleet_groups
        if groups:
            group_count = max(1, min(FLEET_MAX_GROUPS, int(groups)))
            for group in range(1, group_count + 1):
                for role in FLEET_GROUP_ROLES:
                    index += 1
                    lane_id = f"agent-{index}"
                    workdir = f"{base}/fleet/{run_id}/{lane_id}"
                    lanes.append(
                        LaneSpec(
                            lane_id=lane_id,
                            category=role,
                            index=index,
                            workdir=workdir,
                            browser_profile=f"{workdir}/.browser",
                            model=models[(index - 1) % len(models)],
                            label=f"Agent {index}",
                            group=group,
                        )
                    )
            return lanes
        per_model = max(
            1, min(FLEET_GROUP_SIZE, int(agents_per_model or self.settings.fleet_agents_per_model))
        )
        for model in models:
            for _slot in range(per_model):
                index += 1
                role = FLEET_ROLE_ORDER[(index - 1) % len(FLEET_ROLE_ORDER)]
                lane_id = f"agent-{index}"
                workdir = f"{base}/fleet/{run_id}/{lane_id}"
                lanes.append(
                    LaneSpec(
                        lane_id=lane_id,
                        category=role,
                        index=index,
                        workdir=workdir,
                        browser_profile=f"{workdir}/.browser",
                        model=model,
                        label=f"Agent {index}",
                    )
                )
        return lanes

    def fleet_plan(self) -> list[dict[str, Any]]:
        """Roster preview for /api/fleet (no run started)."""
        return [lane.to_dict() for lane in self._fleet_roster("preview")]

    def start_fleet(
        self,
        *,
        objective: str,
        session_id: str = "",
        rounds: int | None = None,
        budget_tokens: int | None = None,
        until_complete: bool = False,
        agents_per_model: int | None = None,
        groups: int | None = None,
        unlimited: bool = False,
    ) -> SwarmRun:
        """Start the default fleet: whole squads of five, every role, all at once."""
        objective = _clean(objective, 8_000)
        if not objective:
            raise ValueError("objective must not be blank")
        run_id = uuid.uuid4().hex[:12]
        unlimited = bool(unlimited)
        until_complete = bool(until_complete or unlimited)
        groups_value: int | None = None
        if groups is not None:
            groups_value = max(1, min(FLEET_MAX_GROUPS, int(groups)))
        elif agents_per_model is None:
            groups_value = max(1, min(FLEET_MAX_GROUPS, int(self.settings.fleet_groups)))
        lanes = self._fleet_roster(run_id, agents_per_model, groups_value)
        if groups_value:
            lanes_per_category = FLEET_GROUP_SIZE
        else:
            lanes_per_category = max(
                1,
                min(
                    FLEET_GROUP_SIZE,
                    int(agents_per_model or self.settings.fleet_agents_per_model),
                ),
            )
        rounds_value = self._rounds_for(rounds, until_complete, unlimited)
        run = SwarmRun(
            run_id=run_id,
            objective=objective,
            session_id=_clean(session_id, 96) or f"fleet-{run_id}",
            categories=tuple(dict.fromkeys(lane.category for lane in lanes)),
            lanes_per_category=lanes_per_category,
            rounds=rounds_value,
            model=self.settings.planner_model,
            root=self.settings.swarm_dir / run_id,
            kali_workdir=self.settings.kali_workdir,
            budget_tokens=budget_tokens or self._budget_for(until_complete, rounds_value, len(lanes)),
            budget_seconds=self.settings.swarm_round_timeout_s * rounds_value,
            lanes=lanes,
            mode="fleet",
            models=self.settings.fleet_models,
            until_complete=until_complete,
        )
        return self._schedule(run)

    def _rounds_for(
        self, rounds: int | None, until_complete: bool, unlimited: bool = False
    ) -> int:
        """Requested rounds, the until-complete cap, or the unlimited ceiling."""
        if unlimited:
            return max(1, int(self.settings.fleet_unlimited_rounds))
        ceiling = self.settings.fleet_max_rounds if until_complete else 50
        if rounds is None or int(rounds) <= 0:
            default = self.settings.fleet_max_rounds if until_complete else self.settings.fleet_rounds
            return max(1, min(int(default), ceiling))
        return max(1, min(int(rounds), ceiling))

    def _budget_for(self, until_complete: bool, rounds: int, lanes: int = 0) -> int:
        """Token ceiling for a run, scaled so every requested round can run.

        A fixed 400k window used to end a 6-lane, 20-round mission around round
        five - the fleet stopped with the task half done because the wallet said
        so.  The window is now ``lanes x rounds x swarm_budget_per_lane_round``
        (never below the base budget), capped by ``swarm_budget_max_tokens``.
        """
        base = int(
            self.settings.fleet_until_complete_budget_tokens
            if until_complete
            else self.settings.swarm_budget_tokens
        )
        lane_count = max(1, int(lanes or self.settings.fleet_size))
        scaled = (
            lane_count
            * max(1, int(rounds))
            * max(1, int(self.settings.swarm_budget_per_lane_round))
        )
        ceiling = max(base, int(self.settings.swarm_budget_max_tokens))
        return int(max(base, min(ceiling, scaled)))

    def _keep_budget_alive(self, run: SwarmRun) -> None:
        """Raise a spent token/wall-clock window instead of ending the mission.

        Called between rounds.  The run may only end on proven success or when
        all requested rounds have run, so reaching the window adds another
        per-round allowance; the hard ceiling (``swarm_budget_max_tokens``) is the
        only thing that can still stop it, and that is reported loudly.
        """
        if not run.budget.exhausted():
            return
        hard_cap = max(
            int(self.settings.swarm_budget_max_tokens),
            int(self.settings.swarm_budget_tokens),
        )
        lanes = max(1, len(run.active_lanes()))
        allowance = lanes * max(1, int(self.settings.swarm_budget_per_lane_round))
        remaining = max(0, hard_cap - run.budget.limit)
        if remaining <= 0:
            # Wallet ceiling: the one mechanical limit that outranks "all rounds".
            run.stopped_reason = run.stopped_reason or (
                f"token ceiling {hard_cap} reached - round {run.round}/{run.rounds}, mission unfinished"
            )
            run.emit(
                "error",
                text=(
                    f"token ceiling {hard_cap} reached at round {run.round}/{run.rounds}. "
                    "The mission is NOT complete; raise AV2_SWARM_BUDGET_MAX_TOKENS or narrow "
                    "the objective."
                ),
            )
            return
        added = run.budget.extend(
            tokens=min(remaining, allowance),
            seconds=int(self.settings.swarm_round_timeout_s) * max(1, run.rounds - run.round + 1),
        )
        run.budget_extensions += 1
        run.emit(
            "notice",
            text=(
                f"token window raised by {added['tokens']} at round {run.round}/{run.rounds} "
                "so the remaining rounds can run - the mission is not cut short by a fixed budget"
            ),
            data={"budget": run.budget.status(), "extensions": run.budget_extensions},
        )


    def start(
        self,
        *,
        objective: str,
        session_id: str = "",
        categories: Iterable[str] | None = None,
        lanes_per_category: int | None = None,
        rounds: int | None = None,
        model: str = "",
        budget_tokens: int | None = None,
        fleet: bool = False,
        until_complete: bool = False,
        agents_per_model: int | None = None,
        groups: int | None = None,
        unlimited: bool = False,
    ) -> SwarmRun:
        """Create a run and schedule it.  Must be called from the event loop."""
        objective = _clean(objective, 8_000)
        if not objective:
            raise ValueError("objective must not be blank")
        unlimited = bool(unlimited)
        until_complete = bool(until_complete or unlimited)
        if fleet:
            return self.start_fleet(
                objective=objective,
                session_id=session_id,
                rounds=rounds,
                budget_tokens=budget_tokens,
                until_complete=until_complete,
                agents_per_model=agents_per_model,
                groups=groups,
                unlimited=unlimited,
            )
        wanted = [
            str(category).strip().lower()
            for category in (categories or self.settings.swarm_categories)
        ]
        wanted = [category for category in wanted if category in SWARM_CATEGORIES]
        if not wanted:
            raise ValueError(f"categories must include at least one of {', '.join(SWARM_CATEGORIES)}")
        lanes = lanes_per_category or self.settings.swarm_agents_per_category
        if agents_per_model:
            lanes = agents_per_model
        # One lane per role is a squad; five categories x five lanes is the
        # 25-agent ceiling, so the classic path can never exceed MAX_LANES.
        lanes = max(1, min(FLEET_GROUP_SIZE, int(lanes)))
        if rounds is None or int(rounds) <= 0:
            if unlimited:
                rounds_value = int(self.settings.fleet_unlimited_rounds)
            elif until_complete:
                rounds_value = self.settings.fleet_max_rounds
            else:
                rounds_value = self.settings.swarm_max_rounds
        else:
            rounds_value = self._rounds_for(rounds, until_complete, unlimited)
        run_id = uuid.uuid4().hex[:12]
        run = SwarmRun(
            run_id=run_id,
            objective=objective,
            session_id=_clean(session_id, 96) or f"swarm-{run_id}",
            categories=wanted,
            lanes_per_category=lanes,
            rounds=rounds_value,
            model=model or self.settings.planner_model,
            root=self.settings.swarm_dir / run_id,
            kali_workdir=self.settings.kali_workdir,
            budget_tokens=budget_tokens
            or self._budget_for(until_complete, rounds_value, len(wanted) * int(lanes)),
            budget_seconds=self.settings.swarm_round_timeout_s * rounds_value,

            until_complete=until_complete,
        )
        return self._schedule(run)

    async def _prepare(self, run: SwarmRun) -> None:
        """Create one terminal directory (and browser profile) per lane."""
        try:
            run.root.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        command = " && ".join(
            f"mkdir -p {shlex.quote(lane.workdir)} {shlex.quote(lane.browser_profile)}"
            for lane in run.lanes
        )
        result = await self.sandbox.exec(command, timeout=90)
        if not result.ok:
            run.emit(
                "error",
                text=f"sandbox could not prepare lane terminals: {result.stderr[:300] or result.stdout[:300]}",
            )
        elif self.settings.agent_terminals:
            # Pre-open one persistent shell per agent so round 1 starts with every
            # terminal alive (and /api/health shows them) instead of paying
            # the shell handshake inside each lane's first command.
            await asyncio.gather(
                *(
                    self.sandbox.exec(
                        "pwd", timeout=45, cwd=lane.workdir, terminal=lane.lane_id
                    )
                    for lane in run.lanes
                ),
                return_exceptions=True,
            )

    async def _lane(
        self,
        run: SwarmRun,
        lane: LaneSpec,
        task: LaneTask,
        round_no: int,
        semaphore: asyncio.Semaphore,
    ) -> None:
        async with semaphore:
            if run.stop_requested or run.budget.exhausted():
                run.lane_state[lane.lane_id] = "skipped"
                return
            run.lane_state[lane.lane_id] = "running"
            run.emit(
                "agent",
                lane=lane.lane_id,
                text=task.mission[:400],
                data={
                    "category": lane.category,
                    "round": round_no,
                    "workdir": lane.workdir,
                    "label": lane.label or lane.lane_id,
                    "agent": lane.label or lane.lane_id,
                    "model": lane.model or run.model,
                    "terminal": lane.lane_id,
                    "task": task.mission[:400],
                },
            )
            try:
                result = await asyncio.wait_for(
                    self._runner.run(run, lane, task, round_no),
                    timeout=self.settings.swarm_round_timeout_s,
                )
            except asyncio.TimeoutError:
                result = LaneResult(
                    lane_id=lane.lane_id,
                    category=lane.category,
                    ok=False,
                    error=f"lane exceeded {self.settings.swarm_round_timeout_s}s",
                )
            except asyncio.CancelledError:
                run.lane_state[lane.lane_id] = "cancelled"
                raise
            except Exception as exc:  # noqa: BLE001 - one lane must not kill the fleet
                result = LaneResult(
                    lane_id=lane.lane_id,
                    category=lane.category,
                    ok=False,
                    error=repr(exc),
                )
            run.results[lane.lane_id] = result
            run.lane_state[lane.lane_id] = "error" if result.error else "done"
            run.memory.post(
                "result",
                lane=lane.lane_id,
                round_no=round_no,
                ok=result.ok,
                summary=result.summary[:3_000],
                steps=result.steps,
                tool_calls=result.tool_calls,
                duplicates=result.duplicates,
                findings=result.findings,
                error=result.error,
            )
            run.emit(
                "tool_result" if result.ok else "error",
                lane=lane.lane_id,
                text=result.summary[:600] or result.error,
                data={
                    "round": round_no,
                    "label": lane.label or lane.lane_id,
                    "agent": lane.label or lane.lane_id,
                    "model": lane.model or run.model,
                    "terminal": lane.lane_id,
                    **result.to_dict(),
                },
            )


    # -- learning: every task ends with a report to the knowledge base -----
    async def _ask_lessons(
        self, run: SwarmRun, lane: LaneSpec, task: LaneTask, result: LaneResult
    ) -> list[dict[str, Any]]:
        """One small model call that turns the task into durable notes."""
        if self.memory is None:
            return []
        system = (
            "You maintain the Learning Board of a security-agent fleet. You get one finished task "
            "and must return ONE JSON object and nothing else:\n"
            '{"notes": [{"kind": "technique|tool|skill|error|strategy|lesson", '
            '"category": "reconnaissance|web-security|vulnerability-validation|tool-usage|'
            'troubleshooting|environment|planning|agent-coordination|reporting|general-lessons", '
            '"title": "short specific title", "body": "what worked or failed, exact command, exact '
            'error, and the fix", "roles": "recon,web", "tools": "ffuf,nmap", '
            '"preconditions": "...", "procedure": "the general procedure", "expected": "...", '
            '"observed": "...", "common_errors": "...", "troubleshooting": "...", '
            '"result": "success|failure|unknown", "tags": "target,service,tool", "confidence": 0.0}]}\n'
            "Record only what the transcript supports. Prefer the exact command line, the tool that "
            "was missing, the error text with its fix, and the strategy that made progress. Reuse a "
            "title you have seen when the lesson is the same. An empty list is acceptable only when "
            "the task taught nothing. Never include a private key, password, API key, token or seed "
            "phrase: describe the exposure and the remediation instead."
        )
        user = (
            f"AGENT: {lane.label or lane.lane_id} ({lane.category}, {lane.model or run.model})\n"
            f"ROUND: {run.round}/{run.rounds}\n"
            f"TASK: {task.mission}\nSUCCESS CRITERIA: {task.success or '(not stated)'}\n"
            f"OUTCOME: {result.reason or 'ended'} | findings={result.findings} "
            f"| tool_calls={result.tool_calls} | duplicates={result.duplicates} "
            f"| enforced={result.enforced}\n"
            f"TRANSCRIPT (verbatim excerpts)\n{result.summary[:3_500]}\n"
        )
        reply = await asyncio.wait_for(
            self.client.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                model=(result.models_used[-1] if result.models_used else (lane.model or run.model)),
                format="json",
                temperature=0.1,
                num_ctx=self.settings.worker_num_ctx,
            ),
            timeout=min(180.0, float(self.settings.llm_timeout_s)),
        )
        run.budget.charge(_tokens(reply))
        payload = _parse_json(reply.content)
        raw = payload.get("notes")
        if isinstance(raw, dict):
            raw = [raw]
        notes: list[dict[str, Any]] = []
        if isinstance(raw, list):
            for item in raw[:8]:
                if not isinstance(item, dict):
                    continue
                title = _clean(item.get("title"), 200)
                if not title:
                    continue
                kind = _clean(item.get("kind"), 20).lower()
                if kind not in {"technique", "tool", "skill", "error", "strategy", "lesson"}:
                    kind = "lesson"
                try:
                    confidence = float(item.get("confidence") or 0.6)
                except (TypeError, ValueError):
                    confidence = 0.6
                category = _clean(item.get("category"), 40).lower()
                if category not in {
                    "reconnaissance",
                    "web-security",
                    "vulnerability-validation",
                    "tool-usage",
                    "troubleshooting",
                    "environment",
                    "planning",
                    "agent-coordination",
                    "reporting",
                    "general-lessons",
                }:
                    category = ""
                outcome = _clean(item.get("result"), 20).lower()
                if outcome not in {"success", "failure", "unknown"}:
                    outcome = "success" if result.findings and not result.error else "unknown"
                notes.append(
                    {
                        "kind": kind,
                        "title": title,
                        "body": _clean(item.get("body"), 2_000),
                        "category": category,
                        "roles": _clean(item.get("roles"), 120) or lane.category,
                        "tools": _clean(item.get("tools"), 200),
                        "tags": _clean(item.get("tags"), 200),
                        "preconditions": _clean(item.get("preconditions"), 1_000),
                        "procedure": _clean(item.get("procedure"), 2_000),
                        "expected": _clean(item.get("expected"), 1_000),
                        "observed": _clean(item.get("observed"), 1_000),
                        "common_errors": _clean(item.get("common_errors"), 1_000),
                        "troubleshooting": _clean(item.get("troubleshooting"), 1_000),
                        "result": outcome,
                        "confidence": max(0.0, min(1.0, confidence)),
                    }
                )
        return notes

    @staticmethod
    def _derived_lessons(
        run: SwarmRun, lane: LaneSpec, task: LaneTask, result: LaneResult
    ) -> list[dict[str, Any]]:
        """Deterministic fallback so a task always leaves a record behind."""
        notes: list[dict[str, Any]] = []
        if result.error:
            notes.append(
                {
                    "kind": "error",
                    "title": f"{lane.category}: {result.error[:140]}",
                    "body": result.error,
                    "category": "troubleshooting",
                    "roles": lane.category,
                    "tags": f"error,{lane.category},auto",
                    "result": "failure",
                    "confidence": 0.4,
                }
            )
        summary = re.sub(r"\s+", " ", result.summary or "").strip()
        if summary:
            notes.append(
                {
                    "kind": "technique" if result.findings else "lesson",
                    "title": f"{lane.category} r{run.round}: {task.mission[:90]}",
                    "body": summary[:1_200],
                    "category": "" if result.findings else "general-lessons",
                    "roles": lane.category,
                    "tags": f"{lane.category},auto",
                    "result": "success" if result.findings else "unknown",
                    "confidence": 0.35,
                }
            )
        return notes

    async def report_knowledge(
        self, run: SwarmRun, lane: LaneSpec | None, task: LaneTask | None, result: LaneResult
    ) -> list[dict[str, Any]]:
        """Store what one lane learned - best effort, never fatal for the lane."""
        try:
            return await self._report_knowledge(run, lane, task, result)
        except Exception as exc:  # noqa: BLE001 - learning must not discard the task
            run.emit(
                "notice",
                lane=lane.lane_id if lane is not None else "",
                text=f"knowledge report skipped: {exc}",
            )
            return []

    async def _report_knowledge(
        self, run: SwarmRun, lane: LaneSpec | None, task: LaneTask | None, result: LaneResult
    ) -> list[dict[str, Any]]:
        """Store what one lane learned, whether the model cooperates or not."""
        if self.memory is None or not bool(self.settings.swarm_knowledge_report) or lane is None:
            return []
        mission = task.mission if task is not None else ""
        notes: list[dict[str, Any]] = []
        if result.tool_calls or result.summary:
            try:
                notes = await self._ask_lessons(run, lane, task or LaneTask(mission=mission), result)
            except (OllamaError, OSError, ValueError, asyncio.TimeoutError) as exc:
                run.emit(
                    "notice",
                    lane=lane.lane_id,
                    text=f"knowledge report fell back to the transcript: {exc}",
                )
        if not notes:
            notes = self._derived_lessons(run, lane, task or LaneTask(mission=mission), result)
        if not notes:
            return []
        written = self.memory.write_many(
            notes,
            source="lane_report",
            status="observed",
            roles=lane.category,
            agent=lane.label or lane.lane_id,
            model=(result.models_used[-1] if result.models_used else (lane.model or run.model)),
            session_id=run.session_id,
            run_id=run.run_id,
            task=mission,
            outcome=(
                "proved"
                if result.findings
                else ("terminated" if result.terminated else "unresolved")
            ),
        )
        if written:
            run.learned += len(written)
            run.memory.post(
                "knowledge",
                lane=lane.lane_id,
                round_no=run.round,
                notes=[str(note.get("title") or "") for note in written],
            )
            run.emit(
                "notice",
                lane=lane.lane_id,
                text=(
                    f"{lane.label or lane.lane_id} reported {len(written)} lesson(s) to the "
                    "knowledge base: " + "; ".join(str(note.get("title"))[:60] for note in written)
                ),
                data={"notes": written, "run_learned": run.learned},
            )
            self.audit.write(
                "swarm.learn",
                run_id=run.run_id,
                session=run.session_id,
                lane=lane.lane_id,
                notes=len(written),
                kinds=[note.get("kind") for note in written],
            )
        return written

    # -- the run itself ----------------------------------------------------
    async def _probe_endpoint(self) -> tuple[bool, str]:
        """One cheap reachability probe, tolerant of a client without ``health``."""
        health = getattr(self.client, "health", None)
        if not callable(health):
            return True, "no health probe"
        try:
            reachable, detail = await health()
        except Exception as exc:  # noqa: BLE001 - the probe must never kill a run
            return False, f"{type(exc).__name__}: {exc}"
        return bool(reachable), str(detail)

    async def _endpoint_problem(self) -> str:
        """Return an actionable sentence when the model endpoint is wholly down.

        A swarm does not fail once when the provider hostname cannot be resolved:
        every lane spends its first step on the same dead socket, so a multi-round
        fleet "finishes" in ten seconds with zero findings and an operator hunting
        a swarm bug that is not there.  One probe before dispatch - retried a
        couple of times for DNS, which is what flaps - turns that into a single
        sentence naming DNS, the host and the fix.
        """
        base_url = str(getattr(self.settings, "ollama_base_url", "") or "")
        detail = ""
        for attempt in range(3):
            reachable, detail = await self._probe_endpoint()
            if reachable:
                return ""
            kind, _hint = classify_model_failure(detail, base_url)
            # DNS flaps (VPN/proxy reconnect) and deserves both retries; anything
            # else gets one more chance and is then taken at its word.
            if kind != "dns" and attempt >= 1:
                break
            await asyncio.sleep(1.0 + attempt)
        _kind, hint = classify_model_failure(detail, base_url)
        return hint or f"the model endpoint is unavailable: {detail}"

    async def _drive(self, run: SwarmRun) -> None:
        run.status = "running"
        run.started_at = utc_now()
        self.audit.write(
            "swarm.start",
            run_id=run.run_id,
            session=run.session_id,
            objective=run.objective[:300],
            lanes=len(run.lanes),
            categories=list(run.categories),
            rounds=run.rounds,
            model=run.model,
            mode=run.mode,
            models=list(run.models),
        )
        run.emit(
            "start",
            text=run.objective,
            data={
                "lanes": len(run.lanes),
                "categories": list(run.categories),
                "model": run.model,
                "models": list(run.models),
                "mode": run.mode,
                "rounds": run.rounds,
                "until_complete": run.until_complete,
                "roster": [lane.to_dict() for lane in run.lanes],
                "enforcement": {
                    "findings_required": self.settings.swarm_min_findings_per_task
                    if self.settings.swarm_enforce_findings
                    else 0,
                    "rounds_enforced": bool(self.settings.swarm_enforce_rounds),
                    "knowledge_report": bool(self.settings.swarm_knowledge_report),
                },
                "lessons_loaded": len(run.lessons.splitlines()) if run.lessons else 0,
                "skills": run.skill_manual.splitlines() if run.skill_manual else [],
            },
        )
        strategist = SwarmStrategist(self.client, self.settings)
        stalled_rounds = 0
        findings_total = 0
        minimum_findings = max(1, int(self.settings.swarm_min_findings_per_task))
        try:
            await self._prepare(run)
            endpoint_problem = await self._endpoint_problem()
            if endpoint_problem:
                # Every lane would die on its first step for the same reason, so
                # report it once, as itself, instead of N "terminated" agents.
                run.status = "failed"
                run.error = endpoint_problem
                run.stopped_reason = "model endpoint unreachable"
                run.completion = {
                    "approved": False,
                    "gate": "connectivity",
                    "findings": int(run.memory.counts().get("finding", 0)),
                    "reason": "the model endpoint could not be reached",
                    "missing": ["a reachable model endpoint"],
                    "round": run.round,
                    "rounds": run.rounds,
                }
                run.memory.post("note", text=endpoint_problem, cause="connectivity")
                run.emit(
                    "error",
                    text=f"model endpoint unreachable - {endpoint_problem}",
                    data={
                        "reason": "model endpoint unreachable",
                        "base_url": str(getattr(self.settings, "ollama_base_url", "") or ""),
                        "lanes": len(run.lanes),
                    },
                )
                self.audit.write("swarm.error", run_id=run.run_id, error=endpoint_problem)
                return
            for round_no in range(1, run.rounds + 1):
                if run.stop_requested:
                    run.stopped_reason = run.stopped_reason or "operator stop"
                    break
                if not run.active_lanes():
                    # Enforcement removed every lane: the run is over, but it is a
                    # termination, not a success and not a completed mission.
                    run.stopped_reason = "every lane was terminated by enforcement"
                    run.emit(
                        "error",
                        text=(
                            "every agent was terminated for stopping without evidence - "
                            "the mission ends without a result"
                        ),
                    )
                    break
                # "Only stop on success or when all rounds are over": a spent token
                # window is raised here instead of ending the mission early.
                self._keep_budget_alive(run)
                run.round = round_no

                executed_before = int(run.ledger.status().get("executed", 0))
                # Fleet round 1 uses deterministic role shards so every agent
                # starts moving immediately; every later round is re-planned by
                # the strategist from the shared board (real collaboration, not
                # N copies of one prompt).
                deterministic = (
                    run.mode == "fleet"
                    and round_no == 1
                    and not self.settings.fleet_strategise_first_round
                )
                if deterministic:
                    plan = _plan_from({}, run, round_no)
                    plan.strategy = "deterministic role shards - every agent owns a different slice"
                    run.emit(
                        "notice",
                        text=(
                            f"round 1/{run.rounds}: {len(run.lanes)} agents dispatched on "
                            "distinct role shards"
                        ),
                        data=plan.to_dict(),
                    )
                else:
                    plan = await strategist.plan(run, round_no)
                run.tasks = dict(plan.tasks)
                run.memory.post(
                    "plan",
                    round_no=round_no,
                    analysis=plan.analysis,
                    strategy=plan.strategy,
                    stop=plan.stop,
                )
                if not deterministic:
                    run.emit(
                        "notice",
                        text=(
                            f"round {round_no}/{run.rounds}: "
                            f"{plan.strategy or plan.analysis or 'fleet working'}"
                        ),
                        data=plan.to_dict(),
                    )
                semaphore = asyncio.Semaphore(max(1, self.settings.swarm_max_concurrency))
                # A lane that ended last round without a proven result gets a
                # continuation mission: same lane, different technique.  This is
                # the "re-strategise together until the rounds are done" rule.
                live = run.active_lanes()
                for lane in live:
                    current = plan.tasks.get(lane.lane_id)
                    if current is None or lane.lane_id not in run.unresolved:
                        continue
                    plan.tasks[lane.lane_id] = LaneTask(
                        mission=(
                            "CONTINUATION - last round this lane proved nothing. Do not restate the "
                            "previous attempt: change the technique, tool or target slice and run the "
                            f"smallest test that can produce evidence. {current.mission}"
                        ),
                        success=current.success,
                        hypothesis=current.hypothesis,
                        avoid=list(current.avoid) + ["repeating last round's failed attempt"],
                    )
                run.tasks = dict(plan.tasks)
                await asyncio.gather(
                    *(
                        self._lane(run, lane, plan.tasks[lane.lane_id], round_no, semaphore)
                        for lane in live
                    )
                )
                findings_total = int(run.memory.counts().get("finding", 0))
                run.unresolved = {
                    lane.lane_id
                    for lane in run.active_lanes()
                    if (result := run.results.get(lane.lane_id)) is not None
                    and not result.terminated
                    and result.findings < minimum_findings
                }

                if run.unresolved:
                    run.emit(
                        "notice",
                        text=(
                            f"round {round_no}/{run.rounds}: {len(run.unresolved)} agent(s) have "
                            "no finding yet and are re-tasked with a different technique next round"
                        ),
                        data={"unresolved": sorted(run.unresolved), "findings": findings_total},
                    )
                proven = findings_total >= minimum_findings
                if plan.stop:
                    approved = False
                    verdict: dict[str, Any] = {
                        "approved": False,
                        "gate": "findings",
                        "findings": findings_total,
                        "reason": "no finding is recorded",
                        "missing": ["at least one finding backed by real tool output"],
                    }
                    if not self.settings.swarm_enforce_findings:
                        # Enforcement explicitly disabled: the operator asked to
                        # trust the strategist's judgement.
                        approved = True
                        verdict["gate"] = "enforcement disabled"
                    elif proven:
                        # A stop is a claim, not a decision: audit it against the
                        # findings and the objective before ending the mission.
                        approved, verdict = await strategist.verify_completion(run)
                    run.completion = {**verdict, "round": round_no, "rounds": run.rounds}
                    if approved:
                        run.mission_complete = True
                        run.emit(
                            "notice",
                            text=(
                                f"mission complete after round {round_no}/{run.rounds}: the "
                                "completion gate accepted the evidence for this objective"
                            ),
                            data={
                                "round": round_no,
                                "rounds": run.rounds,
                                "ledger": run.ledger.status(),
                                "findings": findings_total,
                                "completion": run.completion,
                            },
                        )
                        break
                    missing = "; ".join(str(item) for item in (verdict.get("missing") or [])[:6])
                    run.memory.post(
                        "operator",
                        text=(
                            "ENFORCEMENT: your stop was refused. "
                            + (verdict.get("reason") or "the objective is not proven")
                            + (f". Still missing: {missing}" if missing else "")
                            + ". Assign every live lane the exact evidence that is still missing, "
                            "change technique where an approach already failed, and do not set stop "
                            "again until that evidence is recorded."
                        ),
                        round_no=round_no,
                    )
                    run.emit(
                        "notice",
                        text=(
                            f"stop refused at round {round_no}/{run.rounds}: {findings_total} "
                            f"finding(s) recorded but the completion gate says the objective is not "
                            f"proven ({verdict.get('reason') or 'incomplete'}). The fleet keeps "
                            "working until the gate approves or the rounds are over."
                        ),
                        data={
                            "findings": findings_total,
                            "required": minimum_findings,
                            "completion": run.completion,
                        },
                    )

                if run.until_complete:
                    progress = int(run.ledger.status().get("executed", 0)) - executed_before
                    if progress <= 0:
                        stalled_rounds += 1
                        if stalled_rounds >= 2 and not self.settings.swarm_enforce_rounds:
                            run.emit(
                                "notice",
                                text=(
                                    "two consecutive rounds produced no new tool executions - "
                                    "stopping instead of burning the round cap"
                                ),
                            )
                            break
                        # Enforcement keeps the mission alive: the board carries a
                        # hard directive so the next plan cannot be a repeat.
                        run.memory.post(
                            "operator",
                            text=(
                                f"ENFORCEMENT: round {round_no} produced no new execution. "
                                "Change the approach for every lane: new targets, new tools, "
                                "decompose the objective further. Do not repeat a mission."
                            ),
                            round_no=round_no,
                        )
                        run.emit(
                            "notice",
                            text=(
                                f"round {round_no}/{run.rounds} produced no new execution; "
                                "enforcement forces another re-plan with a different approach"
                                if self.settings.swarm_enforce_rounds
                                else f"round {round_no}/{run.rounds} produced no new execution; "
                                "re-planning once more before giving up"
                            ),
                        )
                    else:
                        stalled_rounds = 0
            if (
                run.until_complete
                and not run.mission_complete
                and not run.stop_requested
                and run.round >= run.rounds
            ):
                run.emit(
                    "notice",
                    text=(
                        f"round cap {run.rounds} reached before the strategist reported "
                        "completion - raising the cap continues the mission"
                    ),
                )
            run.status = "synthesising"
            run.synthesis = await strategist.synthesise(run)
            findings_total = int(run.memory.counts().get("finding", 0))
            if run.stop_requested:
                run.status = "stopped"
            elif run.mission_complete:
                run.status = "completed"
            elif not run.active_lanes():
                run.status = "terminated"
            elif findings_total >= minimum_findings:
                # All rounds ran: the gate decides whether this counts as success
                # or as an exhausted mission that found evidence but not the answer.
                approved, verdict = await strategist.verify_completion(run)
                run.completion = {**verdict, "round": run.round, "rounds": run.rounds}
                run.status = "completed" if approved else "exhausted"
            else:
                run.completion = {
                    "approved": False,
                    "gate": "findings",
                    "findings": findings_total,
                    "reason": "no finding was recorded",
                    "missing": ["at least one finding backed by real tool output"],
                    "round": run.round,
                    "rounds": run.rounds,
                }
                run.status = "exhausted"
            if run.status == "exhausted":
                # Not a success, and the run says so instead of pretending.
                run.emit(
                    "notice",
                    text=(
                        f"mission exhausted after {run.round}/{run.rounds} round(s): "
                        f"{findings_total} finding(s) recorded but the objective is not proven, so "
                        "the task is NOT complete. "
                        f"{run.ledger.status().get('executed', 0)} execution(s) and every lesson are "
                        "on the board and in the knowledge base."
                    ),
                    data={
                        "findings": findings_total,
                        "required": minimum_findings,
                        "completion": run.completion,
                    },
                )

            run.memory.post("synthesis", round_no=run.round, text=run.synthesis[:8_000])
            self.audit.write(
                "swarm.done",
                run_id=run.run_id,
                session=run.session_id,
                status=run.status,
                findings=findings_total,
                learned=run.learned,
                ledger=run.ledger.status(),
                budget=run.budget.status(),
            )
        except asyncio.CancelledError:
            run.status = "cancelled"
            run.stopped_reason = run.stopped_reason or "cancelled by the operator"
            run.emit("notice", text="swarm cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced through the run state
            run.status = "failed"
            run.error = repr(exc)
            self.audit.write("swarm.error", run_id=run.run_id, error=repr(exc))
            run.emit("error", text=f"swarm failed: {exc}")
        finally:
            run.finished_at = utc_now()
            run.done.set()
            run.emit(
                "done",
                text=run.synthesis or run.error,
                data={
                    "status": run.status,
                    "round": run.round,
                    "rounds": run.rounds,
                    "rounds_remaining": max(0, run.rounds - run.round),
                    "mission_complete": run.mission_complete,
                    "completion": dict(run.completion),
                    "stopped_reason": run.stopped_reason,
                    "directives": list(run.directives),
                    "ledger": run.ledger.status(),
                    "budget": run.budget.status(),
                    "budget_extensions": run.budget_extensions,
                    "findings": int(run.memory.counts().get("finding", 0)),
                    "learned": run.learned,
                    "enforced": run.enforced,
                    "unresolved": sorted(run.unresolved),
                    "terminated": sorted(run.terminated),
                },

            )
            with self._lock:
                self._tasks.pop(run.run_id, None)

    # -- reads -------------------------------------------------------------
    def _find(self, run_id: str) -> SwarmRun | None:
        wanted = (run_id or "").strip()
        with self._lock:
            if wanted:
                return self.runs.get(wanted)
            if not self.runs:
                return None
            return max(self.runs.values(), key=lambda item: item.created_at)

    def status(self, run_id: str = "") -> dict[str, Any] | None:
        run = self._find(run_id)
        return run.status_dict() if run is not None else None

    def latest(self) -> dict[str, Any] | None:
        return self.status("")

    def runs_list(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            runs = sorted(self.runs.values(), key=lambda item: item.created_at, reverse=True)
        items: list[dict[str, Any]] = []
        for run in runs[: max(1, limit)]:
            items.append(
                {
                    "run_id": run.run_id,
                    "objective": run.objective[:300],
                    "session_id": run.session_id,
                    "status": run.status,
                    "round": run.round,
                    "rounds": run.rounds,
                    "lanes": len(run.lanes),
                    "categories": list(run.categories),
                    "created_at": run.created_at,
                    "finished_at": run.finished_at,
                    "mode": run.mode,
                    "models": list(run.models),
                    "ledger": run.ledger.status(),
                    "budget": run.budget.status(),
                }
            )
        return items

    def board(self, run_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """The shared board of one run.

        Named ``board`` rather than ``memory``: ``self.memory`` on this manager
        is the durable SQLite knowledge base, and a method of the same name would
        be shadowed by that attribute (it used to 500 the board endpoint).
        """
        run = self._find(run_id)
        return run.memory.to_list(limit) if run is not None else []


    def events(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        run = self._find(run_id)
        return run.public_events(after) if run is not None else []

    # -- control -----------------------------------------------------------
    #: A run in one of these states can still receive an operator directive.
    LIVE_STATUSES = frozenset({"queued", "running", "synthesising"})

    def live_for_session(self, session_id: str = "") -> SwarmRun | None:
        """Newest live run for a session (or the newest live run at all)."""
        wanted = (session_id or "").strip()
        with self._lock:
            candidates = [
                run
                for run in self.runs.values()
                if run.status in self.LIVE_STATUSES and (not wanted or run.session_id == wanted)
            ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.created_at)

    def direct(
        self, text: str, *, session_id: str = "", run_id: str = ""
    ) -> dict[str, Any] | None:
        """Deliver an operator directive to the live fleet.

        This is the "stop and do this instead" path from the chat box: the text
        lands on the board, in the strategist's next plan and in every running
        lane's conversation between steps, and it outranks the old objective.
        """
        clean = _clean(text, 2_000)
        if not clean:
            return None
        run = self._find(run_id) if (run_id or "").strip() else None
        if run is None or run.status not in self.LIVE_STATUSES:
            run = self.live_for_session(session_id)
        if run is None:
            return None
        record = run.add_directive(clean)
        self.audit.write(
            "swarm.directive",
            run_id=run.run_id,
            session=run.session_id,
            text=clean[:300],
            delivered_at_round=run.round,
        )
        return {
            "delivered": bool(record),
            "run_id": run.run_id,
            "session_id": run.session_id,
            "round": run.round,
            "rounds": run.rounds,
            "status": run.status,
            "directive": clean,
        }

    def message(self, run_id: str, text: str) -> dict[str, Any] | None:
        """Operator note injected into a live run's board (directive semantics)."""
        run = self._find(run_id)
        text = _clean(text, 2_000)
        if run is None or not text:
            return None
        record = run.add_directive(text)
        self.audit.write("swarm.operator_note", run_id=run.run_id, text=text[:300])
        return record

    def stop_session(self, session_id: str = "", *, cancel: bool = True) -> list[str]:
        """Stop every live run for a session - the explicit operator brake."""
        wanted = (session_id or "").strip()
        with self._lock:
            runs = [
                run
                for run in self.runs.values()
                if run.status in self.LIVE_STATUSES and (not wanted or run.session_id == wanted)
            ]
        stopped: list[str] = []
        for run in runs:
            self.stop(run.run_id, cancel=cancel)
            stopped.append(run.run_id)
        return stopped


    def subscribe(
        self, run_id: str
    ) -> tuple[SwarmRun, list[dict[str, Any]], asyncio.Queue[dict[str, Any]]] | None:
        """Attach to a run's live event stream (backlog + live queue)."""
        run = self._find(run_id)
        if run is None:
            return None
        backlog, queue = run.subscribe()
        return run, backlog, queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        run = self._find(run_id)
        if run is not None:
            run.unsubscribe(queue)


    def stop(self, run_id: str = "", *, cancel: bool = False) -> dict[str, Any] | None:
        """Request a graceful stop (lanes finish their current call)."""
        run = self._find(run_id)
        if run is None:
            return None
        run.stop_requested = True
        run.emit("notice", text="stop requested by the operator")
        if cancel:
            task = self._tasks.get(run.run_id)
            if task is not None and not task.done():
                task.cancel()
        self.audit.write("swarm.stop", run_id=run.run_id, cancel=cancel)
        return run.status_dict()

    async def wait(self, run_id: str = "", timeout: float = 120.0) -> dict[str, Any] | None:
        """Await completion of a run, returning its status (``None`` if unknown)."""
        run = self._find(run_id)
        if run is None:
            return None
        try:
            await asyncio.wait_for(run.done.wait(), timeout=max(0.1, timeout))
        except asyncio.TimeoutError:
            pass
        return run.status_dict()

    def shutdown(self) -> None:
        """Cancel every background run (used when the API stops)."""
        with self._lock:
            tasks = list(self._tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()


__all__ = [
    "Claim",
    "DEFAULT_LANES",
    "LANE_EXCLUDED_TOOLS",
    "LaneResult",
    "LaneRunner",
    "LaneSpec",
    "LaneTask",
    "MAX_LANES",
    "SWARM_CATEGORIES",
    "SwarmBudget",
    "SwarmLedger",
    "SwarmManager",
    "SwarmMemory",
    "SwarmPlan",
    "SwarmRun",
    "SwarmStrategist",
    "format_status",
    "swarm_prompt_block",
    "utc_now",
]
