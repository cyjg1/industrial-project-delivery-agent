from __future__ import annotations

import os
import re
import threading
from typing import Any
from urllib.parse import urlparse


_LOCK = threading.RLock()
_SECRET_ENV_NAMES = (
    "OPENAI_API_KEY",
    "OPENAI_COMPAT_API_KEY",
    "DASHSCOPE_API_KEY",
    "GLM_API_KEY",
    "ALIYUN_API_KEY",
)
_MODEL_ENV_NAMES = (
    "OPENAI_MODEL",
    "OPENAI_AGENT_MODEL",
    "OPENAI_COMPAT_MODEL",
    "DASHSCOPE_MODEL",
    "GLM_MODEL",
    "LLM_MODEL",
    "AGENT_MAIN_MODEL",
    "AGENT_FAST_MODEL",
    "AGENT_CONVERSATION_MODELS",
)
_BASE_URL_ENV_NAMES = (
    "OPENAI_COMPAT_BASE_URL",
    "DASHSCOPE_BASE_URL",
    "GLM_BASE_URL",
)
_SUPPORTED_PROVIDERS = {"openai", "glm", "openai_compatible"}
_DEFAULT_BASE_URLS = {
    "glm": "https://open.bigmodel.cn/api/paas/v4",
    "openai_compatible": "https://dashscope.aliyuncs.com/compatible-mode/v1",
}


class RuntimeProviderConfigurationError(ValueError):
    pass


def runtime_provider_status() -> dict[str, Any]:
    provider = _normalize_provider(os.getenv("LLM_PROVIDER", ""), allow_empty=True)
    model = _current_model(provider)
    return {
        "provider": provider,
        "model": model,
        "base_url": _current_base_url(provider),
        "api_surface": os.getenv("OPENAI_COMPAT_API_SURFACE", "chat_completions"),
        "api_key_configured": bool(_current_api_key(provider)),
        "configured": bool(provider in _SUPPORTED_PROVIDERS and model and _current_api_key(provider)),
        "persistence": "process_memory",
    }


def configure_runtime_provider(
    *,
    provider: str,
    model: str,
    api_key: str = "",
    base_url: str = "",
    api_surface: str = "chat_completions",
) -> dict[str, Any]:
    normalized = _normalize_provider(provider)
    clean_model = _required_text(model, "model", max_length=200)
    clean_key = api_key.strip()
    clean_surface = _normalize_surface(api_surface)
    clean_base_url = _validated_base_url(base_url or _DEFAULT_BASE_URLS.get(normalized, ""))

    with _LOCK:
        existing_key = _current_api_key(normalized)
        if not clean_key and not existing_key:
            raise RuntimeProviderConfigurationError("API Key is required.")

        for name in _MODEL_ENV_NAMES:
            os.environ.pop(name, None)
        for name in _BASE_URL_ENV_NAMES:
            os.environ.pop(name, None)

        os.environ["LLM_PROVIDER"] = normalized
        os.environ["AGENT_MAIN_MODEL"] = clean_model
        os.environ["AGENT_FAST_MODEL"] = clean_model
        os.environ["AGENT_CONVERSATION_MODELS"] = clean_model
        os.environ["OPENAI_COMPAT_API_SURFACE"] = clean_surface

        if normalized == "openai":
            os.environ["OPENAI_MODEL"] = clean_model
        elif normalized == "glm":
            os.environ["GLM_MODEL"] = clean_model
            os.environ["GLM_BASE_URL"] = clean_base_url
        else:
            os.environ["OPENAI_COMPAT_MODEL"] = clean_model
            os.environ["OPENAI_COMPAT_BASE_URL"] = clean_base_url

        if clean_key:
            for name in _SECRET_ENV_NAMES:
                os.environ.pop(name, None)
            os.environ[_key_env_name(normalized)] = clean_key

    return runtime_provider_status()


