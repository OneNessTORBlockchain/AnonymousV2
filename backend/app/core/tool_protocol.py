"""Tool-call parsing shared by every model tier.

Small local models frequently ignore the native ``tools`` API and instead emit a
fenced JSON block, a ``@@tool`` marker or pseudo-code.  The orchestrator always
prefers native ``tool_calls``; this module provides the deterministic fallback
so a 0.6B-8B model can still drive real tools without a crash or a silent no-op.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

_FENCE_RE = re.compile(r"```(?:json|tool|tool_call)?\s*(\{.*?\})\s*```", re.DOTALL)
_BARE_RE = re.compile(r"(?<![\w`])(\{\s*\"(?:tool|name)\"\s*:.*?\})(?![\w`])", re.DOTALL)
_MARKER_RE = re.compile(r"@@tool\s+([A-Za-z_][\w]*)\s*(\{.*?\})?", re.DOTALL)

_NAME_KEYS = ("tool", "name", "tool_name", "function")
_ARG_KEYS = ("args", "arguments", "parameters", "input")


@dataclass(slots=True)
class ToolCall:
    """A normalised tool invocation."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""
    source: str = "native"

    def as_message_tool_call(self) -> dict[str, Any]:
        return {
            "id": self.call_id or f"call_{abs(hash((self.name, json.dumps(self.args, sort_keys=True)))):x}",
            "type": "function",
            "function": {"name": self.name, "arguments": self.args},
        }

    def as_event(self) -> dict[str, Any]:
        return {"name": self.name, "args": self.args, "call_id": self.call_id, "source": self.source}


def normalise_call(raw: dict[str, Any], *, source: str = "native") -> ToolCall | None:
    """Convert an Ollama tool-call dict (or a text-parsed dict) into a ToolCall."""
    if not isinstance(raw, dict):
        return None
    payload = raw.get("function") if isinstance(raw.get("function"), dict) else raw
    name = ""
    for key in _NAME_KEYS:
        candidate = payload.get(key)
        if isinstance(candidate, str) and candidate.strip():
            name = candidate.strip()
            break
    if not name:
        return None
    args: dict[str, Any] = {}
    for key in _ARG_KEYS:
        candidate = payload.get(key)
        if isinstance(candidate, dict):
            args = candidate
            break
        if isinstance(candidate, str) and candidate.strip():
            parsed = _loads(candidate)
            if isinstance(parsed, dict):
                args = parsed
                break
    return ToolCall(
        name=name,
        args=args,
        call_id=str(raw.get("id") or payload.get("id") or ""),
        source=source,
    )


def normalise_many(raw_calls: Any, *, source: str = "native") -> list[ToolCall]:
    if not isinstance(raw_calls, list):
        return []
    calls: list[ToolCall] = []
    for raw in raw_calls:
        call = normalise_call(raw, source=source)
        if call is not None:
            calls.append(call)
    return calls


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def extract_text_calls(content: str, allowed: set[str] | None = None) -> tuple[list[ToolCall], str]:
    """Best-effort recovery of tool calls embedded in assistant prose.

    Returns ``(calls, cleaned_content)`` where the cleaned content has the
    consumed call blocks removed so the UI does not show duplicated JSON.
    """
    if not content:
        return [], content

    calls: list[ToolCall] = []
    cleaned = content

    for match in _MARKER_RE.finditer(content):
        name = match.group(1)
        payload = _loads(match.group(2) or "") or {}
        if allowed and name not in allowed:
            continue
        calls.append(ToolCall(name=name, args=payload if isinstance(payload, dict) else {}, source="text"))
        cleaned = cleaned.replace(match.group(0), "")

    for pattern in (_FENCE_RE, _BARE_RE):
        for match in pattern.finditer(cleaned):
            payload = _loads(match.group(1))
            call = normalise_call(payload, source="text") if isinstance(payload, dict) else None
            if call is None:
                continue
            if allowed and call.name not in allowed:
                continue
            calls.append(call)
            cleaned = cleaned.replace(match.group(0), "")

    return calls, cleaned.strip()


def render_tool_manual(registry: Any) -> str:
    """Human/LLM readable tool catalogue used in the system prompt fallback."""
    lines: list[str] = []
    for spec in registry.specs():
        params = ", ".join((spec.get("parameters") or {}).get("properties", {}).keys())
        lines.append(f"- {spec['name']}({params}): {spec.get('description', '').strip()}")
    if not lines:
        return ""
    return (
        "TOOL PROTOCOL\n"
        "Prefer native function calling. If it is unavailable, emit exactly one\n"
        'fenced json block per step such as:\n```json\n{"tool": "<name>", "args": {...}}\n```\n'
        "Never invent tool output. Available tools:\n" + "\n".join(lines)
    )
