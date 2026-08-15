from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agent.llm_provider import get_provider
from agent.runtime_provider_config import (
    RuntimeProviderConfigurationError,
    clear_runtime_provider,
    configure_runtime_provider,
    redact_runtime_error,
    runtime_provider_status,
)


class RuntimeProviderConfigRequest(BaseModel):
    provider: str
    model: str
    api_key: str = Field(default="", max_length=4096)
    base_url: str = Field(default="", max_length=500)
    api_surface: str = "chat_completions"


def create_runtime_config_router() -> APIRouter:
    router = APIRouter(prefix="/api/runtime/provider-config", tags=["runtime-config"])

    @router.get("")
    def get_config() -> dict[str, Any]:
        return runtime_provider_status()

    @router.put("")
    def update_config(payload: RuntimeProviderConfigRequest) -> dict[str, Any]:
        try:
            return configure_runtime_provider(
                provider=payload.provider,
                model=payload.model,
                api_key=payload.api_key,
                base_url=payload.base_url,
                api_surface=payload.api_surface,
            )
        except RuntimeProviderConfigurationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("")
    def delete_config() -> dict[str, Any]:
        return clear_runtime_provider()

    @router.post("/test")
    def test_config() -> dict[str, Any]:
        status = runtime_provider_status()
        if not status["configured"]:
            raise HTTPException(status_code=400, detail="Configure a provider first.")
        try:
            provider = get_provider(model_override=str(status["model"]))
            provider.complete("Reply with exactly: OK")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=redact_runtime_error(exc)) from exc
        return {
            "ok": True,
            "provider": status["provider"],
            "model": status["model"],
        }

    return router

