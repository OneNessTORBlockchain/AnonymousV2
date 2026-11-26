"""Tests for the effective LLM base URL and the 307 it caused.

The regression is concrete: a Render deployment set
``AV2_LLM_PROVIDER=openai`` (as ``.env.example`` instructs for Ollama Cloud)
but pasted the bare host ``https://ollama.com``.  Because the provider label
alone selects the OpenAI protocol, the pre-flight probe called
``GET https://ollama.com/models`` - the marketing site - which answers
``307 -> /search`` with an empty body.  The run aborted with
``HTTP 307: `` next to "check the API key, the model id and the account quota".

``Settings.llm_base_url`` now fills the missing ``/v1`` for a bare host, and a
3xx is reported as "wrong base URL" instead of as a credential problem.
"""
from __future__ import annotations

import httpx

from app.config import Settings
from app.core.ollama_client import OllamaClient


def _settings(base: str, provider: str = "openai") -> Settings:
    """Settings with an explicit base URL.

    ``ollama_base_url`` declares a ``validation_alias`` (``AV2_OLLAMA_BASE_URL``
    / ``AV2_LLM_BASE_URL``), so pydantic ignores the bare field name as a
    keyword and would quietly fall back to the Venice default: the alias, or the
    environment, is the only way in.
    """
    return Settings(_env_file=None, llm_provider=provider, AV2_OLLAMA_BASE_URL=base)


def test_bare_provider_host_gets_the_v1_suffix() -> None:
    # The exact value that produced the 307.
    assert _settings("https://ollama.com").llm_base_url == "https://ollama.com/v1"


def test_trailing_slash_and_whitespace_are_normalised() -> None:
    assert _settings("  https://ollama.com/  ").llm_base_url == "https://ollama.com/v1"


def test_versioned_bases_pass_through_untouched() -> None:
    assert _settings("https://api.venice.ai/api/v1", "venice").llm_base_url == (
        "https://api.venice.ai/api/v1"
    )
    assert _settings("https://api.venice.ai/api/v1/").llm_base_url == (
        "https://api.venice.ai/api/v1"
    )
    assert _settings("https://ollama.com/v1").llm_base_url == "https://ollama.com/v1"


def test_native_provider_is_left_alone() -> None:
    local = _settings("http://127.0.0.1:11434", "ollama")
    assert local.llm_base_url == "http://127.0.0.1:11434"


def test_a_base_that_versions_its_own_routes_is_not_rewritten() -> None:
    # A gateway serving /chat/completions under its own path keeps that path:
    # appending /v1 to somebody else's router would break a working deployment.
    gateway = _settings("https://gateway.example/openai")
    assert gateway.llm_base_url == "https://gateway.example/openai"


def test_client_targets_the_effective_base() -> None:
    settings = _settings("https://ollama.com")
    client = OllamaClient(settings)._client()
    # build_request opens no socket, so the AsyncClient needs no event loop
    # here; the URL it produces is the whole assertion.
    request = client.build_request("GET", "/models")
    assert str(request.url) == "https://ollama.com/v1/models"


def test_redirect_is_reported_as_a_base_url_problem() -> None:
    client = OllamaClient(_settings("https://ollama.com"))
    response = httpx.Response(307, headers={"location": "/search"})
    detail = client._redirect_hint("/models", response)  # noqa: SLF001 - message under test
    assert "307" in detail
    assert "/search" in detail
    # The actionable half: the operator needs the base to change, not the key.
    assert "https://ollama.com/v1" in detail
    assert "AV2_LLM_PROVIDER=openai" in detail


def test_http_error_classifies_redirects_and_keeps_401_hint() -> None:
    client = OllamaClient(_settings("https://ollama.com"))
    redirect = str(client._http_error(307, ""))  # noqa: SLF001
    assert "redirect" in redirect and "web page" in redirect
    denied = str(client._http_error(401, '{"error":"Unauthorized"}'))  # noqa: SLF001
    assert "API key was rejected" in denied
