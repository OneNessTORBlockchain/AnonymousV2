"""Pydantic request/response contracts shared by the HTTP and WebSocket APIs."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

AgentName = Literal["captain", "recon", "web", "exploit", "coder", "reporter"]
EngagementMode = Literal["exploit", "intrusive", "recon"]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    session_id: str | None = None
    agent: AgentName = "captain"
    mode: EngagementMode = "exploit"
    model: str | None = None
    stream: bool = True
    #: Fleet rounds for this turn.  ``until_complete`` treats this as a cap and
    #: keeps re-planning until the strategist reports the mission complete.
    rounds: int | None = Field(default=None, ge=1, le=50)
    until_complete: bool = False
    #: "until done": ignore the round cap and keep re-planning until the
    #: completion gate approves (the token/wall-clock window still bounds it).
    unlimited: bool = False
    #: Agents per model for this turn's fleet (1-6); legacy roster shape.
    agents: int | None = Field(default=None, ge=1, le=6)
    #: Squads of five (1-5) => 5 / 10 / 15 / 20 / 25 agents.  Takes precedence
    #: over ``agents`` and defaults to AV2_FLEET_GROUPS.
    groups: int | None = Field(default=None, ge=1, le=5)

    @field_validator("message")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message must not be blank")
        return value


class TurnEvent(BaseModel):
    """A single item in the run event stream (also the WebSocket frame shape)."""

    type: Literal[
        "start",
        "agent",
        "token",
        "thinking",
        "tool_call",
        "tool_result",
        "notice",
        "error",
        "done",
    ]
    session_id: str
    seq: int = 0
    agent: str | None = None
    text: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    ts: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))


class ChatResponse(BaseModel):
    session_id: str
    agent: str
    content: str
    events: list[TurnEvent] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    iterations: int = 0
    model: str = ""
    verified_transactions: list[dict[str, Any]] = Field(default_factory=list)

    class Config:
        json_schema_extra = {"example": {"session_id": "abc123", "agent": "captain", "content": "done"}}


class VerifyTransactionIn(BaseModel):
    """Request body for the standalone verification endpoint."""

    tx_hash: str = Field(min_length=32, max_length=200)
    chain: str = "ethereum"
    rpc_url: str = ""
    explorer_base: str = ""
    expected_from: str = ""
    expected_to: str = ""
    expected_value: str = ""
    expected_chain_id: int | None = Field(default=None, ge=0)
    min_confirmations: int = Field(default=0, ge=0, le=10_000)
    wait_seconds: int = Field(default=0, ge=0, le=300)
    contract_address: str = ""
    event_topic: str = ""


class ScopeEntryIn(BaseModel):
    value: str = Field(min_length=1, max_length=512)
    mode: EngagementMode = "exploit"
    note: str = ""


class SwarmStartIn(BaseModel):
    """Start a swarm: squads of five agents per group, one objective, one ledger."""

    objective: str = Field(min_length=4, max_length=8_000)
    session_id: str | None = None
    categories: list[str] = Field(default_factory=list)
    lanes: int = Field(default=5, ge=1, le=5)
    rounds: int = Field(default=2, ge=1, le=50)
    #: Keep re-planning rounds until the strategist reports the objective
    #: answered.  ``rounds`` then becomes the hard cap instead of the target.
    until_complete: bool = False
    #: "until done": no round cap at all - only the completion gate, the token
    #: window and the wall clock can end the run.
    unlimited: bool = False
    model: str = ""
    #: True starts the default multi-model fleet (squads of five) instead of N
    #: lanes per category.
    fleet: bool = False
    #: Agents per model for the legacy fleet shape (1-6); ``groups`` wins.
    agents: int | None = Field(default=None, ge=1, le=6)
    #: Squads of five (1-5) => 5 / 10 / 15 / 20 / 25 agents.
    groups: int | None = Field(default=None, ge=1, le=5)

    @field_validator("objective")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("objective must not be blank")
        return value


class SwarmNoteIn(BaseModel):
    """Operator note injected into the shared board while lanes are running."""

    message: str = Field(min_length=1, max_length=2_000)

    @field_validator("message")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message must not be blank")
        return value


class SteerIn(BaseModel):
    """A new direction sent while a fleet is running.

    The directive outranks the objective and the current missions: it reaches
    the strategist's next plan and every running lane between steps, so "stop
    and do this instead" is actually obeyed instead of queued behind the old task.
    """

    message: str = Field(min_length=1, max_length=2_000)
    session_id: str = ""
    run_id: str = ""

    @field_validator("message")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message must not be blank")
        return value


class MemoryNoteIn(BaseModel):
    """One durable lesson written by the operator or an agent."""

    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=8_000)
    kind: str = "technique"
    tags: list[str] | str = Field(default_factory=list)
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    agent: str = ""
    source: str = "operator"


class MemoryUpdateIn(BaseModel):
    """Operator review: edit any field of a stored lesson."""

    kind: str | None = None
    title: str | None = None
    body: str | None = None
    tags: list[str] | str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    helpful: bool | None = None


class SkillIn(BaseModel):
    """A custom skill an agent (or the operator) installs for the fleet."""

    name: str = Field(min_length=1, max_length=120)
    code: str = Field(min_length=1, max_length=200_000)
    description: str = Field(default="", max_length=600)
    language: str = "python"
    entry: str = ""
    tags: list[str] | str = Field(default_factory=list)


class SkillRunIn(BaseModel):
    args: str = Field(default="", max_length=2_000)
    timeout_s: int = Field(default=300, ge=5, le=1_800)


class EngagementIn(BaseModel):
    """The operator's written authorization in machine-checkable form."""

    engagement: str = ""
    owner: str = ""
    authorization_ref: str = ""
    allowed_targets: list[str] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=list)
    excluded_targets: list[str] = Field(default_factory=list)
    expires_at: str = ""
    rate_limit_per_minute: int = Field(default=60, ge=1, le=6_000)
    max_command_timeout_s: int = Field(default=300, ge=5, le=3_600)
    max_commands: int = Field(default=0, ge=0)
    dry_run: bool = False
    kill_switch: bool = False
    notes: str = ""


