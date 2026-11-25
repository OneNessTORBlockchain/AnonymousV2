"""Async LLM client: native Ollama tool calling or OpenAI-compatible cloud.

The default deployment points at **Venice AI** (``https://api.venice.ai/api/v1``,
OpenAI chat-completions protocol) and runs ``glm-5-3-flash-uncensored`` for both
tiers.  Setting ``AV2_LLM_PROVIDER=ollama`` (or any ``ollama*`` label) switches
back to the native ``/api/tags`` + ``/api/chat`` protocol; set
``AV2_OLLAMA_BASE_URL`` to the daemon address (``http://127.0.0.1:11434``) too.

Two tiers are used: a tool-call capable planner (captain) and a cheap worker
used to compress tool output.  When the planner ignores the native ``tools``
API we recover the call from prose with :mod:`app.core.tool_protocol`, so
behaviour degrades instead of breaking.
"""
from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import Settings
from .context_window import FitResult, context_error, fit_messages, looks_like_context_overflow
from .tool_protocol import ToolCall, extract_text_calls, normalise_many


class OllamaError(RuntimeError):
    """Raised when the local model service cannot satisfy a request."""


class OllamaContextError(OllamaError):
    """The prompt did not fit the model's context window.

    Separate from :class:`OllamaError` because the fix is a setting, not a retry,
    and because :func:`app.core.swarm.classify_model_failure` has to tell an
    operator "raise the window" rather than "check the API key".
    """


@dataclass(slots=True)
class ModelReply:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    thinking: str = ""
    model: str = ""
    done_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    native_tools: bool = True

    def as_assistant_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [call.as_message_tool_call() for call in self.tool_calls]
        return message


