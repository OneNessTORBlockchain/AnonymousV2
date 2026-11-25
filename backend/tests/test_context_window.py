"""Tests for the context-window fitter and the error it names.

The regression these cover is concrete: a five-agent swarm against a 4096-token
Ollama daemon died on its first call with ``HTTP 400: request (4775 tokens) exceeds
the available context size (4096 tokens)`` and the operator was told to check the
API key.  A prompt bigger than the window must either be trimmed to fit or named as
a window problem - never retried, and never blamed on credentials.
"""
from __future__ import annotations

import pytest

from app.config import (
    CLOUD_MODEL,
    DEEPSEEK_MODEL,
    FLEET_GROUP_SIZE,
    FLEET_MAX_GROUPS,
    Settings,
)
from app.core.context_window import (
    context_error,
    estimate_tokens,
    fit_messages,
    looks_like_context_overflow,
    transcript_tokens,
)
from app.core.ollama_client import OllamaClient, OllamaContextError, OllamaError

SERVER_400 = (
    'HTTP 400: {"error":{"message":"request (4775 tokens) exceeds the available context size '
    '(4096 tokens), try increasing it","type":"invalid_request_error"}}'
)


def _turn(index: int, chars: int = 400) -> list[dict[str, str]]:
    """One user/assistant exchange, sized in characters."""
    body = f"step {index} " + ("x" * chars)
    return [{"role": "user", "content": body}, {"role": "assistant", "content": "ok"}]


def test_estimate_tokens_charges_non_ascii_per_character() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 400) == 100
    # One token per wide character: box-drawing tool output is never under-charged.
    assert estimate_tokens("\u2554" * 100) == 100


def test_small_transcript_is_sent_untouched() -> None:
    messages = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]
    result = fit_messages(messages, context_tokens=32768, reserve_tokens=512)
    assert result.ok and not result.fitted
    assert result.messages == messages
    assert result.prompt_tokens == transcript_tokens(messages)


def test_oldest_turns_are_dropped_before_anything_is_trimmed() -> None:
    messages = [{"role": "system", "content": "mission"}]
    for index in range(12):
        messages.extend(_turn(index))
    window = 1000
    reserve = 128
    result = fit_messages(messages, context_tokens=window, reserve_tokens=reserve)
    assert result.ok
    assert result.prompt_tokens <= window - reserve
    assert result.dropped_messages > 0
    assert result.trimmed_chars == 0
    assert result.messages[0] == {"role": "system", "content": "mission"}
    assert result.messages[-1] == messages[-1]
    assert messages == [dict(message) for message in messages]  # input untouched


def test_tool_call_and_its_reply_leave_together() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "function": {
                        "name": "run_command",
                        "arguments": {"command": "nmap -p- 10.0.0.5 " + "y" * 2000},
                    }
                }
            ],
        },
        {"role": "tool", "content": "port 22 open " + "z" * 2000},
        {"role": "user", "content": "continue"},
    ]
    result = fit_messages(messages, context_tokens=700, reserve_tokens=64)
    roles = [message["role"] for message in result.messages]
    assert result.ok
    assert "tool" not in roles
    assert not any(message.get("tool_calls") for message in result.messages)
    assert roles[0] == "system" and roles[-1] == "user"


def test_oversized_message_is_cut_with_a_marker() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "dump\n" + "q" * 40000},
    ]
    result = fit_messages(messages, context_tokens=4096, reserve_tokens=256)
    assert result.ok
    assert result.trimmed_chars > 0
    assert "trimmed to fit" in result.messages[-1]["content"]
    assert result.prompt_tokens <= 4096 - 256
    assert len(result.messages[-1]["content"]) < 40000


def test_system_prompt_is_never_mutated_and_overflow_is_reported() -> None:
    messages = [
        {"role": "system", "content": "instructions " + "i" * 20000},
        {"role": "user", "content": "go"},
    ]
    result = fit_messages(messages, context_tokens=4096, reserve_tokens=64)
    assert result.ok is False
    assert result.messages[0]["content"].endswith("i" * 100)


def test_reserve_is_kept_free_for_the_answer() -> None:
    messages = [{"role": "user", "content": "a" * 8000}]
    result = fit_messages(messages, context_tokens=4096, reserve_tokens=1024)
    assert result.ok
    assert result.prompt_tokens <= 4096 - 1024


def test_the_incident_payload_is_recognised_as_an_overflow() -> None:
    assert looks_like_context_overflow(SERVER_400)
    assert not looks_like_context_overflow("HTTP 401: invalid api key")
    assert "OLLAMA_CONTEXT_LENGTH" in context_error(
        prompt_tokens=4775, context_tokens=4096, reserve_tokens=512
    )


