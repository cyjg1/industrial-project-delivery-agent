from __future__ import annotations

import os
from dataclasses import dataclass


FAST_ROLES = {"extraction", "verification", "classification"}


@dataclass(frozen=True)
class ModelRoute:
    role: str
    provider: str
    model: str


def resolve_model_route(role: str) -> ModelRoute:
    normalized_role = (role or "conversation").strip().lower()
    provider = os.getenv("LLM_PROVIDER", "").strip().lower()
    if not provider:
        raise RuntimeError(
            "LLM_PROVIDER is required; configure openai, glm, or openai_compatible"
        )

    configured_main = _configured_provider_model(provider)
    main_model = os.getenv("AGENT_MAIN_MODEL", "").strip() or configured_main
    role_override = os.getenv(f"AGENT_{normalized_role.upper()}_MODEL", "").strip()
    if role_override:
        model = role_override
    elif normalized_role in FAST_ROLES:
        model = os.getenv("AGENT_FAST_MODEL", "").strip() or main_model
    else:
        model = main_model
    if not model:
        raise RuntimeError(
            f"No model configured for role={normalized_role!r}; set AGENT_MAIN_MODEL "
            "or the provider's model environment variable."
        )
    return ModelRoute(role=normalized_role, provider=provider, model=model)


def _configured_provider_model(provider: str) -> str:
    if provider == "openai":
        return os.getenv("OPENAI_MODEL", "").strip()
    if provider == "glm":
        return os.getenv("GLM_MODEL", "").strip()
    if provider in {
        "openai_compatible",
        "openai-compatible",
        "openai_compatible_chat",
        "compatible",
        "aliyun",
        "dashscope",
    }:
        return (os.getenv("OPENAI_COMPAT_MODEL", "").strip() or os.getenv("GLM_MODEL", "").strip())
    return os.getenv("LLM_MODEL", "").strip()
