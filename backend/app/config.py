"""Runtime configuration for the Anonymous V2 backend.

Every setting is overridable from the environment with the ``AV2_`` prefix, or
from the ``.env`` file at the repository root.  Windows-friendly defaults keep
the first run zero-config: the model endpoint defaults to Venice AI cloud and
the Kali VM is driven through the Docker CLI, so no Python docker socket
binding is required on the host.

The planner runs on the configured planner model and every task is fanned out
to **squads of five specialists** (recon, web, exploit, coder, reporter), one to
five squads per run, so 5-25 agents work at the same time.  Models are handed
out round-robin across ``AV2_FLEET_MODELS``, so the same squad can already be
spread over several providers.  Venice speaks the OpenAI chat-completions
protocol, so :class:`app.core.ollama_client.OllamaClient` switches to
``/chat/completions`` whenever :attr:`Settings.openai_style` is true.  Point
``AV2_OLLAMA_BASE_URL`` back at a local Ollama daemon (with
``AV2_LLM_PROVIDER=ollama``) to run fully local again.

The Venice key is **not** stored in this file: set ``AV2_LLM_API_KEY`` in the
environment or the repository ``.env``.

The ``OLLAMA_HOST`` / ``OLLAMA_API_KEY`` environment variables are deliberately
*not* read here: they are commonly set machine-wide for a local daemon and would
silently shadow the cloud endpoint, so only ``AV2_*`` names are honoured.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]

#: Model served by Venice AI cloud for the planner and the first fleet tier.
CLOUD_MODEL = "glm-5-3-flash-uncensored"
#: Second cloud model: the fleet runs three lanes on each model at the same time.
DEEPSEEK_MODEL = "deepseek-v4-1-flash-1017-uncensored"

#: One squad is five agents, one per role.  A run launches whole squads so every
#: group always has recon, web, exploit, coder and reporter coverage.
FLEET_GROUP_ROLES: tuple[str, ...] = ("recon", "web", "exploit", "coder", "reporter")
#: Agents in one squad (len is the contract, the UI shows it as "x5").
FLEET_GROUP_SIZE = len(FLEET_GROUP_ROLES)
#: Squads per run: 1-5 groups -> 5, 10, 15, 20 or 25 agents at the same time.
FLEET_MAX_GROUPS = 5


class Settings(BaseSettings):
    """Process-wide settings."""

    model_config = SettingsConfigDict(
        env_prefix="AV2_",
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- api ---------------------------------------------------------------
    app_name: str = "Anonymous V2"
    version: str = "2.1.0"
    host: str = "127.0.0.1"
    port: int = 8420
    log_level: str = "info"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://127.0.0.1:8420", "http://localhost:8420"])
    # Sandboxed documents (``<iframe sandbox>`` without allow-same-origin, plus
    # ``data:``/``blob:``/``file:`` pages) send ``Origin: null``, which no entry in
    # ``cors_origins`` can ever match.  Allowing it is opt-in so the default stays
    # closed: set AV2_CORS_ALLOW_NULL_ORIGIN=true when an external chat panel has
    # to POST /api/chat from a null origin.  ``allow_credentials`` is already true,
    # so the wildcard ``*`` is not an option here - it must be the literal "null".
    cors_allow_null_origin: bool = False

    # ---- model provider ----------------------------------------------------
    # ``venice`` (the default) talks to https://api.venice.ai/api/v1 using the
    # OpenAI chat-completions protocol.  ``ollama`` keeps the native
    # ``/api/chat`` protocol and expects a local listener.  The label is
    # informational and is surfaced by /api/health so operators can see which
    # backend is in use.
    llm_provider: str = "venice"
    ollama_base_url: str = Field(
        default="https://api.venice.ai/api/v1",
        validation_alias=AliasChoices("AV2_OLLAMA_BASE_URL", "AV2_LLM_BASE_URL"),
    )
    #: Venice inference key; set AV2_LLM_API_KEY (or AV2_OLLAMA_API_KEY) in the
    #: environment or the repository .env.  No key is baked into the source.
    ollama_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("AV2_OLLAMA_API_KEY", "AV2_LLM_API_KEY"),
    )
    # Both tiers use the same cloud model: the planner needs native tool
    # calling and the worker summarises bulky tool output.
    planner_model: str = CLOUD_MODEL
    worker_model: str = CLOUD_MODEL
    #: Ceiling on the prompt AV2 is willing to send - not the server's window.
    #: Venice serves 1 048 576 tokens to ``glm-5-3-flash-uncensored`` and 1 000 000 to
    #: ``deepseek-v4-1-flash-1017-uncensored``, and its OpenAI protocol has no field
    #: in which to ask for one, so this number only bounds what
    #: :mod:`app.core.context_window` lets through (this value minus
    #: ``llm_prompt_reserve_tokens``).  131072 is 27x the largest lane prompt the
    #: swarm builds (~4.7 k): wide enough for long tool transcripts, small enough
    #: that a runaway transcript is never posted to a metered endpoint by accident.
    #: On the native Ollama protocol the same number travels as ``options.num_ctx``
    #: and the daemon honours it per request - it reloads the runner whenever the
    #: value changes, so keep both tiers equal.  A local 24 GB CPU box cannot go
    #: past 16384 (see ``docs/CONTEXT-WINDOW.md``).
    planner_num_ctx: int = 131072
    worker_num_ctx: int = 131072
    planner_think: bool = True
    temperature: float = 0.2
    llm_timeout_s: float = 900.0
    #: OpenAI-compatible gateways only.  ``reasoning_effort`` is the OpenAI
    #: spelling of the native ``think`` flag.  Venice accepts it on both cloud
    #: models and lists the allowed values per model: GLM-5.3 Flash offers
    #: ``low | high | max``, DeepSeek-V4.1 Flash adds ``none`` - so ``none`` is not
    #: portable and ``low`` is the cheapest value both accept.  On Ollama's ``/v1``
    #: shim the flag is the only spelling that works (``think`` is ignored and
    #: ``none`` is required): measured against GLM-4.7-Flash IQ2_M on a CPU box,
    #: leaving it on turned a 0.6 s answer into a reply that spent every token of
    #: the budget inside ``message.reasoning`` and returned an empty ``content``.
    #: Empty means "send nothing", so each provider keeps its own default.
    llm_reasoning_effort: str = ""
    #: Ceiling on generated tokens per call: ``options.num_predict`` on the native
    #: protocol, ``max_tokens`` on the OpenAI one.  0 leaves it to the model.  Decode
    #: is the slow half of a turn on a CPU box (5-12 tok/s measured), so a cap is the
    #: only lever that shortens a reply without cutting the prompt.
    llm_max_output_tokens: int = 0
    #: Tokens held back from the context window so the model has room to answer:
    #: a prompt that fills the window produces an immediate ``length`` stop.
    llm_prompt_reserve_tokens: int = 512
    #: Trim a transcript down to the window before sending it
    #: (``app/core/context_window.py``).  On by default: a request that overflows is
    #: refused by the server before a single token is evaluated, which killed every
    #: lane of a swarm on a 4096-token daemon.
    llm_context_fit: bool = True

    # ---- agent loop --------------------------------------------------------
    max_tool_iterations: int = 12
    max_history_messages: int = 40
    exec_max_output_bytes: int = 60_000
    worker_output_chars: int = 6_000

    # ---- swarm -------------------------------------------------------------
    # One task is worked by a fleet: ``swarm_agents_per_category`` lanes per
    # category (6 by default), all running at the same time, each with its own
    # terminal workdir, browser cookie profile and model conversation.  A single
    # dedup ledger guarantees that no command, tool call or HTTP request is
    # executed twice, and one shared blackboard carries results between lanes
    # and into the strategist that re-plans after every barrier.
    swarm_agents_per_category: int = 6
    swarm_categories: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["recon", "web", "exploit", "coder", "reporter"]
    )
    #: Default round count for a classic /api/swarm run.  Up to 20 rounds can be
    #: requested explicitly; ``until_complete`` uses ``fleet_max_rounds`` as the
    #: ceiling and keeps re-planning until the strategist reports completion.
    swarm_max_rounds: int = 3
    swarm_steps_per_lane: int = 3
    swarm_calls_per_step: int = 2
    swarm_max_concurrency: int = 30
    swarm_budget_tokens: int = 400_000
    swarm_round_timeout_s: int = 900

    # ---- fleet: the default execution mode for every task ------------------
    # Every operator task (not idle conversation) is fanned out to a fixed
    # roster of agents: ``fleet_agents_per_model`` lanes on each model in
    # ``fleet_models``.  The default is three GLM-5.3 lanes plus three
    # DeepSeek-V4.1 lanes, all running at the same time, each in its own
    # terminal, workdir and browser profile, with one dedup ledger so no
    # command or request is ever executed twice.
    fleet_enabled: bool = True
    #: Auto-fan-out chat turns.  Set AV2_FLEET_AUTO=false to go back to the
    #: single captain loop (the swarm endpoint stays available either way).
    fleet_auto: bool = True
    fleet_models: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [CLOUD_MODEL, DEEPSEEK_MODEL]
    )
    fleet_agents_per_model: int = Field(default=3, ge=1, le=6)
    #: Squads of five (recon, web, exploit, coder, reporter) launched per run.
    #: 1-5 groups means 5 / 10 / 15 / 20 / 25 agents working the objective at the
    #: same time, every agent with its own terminal, workdir and browser profile.
    fleet_groups: int = Field(default=1, ge=1, le=FLEET_MAX_GROUPS)
    #: Round 1 starts immediately with deterministic role shards; round 2 is
    #: re-planned by the strategist from the shared board.
    fleet_rounds: int = Field(default=2, ge=1, le=50)
    #: Ceiling used when the operator ticks "until complete": the strategist
    #: keeps assigning rounds until it answers the objective (or this cap hits).
    fleet_max_rounds: int = Field(default=50, ge=1, le=50)
    #: Round ceiling for "until done" runs that must not stop on a round count:
    #: the completion gate, the token window and the wall clock are the only
    #: things that can end them.  Nothing is allocated up front - the token
    #: window is still scaled from the lane count.
    fleet_unlimited_rounds: int = Field(default=10_000, ge=50, le=1_000_000)
    fleet_steps_per_agent: int = Field(default=4, ge=1, le=12)
    fleet_calls_per_step: int = Field(default=2, ge=1, le=4)
    #: True makes the strategist plan round 1 too (slower start, one extra
    #: model call before any lane moves).
    fleet_strategise_first_round: bool = False
    #: Hard wall-clock ceiling for one chat turn's fleet (the run is stopped and
    #: synthesised from whatever reached the board when it expires).
    fleet_deadline_s: int = 300
    #: Ceiling for a "continue until the mission is complete" turn.  Long tasks
    #: keep the browser stream open, so this is the operator's outer bound.
    fleet_until_complete_deadline_s: int = Field(default=3_600, ge=300)
    #: Token budget for an until-complete turn.  The fleet's normal 400k budget
    #: would end a 20-round mission after a few rounds.
    fleet_until_complete_budget_tokens: int = Field(default=2_000_000, ge=100_000)
    #: One persistent shell inside av2-kali per agent, keyed by agent id.  The
    #: one-shot ``docker exec`` path stays as the automatic fallback.
    agent_terminals: bool = True

    # ---- autonomy ----------------------------------------------------------
    # Full practical autonomy inside the configured sandbox: the agents plan,
    # write, run and debug without asking for confirmation on routine work.
    # This flag is documentation-level only - no approval gate exists.
    autonomy: str = "full"
    require_confirmation: bool = False

    # ---- godmode (official Hermes Agent skill profile) ---------------------
    # off | scoped | research.  ``scoped`` keeps legitimate security,
    # blockchain and development work unhedged while retaining hard limits.
    godmode_profile: str = "scoped"
    hermes_home: Path = Field(
        default_factory=lambda: Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    )

    # ---- transaction verification -----------------------------------------
    tx_verification_gate: bool = True
    tx_finality_default: int = 12
    tx_rpc_timeout_s: float = 20.0
    #: JSON object of chain -> RPC URL overrides, e.g.
    #: {"ethereum": "https://my-node.example/rpc"}
    rpc_overrides: dict[str, str] = Field(default_factory=dict)
    #: JSON object of chain -> explorer tx base, e.g. {"ethereum": "https://..."}
    explorer_overrides: dict[str, str] = Field(default_factory=dict)

    # ---- sandbox (kali container) -----------------------------------------
    docker_bin: str = "docker"
    kali_container: str = "av2-kali"
    kali_image: str = "oneness-kali-agent:local"
    kali_workdir: str = "/work"
    exec_timeout_s: int = 180

    # ---- sandbox egress ----------------------------------------------------
    # ``tor`` (default): every exec'd command runs with ALL_PROXY /
    # HTTP(S)_PROXY = socks5h://<tor_socks_host>:<tor_socks_port>, so the target
    # only sees a Tor exit node - never the operator's public IP, ISP or city.
    # Tools that ignore proxy variables go through docker/proxychains4.conf.
    # ``direct``: no proxy (opt out when Tor is wrong for the engagement).
    sandbox_egress: str = "tor"
    tor_image: str = "dperson/torproxy:latest"
    tor_container: str = "av2-tor"
    tor_socks_host: str = "tor"
    tor_socks_port: int = 9050
    egress_probe_url: str = "https://api.ipify.org"
    #: ifconfig.co answers JSON to Tor exits as well (ipinfo/ipapi rate-limit them).
    egress_geo_url: str = "https://ifconfig.co/json"

    # ---- policy ------------------------------------------------------------
    # No scope or approval enforcement is applied anywhere. storage/scope.json is
    # an informational target list that is shown to the model as context — never
    # a whitelist. Authorization is attested by the operator before the engagement,
    # so agents work every target and never re-question ownership.

    # ---- operational guardrails -------------------------------------------
    # ``lab`` (default) keeps free agency: commands are annotated for the audit
    # trail and nothing is refused.  ``production`` makes the engagement contract
    # in storage/engagement.json mechanical - target allow-list, authorized action
    # tags, rate limit, command budget, expiry, kill switch and dry-run.
    environment: str = "lab"

    # ---- operator file exchange -------------------------------------------
    # Uploads are streamed to ``storage/uploads/<batch>`` on the host, then
    # copied into the Kali workdir with ``docker cp``.  Downloads are pulled
    # back and served as a single file or a zip of the tree, so the operator can
    # hand files of any format to the fleet and get the results back.
    upload_dir_name: str = "uploads"
    upload_max_bytes: int = Field(default=200_000_000, ge=1_048_576)
    workspace_list_limit: int = Field(default=400, ge=10, le=2_000)

    # ---- enforcement (a task is only done when it is proven done) ----------
    # A lane may not quietly stop with an empty report: until it has recorded at
    # least ``swarm_min_findings_per_task`` findings (or the round barrier /
    # termination ends it) the runner pushes it back to work and to
    # re-strategise.  The strategist may not stop the mission while the round
    # cap is open and nothing was proven.
    swarm_enforce_findings: bool = True
    swarm_min_findings_per_task: int = Field(default=1, ge=0, le=10)
    swarm_enforce_rounds: bool = True
    #: How many enforcement pushes a lane gets before it is *terminated* for
    #: stopping with an empty report.  Termination is the penalty for breaking
    #: the completion rule; the run keeps going with the remaining lanes.
    swarm_max_enforcement: int = Field(default=3, ge=1, le=10)
    #: The strategist's ``stop`` is only honoured when the completion gate
    #: (a model audit of the recorded findings against the objective) agrees.
    swarm_verify_completion: bool = True
    #: Token allowance per lane per round.  A run's budget is
    #: ``lanes x rounds x this`` (never below the base budgets) so a 20-round
    #: fleet is not cut off at round 5 by a fixed window.
    swarm_budget_per_lane_round: int = Field(default=60_000, ge=5_000)
    #: Absolute ceiling for one run, even after scaling: the last safety valve.
    swarm_budget_max_tokens: int = Field(default=12_000_000, ge=100_000)
    #: After every lane task the agent must report what it learned (technique,
    #: tool, error, strategy) into the SQLite knowledge base.
    swarm_knowledge_report: bool = True

    # ---- shared knowledge base + agent skills ------------------------------
    # Every agent reads a digest of this database before a task and writes to it
    # after the task, so techniques, tools, recurring errors and strategies
    # carry across runs and across models.
    memory_db_name: str = "agent_memory.sqlite3"
    memory_digest_limit: int = Field(default=12, ge=1, le=60)
    skills_dir_name: str = "skills"

    # ---- paths -------------------------------------------------------------
    storage_dir: Path = ROOT / "storage"
    frontend_dir: Path = ROOT / "frontend"

    @field_validator("storage_dir", "frontend_dir", "hermes_home", mode="before")
    @classmethod
    def _expand(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(value).expanduser()
        return value

    @field_validator("godmode_profile", mode="before")
    @classmethod
    def _profile(cls, value: object) -> object:
        text = str(value or "scoped").strip().lower()
        return text if text in {"off", "scoped", "research"} else "scoped"

    @field_validator("sandbox_egress", mode="before")
    @classmethod
    def _egress(cls, value: object) -> object:
        text = str(value or "tor").strip().lower()
        return text if text in {"tor", "direct"} else "tor"

    @field_validator("environment", mode="before")
    @classmethod
    def _environment(cls, value: object) -> object:
        text = str(value or "lab").strip().lower()
        return "production" if text in {"production", "prod", "live"} else "lab"

    @field_validator("swarm_categories", mode="before")
    @classmethod
    def _categories(cls, value: object) -> object:
        allowed = ["recon", "web", "exploit", "coder", "reporter"]
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",") if part.strip()]
        if not isinstance(value, (list, tuple)):
            return allowed
        picked = [str(item).strip().lower() for item in value]
        picked = [item for item in picked if item in allowed]
        return list(dict.fromkeys(picked)) or allowed

    @field_validator("fleet_models", mode="before")
    @classmethod
    def _fleet_models(cls, value: object) -> object:
        """Accept ``AV2_FLEET_MODELS=model-a,model-b`` as well as JSON lists."""
        default = [CLOUD_MODEL, DEEPSEEK_MODEL]
        if isinstance(value, str):
            cleaned = value.replace(";", ",").replace("\n", ",")
            value = [part.strip() for part in cleaned.split(",") if part.strip()]
        if not isinstance(value, (list, tuple)):
            return default
        picked = [str(item).strip() for item in value if str(item).strip()]
        return list(dict.fromkeys(picked)) or default

    @property
    def fleet_size(self) -> int:
        """Total agents in one fleet: squads of five, so 5 / 10 / 15 / 20 / 25."""
        groups = max(1, min(FLEET_MAX_GROUPS, int(self.fleet_groups)))
        return groups * FLEET_GROUP_SIZE

    # ---- derived paths -----------------------------------------------------
    @property
    def openai_style(self) -> bool:
        """Whether the endpoint speaks the OpenAI chat-completions protocol.

        Venice AI and most hosted gateways expose ``POST /chat/completions``
        under a ``/v1`` base URL, while Ollama exposes its own ``/api/chat``.
        """
        provider = self.llm_provider.strip().lower()
        if provider in {"venice", "openai", "openai-compatible"}:
            return True
        if provider in {"ollama", "ollama-cloud", "ollama-local"}:
            return False
        return self.ollama_base_url.rstrip("/").endswith("/v1")

    @property
    def tor_proxy_url(self) -> str:
        """SOCKS5 URL handed to every sandbox command (remote DNS via 5h)."""
        return f"socks5h://{self.tor_socks_host}:{self.tor_socks_port}"
    @property
    def audit_path(self) -> Path:
        return self.storage_dir / "audit.jsonl"

    @property
    def scope_path(self) -> Path:
        return self.storage_dir / "scope.json"

    @property
    def sessions_dir(self) -> Path:
        return self.storage_dir / "sessions"

    @property
    def findings_dir(self) -> Path:
        return self.storage_dir / "findings"

    @property
    def engagement_path(self) -> Path:
        """Machine-checkable authorization written by the operator."""
        return self.storage_dir / "engagement.json"

    @property
    def evidence_dir(self) -> Path:
        """Hash-chained evidence records, one JSONL file per session."""
        return self.storage_dir / "evidence"

    @property
    def swarm_dir(self) -> Path:
        """Per-run ledgers, blackboards and lane workdirs metadata."""
        return self.storage_dir / "swarm"

    @property
    def uploads_dir(self) -> Path:
        """Host staging area for operator uploads (one subdirectory per batch)."""
        return self.storage_dir / self.upload_dir_name

    @property
    def memory_path(self) -> Path:
        """SQLite knowledge base every agent reads and writes."""
        return self.storage_dir / "memory" / self.memory_db_name

    @property
    def skills_dir(self) -> Path:
        """Agent-authored skills, one directory per skill (``skill.json``)."""
        return self.storage_dir / self.skills_dir_name

    def ensure_dirs(self) -> None:
        for path in (
            self.storage_dir,
            self.sessions_dir,
            self.findings_dir,
            self.evidence_dir,
            self.swarm_dir,
            self.uploads_dir,
            self.memory_path.parent,
            self.skills_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor used by the FastAPI dependency layer."""
    settings = Settings()
    settings.ensure_dirs()
    return settings
