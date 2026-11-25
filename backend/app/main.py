"""FastAPI application for Anonymous V2.

Serves the JSON API, the streaming turn endpoint and the static operator UI from
one process.  There is no approval endpoint and no scope-enforcement endpoint:
the only HTTP side effects are target bookkeeping, session storage and audit
reads, all of which are records rather than gates.
"""
from __future__ import annotations

import asyncio
import json
import shlex
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from .config import FLEET_GROUP_SIZE, FLEET_MAX_GROUPS, ROOT, Settings, get_settings
from .core.agents import AGENT_META
from .core.blockchain import ALL_CHAINS
from .core.godmode import PROFILE_LABELS, locate_skill
from .core.learning import KnowledgeBase
from .core.orchestrator import Orchestrator
from .core.production import EngagementContract
from .core.tools import AGENT_TOOLS
from .schemas import (
    AgentSpec,
    ChatRequest,
    ChatResponse,
    EngagementIn,
    HealthOut,
    LearningReviewIn,
    LearningUsageIn,
    MemoryNoteIn,
    MemoryUpdateIn,
    ProviderIn,
    ScopeEntryIn,
    SkillIn,
    SkillRunIn,
    SteerIn,
    SwarmNoteIn,
    SwarmStartIn,
    VerifyTransactionIn,
)

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _orchestrator(request: Request) -> Orchestrator:
    return request.app.state.orchestrator


