from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.env_loader import load_project_env  # noqa: E402
from agent.model_catalog import conversation_model_catalog  # noqa: E402


def run_probe() -> dict[str, Any]:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("Install openai before probing model capabilities.") from exc

    base_url = os.getenv("OPENAI_COMPAT_BASE_URL", "").strip()
    api_key = (
        os.getenv("OPENAI_COMPAT_API_KEY", "").strip()
        or os.getenv("DASHSCOPE_API_KEY", "").strip()
        or os.getenv("GLM_API_KEY", "").strip()
    )
    if not base_url or not api_key:
        raise RuntimeError("Configured OpenAI-compatible base URL and API key are required.")
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=60.0)
    listed_models = client.models.list()
    model_ids = {
        str(getattr(model, "id", "") or "")
        for model in listed_models.data
    }
    catalog = conversation_model_catalog()
    chat_results = [
        _probe_chat_model(client, str(row["id"]), model_ids)
        for row in catalog["models"]
    ]
    embedding_result = _probe_embedding(client)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url_host": _host_only(base_url),
        "listed_model_count": len(model_ids),
        "conversation_models": chat_results,
        "embedding": embedding_result,
        "ok": (
            all(row["ok"] for row in chat_results)
            and embedding_result["ok"]
        ),
    }


def _probe_chat_model(
    client: Any,
    model: str,
    listed_models: set[str],
) -> dict[str, Any]:
    started = time.perf_counter()
    tool_names: list[str] = []
    error = ""
    try:
        stream = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "Call ping_tool once with text=ping. Do not answer directly.",
                },
                {"role": "user", "content": "Run the capability probe."},
            ],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "ping_tool",
                        "description": "Use for this capability probe.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                            },
                            "required": ["text"],
                            "additionalProperties": False,
                        },
                    },
                }
            ],
            tool_choice="auto",
            stream=True,
        )
        for chunk in stream:
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            calls = getattr(choices[0].delta, "tool_calls", None) or []
            for call in calls:
                name = str(getattr(getattr(call, "function", None), "name", "") or "")
                if name:
                    tool_names.append(name)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    return {
        "model": model,
        "listed": model in listed_models,
        "streaming_tool_call": "ping_tool" in tool_names,
        "elapsed_ms": elapsed_ms,
        "error": error,
        "ok": not error and "ping_tool" in tool_names,
    }


def _probe_embedding(client: Any) -> dict[str, Any]:
    model = os.getenv(
        "MEMORY_EMBEDDING_MODEL",
        "qwen3.7-text-embedding",
    ).strip()
    started = time.perf_counter()
    try:
        response = client.embeddings.create(
            model=model,
            input=["单据拆分", "票据切分"],
        )
        vectors = [list(row.embedding) for row in response.data]
        similarity = _cosine(vectors[0], vectors[1])
        return {
            "model": model,
            "dimensions": len(vectors[0]),
            "input_count": len(vectors),
            "similarity": round(similarity, 6),
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "error": "",
            "ok": (
                len(vectors) == 2
                and bool(vectors[0])
                and len(vectors[0]) == len(vectors[1])
            ),
        }
    except Exception as exc:
        return {
            "model": model,
            "dimensions": 0,
            "input_count": 0,
            "similarity": 0.0,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
            "ok": False,
        }


def _cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return numerator / (left_norm * right_norm)


def _host_only(url: str) -> str:
    return url.split("://", 1)[-1].split("/", 1)[0]


def main() -> int:
    load_project_env()
    parser = argparse.ArgumentParser(
        description="Probe listed models with the exact streaming function-calling protocol used by the app."
    )
    parser.add_argument(
        "--output",
        default=str(
            PROJECT_ROOT
            / "docs"
            / "test_reports"
            / "model_capability_probe.json"
        ),
    )
    args = parser.parse_args()
    report = run_probe()
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
