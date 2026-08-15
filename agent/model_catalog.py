from __future__ import annotations

import os
from typing import Any

from agent.env_loader import load_project_env
from agent.model_router import resolve_model_route


MODEL_LABELS = {
    "glm-5.2": "GLM 5.2",
    "glm-5.2-fast-preview": "GLM 5.2 Fast Preview",
    "qwen3.5-plus": "Qwen 3.5 Plus",
    "qwen3.7-plus": "Qwen 3.7 Plus",
    "deepseek-v3.2": "DeepSeek V3.2",
    "kimi-k2.5": "Kimi K2.5",
}


def conversation_model_catalog() -> dict[str, Any]:
    """Return only models the server permits for conversation runs."""
    load_project_env()
    try:
        route = resolve_model_route("conversation")
    except RuntimeError:
        return {
            "provider": "",
            "default_model": "",
            "models": [],
            "configured": False,
        }
    configured = [
        model.strip()
        for model in os.getenv("AGENT_CONVERSATION_MODELS", "").split(",")
        if model.strip()
    ]
    ordered = _deduplicate([route.model, *configured])
    return {
        "provider": route.provider,
        "default_model": route.model,
        "configured": True,
        "models": [
            {
                "id": model,
                "label": MODEL_LABELS.get(model, model),
            }
            for model in ordered
        ],
    }


def require_conversation_model(requested: str | None) -> str:
    catalog = conversation_model_catalog()
    model = str(requested or "").strip() or str(catalog["default_model"])
    allowed = {str(row["id"]) for row in catalog["models"]}
    if model not in allowed:
        raise ValueError("Conversation model is not enabled by the server.")
    return model


def _deduplicate(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output
