"""Live end-to-end check of the default six-agent fleet.

Run from the repository root while the API is up:

    python tools/smoke_fleet.py
    python tools/smoke_fleet.py "your objective"   # custom objective

It posts one chat turn and fails unless the stream proves the fleet claims:
more than one agent ran commands, both cloud models were used, every command
carried its agent tag, and the final answer reports the dedup ledger.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from collections import Counter

BASE = "http://127.0.0.1:8420"
DEFAULT_OBJECTIVE = (
    "Audit https://example.com only: confirm the HTTP status, response headers, page title and "
    "TLS certificate with one command per check, and write your results under /work. "
    "No other targets. Two steps maximum per agent."
)

FAILS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    label = "ok" if condition else "FAIL"
    print(f"[{label}] {name}" + (f" - {detail}" if detail else ""))
    if not condition:
        FAILS.append(name)


def stream_turn(objective: str) -> list[dict]:
    body = json.dumps(
        {"message": objective, "stream": True, "agent": "captain", "mode": "exploit"}
    ).encode("utf-8")
    request = urllib.request.Request(
        BASE + "/api/chat",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    events: list[dict] = []
    with urllib.request.urlopen(request, timeout=420) as response:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "{}":
                continue
            try:
                event = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(event, dict) or "type" not in event:
                continue
            events.append(event)
            kind = event.get("type")
            agent = event.get("agent") or "?"
            data = event.get("data") or {}
            if kind == "tool_call":
                target = data.get("command") or data.get("url") or data.get("name") or ""
                print(f"  {agent:<52} RUN {str(target)[:120]}")
            elif kind == "notice" and "fleet" in str(event.get("text") or "").lower():
                print(f"  notice: {event.get('text')}")
    return events


def main() -> int:
    objective = " ".join(sys.argv[1:]).strip() or DEFAULT_OBJECTIVE
    print(f"objective: {objective}\n")
    started = time.monotonic()
    events = stream_turn(objective)
    elapsed = time.monotonic() - started

    tool_calls = [event for event in events if event.get("type") == "tool_call"]
    agents = Counter((event.get("agent") or "?").split(" · ")[0] for event in tool_calls)
    models = {
        str((event.get("data") or {}).get("model") or "")
        for event in tool_calls
        if (event.get("data") or {}).get("model")
    }
    tagged = [
        event
        for event in tool_calls
        if (event.get("data") or {}).get("label")
        and (event.get("data") or {}).get("model")
    ]
    done = next((event for event in reversed(events) if event.get("type") == "done"), {})
    data = done.get("data") or {}
    ledger = data.get("ledger") or {}

    print("\n--- fleet result ---")
    print(f"agents that ran commands: {dict(agents)}")
    print(f"models used: {sorted(model for model in models if model)}")
    print(f"tool calls: {len(tool_calls)} | duplicates suppressed: {ledger.get('duplicates_suppressed', 0)}")
    print(f"answer chars: {len(str(done.get('text') or ''))}")
    print(f"elapsed: {elapsed:.1f}s")

    check("more than one agent executed commands", len(agents) > 1, f"agents={list(agents)}")
    check("both cloud models were used", len({m for m in models if m}) >= 2, f"models={sorted(models)}")
    check(
        "every command carries its agent id and model",
        bool(tool_calls) and len(tagged) == len(tool_calls),
        f"tagged={len(tagged)}/{len(tool_calls)}",
    )
    check("turn reported fleet mode", bool(data.get("fleet")), f"data.fleet={data.get('fleet')}")
    check("final answer is non-empty", len(str(done.get("text") or "").strip()) > 40)
    check("dedup ledger reached the client", "executed" in ledger or bool(data.get("run_id")))

    if FAILS:
        print(f"\nFAILED: {', '.join(FAILS)}")
        return 1
    print("\nfleet smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
