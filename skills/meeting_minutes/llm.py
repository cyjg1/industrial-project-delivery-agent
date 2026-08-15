from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

from agent.env_loader import load_project_env
from agent.provider_config import openai_compatible_config_from_env


class MeetingMinutesModelClient(Protocol):
    model_name: str

    def generate(self, system_prompt: str, user_message: str) -> str:
        ...


@dataclass(frozen=True)
class MeetingMinutesModelConfig:
    provider: str
    model: str
    base_url: str = ""
    api_surface: str = "chat_completions"
    api_key_env: str = ""
    api_key: str = ""
    max_output_tokens: int = 6000

    def redacted(self) -> dict[str, str | int | bool]:
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "api_surface": self.api_surface,
            "api_key_env": self.api_key_env,
            "has_api_key": bool(self.api_key),
            "max_output_tokens": self.max_output_tokens,
        }


class OpenAIChatMeetingMinutesClient:
    def __init__(self, config: MeetingMinutesModelConfig):
        self.config = config
        self.model_name = config.model

    def generate(self, system_prompt: str, user_message: str) -> str:
        if self.config.api_surface != "chat_completions":
            raise RuntimeError("meeting_minutes skill 目前只支持 Chat Completions 接口面。")
        if not self.config.api_key:
            raise RuntimeError(f"{self.config.api_key_env} 未配置，无法调用真实模型。")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("请先安装 openai 依赖后再调用真实模型。") from exc

        kwargs = {"api_key": self.config.api_key}
        if self.config.base_url:
            kwargs["base_url"] = self.config.base_url
        client = OpenAI(**kwargs)
        request = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        }
        if self.config.max_output_tokens > 0:
            request["max_tokens"] = self.config.max_output_tokens
        response = client.chat.completions.create(**request)
        return response.choices[0].message.content or ""


def load_meeting_minutes_model_config(provider: str | None = None) -> MeetingMinutesModelConfig:
    load_project_env()
    selected = (provider or os.getenv("LLM_PROVIDER", "")).strip().lower()
    max_tokens = _max_tokens()
    if not selected:
        raise RuntimeError("meeting_minutes skill 需要配置 LLM_PROVIDER")
    if selected == "openai":
        return MeetingMinutesModelConfig(
            provider="openai",
            model=os.getenv("OPENAI_MODEL") or os.getenv("OPENAI_AGENT_MODEL") or "gpt-4.1-mini",
            base_url=os.getenv("OPENAI_BASE_URL", "").strip().rstrip("/"),
            api_surface="chat_completions",
            api_key_env="OPENAI_API_KEY",
            api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            max_output_tokens=max_tokens,
        )
    if selected in {"openai_compatible", "compatible", "aliyun", "dashscope", "glm"}:
        config = openai_compatible_config_from_env(selected)
        return MeetingMinutesModelConfig(
            provider=config.provider,
            model=config.model,
            base_url=config.base_url,
            api_surface=config.api_surface,
            api_key_env=config.api_key_env,
            api_key=config.api_key,
            max_output_tokens=max_tokens,
        )
    raise RuntimeError(
        f"meeting_minutes skill 不支持 LLM_PROVIDER={selected!r}，请使用 openai 或 openai_compatible。"
    )


def create_meeting_minutes_client(
    config: MeetingMinutesModelConfig | None = None,
) -> MeetingMinutesModelClient:
    resolved = config or load_meeting_minutes_model_config()
    return OpenAIChatMeetingMinutesClient(resolved)


def _max_tokens() -> int:
    raw = os.getenv("MEETING_MINUTES_MAX_OUTPUT_TOKENS") or os.getenv("MAX_OUTPUT_TOKENS") or "6000"
    try:
        return int(raw)
    except ValueError:
        return 6000
