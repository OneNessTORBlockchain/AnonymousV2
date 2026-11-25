"""Turn orchestration: one operator message, one bounded tool-calling loop.

The orchestrator is deliberately thin.  It owns session history, streams model
output, hands tool calls to :class:`~app.core.tools.ToolRuntime`, routes a
captain ``delegate`` call into a nested agent loop and stops on ``finish``.
There is no scope check, no approval queue and no content filter: every
decision about *how* to act belongs to the agent, working the operator's attested
targets without re-litigating ownership.  The only enforced rules are workflow
rules - one shared toolbox for every agent, one action per step, delegation depth,
timeouts and output caps.  A pure greeting is answered conversationally with tools
switched off, so "hello" gets a question about the mission instead of a blind scan.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..config import FLEET_GROUP_SIZE, FLEET_MAX_GROUPS, Settings
from ..schemas import ChatRequest, ChatResponse
from .agents import MAX_DELEGATION_DEPTH, EventSink, SessionStore, build_system_prompt
from .audit import AuditLog
from .blockchain import TransactionVerifier, find_transaction_references
from .conversation import CONVERSATIONAL_FALLBACK, CONVERSATIONAL_INSTRUCTIONS, is_smalltalk
from .evidence import EvidenceStore
from .godmode import build_block, prefill_messages
from .memory import KnowledgeBase
from .ollama_client import ModelReply, OllamaClient, OllamaError
from .policy import PolicyEngine
from .production import GuardrailEngine
from .sandbox import Sandbox
from .skills import SkillVault
from .swarm import SwarmManager, swarm_prompt_block
from .tool_protocol import ToolCall
from .tools import DELEGATABLE, ToolContext, ToolOutcome, ToolRuntime, names_for, specs_for
from .workspace import FileExchange

EmitFn = Any  # Callable[[TurnEvent], Awaitable[None] | None]

#: Tool output handed back to the model is capped here before it enters history.
MAX_TOOL_MESSAGE_CHARS = 12_000


@dataclass(slots=True)
class AgentRun:
    """Result of one agent loop (captain or a delegated specialist)."""

    content: str = ""
    iterations: int = 0
    model: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    stopped: bool = False
    verification_retried: bool = False
    verified: list[dict[str, Any]] = field(default_factory=list)


class Orchestrator:
    """Owns the long-lived engine objects and executes one turn at a time."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.audit = AuditLog(settings.audit_path)
        self.policy = PolicyEngine(settings, self.audit)
        self.sandbox = Sandbox(settings)
        #: Operator <-> container file bridge used by /api/files (uploads and
        #: downloads of any format, single files or whole folders).
        self.exchange = FileExchange(settings, self.sandbox)
        self.tools = ToolRuntime()
        self.verifier = TransactionVerifier(settings)
        self.client = OllamaClient(settings)
        self.sessions = SessionStore(settings.sessions_dir, max_messages=settings.max_history_messages)
        #: Hash-chained evidence store; every finding is sealed here.
        self.evidence = EvidenceStore(settings.evidence_dir)
        #: Durable memory shared by every agent (SQLite) plus the skill registry.
        self.memory = KnowledgeBase(settings.memory_path)
        self.skills = SkillVault(settings, self.sandbox)
        #: Mechanical engagement guardrails (lab annotates, production enforces).
        self.production = GuardrailEngine(settings)
        #: Six-lane-per-category swarms, one dedup ledger per run.
        self.swarm = SwarmManager(
            settings,
            sandbox=self.sandbox,
            guard=self.policy.guard,
            audit=self.audit,
            verifier=self.verifier,
            client=self.client,
            tools=self.tools,
            production=self.production,
            evidence=self.evidence,
            memory=self.memory,
            skills=self.skills,
        )
        self._tool_support: dict[str, bool] = {}
        #: session_id -> lowercase tx hash -> verification record from a successful tool call.
        self._verified: dict[str, dict[str, dict[str, Any]]] = {}

    # -- capabilities ----------------------------------------------------
    async def supports_tools(self, model: str) -> bool:
        """Whether ``model`` advertises native tool calling.

        An unknown capability list is treated as "native": the text-protocol
        fallback in :mod:`app.core.tool_protocol` recovers calls either way.
        """
        if model not in self._tool_support:
            caps = await self.client.model_capabilities(model)
            self._tool_support[model] = True if not caps else "tools" in caps
        return self._tool_support[model]

    # -- public entry point ----------------------------------------------
    async def run_turn(self, request: ChatRequest, on_event: EmitFn | None = None) -> ChatResponse:
        """Run a single operator turn and return the collected response."""
        session_id = (request.session_id or "").strip() or uuid.uuid4().hex[:12]
        sink = EventSink(session_id, on_event)
        history = self.sessions.history(session_id)
        user_message = {"role": "user", "content": request.message}
        model = request.model or self.settings.planner_model
        container_state = await self.sandbox.container_state()
        # Greetings, thanks and "who are you" get a conversational answer with the
        # toolbox switched off; anything with a verb, target or path runs normally.
        conversational = is_smalltalk(request.message)
        # Every real task goes to the fleet by default: whole squads of five,
        # all at the same time.  Conversation stays single-agent so a greeting
        # does not cost a squad of model calls and a sandbox sweep.
        use_fleet = (
            self.settings.fleet_enabled
            and self.settings.fleet_auto
            and not conversational
        )
        requested_groups = int(getattr(request, "groups", 0) or self.settings.fleet_groups)
        requested_groups = max(1, min(FLEET_MAX_GROUPS, requested_groups))
        fleet_size = requested_groups * FLEET_GROUP_SIZE

        await sink.emit(
            "start",
            agent=request.agent,
            data={
                "session_id": session_id,
                "mode": request.mode,
                "model": model,
                "container": container_state,
                "history": len(history),
                "conversational": conversational,
                "provider": self.settings.llm_provider,
                "godmode": self.settings.godmode_profile if not conversational else "off",
                "fleet": use_fleet,
                "fleet_size": fleet_size if use_fleet else 1,
                "fleet_groups": requested_groups if use_fleet else 0,
                "models": list(self.settings.fleet_models) if use_fleet else [model],
                "rounds": request.rounds or self.settings.fleet_rounds,
                "until_complete": bool(request.until_complete or getattr(request, "unlimited", False)),
            },
        )

        if use_fleet:
            return await self._run_fleet(
                request=request,
                session_id=session_id,
                sink=sink,
                user_message=user_message,
                mode=request.mode,
                container_state=container_state,
            )

        try:
            run = await self._run_agent(
                agent=request.agent,
                messages=[*history, user_message],
                sink=sink,
                depth=0,
                model=model,
                mode=request.mode,
                container_state=container_state,
                allow_tools=not conversational,
            )
        except OllamaError as exc:
            message = f"model error: {exc}"
            self.audit.write("turn.error", session=session_id, agent=request.agent, error=str(exc))
            await sink.emit("error", agent=request.agent, text=message)
            await sink.emit(
                "done",
                agent=request.agent,
                text=message,
                data={"session_id": session_id, "error": message, "iterations": 0, "model": model},
            )
            return ChatResponse(
                session_id=session_id,
                agent=request.agent,
                content=message,
                events=sink.events,
                iterations=0,
                model=model,
            )

        content = run.content.strip()
        content, verification = self._apply_verification_report(session_id, content, run)
        if verification["unverified"]:
            await sink.emit(
                "notice",
                agent=request.agent,
                text=(
                    "transaction verification incomplete for "
                    + ", ".join(verification["unverified"])
                    + " - the answer is marked unverified"
                ),
            )
        if content:
            self.sessions.append(session_id, [user_message, {"role": "assistant", "content": content}])
        self.audit.write(
            "turn.done",
            session=session_id,
            agent=request.agent,
            iterations=run.iterations,
            model=run.model,
            chars=len(content),
            tool_calls=len(run.tool_calls),
            verified_transactions=len(verification["verified"]),
            unverified_transactions=len(verification["unverified"]),
        )
        await sink.emit(
            "done",
            agent=request.agent,
            text=content,
            data={
                "session_id": session_id,
                "iterations": run.iterations,
                "model": run.model,
                "tool_calls": len(run.tool_calls),
                "verified_transactions": verification["verified"],
            },
        )
        return ChatResponse(
            session_id=session_id,
            agent=request.agent,
            content=content,
            events=sink.events,
            tool_calls=run.tool_calls,
            iterations=run.iterations,
            model=run.model,
            verified_transactions=run.verified,
        )

    # -- fleet (default execution path) ----------------------------------
    async def _run_fleet(
        self,
        *,
        request: ChatRequest,
        session_id: str,
        sink: EventSink,
        user_message: dict[str, Any],
        mode: str,
        container_state: str,
    ) -> ChatResponse:
        """Fan one operator task out to every fleet agent and relay it live.

        The fleet is the same machinery as a swarm (one ledger, one board, a
        strategist between rounds), but the roster is fixed: whole squads of
        five (recon, web, exploit, coder, reporter), each agent with its own
        terminal and browser profile.  The chat stream receives every lane event
        tagged with its agent id, so the operator sees ``Agent 3 · exploit ·
        deepseek-…`` run a command while ``Agent 1 · recon · glm-…`` runs a
        different one.
        """
        unlimited = bool(getattr(request, "unlimited", False))
        until_complete = bool(getattr(request, "until_complete", False)) or unlimited
        requested_rounds = int(getattr(request, "rounds", 0) or 0)
        if unlimited:
            rounds = int(self.settings.fleet_unlimited_rounds)
        else:
            if requested_rounds <= 0:
                requested_rounds = (
                    self.settings.fleet_max_rounds
                    if until_complete
                    else self.settings.fleet_rounds
                )
            rounds = max(1, min(requested_rounds, self.settings.fleet_max_rounds))
        run = self.swarm.start_fleet(
            objective=request.message,
            session_id=session_id,
            rounds=rounds,
            until_complete=until_complete,
            agents_per_model=getattr(request, "agents", None),
            groups=getattr(request, "groups", None),
            unlimited=unlimited,
        )
        squads = len({lane.group for lane in run.lanes if lane.group}) or 1
        await sink.emit(
            "notice",
            agent="fleet",
            text=(
                f"fleet {run.run_id}: {len(run.lanes)} agents in {squads} squad(s) "
                f"across {len(run.models)} models dispatched at the same time "
                + (
                    "- re-planning until done (no round cap)"
                    if unlimited
                    else (
                        f"- re-planning until complete (cap {rounds} rounds)"
                        if until_complete
                        else (
                            f"- every one of the {rounds} round(s) runs unless the completion gate "
                            "proves the objective"
                        )
                    )
                )
            ),
            data={
                "run_id": run.run_id,
                "mode": run.mode,
                "models": list(run.models),
                "container": container_state,
                "rounds": rounds,
                "until_complete": until_complete,
                "unlimited": unlimited,
                "groups": squads,
                "roster": [lane.to_dict() for lane in run.lanes],
            },
        )
        backlog, queue = run.subscribe()
        for event in backlog:
            await self._relay_fleet_event(run, event, sink)

        deadline_seconds = (
            int(self.settings.fleet_until_complete_deadline_s)
            if until_complete
            else max(60, int(self.settings.fleet_deadline_s)) * rounds
        )
        deadline = time.monotonic() + max(60, deadline_seconds)
        detached = False
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # The chat stream's budget is a *stream* budget, not a mission
                    # budget: detach instead of cancelling.  The fleet keeps
                    # working (success or all rounds) and the operator watches it
                    # in the swarm panel; a new message steers it as a directive.
                    await sink.emit(
                        "notice",
                        agent="fleet",
                        text=(
                            "chat stream detached from the run: the fleet keeps working until the "
                            "mission is proven or all rounds are over - follow it in the swarm panel "
                            f"or steer it with a new message (run {run.run_id})"
                        ),
                        data={"run_id": run.run_id, "detached": True},
                    )
                    detached = True
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=min(20.0, remaining))
                except asyncio.TimeoutError:
                    if run.done.is_set():
                        break
                    running = sum(1 for state in run.lane_state.values() if state == "running")
                    await sink.emit(
                        "notice",
                        agent="fleet",
                        text=f"fleet heartbeat: {running} agents running",
                        data={"run_id": run.run_id, **run.ledger.status()},
                    )
                    continue
                await self._relay_fleet_event(run, event, sink)
                if event.get("type") == "done":
                    break
        except asyncio.CancelledError:
            self.swarm.stop(run.run_id, cancel=True)
            raise
        finally:
            self.swarm.unsubscribe(run.run_id, queue)

        content = (run.synthesis or "").strip()
        if not content or detached:
            # A detached stream has no synthesis yet: answer with what is proven
            # now and say plainly that the mission is still running.
            interim = self._fleet_fallback(run)
            content = interim if not content else content + "\n\n---\n\n" + interim

        agent_run = AgentRun(model=self.settings.planner_model)
        content, verification = self._apply_verification_report(session_id, content, agent_run)
        if content:
            self.sessions.append(
                session_id,
                [user_message, {"role": "assistant", "content": content}],
            )
        iterations = sum(result.steps for result in run.results.values())
        tool_calls = sum(result.tool_calls for result in run.results.values())
        duplicates = sum(result.duplicates for result in run.results.values())
        self.audit.write(
            "turn.fleet",
            session=session_id,
            agent=request.agent,
            run_id=run.run_id,
            mode=mode,
            models=list(run.models),
            lanes=len(run.lanes),
            status=run.status,
            iterations=iterations,
            tool_calls=tool_calls,
            duplicates=duplicates,
            chars=len(content),
        )
        await sink.emit(
            "done",
            agent=request.agent,
            text=content,
            data={
                "session_id": session_id,
                "model": self.settings.planner_model,
                "models": list(run.models),
                "run_id": run.run_id,
                "fleet": True,
                "rounds": run.round,
                "rounds_planned": run.rounds,
                "until_complete": run.until_complete,
                "mission_complete": run.mission_complete,
                "status": run.status,
                "findings": int(run.memory.counts().get("finding", 0)),
                "learned": run.learned,
                "enforced": run.enforced,
                "unresolved": sorted(run.unresolved),
                "terminated": sorted(run.terminated),
                "directives": list(run.directives),
                "completion": dict(run.completion),
                "stopped_reason": run.stopped_reason,
                "detached": detached,
                "iterations": iterations,

                "tool_calls": tool_calls,
                "duplicates": duplicates,
                "ledger": run.ledger.status(),
                "verified_transactions": verification["verified"],
            },
        )
        return ChatResponse(
            session_id=session_id,
            agent=request.agent,
            content=content,
            events=sink.events,
            tool_calls=[
                {
                    "agent": result.lane_id,
                    "category": result.category,
                    "calls": result.tool_calls,
                    "duplicates": result.duplicates,
                    "findings": result.findings,
                }
                for result in run.results.values()
            ],
            iterations=iterations,
            model=self.settings.planner_model,
            verified_transactions=[],
        )

    async def _relay_fleet_event(
        self, run: Any, event: dict[str, Any], sink: EventSink
    ) -> None:
        """Map one swarm event onto a chat turn event, tagged with its agent."""
        kind = str(event.get("type") or "notice")
        lane_id = str(event.get("lane") or "")
        lane = run.lane(lane_id) if lane_id else None
        data = dict(event.get("data") or {})
        if lane is not None:
            data.setdefault("label", lane.label or lane.lane_id)
            data.setdefault("agent_label", lane.label or lane.lane_id)
            data.setdefault("category", lane.category)
            data.setdefault("model", lane.model or run.model)
            data.setdefault("terminal", lane.lane_id)
            data.setdefault("workdir", lane.workdir)
        data.setdefault("run_id", run.run_id)
        label = run.lane_label(lane_id) if lane_id else "fleet"
        text = str(event.get("text") or "")

        if kind == "start":
            await sink.emit(
                "notice",
                agent="fleet",
                text=(
                    f"fleet {run.run_id} started: {len(run.lanes)} agents, "
                    f"{', '.join(run.models)}"
                ),
                data={**data, "models": list(run.models), "roster": [lane.to_dict() for lane in run.lanes]},
            )
            return
        if kind == "agent":
            task = str(data.get("task") or text)
            data["task"] = task
            await sink.emit("agent", agent=label, text=task, data=data)
            return
        if kind == "token":
            await sink.emit("token", agent=label, text=text, data=data)
            return
        if kind == "thinking":
            await sink.emit("thinking", agent=label, text=text, data=data)
            return
        if kind == "tool_call":
            await sink.emit(
                "tool_call",
                agent=label,
                text=str(data.get("name") or "tool"),
                data=data,
            )
            return
        if kind == "tool_result" and data.get("name"):
            await sink.emit("tool_result", agent=label, text=text, data=data)
            return
        if kind == "tool_result":
            await sink.emit(
                "notice",
                agent=label,
                text=f"{label} finished: {text[:300]}",
                data=data,
            )
            return
        if kind == "done":
            await sink.emit(
                "notice",
                agent="fleet",
                text=f"fleet finished: {data.get('status') or run.status}",
                data=data,
            )
            return
        await sink.emit(
            "error" if kind == "error" else "notice",
            agent=label,
            text=text,
            data=data,
        )

    @staticmethod
    def _fleet_fallback(run: Any) -> str:
        """Answer even when the synthesis call failed or the deadline hit."""
        ledger = run.ledger.status()
        lines = [
            f"# Fleet {run.run_id}",
            "",
            f"**Objective:** {run.objective}",
            f"**Status:** {run.status} after round {run.round}/{run.rounds}",
            f"**Agents:** {len(run.lanes)} across {', '.join(run.models)}",
            f"**Dedup:** {ledger.get('executed', 0)} executed, "
            f"{ledger.get('duplicates_suppressed', 0)} repeats suppressed",
            "",
            "## Agent reports",
        ]
        if not run.results:
            lines.append("- no agent produced a report before the deadline")
        for result in run.results.values():
            summary = (result.summary or result.error or "no report").strip()
            lines.append(
                f"- **{run.lane_label(result.lane_id)}** — {result.tool_calls} calls, "
                f"{result.duplicates} duplicates, {result.findings} findings: "
                f"{summary[:600]}"
            )
        lines.extend(
            [
                "",
                "Claims without the six evidence elements remain unconfirmed.",
            ]
        )
        return "\n".join(lines)

    # -- agent loop ------------------------------------------------------
    async def _run_agent(
        self,
        *,
        agent: str,
        messages: list[dict[str, Any]],
        sink: EventSink,
        depth: int,
        model: str,
        mode: str,
        container_state: str,
        task: str = "",
        allow_tools: bool = True,
    ) -> AgentRun:
        """Drive one agent until it calls ``finish``, answers, or runs out of steps."""
        run = AgentRun(model=model)
        ctx = ToolContext(
            settings=self.settings,
            sandbox=self.sandbox,
            guard=self.policy.guard,
            audit=self.audit,
            verifier=self.verifier,
            session_id=sink.session_id,
            agent=agent,
            mode=mode,
            agent_label=agent.title(),
            model=model,
            terminal=agent,
            production=self.production,
            evidence=self.evidence,
            swarm=self.swarm,
            memory=self.memory,
            skills=self.skills,
        )
        allowed = names_for(agent) if allow_tools else set()
        native = allow_tools and await self.supports_tools(model)
        tool_specs: list[dict[str, Any]] | None = specs_for(agent) if native else None
        # GODMODE prefill establishes the operating frame before the real turn.
        # It is ephemeral: never written to the session store.
        prefill = prefill_messages(self.settings.godmode_profile) if allow_tools else []
        conversation = [
            {
                "role": "system",
                "content": self._system_prompt(
                    agent,
                    mode,
                    model,
                    container_state,
                    native,
                    task,
                    depth,
                    conversational=not allow_tools,
                ),
            },
            *prefill,
            *messages,
        ]
        await sink.emit(
            "agent",
            agent=agent,
            data={
                "depth": depth,
                "model": model,
                "native_tools": native,
                "conversational": not allow_tools,
                "tools": sorted(allowed),
                "task": task,
            },
        )

        last_text = ""
        for iteration in range(1, self.settings.max_tool_iterations + 1):
            run.iterations = iteration
            reply = await self._stream(
                conversation,
                sink=sink,
                agent=agent,
                model=model,
                tool_specs=tool_specs,
                allowed=allowed,
            )
            if reply.content.strip():
                last_text = reply.content.strip()

            if not allow_tools:
                # Conversation mode: tools were never offered, so ignore anything the
                # model still imagined and make sure the operator gets a real reply.
                run.content = last_text or CONVERSATIONAL_FALLBACK
                run.stopped = True
                return run

            if not reply.tool_calls:
                pending = (
                    self._unverified_refs(sink.session_id, reply.content)
                    if allow_tools and self.settings.tx_verification_gate
                    else []
                )
                if pending and not run.verification_retried:
                    # Never let a success claim stand on an unverified hash: ask the
                    # model once to run verify_transaction before it finalizes.
                    run.verification_retried = True
                    hashes = ", ".join(ref["hash"] for ref in pending[:4])
                    conversation.append(
                        {
                            "role": "user",
                            "content": (
                                "VERIFICATION REQUIRED: your answer references transaction hash(es) "
                                f"{hashes} without a successful verify_transaction result in this session. "
                                "Call verify_transaction now with the hash and the chain, then report only what "
                                "it returns (verified=true plus the explorer URL, or the exact failure status). "
                                "Do not finalize a success claim before that call succeeds."
                            ),
                        }
                    )
                    await sink.emit(
                        "notice",
                        agent=agent,
                        text=f"verification required before finalizing: {hashes}",
                    )
                    continue
                run.content = last_text
                run.stopped = True
                return run

            conversation.append(reply.as_assistant_message())
            for call in reply.tool_calls:
                await sink.emit(
                    "tool_call",
                    agent=agent,
                    text=call.name,
                    data={"name": call.name, "args": call.args, "call_id": call.call_id, "source": call.source},
                )
                outcome = await self._execute(
                    ctx,
                    call,
                    sink=sink,
                    depth=depth,
                    model=model,
                    mode=mode,
                    container_state=container_state,
                )
                run.tool_calls.append(
                    {
                        "agent": agent,
                        "name": call.name,
                        "args": call.args,
                        "ok": outcome.ok,
                        "delegate": outcome.delegate,
                    }
                )
                if call.name == "verify_transaction" and outcome.data.get("verified"):
                    record = outcome.data
                    self._verified.setdefault(sink.session_id, {})[
                        str(record.get("tx_hash", "")).lower()
                    ] = record
                    run.verified.append(record)
                conversation.append(
                    {
                        "role": "tool",
                        "tool_name": call.name,
                        "content": outcome.content[:MAX_TOOL_MESSAGE_CHARS],
                    }
                )
                await sink.emit(
                    "tool_result",
                    agent=agent,
                    text=outcome.content[:4000],
                    data={
                        "name": call.name,
                        "ok": outcome.ok,
                        "stop": outcome.stop,
                        "delegate": outcome.delegate,
                        "args": call.args,
                        "content": outcome.content,
                    },
                )
                if outcome.stop:
                    pending = (
                        self._unverified_refs(sink.session_id, outcome.content)
                        if allow_tools and self.settings.tx_verification_gate
                        else []
                    )
                    if pending and not run.verification_retried:
                        run.verification_retried = True
                        hashes = ", ".join(ref["hash"] for ref in pending[:4])
                        conversation.append(
                            {
                                "role": "user",
                                "content": (
                                    "VERIFICATION REQUIRED: the final answer you just produced references "
                                    f"unverified hash(es) {hashes}. The turn cannot finish yet. Call "
                                    "verify_transaction for each hash with the chain and expected fields, then "
                                    "call finish again with the verified result (or the exact failure status)."
                                ),
                            }
                        )
                        await sink.emit(
                            "notice",
                            agent=agent,
                            text=f"finish blocked until verification: {hashes}",
                        )
                        break
                    run.content = outcome.content.strip() or last_text
                    run.stopped = True
                    return run

        # Step budget exhausted without finish: report the last thing the agent said.
        run.content = last_text or (
            f"Stopped after {self.settings.max_tool_iterations} steps without a final answer."
        )
        return run

    # -- one model call --------------------------------------------------
    async def _stream(
        self,
        conversation: list[dict[str, Any]],
        *,
        sink: EventSink,
        agent: str,
        model: str,
        tool_specs: list[dict[str, Any]] | None,
        allowed: set[str],
    ) -> ModelReply:
        async def on_token(kind: str, text: str) -> None:
            await sink.emit("token" if kind == "content" else "thinking", agent=agent, text=text)

        try:
            return await self.client.chat_stream(
                conversation,
                model=model,
                tools=tool_specs,
                think=self.settings.planner_think,
                num_ctx=self.settings.planner_num_ctx,
                temperature=self.settings.temperature,
                allowed_tools=allowed,
                on_token=on_token,
            )
        except OllamaError as exc:
            if tool_specs is None or "tool" not in str(exc).lower():
                raise
            # The model rejects the native tools API: turn it off and retry once
            # with the plain-text tool manual instead of failing the whole turn.
            self._tool_support[model] = False
            conversation[0]["content"] += (
                "\n\nTOOL PROTOCOL\nThe native tools API is unavailable. Emit exactly one fenced json "
                'block per step: ```json\n{"tool": "<name>", "args": {...}}\n```'
            )
            await sink.emit("error", agent=agent, text=f"native tools unavailable ({exc}); using text protocol")
            return await self.client.chat_stream(
                conversation,
                model=model,
                tools=None,
                think=self.settings.planner_think,
                num_ctx=self.settings.planner_num_ctx,
                temperature=self.settings.temperature,
                allowed_tools=allowed,
                on_token=on_token,
            )

    # -- tool dispatch ---------------------------------------------------
    async def _execute(
        self,
        ctx: ToolContext,
        call: ToolCall,
        *,
        sink: EventSink,
        depth: int,
        model: str,
        mode: str,
        container_state: str,
    ) -> ToolOutcome:
        if call.name != "delegate":
            return await self.tools.execute(ctx, call.name, call.args)

        agent = str(call.args.get("agent") or "").strip().lower()
        task = str(call.args.get("task") or "").strip()
        if depth >= MAX_DELEGATION_DEPTH:
            return ToolOutcome(
                ok=False,
                content=f"delegation depth limit ({MAX_DELEGATION_DEPTH}) reached; do the work yourself or finish.",
            )
        if agent not in DELEGATABLE:
            return ToolOutcome(ok=False, content=f"'agent' must be one of: {', '.join(DELEGATABLE)}")
        if not task:
            return ToolOutcome(ok=False, content="'task' is required")
        if agent not in self._delegatable_for(ctx.agent):
            return ToolOutcome(ok=False, content=f"the {ctx.agent} agent cannot delegate")

        self.audit.write("delegate.start", session=ctx.session_id, agent=agent, task=task[:500])
        sub = await self._run_agent(
            agent=agent,
            messages=[{"role": "user", "content": task}],
            sink=sink,
            depth=depth + 1,
            model=model,
            mode=mode,
            container_state=container_state,
            task=task,
        )
        content = sub.content.strip() or "(the specialist returned no text)"
        self.audit.write(
            "delegate.done",
            session=ctx.session_id,
            agent=agent,
            iterations=sub.iterations,
            chars=len(content),
        )
        return ToolOutcome(
            ok=True,
            content=f"[{agent} result]\n{content}",
            data={"agent": agent, "task": task, "iterations": sub.iterations, "content": content},
        )

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _delegatable_for(agent: str) -> tuple[str, ...]:
        """Every agent may delegate; ``ctx.agent`` is kept for audit symmetry."""
        return DELEGATABLE

    # -- verification gate ----------------------------------------------
    def _unverified_refs(self, session_id: str, text: str) -> list[dict[str, str]]:
        verified = self._verified.get(session_id, {})
        return [
            ref
            for ref in find_transaction_references(text)
            if ref["hash"].lower() not in verified
        ]

    def _apply_verification_report(
        self, session_id: str, content: str, run: AgentRun
    ) -> tuple[str, dict[str, list[str]]]:
        """Append authoritative on-chain evidence (or a hard warning) to the answer."""
        refs = find_transaction_references(content)
        records = self._verified.get(session_id, {})
        verified: list[str] = []
        unverified: list[str] = []
        if not refs:
            return content, {"verified": verified, "unverified": unverified}

        lines = ["", "", "---", "### On-chain verification (independent RPC check)"]
        for ref in refs:
            record = records.get(ref["hash"].lower())
            if record and record.get("verified"):
                verified.append(ref["hash"])
                lines.append(
                    f"- VERIFIED `{ref['hash']}` on {record.get('chain')} "
                    f"(chain id {record.get('chain_id')}), status {record.get('receipt_status')}, "
                    f"{record.get('confirmations')} confirmation(s), block {record.get('block_number')} — "
                    f"{record.get('explorer_url')}"
                )
            else:
                unverified.append(ref["hash"])
                status = (record or {}).get("status") or "not verified in this session"
                lines.append(
                    f"- NOT VERIFIED `{ref['hash']}` — {status}. Any success claim above is unconfirmed."
                )

        if unverified:
            self.audit.write(
                "tx.claim_unverified",
                session=session_id,
                agent=run.model,
                hashes=unverified,
            )
            header = (
                "> **TRANSACTION VERIFICATION INCOMPLETE** — this answer references a transaction hash that "
                "was not verified on-chain in this session. Do not treat it as confirmed.\n\n"
            )
            return header + content + "\n".join(lines), {"verified": verified, "unverified": unverified}

        self.audit.write("tx.claim_verified", session=session_id, hashes=verified)
        return content + "\n".join(lines), {"verified": verified, "unverified": unverified}

    def _system_prompt(
        self,
        agent: str,
        mode: str,
        model: str,
        container_state: str,
        native: bool,
        task: str,
        depth: int,
        conversational: bool = False,
    ) -> str:
        extra = ""
        if conversational:
            extra = CONVERSATIONAL_INSTRUCTIONS
        elif task and depth:
            extra = (
                f"Task delegated to you:\n{task}\n\n"
                "Do the work yourself, then call finish with the result when done."
            )
        if not conversational:
            # Guardrails and swarm scale are mechanical operating facts, so they
            # live in the system prompt rather than in a user turn.
            try:
                lessons = self.memory.digest(
                    limit=self.settings.memory_digest_limit, chars=2_500
                )
            except Exception:  # noqa: BLE001 - memory is advisory, never a blocker
                lessons = ""
            memory_block = (
                "DURABLE MEMORY (shared by every agent and every past run - use memory_search and "
                "memory_write)\n" + lessons
                if lessons
                else "DURABLE MEMORY\n(empty so far - record what you learn with memory_write)"
            )
            extra = "\n\n".join(
                part
                for part in (
                    extra,
                    self.production.describe(),
                    swarm_prompt_block(),
                    memory_block,
                )
                if part
            )
        return build_system_prompt(
            agent=agent,
            mode=mode,
            scope=self.policy.scope(),
            native_tools=native,
            tools_available=not conversational,
            container_state=container_state,
            model=model,
            extra=extra,
            godmode_block="" if conversational else build_block(self.settings.godmode_profile),
        )


__all__ = ["AgentRun", "Orchestrator"]