class HealthOut(BaseModel):
    status: str
    version: str
    provider: str
    ollama: bool
    ollama_detail: str
    sandbox: bool
    sandbox_detail: str
    planner_model: str
    worker_model: str
    planner_tools: bool
    #: Context window AV2 asks the server for (``options.num_ctx``, native protocol
    #: only).  A swarm prompt is ~4.7 k tokens, so anything under that cannot work -
    #: docs/CONTEXT-WINDOW.md.
    context_window: int = 0
    context_reserve: int = 0
    godmode: str
    godmode_skill: bool
    godmode_skill_path: str
    transaction_gate: bool
    scope_count: int
    egress: str = "direct"
    fleet: bool = True
    fleet_auto: bool = True
    fleet_models: list[str] = Field(default_factory=list)
    fleet_size: int = 5
    fleet_groups: int = 1
    fleet_group_size: int = 5
    max_groups: int = 5
    agent_terminals: list[dict[str, Any]] = Field(default_factory=list)


class ProviderIn(BaseModel):
    """Endpoint, model and key an operator can change without editing ``.env``.

    Every field is optional: an empty string keeps the current value, so the
    form can be submitted with only the model changed.  The key is never
    echoed back - :func:`main.provider_get` returns only ``api_key_set``.
    """

    provider: str = Field(default="", max_length=40)
    base_url: str = Field(default="", max_length=500)
    api_key: str = Field(default="", max_length=500)
    model: str = Field(default="", max_length=200)
    #: Optional comma-separated or JSON list of extra fleet models.
    fleet_models: list[str] | str = Field(default_factory=list)


class AgentSpec(BaseModel):
    name: str
    title: str
    mission: str
    tools: list[str]
