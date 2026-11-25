"""Core engine: model access, tool protocol, sandbox, target notes and audit.

Nothing here enforces a policy: :class:`~app.core.tools.ToolRuntime` runs whatever
an agent asks for inside the Kali container, and
:class:`~app.core.policy.PolicyEngine` merely records targets and audit events.
"""
from __future__ import annotations

__all__ = [
    "agents",
    "audit",
    "ollama_client",
    "policy",
    "sandbox",
    "scope_guard",
    "tool_protocol",
    "tools",
]
