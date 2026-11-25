"""Manual connectivity smoke test for the local Ollama listener.

Run from the repository root:

    python tools/smoke_ollama.py
    python tools/smoke_ollama.py "check that tools work" --tool-call

It is intentionally dependency-light (httpx only) and never touches the sandbox,
so it is safe to run before the Kali container exists.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import get_settings  # noqa: E402
from app.core.ollama_client import OllamaClient, OllamaError  # noqa: E402

DEMO_TOOL = {
    "type": "function",
    "function": {
        "name": "run_command",
        "description": "Run a shell command inside the Kali sandbox container.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "Command line to execute"}},
            "required": ["command"],
        },
    },
}


async def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test the Ollama endpoint.")
    parser.add_argument("prompt", nargs="?", default="Reply with the single word: ready")
    parser.add_argument("--tool-call", action="store_true", help="advertise a demo tool and print recovered calls")
    parser.add_argument("--stream", action="store_true", help="stream tokens instead of a single request")
    parser.add_argument("--model", default=None)
    parser.add_argument("--list-models", action="store_true", help="enumerate every model and its capabilities")
    parser.add_argument(
        "--num-ctx",
        type=int,
        default=None,
        help="context window to request (honoured on the native /api/chat protocol only)",
    )
    parser.add_argument(
        "--pad",
        type=int,
        default=0,
        help="prefix the prompt with N thousand characters of filler to exercise the window",
    )
    args = parser.parse_args()

    settings = get_settings()
    client = OllamaClient(settings)
    print(f"provider      : {settings.llm_provider}")
    print(f"protocol      : {'openai ' if settings.openai_style else 'native '}({'/v1' if settings.openai_style else '/api/chat'})")
    print(f"base_url      : {settings.ollama_base_url}")
    print(f"planner_model : {settings.planner_model}")
    print(f"worker_model  : {settings.worker_model}")
    print(f"num_ctx       : {args.num_ctx or settings.planner_num_ctx} (reserve {settings.llm_prompt_reserve_tokens})")

    ok, detail = await client.health()
    print(f"health        : {ok} ({detail})")
    if not ok:
        return 1

    models = await client.list_models()
    print(f"models        : {len(models)}")
    if args.list_models:
        for name in models:
            caps = await client.model_capabilities(name)
            print(f"  - {name}  capabilities={caps or ['unknown']}")

    model = args.model or settings.planner_model
    capabilities = await client.model_capabilities(model)
    print(f"target caps   : {capabilities or ['unknown']}")
    tools = [DEMO_TOOL] if args.tool_call else None
    if args.tool_call:
        prompt = args.prompt if args.prompt != "Reply with the single word: ready" else (
            "Use the run_command tool to list the files in /work."
        )
    else:
        prompt = args.prompt

    messages = [
        {"role": "system", "content": "You are a concise operations assistant."},
        {"role": "user", "content": prompt},
    ]
    if args.pad > 0:
        messages[1]["content"] = "filler " * (args.pad * 250) + "\n" + prompt

    try:
        if args.stream:
            def on_token(kind: str, text: str) -> None:
                print(text, end="", flush=True)

            reply = await client.chat_stream(
                messages, model=model, tools=tools, num_ctx=args.num_ctx, on_token=on_token
            )
            print()
        else:
            reply = await client.chat(messages, model=model, tools=tools, num_ctx=args.num_ctx)
    except OllamaError as exc:
        print(f"chat failed   : {exc}")
        return 1

    print(f"model used    : {reply.model}")
    print(f"native tools  : {reply.native_tools}")
    print(f"usage         : {reply.usage}")
    print(f"content       : {reply.content.strip()[:800]!r}")
    if reply.thinking:
        print(f"thinking      : {reply.thinking.strip()[:400]!r}")
    print(f"tool_calls    : {[c.as_event() for c in reply.tool_calls]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
