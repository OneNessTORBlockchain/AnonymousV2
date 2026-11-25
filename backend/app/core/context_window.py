"""Keep every request inside the model's context window.

Ollama starts a runner at the window in ``OLLAMA_CONTEXT_LENGTH`` - 4096 unless the
daemon or the model's Modelfile says otherwise - unless the request itself carries
``options.num_ctx``.  The OpenAI ``/v1`` shim has nowhere to put ``options``, so a
prompt written for 32 k still lands in a 4 k window and the server refuses the whole
request before evaluating a single token::

    HTTP 400: request (4775 tokens) exceeds the available context size (4096 tokens)

That is what killed every lane of a five-agent swarm against the Render brain: one
lane's system prompt alone is ~4.6 k tokens, so all four lanes died on their first
call while the operator was told to check the API key.  Two things are needed, and
this module is the second:

1. ask for a bigger window - ``AV2_PLANNER_NUM_CTX``, which *is* honoured on the
   native ``/api/chat`` path (see ``docs/CONTEXT-WINDOW.md``);
2. never send more than the window we asked for, which is what
   :func:`fit_messages` does: it estimates the prompt, drops the oldest whole
   turns, shrinks the bulkiest text, and only gives up (``FitResult.ok`` false) when
   the system prompt alone does not fit - so the caller can raise an error that
   names the real fix instead of a generic HTTP 400.

The estimate is a heuristic (four ASCII characters per token, one token per
non-ASCII character) rather than a tokeniser dependency: it only has to be
conservative enough to stay under the limit, and being 10 % wrong costs a little
window, not a failed run.
"""
from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: Average characters per token for English prose, JSON and code mixed together.
CHARS_PER_TOKEN = 4.0
#: Per-message framing cost: role markers, separators, template tokens.
MESSAGE_OVERHEAD_TOKENS = 4
#: Never plan a prompt smaller than this, however small the window claims to be.
MIN_PROMPT_BUDGET = 256
#: Appended to a message whose text was cut, so the model knows it is partial.
TRIM_MARKER = "\n...[trimmed to fit a {window}-token context window]"
#: Shortest text worth keeping when a message has to be cut.
MIN_KEPT_CHARS = 256
#: What a server says when the prompt, not the endpoint, was the problem.  Shared
#: with :func:`app.core.swarm.classify_model_failure` so both agree on the cause.
CONTEXT_OVERFLOW_MARKERS = (
    "exceeds the available context size",
    "exceeds the context length",
    "context length exceeded",
    "maximum context length",
    "context window is too small",
    "too many tokens",
    "prompt is too long",
)


def looks_like_context_overflow(text: str) -> bool:
    """Whether an error string means "the prompt did not fit the window"."""
    low = (text or "").lower()
    return any(marker in low for marker in CONTEXT_OVERFLOW_MARKERS)


def estimate_tokens(value: Any) -> int:
    """Heuristic prompt-token count for a string, mapping or list.

    Non-ASCII text (CJK, box-drawing output, ANSI-coloured banners) is charged one
    token per character: those characters cost two to three tokens each in practice
    and over-charging is the safe direction.
    """
    if value is None:
        return 0
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if not text:
        return 0
    wide = sum(1 for char in text if ord(char) > 127)
    narrow = len(text) - wide
    return int(math.ceil(narrow / CHARS_PER_TOKEN + wide))


def message_tokens(message: Mapping[str, Any]) -> int:
    """Estimated cost of one transcript entry, tool calls included."""
    total = MESSAGE_OVERHEAD_TOKENS + estimate_tokens(message.get("content") or "")
    for call in message.get("tool_calls") or []:
        function = call.get("function") if isinstance(call, Mapping) else None
        if isinstance(function, Mapping):
            total += estimate_tokens(function.get("name") or "")
            total += estimate_tokens(function.get("arguments") or "")
        else:
            total += estimate_tokens(call)
    return total


def transcript_tokens(messages: Sequence[Mapping[str, Any]]) -> int:
    """Estimated prompt cost of a whole transcript."""
    return sum(message_tokens(message) for message in messages)


def _units(messages: Sequence[Mapping[str, Any]]) -> list[list[int]]:
    """Group the transcript into droppable units.

    An assistant message that requested tools and the ``role="tool"`` replies that
    answer it are one unit: dropping the call but keeping its result is a 400 from
    every OpenAI-compatible gateway, and Ollama reads it as a stray observation.
    """
    units: list[list[int]] = []
    index = 0
    total = len(messages)
    while index < total:
        role = str(messages[index].get("role") or "user")
        unit = [index]
        index += 1
        if role == "assistant" and messages[unit[0]].get("tool_calls"):
            while index < total and str(messages[index].get("role") or "") == "tool":
                unit.append(index)
                index += 1
        units.append(unit)
    return units


