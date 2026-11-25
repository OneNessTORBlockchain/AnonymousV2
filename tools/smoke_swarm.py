"""Offline self-test for the swarm engine, guardrails and evidence chain.

Run from the repository root:

    python tools/smoke_swarm.py

It never touches Docker, the model or the network: it exercises the parts that
must be true before a live run - the dedup ledger, the deterministic lane
planner, the production guardrail contract and the hash-chained evidence store.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import Settings  # noqa: E402
from app.core.evidence import EvidenceStore, build_evidence  # noqa: E402
from app.core.production import EngagementContract, GuardrailEngine  # noqa: E402
from app.core.swarm import (  # noqa: E402
    SwarmBudget,
    SwarmLedger,
    SwarmMemory,
    SwarmRun,
    _endpoint_host,
    _objective_shard,
    _plan_from,
    classify_model_failure,
    format_status,
)

FAILS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    label = "ok" if condition else "FAIL"
    print(f"[{label}] {name}" + (f" - {detail}" if detail and not condition else ""))
    if not condition:
        FAILS.append(name)


def test_ledger(tmp: Path) -> None:
    ledger = SwarmLedger(tmp / "ledger.jsonl")
    command = "nmap -sV -p 8081,8082,8083 10.0.0.252"
    key_one = ledger.key_for("run_command", {"command": command, "timeout_s": 60})
    key_two = ledger.key_for(
        "run_command",
        {"command": "  NMAP   -sV -p 8081,8082,8083 10.0.0.252 ", "timeout_s": 900},
    )
    check("same command normalises to one key", key_one == key_two)
    first = ledger.claim(key_one, "recon-1", "run_command")
    second = ledger.claim(key_two, "recon-4", "run_command")
    check("first claim wins", first.duplicate is False)
    check(
        "second lane is refused the repeat",
        second.duplicate is True and second.owner == "recon-1",
        f"duplicate={second.duplicate} owner={second.owner}",
    )
    ledger.record(
        key_one,
        "recon-1",
        "run_command",
        True,
        "8081 open http\n8082 open http\n8083 open http",
    )
    replay = ledger.claim(key_two, "recon-5", "run_command")
    check("duplicate receives the recorded result", replay.duplicate and "8081 open" in replay.content)
    browser_one = ledger.key_for("browser", {"method": "GET", "url": "http://10.0.0.252:8081/"})
    browser_two = ledger.key_for(
        "browser",
        {"method": "get", "url": "http://10.0.0.252:8081/", "headers": "X-Test: 1"},
    )
    check("same HTTP request normalises to one key", browser_one == browser_two)
    ledger.claim(browser_one, "web-1", "browser")
    status = ledger.status()
    check(
        "ledger counters",
        status["unique_claims"] == 2 and status["duplicates_suppressed"] >= 2,
        str(status),
    )
    check("ledger journal written", (tmp / "ledger.jsonl").exists())


def test_planner(tmp: Path) -> None:
    run = SwarmRun(
        run_id="smoke",
        objective="map the three ATM web services and prove the riskiest finding",
        session_id="s1",
        categories=("recon", "web", "exploit", "coder", "reporter"),
        lanes_per_category=5,
        rounds=2,
        model="test-model",
        root=tmp / "run",
        kali_workdir="/work",
        budget_tokens=100_000,
        budget_seconds=600,
    )
    check("five lanes per category", len(run.lanes) == 25, str(len(run.lanes)))
    check("every lane has its own terminal", len({lane.workdir for lane in run.lanes}) == 25)
    check(
        "every lane has its own browser profile",
        len({lane.browser_profile for lane in run.lanes}) == 25,
    )
    plan = _plan_from({}, run, 1)
    missions = [task.mission for task in plan.tasks.values()]
    check("planner assigns every lane without a model", len(plan.tasks) == 25, str(len(plan.tasks)))
    check("no duplicated missions", len(set(missions)) == 25, str(len(set(missions))))
    check(
        "fallback missions quote the objective",
        all("map the three ATM web services" in mission for mission in missions),
        missions[0][:160],
    )
    run.directives.append("stop scanning, check the login form")
    steered = _objective_shard(run, "Fingerprint every reachable service.")
    run.directives.pop()
    check(
        "fallback missions carry the newest directive",
        "check the login form" in steered and "Fingerprint every reachable service." in steered,
        steered[:200],
    )
    check(
        "host is parsed out of the provider URL",
        _endpoint_host("https://api.venice.ai/api/v1") == "api.venice.ai"
        and _endpoint_host("http://[::1]:11434/api") == "::1",
    )
    dns_kind, dns_hint = classify_model_failure(
        "chat failed: [Errno 11001] getaddrinfo failed", "https://api.venice.ai/api/v1"
    )
    check("a host lookup failure is diagnosed as DNS", dns_kind == "dns", dns_kind)
    check("the DNS hint names the unresolvable host", "api.venice.ai" in dns_hint, dns_hint)
    http_kind, http_hint = classify_model_failure(
        "HTTP 401: invalid api key", "https://api.venice.ai/api/v1"
    )
    check(
        "an API error is not blamed on DNS",
        http_kind == "http" and "API key" in http_hint,
        f"{http_kind}: {http_hint}",
    )
    check(
        "a refused connection is its own failure",
        classify_model_failure("ConnectError: All connection attempts failed", "")[0] == "connect",
    )
    memory = SwarmMemory(tmp / "board.jsonl")
    memory.post("plan", round_no=1, strategy="split the HTTP surface")
    memory.post(
        "result",
        lane="recon-1",
        round_no=1,
        ok=True,
        summary="8081..8083 open, vendor pages",
        tool_calls=2,
        duplicates=1,
    )
    check("board digest carries lane results", "8081..8083 open" in memory.digest())
    budget = SwarmBudget(10_000, 60)
    budget.charge(9_900)
    check("budget not exhausted yet", budget.exhausted() is False and budget.remaining == 100)
    budget.charge(200)
    check("budget stops the fleet", budget.exhausted() is True)
    rendered = format_status(run.status_dict())
    check("status rendering", "SWARM smoke" in rendered and "lanes=25" in rendered, rendered[:200])


def test_guardrails(tmp: Path) -> None:
    settings = Settings(storage_dir=tmp, environment="production")
    engine = GuardrailEngine(settings)
    refused = engine.evaluate("nmap -sV -p 8081 10.0.0.252")
    check(
        "no contract = refuse",
        refused.allowed is False and "no engagement contract" in " ".join(refused.reasons),
        str(refused.reasons),
    )
    contract = EngagementContract.from_mapping(
        {
            "engagement": "atm-lab",
            "owner": "Kathy",
            "authorization_ref": "AV2-LAB-2026-11",
            "allowed_targets": ["10.0.0.252:8081-8083"],
            "allowed_actions": ["recon", "vulnerability-scan"],
            "rate_limit_per_minute": 100,
            "max_commands": 50,
            "expires_at": "2999-01-01T00:00:00+00:00",
        }
    )
    engine.save(contract)
    allowed = engine.evaluate("curl -sS http://10.0.0.252:8082/login")
    check("in-scope command allowed", allowed.allowed, str(allowed.reasons))
    wrong_port = engine.evaluate("curl -sS http://10.0.0.252:9999/")
    check(
        "out-of-scope port refused",
        wrong_port.allowed is False and "9999" in " ".join(wrong_port.reasons),
        str(wrong_port.reasons),
    )
    destructive = engine.evaluate("sqlmap -u http://10.0.0.252:8081/ --drain")
    check(
        "destructive tag refused",
        destructive.allowed is False and "fund-drain" in destructive.actions,
        f"actions={destructive.actions} reasons={destructive.reasons}",
    )
    contract.kill_switch = True
    engine.save(contract)
    check(
        "kill switch refuses everything",
        engine.evaluate("curl http://10.0.0.252:8081/").allowed is False,
    )
    contract.kill_switch = False
    contract.dry_run = True
    engine.save(contract)
    check(
        "dry run never executes",
        engine.evaluate("curl http://10.0.0.252:8081/").allowed is False,
    )
    check(
        "describe() names the contract",
        "authorization:" in engine.describe().lower(),
        engine.describe(),
    )


def test_evidence(tmp: Path) -> None:
    store = EvidenceStore(tmp / "evidence")
    complete = build_evidence(
        title="Default admin login on the terminal service",
        target="10.0.0.252:8083",
        vulnerability="CWE-798 hardcoded credentials",
        command="curl -sS -d 'user=admin&pass=admin' http://10.0.0.252:8083/login",
        output="HTTP/1.1 302 Found\nLocation: /dashboard\nSet-Cookie: session=...",
        reproduction="1. POST the vendor default pair\n2. follow the redirect to /dashboard",
        impact="Full administrative control of the ATM terminal service",
        verdict="confirmed",
        severity="critical",
        session_id="smoke",
    )
    check("complete record is confirmed", complete.status == "confirmed", str(complete.proof_gaps))
    store.append(complete)
    partial = build_evidence(
        title="Possible SQL injection",
        target="10.0.0.252:8081",
        verdict="confirmed",
        session_id="smoke",
    )
    stored = store.append(partial)
    check("incomplete record downgraded", stored.status == "unconfirmed" and bool(stored.proof_gaps))
    chain = store.verify_chain("smoke")
    check("hash chain intact", chain["ok"] is True and chain["records"] == 2, str(chain))
    check("summary counts", store.summary("smoke")["confirmed"] == 1, str(store.summary("smoke")))


def test_tools(tmp: Path) -> None:
    """The shared toolbox must expose the new tools and seal findings as evidence."""
    import asyncio

    from app.core.audit import AuditLog
    from app.core.blockchain import TransactionVerifier
    from app.core.sandbox import Sandbox
    from app.core.scope_guard import ScopeGuard, ScopeStore
    from app.core.tools import ToolContext, ToolRuntime, manual_for, specs_for

    settings = Settings(storage_dir=tmp / "tools", environment="lab")
    settings.ensure_dirs()
    ctx = ToolContext(
        settings=settings,
        sandbox=Sandbox(settings),
        guard=ScopeGuard(ScopeStore(settings.scope_path)),
        audit=AuditLog(settings.audit_path),
        verifier=TransactionVerifier(settings),
        session_id="tool-smoke",
        agent="exploit",
        workdir="/work/swarm/smoke/exploit-1",
        evidence=EvidenceStore(settings.evidence_dir),
    )
    names = {spec["function"]["name"] for spec in specs_for("exploit")}
    check(
        "browser + swarm tools registered",
        {"browser", "swarm_execute", "swarm_status"} <= names,
        str(sorted(names)),
    )
    check("manual lists the browser tool", "browser(" in manual_for("exploit"))
    outcome = asyncio.run(
        ToolRuntime().execute(
            ctx,
            "add_finding",
            {
                "title": "TLS certificate valid only for localhost",
                "target": "10.0.0.252:8443",
                "severity": "medium",
                "vulnerability": "CWE-297 improper certificate hostname validation",
                "command": "openssl s_client -connect 10.0.0.252:8443 </dev/null",
                "output": "subject=CN=localhost\nX509v3 Subject Alternative Name: DNS:localhost",
                "reproduction": "openssl s_client -connect 10.0.0.252:8443 </dev/null",
                "impact": "LAN mTLS clients cannot verify the host they connect to",
                "verdict": "confirmed",
            },
        )
    )
    check("add_finding succeeds", outcome.ok, outcome.content)
    finding = outcome.data.get("finding") or {}
    check(
        "finding is sealed as confirmed evidence",
        bool(finding.get("evidence_id")) and finding.get("evidence_status") == "confirmed",
        str(finding.get("proof_gaps")),
    )
    check("finding written to the findings journal", ctx.findings_file().exists())


def test_enforcement(tmp: Path) -> None:
    """Completion rule, termination penalty, operator directives and budget scaling."""
    import asyncio

    from app.core.swarm import (
        LaneResult,
        LaneTask,
        SwarmStrategist,
        _lane_may_finish,
        _lane_system_prompt,
        _lane_user_prompt,
    )

    settings, manager, _lane, run, client_cls = _enforcement_fixtures(tmp)

    spent = run()
    spent.budget.used = spent.budget.limit
    allowed, _reason = _lane_may_finish(
        spent, settings, findings=0, text="done", step=1, steps_budget=4
    )
    check("a spent budget is not a completion path", allowed is False)
    allowed, _ = _lane_may_finish(spent, settings, findings=1, text="x", step=1, steps_budget=4)
    check("a recorded finding completes the task", allowed is True)
    allowed, _ = _lane_may_finish(spent, settings, findings=0, text="x", step=4, steps_budget=4)
    check("the round barrier completes the task", allowed is True)
    spent.stop_requested = True
    allowed, _ = _lane_may_finish(spent, settings, findings=0, text="x", step=1, steps_budget=4)
    check("the operator stop completes the task", allowed is True)

    scaled = manager(client_cls(plan_stop=False, audit_complete=False))._budget_for(False, 20, 6)
    check(
        "the token window scales with lanes x rounds",
        scaled >= 6 * 20 * settings.swarm_budget_per_lane_round
        and scaled <= settings.swarm_budget_max_tokens,
        str(scaled),
    )
    long_run = run("extend", rounds=10, budget_tokens=12_000)
    before = long_run.budget.limit
    long_run.budget.used = long_run.budget.limit
    manager(client_cls(plan_stop=False, audit_complete=False))._keep_budget_alive(long_run)
    check(
        "the window is raised instead of ending the run",
        long_run.budget.limit > before and long_run.budget_extensions == 1,
        f"{before}->{long_run.budget.limit}",
    )

    steered = run("steer", session_id="enforce-2")
    steered.status = "running"
    boss = manager(client_cls(plan_stop=False, audit_complete=False))
    boss.runs[steered.run_id] = steered
    delivered = boss.direct("stop scanning, check the login form", session_id="enforce-2")
    check(
        "an operator directive is delivered to the live run",
        bool(delivered and delivered["delivered"]),
    )
    check("the directive reaches the board", "check the login form" in steered.memory.digest(4_000))
    check(
        "the directive leads the lane prompt",
        _lane_user_prompt(steered, steered.lanes[0], LaneTask(mission="scan"), 2)
        .lstrip()
        .startswith("OPERATOR DIRECTIVE"),
    )
    check(
        "the directive is in the lane system prompt",
        "check the login form" in _lane_system_prompt(steered, steered.lanes[0], ""),
    )
    check("another session is not steered", boss.direct("x", session_id="other") is None)

    async def gate() -> None:
        proven = run("gate")
        proven.memory.post("finding", lane="agent-1", text="default creds land on /admin")
        proven.results["agent-1"] = LaneResult(
            lane_id="agent-1", category="recon", findings=1, reason="finding recorded"
        )
        strategist = SwarmStrategist(client_cls(plan_stop=True, audit_complete=True), settings)
        approved, verdict = await strategist.verify_completion(proven)
        check("the completion gate approves proven evidence", approved is True, str(verdict))
        proven.results["agent-1"] = LaneResult(lane_id="agent-1", category="recon", findings=0)
        approved, _ = await strategist.verify_completion(proven)
        check("the finding floor blocks an unevidenced stop", approved is False)
        approved, _ = await strategist.verify_completion(run("empty"))
        check("no finding can never be approved", approved is False)

        # A strategist that keeps claiming "stop" cannot end a run early: every
        # planned round runs and the status stays exhausted, never completed.
        stubborn_client = client_cls(plan_stop=True, audit_complete=False, tool_noise=True)

        stubborn_boss = manager(stubborn_client)
        stubborn = stubborn_boss.start_fleet(
            objective="prove it", session_id="enforce-3", rounds=3
        )
        status = await stubborn_boss.wait(stubborn.run_id, timeout=120)
        check("every requested round runs", stubborn.round == 3, f"round={stubborn.round}")
        check("an unproven stop ends exhausted", status["status"] == "exhausted", status["status"])

        # An agent that only ever says "done" is warned, then terminated.
        doomed_client = client_cls(plan_stop=False, audit_complete=False)
        doomed_boss = manager(doomed_client)
        doomed = doomed_boss.start_fleet(
            objective="prove it", session_id="enforce-4", rounds=5
        )
        status = await doomed_boss.wait(doomed.run_id, timeout=120)
        check(
            "empty stops terminate the lane",
            sorted(status["enforcement"]["terminated"]) == [
                "agent-1",
                "agent-2",
                "agent-3",
                "agent-4",
                "agent-5",
            ],
            str(status["enforcement"]),
        )
        check("termination is not completion", status["status"] == "terminated", status["status"])
        check("enforcement pushes are counted", status["enforcement"]["pushes"] >= 4)

        # A provider hostname that does not resolve must fail the run once, with
        # the reason, instead of burning every round on six dead sockets.
        class _DeadEndpoint:
            """Nothing resolves; ``chat`` must never be reached."""

            def __init__(self) -> None:
                self.chat_calls = 0

            async def health(self):
                return False, "chat failed: [Errno 11001] getaddrinfo failed"

            async def chat(self, *args, **kwargs):
                from app.core.ollama_client import OllamaError

                self.chat_calls += 1
                raise OllamaError("chat failed: [Errno 11001] getaddrinfo failed")

        dead_client = _DeadEndpoint()
        dead_boss = manager(dead_client)
        dead = dead_boss.start_fleet(
            objective="prove it", session_id="enforce-5", rounds=6
        )
        status = await dead_boss.wait(dead.run_id, timeout=120)
        check(
            "an unreachable endpoint fails the run instead of burning the rounds",
            status["status"] == "failed",
            status["status"],
        )
        check(
            "the failed run names DNS and the host",
            "DNS lookup failed" in (status["error"] or "")
            and "host cannot resolve" in (status["error"] or ""),
            status["error"] or "",
        )
        check("no round was dispatched", dead.round == 0, f"round={dead.round}")
        check("no lane reached the model", dead_client.chat_calls == 0, str(dead_client.chat_calls))
        check(
            "the connectivity gate explains the stop",
            status["completion"].get("gate") == "connectivity"
            and status["stopped_reason"] == "model endpoint unreachable",
            str(status["completion"]),
        )

    asyncio.run(gate())


def _enforcement_fixtures(tmp: Path):
    """Stubs + factories for the enforcement tests (no Docker, model or network)."""
    import types

    from app.config import Settings
    from app.core.swarm import LaneSpec, SwarmManager, SwarmRun

    settings = Settings(
        storage_dir=tmp / "enforce",
        swarm_enforce_findings=True,
        swarm_min_findings_per_task=1,
        swarm_max_enforcement=2,
        swarm_verify_completion=True,
        fleet_models=["test-model"],
        fleet_agents_per_model=2,
        swarm_round_timeout_s=30,
        llm_timeout_s=30,
    )

    class _Sandbox:
        async def exec(self, command, timeout=60, cwd="", terminal=""):
            return types.SimpleNamespace(ok=True, stdout="", stderr="", exit_code=0)

    class _Audit:
        def write(self, *args, **kwargs):
            return None

    class _Client:
        """Strategist + completion auditor + a lane that only ever says "done"."""

        def __init__(
            self, *, plan_stop: bool, audit_complete: bool, tool_noise: bool = False
        ) -> None:
            import json

            from app.core.ollama_client import ModelReply

            self._json = json
            self._reply = ModelReply
            self.plan_stop = plan_stop
            self.audit_complete = audit_complete
            self.tool_noise = tool_noise
            self.plans = 0


        async def chat(self, messages, **kwargs):
            system = str((messages[0] or {}).get("content") or "") if messages else ""
            if kwargs.get("format") == "json" and "strategist of a swarm" in system:
                self.plans += 1
                return self._reply(
                    content=self._json.dumps(
                        {
                            "analysis": "a",
                            "strategy": "s",
                            "stop": self.plan_stop,
                            "tasks": [
                                {"lane": f"agent-{i}", "mission": f"slice {i}", "success": "prove it"}
                                for i in (1, 2)
                            ],
                        }
                    )
                )
            if kwargs.get("format") == "json" and "audit the completion claim" in system:
                return self._reply(
                    content=self._json.dumps(
                        {"complete": self.audit_complete, "reason": "r", "missing": ["prove it"]}
                    )
                )
            if kwargs.get("format") == "json":
                return self._reply(content=self._json.dumps({"notes": []}))
            if self.tool_noise:
                # A tool call that the lane is not allowed to use: no enforcement
                # (the turn is not empty) and no finding, so the run keeps going
                # through every round while the strategist claims it is done.
                from app.core.tool_protocol import ToolCall

                return self._reply(
                    content="delegating",
                    tool_calls=[ToolCall(name="delegate", args={"task": "x"}, call_id="noise")],
                )
            return self._reply(content="I am done.")


    def manager(client) -> SwarmManager:
        return SwarmManager(
            settings,
            sandbox=_Sandbox(),
            guard=None,
            audit=_Audit(),
            verifier=None,
            client=client,
            tools=None,
            memory=None,
            skills=None,
        )

    def lane(lane_id: str = "agent-1") -> LaneSpec:
        return LaneSpec(
            lane_id=lane_id,
            category="recon",
            index=1,
            workdir=f"/work/{lane_id}",
            browser_profile=f"/work/{lane_id}/.browser",
            model="test-model",
            label="Agent 1",
        )

    def run(run_id: str = "enforce", **overrides) -> SwarmRun:
        values = dict(
            run_id=run_id,
            objective="prove the login flow",
            session_id="enforce-1",
            categories=["recon"],
            lanes_per_category=1,
            rounds=4,
            model="test-model",
            root=tmp / "enforce" / run_id,
            kali_workdir="/work",
            budget_tokens=400_000,
            budget_seconds=60,
            lanes=[lane()],
            mode="fleet",
            models=["test-model"],
        )
        values.update(overrides)
        return SwarmRun(**values)

    return settings, manager, lane, run, _Client


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        test_ledger(tmp)
        test_planner(tmp)
        test_guardrails(tmp)
        test_evidence(tmp)
        test_tools(tmp)
        test_enforcement(tmp)

    print()
    if FAILS:
        print(f"{len(FAILS)} check(s) failed: {', '.join(FAILS)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