def test_a_message_that_cannot_shrink_further_does_not_loop() -> None:
    """A ~300-char message cannot be cut: the marker costs more than the cut saves."""
    messages = [
        {"role": "system", "content": "instructions " + "i" * 4000},
        {"role": "user", "content": "u" * 300},
    ]
    result = fit_messages(messages, context_tokens=400, reserve_tokens=64)
    assert result.ok is False
    assert result.messages[-1]["content"] == "u" * 300
    assert result.trimmed_chars == 0


def test_native_payload_carries_window_and_output_cap() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="ollama",
        ollama_base_url="http://127.0.0.1:11434",
        planner_num_ctx=16384,
        llm_max_output_tokens=256,
    )
    client = OllamaClient(settings)
    payload = client._payload(
        [{"role": "user", "content": "hi"}],
        model="m",
        tools=None,
        stream=False,
        think=False,
        num_ctx=16384,
        temperature=None,
        format=None,
    )
    assert payload["options"]["num_ctx"] == 16384
    assert payload["options"]["num_predict"] == 256
    assert "think" not in payload


def test_openai_payload_keeps_max_tokens_and_has_no_options() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="openai",
        ollama_base_url="https://api.example.com/v1",
        llm_max_output_tokens=256,
        llm_reasoning_effort="none",
    )
    client = OllamaClient(settings)
    payload = client._payload(
        [{"role": "user", "content": "hi"}],
        model="m",
        tools=None,
        stream=False,
        think=False,
        num_ctx=16384,
        temperature=None,
        format=None,
    )
    assert payload["max_tokens"] == 256
    assert "options" not in payload
    assert payload["reasoning_effort"] == "none"


def test_client_refuses_a_prompt_that_cannot_fit() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="ollama",
        ollama_base_url="http://127.0.0.1:11434",
        planner_num_ctx=4096,
        llm_prompt_reserve_tokens=256,
    )
    client = OllamaClient(settings)
    messages = [
        {"role": "system", "content": "instructions " + "i" * 30000},
        {"role": "user", "content": "go"},
    ]
    with pytest.raises(OllamaContextError) as excinfo:
        client.fit(messages, num_ctx=4096)
    assert "context window" in str(excinfo.value)


def test_client_records_the_window_it_used() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="ollama",
        ollama_base_url="http://127.0.0.1:11434",
        planner_num_ctx=32768,
        llm_prompt_reserve_tokens=256,
    )
    client = OllamaClient(settings)
    fitted = client.fit([{"role": "user", "content": "hi"}], num_ctx=None)
    assert fitted.context_tokens == 32768
    assert not fitted.fitted


def test_defaults_wire_venice_and_both_models() -> None:
    """The regression this guards: ``.env`` was left on the retired Render GLM gate.

    A fresh checkout must talk to Venice over the OpenAI protocol, plan with one
    cloud model, fan the fleet out over both, and allow a prompt far larger than the
    ~4.7 k tokens one lane builds - the 4 096-token window is what killed a swarm.
    """
    settings = Settings(_env_file=None)
    assert settings.llm_provider == "venice"
    assert settings.openai_style
    assert settings.ollama_base_url == "https://api.venice.ai/api/v1"
    assert settings.planner_model == CLOUD_MODEL
    assert settings.worker_model == CLOUD_MODEL
    assert settings.fleet_models == [CLOUD_MODEL, DEEPSEEK_MODEL]
    # One squad of five (recon, web, exploit, coder, reporter) is the default;
    # the operator can ask for 1-5 squads => 5 / 10 / 15 / 20 / 25 agents.
    assert settings.fleet_groups == 1
    assert FLEET_MAX_GROUPS == 5
    assert settings.fleet_size == FLEET_GROUP_SIZE == 5
    assert settings.planner_num_ctx == settings.worker_num_ctx
    assert settings.worker_num_ctx >= 65536


def test_billing_failures_name_the_fix_instead_of_the_key() -> None:
    """401 and 402 are both "not the network": one is the key, one is the balance."""
    settings = Settings(
        _env_file=None,
        llm_provider="venice",
        ollama_base_url="https://api.venice.ai/api/v1",
    )
    client = OllamaClient(settings)

    unauthorised = client._http_error(401, "")
    assert isinstance(unauthorised, OllamaError)
    assert "AV2_LLM_API_KEY" in str(unauthorised)

    # Venice's literal 402 body, captured live on 23 Nov 2026.
    unpaid = client._http_error(
        402,
        '{"error":"Insufficient USD or Diem balance to complete request. '
        'Visit https://venice.ai/settings/api to add credits."}',
    )
    assert "top up" in str(unpaid)
    assert "Insufficient USD" in str(unpaid)
    assert not isinstance(unpaid, OllamaContextError)