@dataclass(slots=True)
class FitResult:
    """Outcome of :func:`fit_messages`.

    ``messages`` is what the caller must actually send and ``prompt_tokens`` is the
    estimate for it.  ``ok`` is false only when the pinned turns - every system
    prompt plus the newest message - cannot be made to fit, which no amount of
    trimming can fix: the window itself has to grow.
    """

    messages: list[dict[str, Any]] = field(default_factory=list)
    context_tokens: int = 0
    prompt_tokens: int = 0
    dropped_messages: int = 0
    trimmed_chars: int = 0
    ok: bool = True

    @property
    def fitted(self) -> bool:
        """Whether the transcript had to change to fit the window."""
        return bool(self.dropped_messages or self.trimmed_chars)

    def describe(self) -> str:
        """One line for an event, a log or a test failure."""
        parts = [f"{self.prompt_tokens}/{self.context_tokens} prompt tokens"]
        if self.dropped_messages:
            parts.append(f"dropped {self.dropped_messages} older messages")
        if self.trimmed_chars:
            parts.append(f"trimmed {self.trimmed_chars} chars")
        return ", ".join(parts)


def fit_messages(
    messages: Sequence[Mapping[str, Any]],
    *,
    context_tokens: int,
    reserve_tokens: int = 512,
    keep_last: int = 1,
) -> FitResult:
    """Trim a transcript until it fits ``context_tokens`` minus the answer reserve.

    The order is the one that costs the model the least: oldest whole turns go
    first, then the bulkiest remaining text is halved repeatedly.  System prompts are
    never cut - instructions are the one part of a prompt that cannot be guessed
    back - so if they plus the newest message still overflow, the result reports
    ``ok=False`` and the caller raises an error that names the fix.
    """
    window = max(MIN_PROMPT_BUDGET, int(context_tokens or 0))
    budget = max(MIN_PROMPT_BUDGET, window - max(0, int(reserve_tokens)))
    working = [dict(message) for message in messages]
    tokens = [message_tokens(message) for message in working]
    total = sum(tokens)
    result = FitResult(messages=working, context_tokens=window, prompt_tokens=total)
    if total <= budget:
        return result

    pinned = {
        index for index, message in enumerate(working) if str(message.get("role") or "") == "system"
    }
    if keep_last > 0:
        pinned.update(range(max(0, len(working) - keep_last), len(working)))
    alive = [True] * len(working)

    # 1. drop the oldest whole turns, never a pinned one.
    for unit in _units(working):
        if total <= budget:
            break
        if any(index in pinned for index in unit):
            continue
        for index in unit:
            alive[index] = False
            total -= tokens[index]
            result.dropped_messages += 1

    # 2. halve the largest remaining text until it fits.  System prompts are exempt;
    #    the newest message is not, because a 60 kB tool dump is worth less than the
    #    instructions that say what to do with it.  A message that is already close to
    #    the minimum cannot be helped - cutting it buys less than the marker costs, so
    #    it is skipped instead of being squeezed forever.
    marker = TRIM_MARKER.format(window=window)
    stalled: set[int] = set()
    while total > budget:
        candidates = [
            index
            for index in range(len(working))
            if alive[index]
            and index not in stalled
            and str(working[index].get("role") or "") != "system"
            and len(str(working[index].get("content") or "")) > MIN_KEPT_CHARS + len(marker)
        ]
        if not candidates:
            break
        index = max(candidates, key=lambda position: len(str(working[position].get("content") or "")))
        message = working[index]
        content = str(message.get("content") or "")
        keep_chars = max(MIN_KEPT_CHARS, len(content) // 2)
        if keep_chars + len(marker) >= len(content):
            stalled.add(index)
            continue
        message["content"] = content[:keep_chars] + marker
        result.trimmed_chars += len(content) - keep_chars
        refreshed = message_tokens(message)
        total += refreshed - tokens[index]
        tokens[index] = refreshed

    result.messages = [message for index, message in enumerate(working) if alive[index]]
    result.prompt_tokens = total
    result.ok = total <= budget
    return result


def context_error(
    *,
    prompt_tokens: int,
    context_tokens: int,
    reserve_tokens: int,
    model: str = "",
) -> str:
    """Sentence naming the setting to change when the window is too small."""
    return (
        f"the prompt needs {prompt_tokens} tokens (plus a {reserve_tokens}-token answer reserve) "
        f"and the {context_tokens}-token context window of {model or 'the model'} cannot hold it. "
        "Raise the window: AV2_PLANNER_NUM_CTX / AV2_WORKER_NUM_CTX are sent as options.num_ctx on "
        "the native /api/chat path, and the inference host has to allow it too "
        "(OLLAMA_CONTEXT_LENGTH, or PARAMETER num_ctx in the Modelfile). Ollama's /v1 shim drops "
        "options, so an OpenAI-style base URL can only use the window the daemon booted with. "
        "See docs/CONTEXT-WINDOW.md."
    )

    """Group the transcript into droppable units.

    An assistant message that requested tools and the ``role="tool"`` replies that
    answer it are one unit: dropping the call but keeping its result is a 400 from
    every OpenAI-compatible gateway, and Ollama reads it as a stray observation.
    """
    units: list[list[int]] = []
    index = 0
    total = len(messages)
    while index < total:
        role = str(messages[index].get("role") or "user")
        unit = [index]
        index += 1
        if role == "assistant" and messages[unit[0]].get("tool_calls"):
            while index < total and str(messages[index].get("role") or "") == "tool":
                unit.append(index)
                index += 1
        units.append(unit)
    return units