class OllamaClient:
    """Dependency-light wrapper over Ollama ``/api/chat`` or OpenAI ``/chat/completions``."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        #: Venice/OpenAI-compatible endpoints use ``/models`` + ``/chat/completions``.
        self.openai_style = settings.openai_style

    def refresh(self) -> None:
        """Recompute cache derived from settings after /api/provider changes them.

        Everything else (base URL, key, model ids) is read per request, so this
        single flag is the only thing that must be recomputed when the operator
        points AV2 at a different endpoint while the process is running.
        """
        self.openai_style = self.settings.openai_style

    # -- plumbing ---------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.ollama_api_key:
            headers["Authorization"] = f"Bearer {self.settings.ollama_api_key}"
        return headers

    def _client(self, timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.settings.ollama_base_url.rstrip("/"),
            headers=self._headers(),
            timeout=httpx.Timeout(timeout or self.settings.llm_timeout_s, connect=10.0),
        )
    # -- context window ---------------------------------------------------
    def context_window(self, num_ctx: int | None = None) -> int:
        """Tokens the next request asks the server for.

        On the native protocol this travels as ``options.num_ctx``, which Ollama
        honours per request - and reloads the runner when the value changes, so the
        planner and worker tiers should agree on it or every alternation pays a model
        load.  On the OpenAI protocol there is nowhere to put it: the window is
        whatever the daemon booted with, and this number only bounds what we are
        willing to send (see ``docs/CONTEXT-WINDOW.md``).
        """
        return int(num_ctx or self.settings.planner_num_ctx or 0)

    def fit(self, messages: list[dict[str, Any]], num_ctx: int | None = None) -> FitResult:
        """Trim the transcript to the window, and refuse what still cannot fit.

        Raises :class:`OllamaContextError` when even the pinned part - the system
        prompt plus the newest message - overflows, because a generic HTTP 400 from
        the server is exactly what hid this bug: the operator was told to check the
        API key while the real answer was "the window is 4 k and the prompt is 4.7 k".
        """
        window = self.context_window(num_ctx)
        if not self.settings.llm_context_fit or window <= 0:
            return FitResult(
                messages=[dict(message) for message in messages],
                context_tokens=window,
                prompt_tokens=0,
            )
        result = fit_messages(
            messages,
            context_tokens=window,
            reserve_tokens=self.settings.llm_prompt_reserve_tokens,
        )
        if not result.ok:
            raise OllamaContextError(
                context_error(
                    prompt_tokens=result.prompt_tokens,
                    context_tokens=window,
                    reserve_tokens=self.settings.llm_prompt_reserve_tokens,
                    model=self.settings.planner_model,
                )
            )
        return result

    #: Actionable text for the status codes that look like a bug but are not: 401
    #: is "wrong or revoked key", 402 is "the key is fine and the account is out of
    #: credit", 429 is "slow down or buy more".  Venice's exact 401/402 answers were
    #: reproduced against the live API on 23 Nov 2026.
    _STATUS_HINTS = {
        401: "the API key was rejected - check AV2_LLM_API_KEY in .env "
             "(keys: https://venice.ai/settings/api)",
        402: "the account has no credit left - top up at https://venice.ai/settings/api, "
             "then retry; the key itself is valid",
        429: "rate limit or quota reached - lower AV2_FLEET_AGENTS_PER_MODEL, "
             "AV2_FLEET_ROUNDS or wait",
    }

    def _http_error(self, status: int, body: str) -> OllamaError:
        """Classify a non-200: an overflow is a setting, billing is not a network fault.

        Venice answers a rejected key with 401 and an empty balance with
        ``402 {"error":"Insufficient USD or Diem balance to complete request."}``.
        Both used to reach the operator as a bare ``HTTP 401``/``HTTP 402`` next to
        the swarm's generic "check the API key, the model id and the account quota"
        line; naming the fix - and keeping the server's own sentence - is what makes
        the difference between a five-minute fix and a re-wire of the wrong thing.
        """
        text = (body or "").strip()
        if looks_like_context_overflow(text):
            return OllamaContextError(f"HTTP {status}: {text[:300]}")
        detail = text[:400] or "empty response body"
        hint = self._STATUS_HINTS.get(status)
        if hint:
            return OllamaError(f"HTTP {status}: {hint} [{detail}]")
        return OllamaError(f"HTTP {status}: {detail}")

    @staticmethod
    def _note_window(reply: ModelReply, fit: FitResult) -> ModelReply:
        """Record how the prompt fitted, next to the token counts it explains."""
        if fit.context_tokens:
            reply.usage["context_window"] = fit.context_tokens
        if fit.dropped_messages:
            reply.usage["dropped_messages"] = fit.dropped_messages
        if fit.trimmed_chars:
            reply.usage["trimmed_chars"] = fit.trimmed_chars
        return reply



    # -- discovery --------------------------------------------------------
    async def health(self) -> tuple[bool, str]:
        path = "/models" if self.openai_style else "/api/tags"
        try:
            async with self._client(timeout=20) as client:
                response = await client.get(path)
        except (httpx.HTTPError, OSError) as exc:
            return False, f"{self.settings.ollama_base_url} unreachable: {exc}"
        if response.status_code != 200:
            return False, f"HTTP {response.status_code}: {response.text[:200]}"
        return True, f"{self.settings.ollama_base_url} ok"

    async def list_models(self) -> list[str]:
        path = "/models" if self.openai_style else "/api/tags"
        try:
            async with self._client(timeout=30) as client:
                response = await client.get(path)
                response.raise_for_status()
        except (httpx.HTTPError, OSError) as exc:
            raise OllamaError(f"cannot list models: {exc}") from exc
        payload = response.json()
        if self.openai_style:
            # OpenAI shape: {"data": [{"id": "glm-5-3-flash-uncensored", ...}]}
            return [
                str(m.get("id", ""))
                for m in payload.get("data", [])
                if isinstance(m, dict) and m.get("id")
            ]
        return [m.get("name", "") for m in payload.get("models", []) if isinstance(m, dict)]

    async def model_capabilities(self, model: str) -> list[str]:
        """Ollama >= 0.5 exposes ``/api/show`` capabilities (e.g. ``tools``).

        OpenAI-compatible gateways rarely expose a per-model capability probe, so
        the hosted tiers are advertised as tool-capable (Venice reports
        ``supportsFunctionCalling`` for every GLM model we run).
        """
        if self.openai_style:
            return ["completion", "tools"]
        try:
            async with self._client(timeout=30) as client:
                response = await client.post("/api/show", json={"model": model})
                if response.status_code != 200:
                    return []
                payload = response.json()
        except (httpx.HTTPError, OSError, ValueError):
            return []
        caps = payload.get("capabilities") if isinstance(payload, dict) else None
        return [str(c) for c in caps] if isinstance(caps, list) else []

    async def supports_tools(self, model: str) -> bool:
        return "tools" in await self.model_capabilities(model)

    # -- inference --------------------------------------------------------
    def _openai_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Translate the internal (Ollama-flavoured) transcript to OpenAI format.

        OpenAI requires ``tool_calls[].id`` on the assistant message and a
        matching ``tool_call_id`` on every ``role="tool"`` reply; the internal
        transcript carries ``tool_name`` instead, so ids are minted and handed
        out in call order.  A tool result with no pending call id is demoted to
        a user message instead of provoking a 400 from the gateway.
        """
        out: list[dict[str, Any]] = []
        pending: list[str] = []
        for index, message in enumerate(messages):
            role = str(message.get("role") or "user")
            if role == "assistant":
                item: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
                raw_calls = message.get("tool_calls")
                if isinstance(raw_calls, list) and raw_calls:
                    converted: list[dict[str, Any]] = []
                    pending = []
                    for position, raw in enumerate(raw_calls):
                        fn = raw.get("function") if isinstance(raw, dict) and isinstance(raw.get("function"), dict) else raw
                        if not isinstance(fn, dict):
                            continue
                        args = fn.get("arguments")
                        if not isinstance(args, str):
                            args = json.dumps(args or {}, ensure_ascii=False)
                        call_id = str((raw.get("id") if isinstance(raw, dict) else "") or "")
                        call_id = call_id or f"call_{index}_{position}"
                        pending.append(call_id)
                        converted.append(
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {"name": str(fn.get("name") or ""), "arguments": args},
                            }
                        )
                    if converted:
                        item["tool_calls"] = converted
                out.append(item)
                continue
            if role == "tool":
                content = str(message.get("content") or "")
                call_id = str(message.get("tool_call_id") or (pending.pop(0) if pending else ""))
                if not call_id:
                    out.append({"role": "user", "content": f"tool output:\n{content}"})
                    continue
                out.append({"role": "tool", "tool_call_id": call_id, "content": content})
                continue
            out.append({"role": role, "content": message.get("content") or ""})
        return out

    def _openai_payload(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        tools: list[dict[str, Any]] | None,
        stream: bool,
        temperature: float | None,
        format: str | dict[str, Any] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": self._openai_messages(messages),
            "stream": stream,
            "temperature": self.settings.temperature if temperature is None else temperature,
        }
        # The native protocol's ``think`` flag has no OpenAI spelling; the
        # gateway uses ``reasoning_effort``, and Ollama's /v1 shim is strict about
        # it -- "none" is honoured, ``think`` is ignored.  A thinking model left
        # on spends the whole max_tokens budget in message.reasoning and returns
        # an empty message.content, which reads downstream as an empty reply.
        effort = (self.settings.llm_reasoning_effort or "").strip()
        if effort:
            payload["reasoning_effort"] = effort
        # Venice accepts the same {"type": "function", "function": {...}} shape
        # Ollama uses, so the tool specs are forwarded untouched.
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if format:
            payload["response_format"] = {"type": "json_object"}
        # Decode is the slow half of a turn on a CPU box (5-12 tok/s measured), so a
        # cap on generated tokens is the one lever that shortens a reply without
        # shrinking the prompt.  0 means "no opinion" and the field is left out.
        if self.settings.llm_max_output_tokens > 0:
            payload["max_tokens"] = self.settings.llm_max_output_tokens
        return payload

    def _payload(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        tools: list[dict[str, Any]] | None,
        stream: bool,
        think: bool,
        num_ctx: int | None,
        temperature: float | None,
        format: str | dict[str, Any] | None,
    ) -> dict[str, Any]:
        if self.openai_style:
            return self._openai_payload(
                messages,
                model=model,
                tools=tools,
                stream=stream,
                temperature=temperature,
                format=format,
            )
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
            "options": {
                "temperature": self.settings.temperature if temperature is None else temperature,
                "num_ctx": num_ctx or self.settings.planner_num_ctx,
            },
        }
        if self.settings.llm_max_output_tokens > 0:
            payload["options"]["num_predict"] = self.settings.llm_max_output_tokens
        if tools:
            payload["tools"] = tools
        if think:
            payload["think"] = True
        if format:
            payload["format"] = format
        return payload

    def _finish(
        self,
        messages_data: dict[str, Any],
        *,
        model: str,
        content: str,
        thinking: str,
        raw_calls: Any,
        tools: list[dict[str, Any]] | None,
        allowed_tools: set[str] | None,
        done_reason: str = "stop",
    ) -> ModelReply:
        calls = normalise_many(raw_calls, source="native")
        native = bool(calls)
        if not calls and tools:
            calls, cleaned = extract_text_calls(content, allowed_tools)
            if calls:
                content = cleaned
                native = False
        return ModelReply(
            content=content,
            tool_calls=calls,
            thinking=thinking,
            model=messages_data.get("model", model),
            done_reason=messages_data.get("done_reason", done_reason),
            usage={
                "prompt_tokens": messages_data.get("prompt_eval_count", 0),
                "completion_tokens": messages_data.get("eval_count", 0),
                "total_ms": int((messages_data.get("total_duration") or 0) / 1_000_000),
            },
            native_tools=native,
        )

    def _finish_openai(
        self,
        data: dict[str, Any],
        *,
        model: str,
        content: str,
        thinking: str,
        raw_calls: Any,
        tools: list[dict[str, Any]] | None,
        allowed_tools: set[str] | None,
        finish_reason: str = "stop",
    ) -> ModelReply:
        calls = normalise_many(raw_calls, source="native")
        native = bool(calls)
        if not calls and tools:
            calls, cleaned = extract_text_calls(content, allowed_tools)
            if calls:
                content = cleaned
                native = False
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return ModelReply(
            content=content,
            tool_calls=calls,
            thinking=thinking,
            model=str(data.get("model") or model),
            done_reason=finish_reason,
            usage={
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_ms": 0,
            },
            native_tools=native,
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        think: bool = False,
        num_ctx: int | None = None,
        temperature: float | None = None,
        format: str | dict[str, Any] | None = None,
        allowed_tools: set[str] | None = None,
    ) -> ModelReply:
        """Single blocking completion with tool-call recovery."""
        model_name = model or self.settings.planner_model
        fitted = self.fit(messages, num_ctx)
        payload = self._payload(
            fitted.messages,
            model=model_name,
            tools=tools,
            stream=False,
            think=think,
            num_ctx=num_ctx,
            temperature=temperature,
            format=format,
        )
        path = "/chat/completions" if self.openai_style else "/api/chat"
        try:
            async with self._client() as client:
                response = await client.post(path, json=payload)
                if response.status_code == 400 and "response_format" in payload:
                    # Some gateways reject JSON mode; the prompts already ask for
                    # JSON, so retry once without it instead of failing the turn.
                    payload.pop("response_format", None)
                    response = await client.post(path, json=payload)
        except (httpx.HTTPError, OSError) as exc:
            raise OllamaError(f"chat failed: {exc}") from exc
        if response.status_code != 200:
            raise self._http_error(response.status_code, response.text)

        data = response.json()
        if self.openai_style:
            choices = data.get("choices") if isinstance(data, dict) else None
            choice = choices[0] if isinstance(choices, list) and choices else {}
            choice = choice if isinstance(choice, dict) else {}
            message = choice.get("message") or {}
            return self._note_window(
                self._finish_openai(
                    data,
                    model=model_name,
                    content=message.get("content") or "",
                    thinking=message.get("reasoning_content") or message.get("reasoning") or "",
                    raw_calls=message.get("tool_calls"),
                    tools=tools,
                    allowed_tools=allowed_tools,
                    finish_reason=str(choice.get("finish_reason") or "stop"),
                ),
                fitted,
            )
        message = data.get("message") or {}
        return self._note_window(
            self._finish(
                data,
                model=model_name,
                content=message.get("content") or "",
                thinking=message.get("thinking") or "",
                raw_calls=message.get("tool_calls"),
                tools=tools,
                allowed_tools=allowed_tools,
            ),
            fitted,
        )

    @staticmethod
    def _merge_streamed_call(buffer: dict[int, dict[str, Any]], chunk: Any) -> None:
        """Accumulate OpenAI streaming tool-call fragments into one call dict."""
        if not isinstance(chunk, dict):
            return
        index = chunk.get("index")
        index = index if isinstance(index, int) else len(buffer)
        slot = buffer.setdefault(
            index,
            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
        )
        if chunk.get("id"):
            slot["id"] = str(chunk["id"])
        function = chunk.get("function")
        if not isinstance(function, dict):
            return
        if function.get("name"):
            slot["function"]["name"] += str(function["name"])
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            slot["function"]["arguments"] += arguments
        elif isinstance(arguments, dict):
            slot["function"]["arguments"] += json.dumps(arguments, ensure_ascii=False)

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        think: bool = False,
        num_ctx: int | None = None,
        temperature: float | None = None,
        allowed_tools: set[str] | None = None,
        on_token: Callable[[str, str], Awaitable[None] | None] | None = None,
    ) -> ModelReply:
        """Streaming completion.

        ``on_token(kind, text)`` is awaited for every delta where ``kind`` is
        ``"content"`` or ``"thinking"``.  The returned :class:`ModelReply` holds
        the accumulated text and any recovered tool calls, so the caller can run
        the tool loop with exactly the same code path as :meth:`chat`.
        """
        model_name = model or self.settings.planner_model
        fitted = self.fit(messages, num_ctx)
        payload = self._payload(
            fitted.messages,
            model=model_name,
            tools=tools,
            stream=True,
            think=think,
            num_ctx=num_ctx,
            temperature=temperature,
            format=None,
        )
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        raw_calls: list[Any] = []
        calls_by_index: dict[int, dict[str, Any]] = {}
        finish_reason = "stop"
        last: dict[str, Any] = {}

        async def _emit(kind: str, text: str) -> None:
            if not text or on_token is None:
                return
            result = on_token(kind, text)
            if inspect.isawaitable(result):
                await result

        try:
            path = "/chat/completions" if self.openai_style else "/api/chat"
            async with self._client() as client:
                async with client.stream("POST", path, json=payload) as response:
                    if response.status_code != 200:
                        body = (await response.aread()).decode("utf-8", "replace")
                        raise self._http_error(response.status_code, body)
                    async for raw_line in response.aiter_lines():
                        line = raw_line.strip()
                        if not line or line.startswith(":"):
                            continue
                        if self.openai_style:
                            if line.startswith("data:"):
                                line = line[5:].strip()
                            if line == "[DONE]":
                                break
                        try:
                            chunk = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(chunk, dict):
                            continue
                        last = chunk
                        if chunk.get("error"):
                            raise OllamaError(str(chunk["error"]))
                        if self.openai_style:
                            choices = chunk.get("choices") or []
                            if not choices or not isinstance(choices[0], dict):
                                continue
                            choice = choices[0]
                            delta_block = choice.get("delta") or {}
                            delta = delta_block.get("content") or ""
                            if delta:
                                content_parts.append(delta)
                                await _emit("content", delta)
                            thought = (
                                delta_block.get("reasoning_content")
                                or delta_block.get("reasoning")
                                or ""
                            )
                            if thought:
                                thinking_parts.append(thought)
                                await _emit("thinking", thought)
                            for fragment in delta_block.get("tool_calls") or []:
                                self._merge_streamed_call(calls_by_index, fragment)
                            if choice.get("finish_reason"):
                                finish_reason = str(choice["finish_reason"])
                            continue
                        message = chunk.get("message") or {}
                        delta = message.get("content") or ""
                        if delta:
                            content_parts.append(delta)
                            await _emit("content", delta)
                        thought = message.get("thinking") or ""
                        if thought:
                            thinking_parts.append(thought)
                            await _emit("thinking", thought)
                        if message.get("tool_calls"):
                            raw_calls.extend(message["tool_calls"])
        except (httpx.HTTPError, OSError) as exc:
            raise OllamaError(f"chat stream failed: {exc}") from exc

        if self.openai_style:
            return self._note_window(
                self._finish_openai(
                    last,
                    model=model_name,
                    content="".join(content_parts),
                    thinking="".join(thinking_parts),
                    raw_calls=[calls_by_index[key] for key in sorted(calls_by_index)],
                    tools=tools,
                    allowed_tools=allowed_tools,
                    finish_reason=finish_reason,
                ),
                fitted,
            )
        return self._note_window(
            self._finish(
                last,
                model=model_name,
                content="".join(content_parts),
                thinking="".join(thinking_parts),
                raw_calls=raw_calls,
                tools=tools,
                allowed_tools=allowed_tools,
            ),
            fitted,
        )

    async def summarise(self, text: str, *, limit: int | None = None, instruction: str = "") -> str:
        """Compress bulky tool output with the cheap worker model."""
        budget = limit or self.settings.worker_output_chars
        if len(text) <= budget:
            return text
        prompt = (
            (instruction or "Summarise this command output for an operator. Keep every host, port, "
             "version, credential hint, error string and file path. Drop banner art and repetition.")
            + "\n\n---\n"
            + text[: budget * 8]
        )
        try:
            reply = await self.chat(
                [{"role": "user", "content": prompt}],
                model=self.settings.worker_model,
                temperature=0.1,
                num_ctx=self.settings.worker_num_ctx,
            )
        except OllamaError:
            return text[:budget] + "\n...[truncated]"
        summary = reply.content.strip() or text[:budget]
        return summary[:budget]