def _sse(event: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")


#: Keys /api/provider may rewrite in the repository .env.  Everything else in the
#: file - comments, the sandbox settings, other keys - is preserved byte for byte.
ENV_WRITABLE = frozenset(
    {
        "AV2_LLM_PROVIDER",
        "AV2_OLLAMA_BASE_URL",
        "AV2_OLLAMA_API_KEY",
        "AV2_PLANNER_MODEL",
        "AV2_WORKER_MODEL",
        "AV2_FLEET_MODELS",
    }
)


def _env_path() -> Path:
    return ROOT / ".env"


def _mask_key(key: str) -> str:
    """``sk-abcdef…1234``: enough to recognise a key, never enough to use it."""
    text = str(key or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "•" * len(text)
    return f"{text[:4]}…{text[-4:]}"


def _write_env(updates: dict[str, str]) -> Path:
    """Rewrite only the given AV2_* keys, keeping every other line intact."""
    path = _env_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    pending = {key: str(value) for key, value in updates.items() if key in ENV_WRITABLE}
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in pending:
            out.append(f"{key}={pending[key]}")
            seen.add(key)
        else:
            out.append(line)
    missing = [key for key in pending if key not in seen]
    if missing:
        if out and out[-1].strip():
            out.append("")
        out.append("# Written by the operator console (/api/provider).")
        out.extend(f"{key}={pending[key]}" for key in missing)
    path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
    return path


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.ensure_dirs()
    orchestrator = Orchestrator(settings)
    app.state.settings = settings
    app.state.orchestrator = orchestrator
    try:
        ok, detail = await orchestrator.sandbox.ensure_container()
        orchestrator.audit.write("server.start", sandbox=detail, sandbox_ok=ok)
    except Exception as exc:  # noqa: BLE001 - a missing container must not block the API
        orchestrator.audit.write("server.start", sandbox_error=repr(exc))
    yield
    # Swarm runs are background tasks; cancel them so a restart cannot leave
    # lanes executing against the sandbox after the API is gone.
    orchestrator.swarm.shutdown()
    # Close the persistent per-agent shells inside the Kali container.
    await orchestrator.sandbox.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        description="Free-agency multi-agent security workbench with a Kali tool sandbox.",
        lifespan=lifespan,
    )
    cors_origins = list(settings.cors_origins)
    if settings.cors_allow_null_origin:
        # "null" is compared as a literal origin string, never as a wildcard.
        cors_origins.append("null")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health", response_model=HealthOut, tags=["system"])
    async def health(request: Request) -> HealthOut:
        orch = _orchestrator(request)
        (ollama_ok, ollama_detail), (docker_ok, docker_detail), container = await asyncio.gather(
            orch.client.health(),
            orch.sandbox.docker_available(),
            orch.sandbox.container_state(),
        )
        sandbox_ok = docker_ok and container == "running"
        sandbox_detail = f"docker {docker_detail}; container {settings.kali_container}={container}"
        planner_tools = await orch.supports_tools(settings.planner_model)
        skill = locate_skill(settings)
        return HealthOut(
            status="ok" if ollama_ok and sandbox_ok else "degraded",
            version=settings.version,
            provider=settings.llm_provider,
            ollama=ollama_ok,
            ollama_detail=ollama_detail,
            sandbox=sandbox_ok,
            sandbox_detail=sandbox_detail,
            planner_model=settings.planner_model,
            worker_model=settings.worker_model,
            planner_tools=planner_tools,
            context_window=settings.planner_num_ctx,
            context_reserve=settings.llm_prompt_reserve_tokens,
            godmode=settings.godmode_profile,
            godmode_skill=skill.installed,
            godmode_skill_path=skill.path,
            transaction_gate=settings.tx_verification_gate,
            scope_count=orch.policy.scope_count(),
            egress=await orch.sandbox.egress_mode(),
            fleet=bool(settings.fleet_enabled),
            fleet_auto=bool(settings.fleet_auto),
            fleet_models=list(settings.fleet_models),
            fleet_size=settings.fleet_size,
            fleet_groups=settings.fleet_groups,
            fleet_group_size=FLEET_GROUP_SIZE,
            max_groups=FLEET_MAX_GROUPS,
            agent_terminals=orch.sandbox.terminal_state(),
        )

    @app.get("/api/egress", tags=["system"])
    async def egress(request: Request) -> dict[str, Any]:
        """Show the address a target sees next to the operator's own address.

        The sandbox probe runs through the normal tool path, so with the Tor
        sidecar up it returns an exit node - unrelated to this device or its
        location.  ``distinct`` is the comparison an operator cares about.
        """
        orch = _orchestrator(request)
        report = await orch.sandbox.egress_report()
        host_ip = ""
        try:
            host_ip = await asyncio.to_thread(_host_public_ip, settings.egress_probe_url)
        except Exception as exc:  # noqa: BLE001 - the comparison is best effort
            report["host_detail"] = repr(exc)
        report["host_ip"] = host_ip
        report["distinct"] = bool(report.get("ip")) and report["ip"] != host_ip
        report["note"] = (
            "sandbox traffic leaves through Tor; targets never see the operator address"
            if report["mode"] == "tor"
            else "direct egress - targets see the operator public address"
        )
        orch.audit.write(
            "egress.probe",
            mode=report["mode"],
            ip=report.get("ip", ""),
            host_ip=host_ip,
            distinct=report["distinct"],
        )
        return report

    @app.get("/api/agents", response_model=list[AgentSpec], tags=["agents"])
    async def agents() -> list[AgentSpec]:
        return [
            AgentSpec(name=name, title=title, mission=mission, tools=list(AGENT_TOOLS.get(name, ())))
            for name, (title, mission) in AGENT_META.items()
        ]

    @app.get("/api/fleet", tags=["agents"])
    async def fleet(request: Request) -> dict[str, Any]:
        """The default roster: squads of five, each agent with its own terminal."""
        orch = _orchestrator(request)
        return {
            "enabled": bool(settings.fleet_enabled),
            "auto": bool(settings.fleet_auto),
            "models": list(settings.fleet_models),
            "agents_per_model": settings.fleet_agents_per_model,
            "groups": settings.fleet_groups,
            "group_size": FLEET_GROUP_SIZE,
            "max_groups": FLEET_MAX_GROUPS,
            "size": settings.fleet_size,
            "rounds": settings.fleet_rounds,
            "max_rounds": settings.fleet_max_rounds,
            "unlimited_rounds": settings.fleet_unlimited_rounds,
            "until_complete_deadline_s": settings.fleet_until_complete_deadline_s,
            "steps_per_agent": settings.fleet_steps_per_agent,
            "deadline_s": settings.fleet_deadline_s,
            "enforcement": {
                "findings_required": settings.swarm_min_findings_per_task
                if settings.swarm_enforce_findings
                else 0,
                "max_enforcement_pushes": settings.swarm_max_enforcement,
                "verify_completion": bool(settings.swarm_verify_completion),
                "budget_per_lane_round": settings.swarm_budget_per_lane_round,
                "budget_max_tokens": settings.swarm_budget_max_tokens,
            },
            "roster": orch.swarm.fleet_plan(),

            "terminals": orch.sandbox.terminal_state(),
            "latest": orch.swarm.latest(),
        }

    # -- provider: point AV2 at any OpenAI-compatible endpoint ---------------
    @app.get("/api/provider", tags=["system"])
    async def provider_get(request: Request) -> dict[str, Any]:
        """Current endpoint/model configuration; the API key is never returned."""
        orch = _orchestrator(request)
        ok, detail = await orch.client.health()
        return {
            "provider": settings.llm_provider,
            "base_url": settings.ollama_base_url,
            "api_key_set": bool(settings.ollama_api_key),
            "api_key_hint": _mask_key(settings.ollama_api_key),
            "model": settings.planner_model,
            "worker_model": settings.worker_model,
            "fleet_models": list(settings.fleet_models),
            "openai_style": settings.openai_style,
            "reachable": ok,
            "detail": detail,
            "env_path": str(_env_path()),
        }

    @app.post("/api/provider", tags=["system"])
    async def provider_set(payload: ProviderIn, request: Request) -> dict[str, Any]:
        """Apply an endpoint/model/key now and persist it to the repository .env.

        Empty fields keep their current value, so the form can change just the
        model or just the key.  The live client is refreshed immediately - no
        restart - because :class:`OllamaClient` reads the settings on every call;
        only the cached ``openai_style`` flag has to be recomputed, which is what
        ``refresh()`` does.
        """
        orch = _orchestrator(request)
        updates: dict[str, str] = {}
        provider = payload.provider.strip().lower()
        if provider:
            settings.llm_provider = provider
            updates["AV2_LLM_PROVIDER"] = provider
        base_url = payload.base_url.strip().rstrip("/")
        if base_url:
            settings.ollama_base_url = base_url
            updates["AV2_OLLAMA_BASE_URL"] = base_url
        key = payload.api_key.strip()
        if key:
            settings.ollama_api_key = key
            updates["AV2_OLLAMA_API_KEY"] = key
        model = payload.model.strip()
        if model:
            settings.planner_model = model
            settings.worker_model = model
            updates["AV2_PLANNER_MODEL"] = model
            updates["AV2_WORKER_MODEL"] = model
        raw = payload.fleet_models
        if isinstance(raw, str):
            raw = [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]
        picked = [str(item).strip() for item in (raw or []) if str(item).strip()]
        if picked:
            settings.fleet_models = list(dict.fromkeys(picked))
        elif model:
            # A new endpoint usually serves a different model id; keep the fleet
            # and the planner on the same one unless the operator listed more.
            settings.fleet_models = [model]
        if updates or picked or model:
            updates["AV2_FLEET_MODELS"] = ",".join(settings.fleet_models)
            _write_env(updates)
        orch.client.refresh()
        ok, detail = await orch.client.health()
        orch.audit.write(
            "provider.update",
            fields=sorted(updates),
            provider=settings.llm_provider,
            base_url=settings.ollama_base_url,
            model=settings.planner_model,
            fleet_models=list(settings.fleet_models),
            reachable=bool(ok),
        )
        return {
            "applied": True,
            "provider": settings.llm_provider,
            "base_url": settings.ollama_base_url,
            "api_key_set": bool(settings.ollama_api_key),
            "api_key_hint": _mask_key(settings.ollama_api_key),
            "model": settings.planner_model,
            "fleet_models": list(settings.fleet_models),
            "openai_style": settings.openai_style,
            "reachable": ok,
            "detail": detail,
            "env_path": str(_env_path()),
        }

    @app.get("/browser.png", include_in_schema=False)
    async def brand_mark() -> FileResponse:
        """The repo-root brand mark, used as favicon and sidebar logo."""
        icon = ROOT / "browser.png"
        if not icon.is_file():
            raise HTTPException(status_code=404, detail="browser.png is missing")
        return FileResponse(icon, media_type="image/png")


    # -- engagement guardrails -------------------------------------------
    @app.get("/api/engagement", tags=["guardrails"])
    async def get_engagement(request: Request) -> dict[str, Any]:
        orch = _orchestrator(request)
        return {
            "guardrails": orch.production.status(),
            "contract": orch.production.contract.to_dict() if orch.production.contract else None,
            "prompt": orch.production.describe(),
        }

    @app.put("/api/engagement", tags=["guardrails"])
    async def put_engagement(payload: EngagementIn, request: Request) -> dict[str, Any]:
        """Write the authorization the production guardrails check mechanically."""
        orch = _orchestrator(request)
        contract = EngagementContract.from_mapping(payload.model_dump())
        orch.production.save(contract)
        orch.audit.write(
            "engagement.saved",
            owner=contract.owner,
            authorization_ref=contract.authorization_ref,
            targets=len(contract.allowed_targets),
            actions=contract.allowed_actions,
            dry_run=contract.dry_run,
            kill_switch=contract.kill_switch,
        )
        return {"saved": True, "guardrails": orch.production.status()}

    @app.post("/api/engagement/kill-switch", tags=["guardrails"])
    async def engagement_kill_switch(
        request: Request, engaged: bool = Query(default=True)
    ) -> dict[str, Any]:
        orch = _orchestrator(request)
        contract = orch.production.contract
        if contract is None:
            raise HTTPException(
                status_code=409,
                detail="no engagement contract loaded; PUT /api/engagement first",
            )
        contract.kill_switch = bool(engaged)
        orch.production.save(contract)
        if engaged:
            # Engage the brake on the fleet as well as on the next command.
            orch.swarm.stop("", cancel=True)
        orch.audit.write("engagement.kill_switch", engaged=bool(engaged))
        return {"engaged": bool(engaged), "guardrails": orch.production.status()}

    @app.post("/api/engagement/dry-run", tags=["guardrails"])
    async def engagement_dry_run(
        request: Request, enabled: bool = Query(default=True)
    ) -> dict[str, Any]:
        orch = _orchestrator(request)
        contract = orch.production.contract
        if contract is None:
            raise HTTPException(
                status_code=409,
                detail="no engagement contract loaded; PUT /api/engagement first",
            )
        contract.dry_run = bool(enabled)
        orch.production.save(contract)
        orch.audit.write("engagement.dry_run", enabled=bool(enabled))
        return {"dry_run": bool(enabled), "guardrails": orch.production.status()}

    # -- evidence ---------------------------------------------------------
    @app.get("/api/evidence/summary", tags=["evidence"])
    async def evidence_summary(
        request: Request, session_id: str = Query(default="")
    ) -> dict[str, Any]:
        orch = _orchestrator(request)
        return orch.evidence.summary(session_id.strip())

    @app.get("/api/evidence/verify", tags=["evidence"])
    async def evidence_verify(
        request: Request, session_id: str = Query(min_length=1)
    ) -> dict[str, Any]:
        """Recompute the hash chain: proves no evidence line was edited."""
        orch = _orchestrator(request)
        return {"session_id": session_id, **orch.evidence.verify_chain(session_id)}

    @app.get("/api/evidence", tags=["evidence"])
    async def evidence_list(
        request: Request,
        session_id: str | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=2_000),
    ) -> dict[str, Any]:
        orch = _orchestrator(request)
        records = (
            orch.evidence.read(session_id) if session_id else orch.evidence.all()
        )
        return {"count": len(records), "records": records[:limit]}

    @app.get("/api/evidence/{evidence_id}", tags=["evidence"])
    async def evidence_get(evidence_id: str, request: Request) -> dict[str, Any]:
        record = _orchestrator(request).evidence.get(evidence_id)
        if record is None:
            raise HTTPException(status_code=404, detail="evidence record not found")
        return record


    # -- blockchain ------------------------------------------------------
    @app.get("/api/chains", tags=["blockchain"])
    async def chains() -> dict[str, Any]:
        return {
            "count": len(ALL_CHAINS),
            "godmode_profiles": PROFILE_LABELS,
            "chains": [
                {
                    "key": spec.key,
                    "name": spec.name,
                    "kind": spec.kind,
                    "chain_id": spec.chain_id,
                    "native_symbol": spec.native_symbol,
                    "decimals": spec.decimals,
                    "finality": spec.finality,
                    "explorer": spec.explorer,
                    "rpc": list(spec.rpc),
                }
                for spec in ALL_CHAINS.values()
            ],
        }

    @app.post("/api/transactions/verify", tags=["blockchain"])
    async def verify_transaction(payload: VerifyTransactionIn, request: Request) -> dict[str, Any]:
        """Independently verify a hash; this endpoint never signs or sends."""
        orch = _orchestrator(request)
        result = await orch.verifier.verify(**payload.model_dump())
        orch.audit.write(
            "api.verify_transaction",
            tx_hash=payload.tx_hash,
            chain=result.chain,
            status=result.status,
            verified=result.verified,
            confirmations=result.confirmations,
        )
        return result.to_dict()


    # -- targets ---------------------------------------------------------
    @app.get("/api/scope", tags=["scope"])
    async def list_scope(request: Request) -> dict[str, Any]:
        orch = _orchestrator(request)
        entries = orch.policy.scope()
        return {"count": len(entries), "entries": entries}

    @app.post("/api/scope", tags=["scope"])
    async def add_scope(payload: ScopeEntryIn, request: Request) -> dict[str, Any]:
        orch = _orchestrator(request)
        entry = orch.policy.add_scope(payload.value, mode=payload.mode, note=payload.note)
        return entry.to_dict()

    @app.delete("/api/scope", tags=["scope"])
    async def delete_scope(request: Request, value: str | None = Query(default=None)) -> dict[str, Any]:
        orch = _orchestrator(request)
        if value is None:
            orch.policy.clear_scope()
            return {"cleared": True, "count": 0}
        removed = orch.policy.remove_scope(value)
        if not removed:
            raise HTTPException(status_code=404, detail=f"target not noted: {value}")
        return {"removed": value, "count": orch.policy.scope_count()}

    # -- history ---------------------------------------------------------
    @app.get("/api/sessions", tags=["sessions"])
    async def list_sessions(request: Request) -> dict[str, Any]:
        orch = _orchestrator(request)
        root: Path = settings.sessions_dir
        items: list[dict[str, Any]] = []
        if root.is_dir():
            for path in sorted(root.glob("*.json")):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                messages = payload.get("messages") if isinstance(payload, dict) else None
                items.append(
                    {
                        "session_id": payload.get("session_id", path.stem) if isinstance(payload, dict) else path.stem,
                        "updated_at": payload.get("updated_at", "") if isinstance(payload, dict) else "",
                        "messages": len(messages) if isinstance(messages, list) else 0,
                        "preview": _preview(messages),
                    }
                )
        items.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        return {"count": len(items), "sessions": items}

    @app.get("/api/sessions/{session_id}", tags=["sessions"])
    async def get_session(session_id: str, request: Request) -> dict[str, Any]:
        orch = _orchestrator(request)
        history = orch.sessions.history(session_id)
        if not history:
            raise HTTPException(status_code=404, detail="session not found")
        return {"session_id": session_id, "messages": history}

    @app.delete("/api/sessions/{session_id}", tags=["sessions"])
    async def delete_session(session_id: str, request: Request) -> dict[str, Any]:
        orch = _orchestrator(request)
        orch.sessions.reset(session_id)
        return {"deleted": session_id}

    @app.get("/api/findings", tags=["findings"])
    async def findings(request: Request, session_id: str | None = Query(default=None)) -> dict[str, Any]:
        root: Path = settings.findings_dir
        collected: list[dict[str, Any]] = []
        if root.is_dir():
            for path in sorted(root.glob("*.jsonl")):
                if session_id and path.stem != session_id:
                    continue
                try:
                    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    continue
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict):
                        record.setdefault("session_id", path.stem)
                        collected.append(record)
        collected.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        return {"count": len(collected), "findings": collected}

    @app.get("/api/audit", tags=["system"])
    async def audit(request: Request, limit: int = Query(default=100, ge=1, le=2000)) -> dict[str, Any]:
        orch = _orchestrator(request)
        events = orch.audit.tail(limit)
        return {"count": len(events), "events": events}


    # -- workspace: upload files into the VM, download the results --------
    @app.post("/api/files/upload", tags=["workspace"])
    async def files_upload(
        request: Request,
        name: str = Query(min_length=1, max_length=300),
        batch: str = Query(default="", max_length=64),
        rel: str = Query(default="", max_length=700),
    ) -> dict[str, Any]:
        """Stream one file of any format into the host staging area.

        The request body is the raw file (``application/octet-stream``): there
        is no multipart dependency, so the browser can send a ``File`` directly
        and large binaries are never buffered whole in memory.
        """
        orch = _orchestrator(request)
        length = request.headers.get("content-length")
        if length:
            try:
                if int(length) > orch.settings.upload_max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"upload exceeds the {orch.settings.upload_max_bytes} byte limit",
                    )
            except ValueError:
                pass
        batch_id = (batch or "").strip() or orch.exchange.batch_id()
        try:
            staged = await orch.exchange.receive(
                batch=batch_id, name=name, rel=rel, chunks=request.stream()
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=413 if "limit" in str(exc).lower() else 400, detail=str(exc)
            ) from exc
        staged.pop("local", None)
        return {"ok": True, "batch": batch_id, "file": staged}

    @app.post("/api/files/finish", tags=["workspace"])
    async def files_finish(
        request: Request,
        batch: str = Query(min_length=1, max_length=64),
        dest: str = Query(default="", max_length=500),
    ) -> dict[str, Any]:
        """Publish a staged batch into the Kali container with one ``docker cp``."""
        orch = _orchestrator(request)
        try:
            result = await orch.exchange.push_batch(batch, dest)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        orch.audit.write(
            "files.upload",
            batch=batch,
            dest=result.get("dest", ""),
            files=result.get("files", 0),
            bytes=result.get("bytes", 0),
        )
        return result

    @app.get("/api/files", tags=["workspace"])
    async def files_list(
        request: Request,
        path: str = Query(default="", max_length=500),
        limit: int = Query(default=0, ge=0, le=2_000),
    ) -> dict[str, Any]:
        """List the sandbox workspace (agent-created files and folders included)."""
        orch = _orchestrator(request)
        try:
            return await orch.exchange.listing(path, limit or None)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/files/download", tags=["workspace"])
    async def files_download(
        request: Request, path: str = Query(min_length=1, max_length=500)
    ) -> FileResponse:
        """Download one VM file, or a zip of a VM folder, to the operator."""
        orch = _orchestrator(request)
        try:
            local, download_name, media_type, workspace = await orch.exchange.download(path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        orch.audit.write("files.download", path=path, name=download_name)
        return FileResponse(
            str(local),
            filename=download_name,
            media_type=media_type,
            background=BackgroundTask(orch.exchange.discard, workspace),
        )

    @app.delete("/api/files", tags=["workspace"])
    async def files_delete(
        request: Request, path: str = Query(min_length=1, max_length=500)
    ) -> dict[str, Any]:
        """Delete one file or one folder (recursively) from the workspace."""
        orch = _orchestrator(request)
        try:
            result = await orch.exchange.delete(path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        orch.audit.write("files.delete", path=path, kind=result.get("kind", ""))
        return result

    @app.post("/api/files/clear", tags=["workspace"])
    async def files_clear(request: Request) -> dict[str, Any]:
        """Delete every file and folder in the workspace (agent terminals reset)."""
        orch = _orchestrator(request)
        try:
            result = await orch.exchange.clear()
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        orch.audit.write(
            "files.clear", root=result.get("root", ""), remaining=result.get("remaining", 0)
        )
        return result

    # -- durable knowledge base -------------------------------------------
    @app.get("/api/memory", tags=["memory"])
    async def memory_list(
        request: Request,
        query: str = Query(default="", max_length=200),
        kind: str = Query(default="", max_length=20),
        tag: str = Query(default="", max_length=60),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        """Review what the fleet learned: techniques, tools, errors, strategies."""
        orch = _orchestrator(request)
        notes = orch.memory.search(query, kind=kind, tag=tag, limit=limit)
        return {"count": len(notes), "notes": notes, "stats": orch.memory.stats()}

    @app.get("/api/memory/stats", tags=["memory"])
    async def memory_stats(request: Request) -> dict[str, Any]:
        orch = _orchestrator(request)
        return orch.memory.stats()

    @app.get("/api/memory/digest", tags=["memory"])
    async def memory_digest(
        request: Request,
        query: str = Query(default="", max_length=200),
        limit: int = Query(default=12, ge=1, le=60),
    ) -> dict[str, Any]:
        """The exact block agents receive in their prompts."""
        orch = _orchestrator(request)
        text = orch.memory.digest(limit=limit, query=query)
        return {"digest": text, "chars": len(text)}

    @app.post("/api/memory", tags=["memory"])
    async def memory_write(payload: MemoryNoteIn, request: Request) -> dict[str, Any]:
        """Operator note (legacy path).  ``/api/learning`` is the full board API."""
        orch = _orchestrator(request)
        try:
            record = orch.memory.write(
                kind=payload.kind,
                title=payload.title,
                body=payload.body,
                tags=payload.tags,
                confidence=payload.confidence,
                category=payload.category,
                roles=payload.roles,
                tools=payload.tools,
                preconditions=payload.preconditions,
                procedure=payload.procedure,
                expected=payload.expected,
                observed=payload.observed,
                common_errors=payload.common_errors,
                troubleshooting=payload.troubleshooting,
                result=payload.result,
                scope=payload.scope,
                source=payload.source or "operator",
                agent=payload.agent or "operator",
                reviewer="operator",
                allow_review=True,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        orch.audit.write("memory.operator_write", note_id=record.get("id"), kind=record.get("kind"))
        return {"ok": True, "note": record}

    @app.get("/api/memory/{note_id}", tags=["memory"])
    async def memory_get(note_id: int, request: Request) -> dict[str, Any]:
        record = _orchestrator(request).memory.get(note_id)
        if record is None:
            raise HTTPException(status_code=404, detail="note not found")
        return record

    @app.put("/api/memory/{note_id}", tags=["memory"])
    async def memory_update(
        note_id: int, payload: MemoryUpdateIn, request: Request
    ) -> dict[str, Any]:
        """Edit a stored lesson - the operator reviews and corrects the fleet."""
        orch = _orchestrator(request)
        changes = payload.model_dump(exclude_unset=True, exclude_none=True)
        try:
            record = orch.memory.update(note_id, reviewer="operator", **changes)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if record is None:
            raise HTTPException(status_code=404, detail="note not found")
        orch.audit.write("memory.operator_update", note_id=note_id, fields=sorted(changes))
        return {"ok": True, "note": record}

    @app.delete("/api/memory/{note_id}", tags=["memory"])
    async def memory_delete(note_id: int, request: Request) -> dict[str, Any]:
        orch = _orchestrator(request)
        if not orch.memory.delete(note_id):
            raise HTTPException(status_code=404, detail="note not found")
        orch.audit.write("memory.operator_delete", note_id=note_id)
        return {"deleted": note_id}

    # -- Learning Board (structured, searchable, reviewable knowledge) ----
    @app.get("/api/learning/categories", tags=["learning"])
    async def learning_categories() -> dict[str, Any]:
        """Categories, roles, statuses and the purpose of each category."""
        return KnowledgeBase.categories()

    @app.get("/api/learning/stats", tags=["learning"])
    async def learning_stats(request: Request) -> dict[str, Any]:
        return _orchestrator(request).memory.stats()

    @app.get("/api/learning/recommend", tags=["learning"])
    async def learning_recommend(
        request: Request,
        task: str = Query(default="", max_length=2_000),
        role: str = Query(default="", max_length=20),
        tools: str = Query(default="", max_length=300),
        category: str = Query(default="", max_length=40),
        limit: int = Query(default=5, ge=1, le=25),
    ) -> dict[str, Any]:
        """Role-aware retrieval: what this specialist should read before starting."""
        memory = _orchestrator(request).memory
        notes = memory.recommend(task, role=role, tools=tools, category=category, limit=limit)
        return {
            "count": len(notes),
            "task": task,
            "role": role,
            "notes": notes,
            "digest": memory.render(notes, chars=2_500),
        }

    @app.get("/api/learning", tags=["learning"])
    async def learning_list(
        request: Request,
        query: str = Query(default="", max_length=400),
        kind: str = Query(default="", max_length=20),
        tag: str = Query(default="", max_length=60),
        category: str = Query(default="", max_length=40),
        role: str = Query(default="", max_length=20),
        tool: str = Query(default="", max_length=60),
        status: str = Query(default="", max_length=20),
        result: str = Query(default="", max_length=20),
        sort: str = Query(default="relevance", max_length=10),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        """Search, filter and rank the board."""
        memory = _orchestrator(request).memory
        notes = memory.search(
            query,
            kind=kind,
            tag=tag,
            category=category,
            role=role,
            tool=tool,
            status=status,
            result=result,
            limit=limit,
            sort=sort,
        )
        return {
            "count": len(notes),
            "notes": notes,
            "stats": memory.stats(),
            "filters": {
                "query": query,
                "kind": kind,
                "category": category,
                "role": role,
                "tool": tool,
                "status": status,
                "result": result,
            },
        }

    @app.post("/api/learning", tags=["learning"])
    async def learning_create(payload: MemoryNoteIn, request: Request) -> dict[str, Any]:
        """Create a lesson.  ``review=true`` validates it as the operator's own."""
        orch = _orchestrator(request)
        status = "validated" if payload.review else ""
        try:
            record = orch.memory.write(
                kind=payload.kind,
                title=payload.title,
                body=payload.body,
                tags=payload.tags,
                confidence=payload.confidence,
                category=payload.category,
                roles=payload.roles,
                tools=payload.tools,
                preconditions=payload.preconditions,
                procedure=payload.procedure,
                expected=payload.expected,
                observed=payload.observed,
                common_errors=payload.common_errors,
                troubleshooting=payload.troubleshooting,
                status=status,
                result=payload.result,
                scope=payload.scope,
                source=payload.source or "operator",
                agent=payload.agent or "operator",
                reviewer="operator" if payload.review else "",
                allow_review=payload.review,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        orch.audit.write(
            "learning.create",
            note_id=record.get("id"),
            kind=record.get("kind"),
            status=record.get("status"),
            duplicate_of=record.get("duplicate_of", 0),
        )
        return {"ok": True, "note": record}

    @app.get("/api/learning/{note_id}", tags=["learning"])
    async def learning_get(note_id: int, request: Request) -> dict[str, Any]:
        memory = _orchestrator(request).memory
        record = memory.get(note_id)
        if record is None:
            raise HTTPException(status_code=404, detail="lesson not found")
        return {
            "note": record,
            "related": memory.related(note_id, limit=5),
            "versions": memory.versions(note_id, limit=20),
            "usage": memory.usage(note_id, limit=20),
        }

    @app.put("/api/learning/{note_id}", tags=["learning"])
    async def learning_update(
        note_id: int, payload: MemoryUpdateIn, request: Request
    ) -> dict[str, Any]:
        """Operator edit.  The audit row names the operator; history is versioned."""
        orch = _orchestrator(request)
        changes = payload.model_dump(exclude_unset=True, exclude_none=True)
        try:
            record = orch.memory.update(note_id, reviewer="operator", **changes)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if record is None:
            raise HTTPException(status_code=404, detail="lesson not found")
        orch.audit.write("learning.update", note_id=note_id, fields=sorted(changes))
        return {"ok": True, "note": record}

    @app.delete("/api/learning/{note_id}", tags=["learning"])
    async def learning_delete(note_id: int, request: Request) -> dict[str, Any]:
        orch = _orchestrator(request)
        if not orch.memory.delete(note_id):
            raise HTTPException(status_code=404, detail="lesson not found")
        orch.audit.write("learning.delete", note_id=note_id)
        return {"deleted": note_id}

    @app.get("/api/learning/{note_id}/related", tags=["learning"])
    async def learning_related(
        note_id: int, request: Request, limit: int = Query(default=5, ge=1, le=20)
    ) -> dict[str, Any]:
        memory = _orchestrator(request).memory
        if memory.get(note_id) is None:
            raise HTTPException(status_code=404, detail="lesson not found")
        notes = memory.related(note_id, limit=limit)
        return {"count": len(notes), "notes": notes}

    @app.get("/api/learning/{note_id}/versions", tags=["learning"])
    async def learning_versions(
        note_id: int, request: Request, limit: int = Query(default=50, ge=1, le=200)
    ) -> dict[str, Any]:
        memory = _orchestrator(request).memory
        if memory.get(note_id) is None:
            raise HTTPException(status_code=404, detail="lesson not found")
        versions = memory.versions(note_id, limit=limit)
        return {"count": len(versions), "versions": versions}

    @app.post("/api/learning/{note_id}/usage", tags=["learning"])
    async def learning_usage(
        note_id: int, payload: LearningUsageIn, request: Request
    ) -> dict[str, Any]:
        """Record a reference/success/failure signal for a lesson."""
        orch = _orchestrator(request)
        try:
            record = orch.memory.record_usage(
                note_id,
                action=payload.action,
                agent=payload.agent,
                run_id=payload.run_id,
                detail=payload.detail,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if record is None:
            raise HTTPException(status_code=404, detail="lesson not found")
        orch.audit.write(
            "learning.usage", note_id=note_id, action=payload.action, agent=payload.agent
        )
        return {"ok": True, "note": record}

    @app.post("/api/learning/{note_id}/review", tags=["learning"])
    async def learning_review(
        note_id: int, payload: LearningReviewIn, request: Request
    ) -> dict[str, Any]:
        """Validate, observe or deprecate a lesson.  ``reviewer`` is required."""
        orch = _orchestrator(request)
        try:
            record = orch.memory.review(
                note_id,
                status=payload.status,
                reviewer=payload.reviewer,
                note=payload.note,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if record is None:
            raise HTTPException(status_code=404, detail="lesson not found")
        orch.audit.write(
            "learning.review",
            note_id=note_id,
            status=payload.status,
            reviewer=payload.reviewer,
        )
        return {"ok": True, "note": record}


    # -- agent-authored skills -------------------------------------------
    @app.get("/api/skills", tags=["skills"])
    async def skills_list(
        request: Request, limit: int = Query(default=200, ge=1, le=500)
    ) -> dict[str, Any]:
        orch = _orchestrator(request)
        items = orch.skills.list(limit)
        return {
            "count": len(items),
            "skills": items,
            "root": str(orch.skills.root),
            "container_root": orch.skills.container_root,
        }

    @app.post("/api/skills", tags=["skills"])
    async def skills_create(payload: SkillIn, request: Request) -> dict[str, Any]:
        orch = _orchestrator(request)
        try:
            record = orch.skills.create(
                name=payload.name,
                description=payload.description,
                code=payload.code,
                language=payload.language,
                entry=payload.entry,
                author="operator",
                tags=payload.tags,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        installed = await orch.skills.install(str(record["slug"]))
        orch.audit.write(
            "skill.operator_create",
            slug=record.get("slug"),
            language=record.get("language"),
            installed=bool(installed.get("ok")),
        )
        public = {key: value for key, value in record.items() if key != "code"}
        return {"ok": bool(installed.get("ok")), "skill": public, "install": installed}

    @app.get("/api/skills/{name}", tags=["skills"])
    async def skills_get(name: str, request: Request) -> dict[str, Any]:
        record = _orchestrator(request).skills.get(name)
        if record is None:
            raise HTTPException(status_code=404, detail="no such skill")
        return record

    @app.delete("/api/skills/{name}", tags=["skills"])
    async def skills_delete(name: str, request: Request) -> dict[str, Any]:
        orch = _orchestrator(request)
        record = orch.skills.resolve(name)
        removed = orch.skills.delete(name)
        if not removed:
            raise HTTPException(status_code=404, detail="no such skill")
        if record is not None:
            await orch.sandbox.exec(
                f"rm -rf -- {shlex.quote(orch.skills.container_dir(str(record['slug'])))}", timeout=60
            )
        orch.audit.write("skill.operator_delete", name=name)
        return {"deleted": name}

    @app.post("/api/skills/{name}/run", tags=["skills"])
    async def skills_run(name: str, payload: SkillRunIn, request: Request) -> dict[str, Any]:
        orch = _orchestrator(request)
        outcome = await orch.skills.run(name, args=payload.args, timeout=payload.timeout_s)
        orch.audit.write("skill.operator_run", name=name, ok=bool(outcome.get("ok")))
        if not outcome.get("ok") and str(outcome.get("content") or "").startswith("no such skill"):
            raise HTTPException(status_code=404, detail=str(outcome.get("content")))
        return {"name": name, **outcome}

    # -- swarm ------------------------------------------------------------
    @app.post("/api/swarm", tags=["swarm"])
    async def swarm_start(payload: SwarmStartIn, request: Request) -> dict[str, Any]:
        """Start a fleet: squads of five, one objective, one dedup ledger."""
        orch = _orchestrator(request)
        try:
            run = orch.swarm.start(
                objective=payload.objective,
                session_id=payload.session_id or "",
                categories=payload.categories or None,
                lanes_per_category=payload.lanes,
                rounds=payload.rounds,
                model=payload.model,
                fleet=payload.fleet,
                until_complete=payload.until_complete,
                agents_per_model=payload.agents,
                groups=payload.groups,
                unlimited=payload.unlimited,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"started": True, **run.status_dict()}

    @app.get("/api/swarm", tags=["swarm"])
    async def swarm_list(
        request: Request, limit: int = Query(default=20, ge=1, le=200)
    ) -> dict[str, Any]:
        orch = _orchestrator(request)
        runs = orch.swarm.runs_list(limit)
        return {
            "count": len(runs),
            "runs": runs,
            "defaults": {
                "lanes": settings.swarm_agents_per_category,
                "categories": settings.swarm_categories,
                "rounds": settings.swarm_max_rounds,
                "max_rounds": settings.fleet_max_rounds,
                "steps_per_lane": settings.swarm_steps_per_lane,
                "max_concurrency": settings.swarm_max_concurrency,
                "budget_tokens": settings.swarm_budget_tokens,
                "fleet_models": list(settings.fleet_models),
                "fleet_size": settings.fleet_size,
                "fleet_groups": settings.fleet_groups,
                "group_size": FLEET_GROUP_SIZE,
                "max_groups": FLEET_MAX_GROUPS,
                "fleet_rounds": settings.fleet_rounds,
                "unlimited_rounds": settings.fleet_unlimited_rounds,
                "until_complete_deadline_s": settings.fleet_until_complete_deadline_s,
                "enforcement": {
                    "findings_required": settings.swarm_min_findings_per_task
                    if settings.swarm_enforce_findings
                    else 0,
                    "rounds_enforced": bool(settings.swarm_enforce_rounds),
                    "knowledge_report": bool(settings.swarm_knowledge_report),
                },
                "knowledge": orch.memory.stats(),
                "skills": len(orch.skills.list()),
            },
        }

    @app.get("/api/swarm/{run_id}", tags=["swarm"])
    async def swarm_status(run_id: str, request: Request) -> dict[str, Any]:
        status = _orchestrator(request).swarm.status(run_id)
        if status is None:
            raise HTTPException(status_code=404, detail="swarm run not found")
        return status

    @app.get("/api/swarm/{run_id}/memory", tags=["swarm"])
    async def swarm_memory(
        run_id: str, request: Request, limit: int = Query(default=200, ge=1, le=2_000)
    ) -> dict[str, Any]:
        records = _orchestrator(request).swarm.board(run_id, limit)

        return {"count": len(records), "records": records}

    @app.post("/api/swarm/{run_id}/message", tags=["swarm"])
    async def swarm_message(
        run_id: str, payload: SwarmNoteIn, request: Request
    ) -> dict[str, Any]:
        """Inject an operator note into the shared board for the next round."""
        record = _orchestrator(request).swarm.message(run_id, payload.message)
        if record is None:
            raise HTTPException(status_code=404, detail="swarm run not found")
        return {"posted": True, "record": record}

    @app.post("/api/swarm/{run_id}/stop", tags=["swarm"])
    async def swarm_stop(
        run_id: str, request: Request, cancel: bool = Query(default=False)
    ) -> dict[str, Any]:
        status = _orchestrator(request).swarm.stop(run_id, cancel=cancel)
        if status is None:
            raise HTTPException(status_code=404, detail="swarm run not found")
        return {"stopped": True, "cancel": cancel, **status}

    @app.get("/api/swarm/{run_id}/events", tags=["swarm"])
    async def swarm_events(
        run_id: str, request: Request, after: int = Query(default=0, ge=0)
    ) -> StreamingResponse:
        """Live event stream for one run (backlog first, then lane traffic)."""
        orch = _orchestrator(request)
        subscription = orch.swarm.subscribe(run_id)
        if subscription is None:
            raise HTTPException(status_code=404, detail="swarm run not found")
        run, backlog, queue = subscription

        async def frames() -> AsyncIterator[bytes]:
            try:
                for event in backlog:
                    if int(event.get("seq", 0)) > after:
                        yield _sse(event)
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        if run.done.is_set():
                            break
                        yield b": keep-alive\n\n"
                        continue
                    yield _sse(event)
                    if event.get("type") == "done":
                        break
            finally:
                orch.swarm.unsubscribe(run_id, queue)
            yield b"event: end\ndata: {}\n\n"

        return StreamingResponse(frames(), media_type="text/event-stream", headers=SSE_HEADERS)

    # -- chat ------------------------------------------------------------
    @app.post("/api/chat/steer", tags=["chat"])
    async def chat_steer(payload: SteerIn, request: Request) -> dict[str, Any]:
        """Send a new direction to a live fleet; it supersedes the current plan."""
        orch = _orchestrator(request)
        delivered = orch.swarm.direct(
            payload.message, session_id=payload.session_id, run_id=payload.run_id
        )
        if not delivered:
            raise HTTPException(
                status_code=409,
                detail=(
                    "no live fleet to steer - nothing is running, so send the message as a new "
                    "turn instead"
                ),
            )
        return delivered

    @app.post("/api/chat/stop", tags=["chat"])
    async def chat_stop(
        request: Request,
        session_id: str = Query(default=""),
        run_id: str = Query(default=""),
    ) -> dict[str, Any]:
        """Stop the live fleet for a session (or one run) - the operator brake."""
        orch = _orchestrator(request)
        wanted = run_id.strip()
        if wanted:
            status = orch.swarm.stop(wanted, cancel=True)
            stopped = [wanted] if status else []
        else:
            stopped = orch.swarm.stop_session(session_id, cancel=True)
        return {"stopped": stopped, "count": len(stopped)}

    @app.post("/api/chat", response_model=ChatResponse, tags=["chat"])

    async def chat(payload: ChatRequest, request: Request):
        """Run one turn.  ``stream=true`` returns SSE frames, otherwise JSON."""
        orch = _orchestrator(request)
        if not payload.stream:
            return await orch.run_turn(payload)

        async def frames() -> AsyncIterator[bytes]:
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

            async def on_event(event: Any) -> None:
                await queue.put(event.model_dump(mode="json"))

            runner = asyncio.create_task(orch.run_turn(payload, on_event))
            try:
                while True:
                    getter = asyncio.create_task(queue.get())
                    done, _pending = await asyncio.wait({getter, runner}, return_when=asyncio.FIRST_COMPLETED)
                    if getter in done:
                        yield _sse(getter.result())
                        continue
                    getter.cancel()
                    while not queue.empty():
                        yield _sse(queue.get_nowait())
                    break

                if runner.done() and runner.exception() is not None:
                    exc = runner.exception()
                    yield _sse(
                        {
                            "type": "error",
                            "session_id": payload.session_id or "",
                            "seq": 0,
                            "agent": payload.agent,
                            "text": str(exc),
                            "data": {},
                            "ts": "",
                        }
                    )
                yield b"event: end\ndata: {}\n\n"
            finally:
                if not runner.done():
                    runner.cancel()

        return StreamingResponse(frames(), media_type="text/event-stream", headers=SSE_HEADERS)

    if settings.frontend_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(settings.frontend_dir), html=True), name="frontend")

    return app


def _host_public_ip(url: str, timeout: float = 8.0) -> str:
    """Operator-side lookup, used only to prove the sandbox address differs.

    Explicitly bypasses any proxy in the host environment so the value is the
    machine's own egress address, not a leftover proxy.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as response:
        return response.read(120).decode("utf-8", "replace").strip()


def _preview(messages: Any) -> str:
    """First operator message of a session, trimmed for the sidebar."""
    if not isinstance(messages, list):
        return ""
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "user":
            text = str(message.get("content") or "").strip().replace("\n", " ")
            return text[:90]
    return ""


app = create_app()


if __name__ == "__main__":  # pragma: no cover - developer convenience
    import uvicorn

    _settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=_settings.host,
        port=_settings.port,
        log_level=_settings.log_level,
        reload=False,
    )


__all__ = ["app", "create_app"]


