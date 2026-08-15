from __future__ import annotations

import os
from dataclasses import dataclass

from agent.model_router import resolve_model_route


class ProviderConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenAICompatibleConfig:
    provider: str
    api_key: str
    api_key_env: str
    model: str
    base_url: str
    api_surface: str


def openai_compatible_config_from_env(provider: str) -> OpenAICompatibleConfig:
    normalized_provider = provider.strip().lower()
    api_key_envs = _key_envs(normalized_provider)
    api_key_env, api_key = _first_env_value(api_key_envs)
    model = _first_env_value(_model_envs(normalized_provider))[1]
    if not model:
        model = resolve_model_route("conversation").model
    base_url = (
        _first_env_value(_base_url_envs(normalized_provider))[1]
        or _default_base_url(normalized_provider)
    )
    api_surface = (
        os.getenv("OPENAI_COMPAT_API_SURFACE")
        or os.getenv("GLM_API_SURFACE")
        or os.getenv("DASHSCOPE_API_SURFACE")
        or "chat_completions"
    )
    return OpenAICompatibleConfig(
        provider=normalized_provider,
        api_key=api_key,
        api_key_env=api_key_env or api_key_envs[0],
        model=model,
        base_url=base_url.rstrip("/"),
        api_surface=_normalize_api_surface(api_surface),
    )


def _first_env_value(names: list[str]) -> tuple[str, str]:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return name, value
    return "", ""


def _key_envs(provider: str) -> list[str]:
    if provider in {"aliyun", "dashscope"}:
        return [
            "DASHSCOPE_API_KEY",
            "OPENAI_COMPAT_API_KEY",
            "ALIYUN_API_KEY",
            "GLM_API_KEY",
        ]
    if provider == "glm":
        return [
            "GLM_API_KEY",
            "OPENAI_COMPAT_API_KEY",
            "DASHSCOPE_API_KEY",
            "ALIYUN_API_KEY",
        ]
    return [
        "OPENAI_COMPAT_API_KEY",
        "DASHSCOPE_API_KEY",
        "GLM_API_KEY",
        "ALIYUN_API_KEY",
    ]


def _model_envs(provider: str) -> list[str]:
    if provider == "glm":
        return ["GLM_MODEL", "OPENAI_COMPAT_MODEL", "DASHSCOPE_MODEL"]
    if provider in {"aliyun", "dashscope"}:
        return ["DASHSCOPE_MODEL", "OPENAI_COMPAT_MODEL", "GLM_MODEL"]
    return ["OPENAI_COMPAT_MODEL", "GLM_MODEL", "DASHSCOPE_MODEL"]


def _base_url_envs(provider: str) -> list[str]:
    if provider == "glm":
        return ["GLM_BASE_URL", "OPENAI_COMPAT_BASE_URL", "DASHSCOPE_BASE_URL"]
    if provider in {"aliyun", "dashscope"}:
        return ["DASHSCOPE_BASE_URL", "OPENAI_COMPAT_BASE_URL", "GLM_BASE_URL"]
    return ["OPENAI_COMPAT_BASE_URL", "DASHSCOPE_BASE_URL", "GLM_BASE_URL"]


def _default_base_url(provider: str) -> str:
    if provider == "glm":
        return "https://open.bigmodel.cn/api/paas/v4"
    return "https://dashscope.aliyuncs.com/compatible-mode/v1"


def _normalize_api_surface(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_").replace(".", "_")
    if normalized in {"chat", "chat_completions", "chatcompletions"}:
        return "chat_completions"
    if normalized in {"response", "responses"}:
        return "responses"
    raise ProviderConfigurationError(
        "Unsupported OpenAI-compatible API surface. Use chat_completions or responses."
    )