def clear_runtime_provider() -> dict[str, Any]:
    with _LOCK:
        for name in (
            "LLM_PROVIDER",
            "OPENAI_COMPAT_API_SURFACE",
            *_SECRET_ENV_NAMES,
            *_MODEL_ENV_NAMES,
            *_BASE_URL_ENV_NAMES,
        ):
            os.environ.pop(name, None)
    return runtime_provider_status()


def redact_runtime_error(error: BaseException) -> str:
    message = str(error).strip() or error.__class__.__name__
    for name in _SECRET_ENV_NAMES:
        secret = os.getenv(name, "")
        if secret:
            message = message.replace(secret, "[REDACTED]")
    message = re.sub(r"(?i)(api[_ -]?key\s*[=:]\s*)\S+", r"\1[REDACTED]", message)
    return message[:600]


def _normalize_provider(provider: str, *, allow_empty: bool = False) -> str:
    normalized = provider.strip().lower().replace("-", "_")
    aliases = {
        "compatible": "openai_compatible",
        "openai_compatible_chat": "openai_compatible",
        "dashscope": "openai_compatible",
        "aliyun": "openai_compatible",
    }
    normalized = aliases.get(normalized, normalized)
    if allow_empty and normalized in {"", "mock"}:
        return ""
    if normalized not in _SUPPORTED_PROVIDERS:
        raise RuntimeProviderConfigurationError(
            "Unsupported provider. Use openai, glm, or openai_compatible."
        )
    return normalized


def _required_text(value: str, field: str, *, max_length: int) -> str:
    clean = value.strip()
    if not clean:
        raise RuntimeProviderConfigurationError(f"{field} is required.")
    if len(clean) > max_length:
        raise RuntimeProviderConfigurationError(f"{field} is too long.")
    return clean


def _normalize_surface(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_") or "chat_completions"
    if normalized not in {"chat_completions", "responses"}:
        raise RuntimeProviderConfigurationError(
            "api_surface must be chat_completions or responses."
        )
    return normalized


def _validated_base_url(value: str) -> str:
    if not value:
        return ""
    if len(value) > 500:
        raise RuntimeProviderConfigurationError("base_url is too long.")
    parsed = urlparse(value)
    if parsed.username or parsed.password:
        raise RuntimeProviderConfigurationError("base_url must not contain credentials.")
    host = (parsed.hostname or "").lower()
    loopback = host in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise RuntimeProviderConfigurationError(
            "base_url must use HTTPS; HTTP is allowed only for a loopback address."
        )
    if not host:
        raise RuntimeProviderConfigurationError("base_url is invalid.")
    return value.rstrip("/")


def _key_env_name(provider: str) -> str:
    if provider == "openai":
        return "OPENAI_API_KEY"
    if provider == "glm":
        return "GLM_API_KEY"
    return "OPENAI_COMPAT_API_KEY"


def _current_api_key(provider: str) -> str:
    if provider == "openai":
        return os.getenv("OPENAI_API_KEY", "").strip()
    if provider == "glm":
        return os.getenv("GLM_API_KEY", "").strip()
    if provider == "openai_compatible":
        return (
            os.getenv("OPENAI_COMPAT_API_KEY", "").strip()
            or os.getenv("DASHSCOPE_API_KEY", "").strip()
        )
    return ""


def _current_model(provider: str) -> str:
    if provider == "openai":
        return os.getenv("OPENAI_MODEL", "").strip()
    if provider == "glm":
        return os.getenv("GLM_MODEL", "").strip()
    if provider == "openai_compatible":
        return os.getenv("OPENAI_COMPAT_MODEL", "").strip()
    return ""


def _current_base_url(provider: str) -> str:
    if provider == "glm":
        return os.getenv("GLM_BASE_URL", "").strip() or _DEFAULT_BASE_URLS["glm"]
    if provider == "openai_compatible":
        return (
            os.getenv("OPENAI_COMPAT_BASE_URL", "").strip()
            or _DEFAULT_BASE_URLS["openai_compatible"]
        )
    return ""

