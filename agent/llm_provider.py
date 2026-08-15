from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator
from typing import Protocol

from agent.env_loader import load_project_env
from agent.model_router import resolve_model_route


class LLMJsonValidationError(ValueError):
    pass


class LLMStreamInterruptedError(RuntimeError):
    def __init__(self, message: str, *, partial: bool, attempts: int) -> None:
        super().__init__(message)
        self.partial = partial
        self.attempts = attempts


LOGGER = logging.getLogger(__name__)


class LLMProvider(Protocol):
    name: str

    def complete(self, prompt: str) -> str:
        ...

    def complete_json(self, prompt: str) -> str:
        ...

    def stream_chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> Iterator["ChatStreamEvent"]:
        ...


@dataclass(frozen=True)
class ChatToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]
    reason: str = ""


@dataclass(frozen=True)
class ChatStreamEvent:
    kind: str
    text: str = ""
    tool_calls: list[ChatToolCall] | None = None


@dataclass
class OpenAIProvider:
    api_key: str
    model: str
    name: str = "openai"

    def complete(self, prompt: str) -> str:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("Install openai before using LLM_PROVIDER=openai") from exc
        client = OpenAI(api_key=self.api_key)
        response = client.responses.create(model=self.model, input=prompt)
        return response.output_text

    def complete_json(self, prompt: str) -> str:
        return _complete_chat_json(api_key=self.api_key, model=self.model, prompt=prompt)

    def stream_chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> Iterator[ChatStreamEvent]:
        return _stream_openai_compatible_chat(
            api_key=self.api_key,
            model=self.model,
            messages=messages,
            tools=tools,
        )


@dataclass
class GLMProvider:
    api_key: str
    model: str
    name: str = "glm"
    base_url: str = "https://open.bigmodel.cn/api/paas/v4/"

    def complete(self, prompt: str) -> str:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("Install openai before using LLM_PROVIDER=glm") from exc
        client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        response = client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content or ""

    def complete_json(self, prompt: str) -> str:
        return _complete_chat_json(
            api_key=self.api_key,
            model=self.model,
            prompt=prompt,
            base_url=self.base_url,
        )

    def stream_chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> Iterator[ChatStreamEvent]:
        return _stream_openai_compatible_chat(
            api_key=self.api_key,
            model=self.model,
            messages=messages,
            tools=tools,
            base_url=self.base_url,
        )


@dataclass
class OpenAICompatibleChatProvider:
    api_key: str
    model: str
    name: str = "openai_compatible_chat"
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    def complete(self, prompt: str) -> str:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("Install openai before using LLM_PROVIDER=openai_compatible") from exc
        client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": "严格执行调用方提示，只基于给定上下文作答，不虚构事实，也不强加固定回答格式。",
                },
                {"role": "user", "content": prompt},
            ],
        )
        return response.choices[0].message.content or ""

    def complete_json(self, prompt: str) -> str:
        return _complete_chat_json(
            api_key=self.api_key,
            model=self.model,
            prompt=prompt,
            base_url=self.base_url,
        )

    def stream_chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> Iterator[ChatStreamEvent]:
        return _stream_openai_compatible_chat(
            api_key=self.api_key,
            model=self.model,
            messages=messages,
            tools=tools,
            base_url=self.base_url,
        )


def get_provider(
    role: str = "conversation",
    model_override: str = "",
) -> LLMProvider:
    load_project_env()
    provider = os.getenv("LLM_PROVIDER", "").strip().lower()
    if not provider:
        raise RuntimeError(
            "LLM_PROVIDER is required; configure openai, glm, or openai_compatible"
        )
    if provider == "openai":
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
        route = resolve_model_route(role)
        return OpenAIProvider(api_key=key, model=model_override or route.model)
    if provider == "glm":
        key = os.getenv("GLM_API_KEY")
        if not key:
            raise RuntimeError("GLM_API_KEY is required when LLM_PROVIDER=glm")
        route = resolve_model_route(role)
        return GLMProvider(
            api_key=key,
            model=model_override or route.model,
            base_url=os.getenv("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/"),
        )
    if provider in {
        "openai_compatible",
        "openai-compatible",
        "openai_compatible_chat",
        "compatible",
        "aliyun",
        "dashscope",
    }:
        key = (
            os.getenv("OPENAI_COMPAT_API_KEY")
            or os.getenv("DASHSCOPE_API_KEY")
            or os.getenv("GLM_API_KEY")
        )
        if not key:
            raise RuntimeError("OPENAI_COMPAT_API_KEY is required when LLM_PROVIDER=openai_compatible")
        route = resolve_model_route(role)
        return OpenAICompatibleChatProvider(
            api_key=key,
            model=model_override or route.model,
            base_url=os.getenv("OPENAI_COMPAT_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        )
    raise RuntimeError(f"Unsupported LLM_PROVIDER={provider!r}")


def _complete_chat_json(
    *,
    api_key: str,
    model: str,
    prompt: str,
    base_url: str | None = None,
) -> str:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("Install openai before using a real JSON provider") from exc
    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    client = OpenAI(**kwargs)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "Return one valid JSON object and no surrounding prose."},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
    )
    return response.choices[0].message.content or ""


def parse_llm_candidate_json(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMJsonValidationError("LLM output is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise LLMJsonValidationError("LLM output must be a JSON object")
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise LLMJsonValidationError("LLM output must include non-empty items")
    for index, item in enumerate(items):
        _validate_item(index, item)
    return payload


def _validate_item(index: int, item: Any) -> None:
    if not isinstance(item, dict):
        raise LLMJsonValidationError(f"items[{index}] must be an object")
    for field in ["item_id", "category", "title", "description", "status", "evidence_refs"]:
        if field not in item:
            raise LLMJsonValidationError(f"items[{index}] missing {field}")
    if item["status"] not in {"candidate", "confirmed", "rejected"}:
        raise LLMJsonValidationError(f"items[{index}] has invalid status")
    refs = item["evidence_refs"]
    if not isinstance(refs, list) or not refs:
        raise LLMJsonValidationError(f"items[{index}] must include evidence_refs")
    for ref_index, ref in enumerate(refs):
        if not isinstance(ref, dict):
            raise LLMJsonValidationError(f"items[{index}].evidence_refs[{ref_index}] must be an object")
        for field in ["source_doc_id", "source_kind", "locator", "quote"]:
            if not ref.get(field):
                raise LLMJsonValidationError(
                    f"items[{index}].evidence_refs[{ref_index}] missing {field}"
                )


def _stream_openai_compatible_chat(
    *,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    base_url: str | None = None,
) -> Iterator[ChatStreamEvent]:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("Install openai before using a real chat provider") from exc
    client_kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url
    client = OpenAI(**client_kwargs)
    request: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
    if tools:
        request["tools"] = tools
        request["tool_choice"] = "auto"

    def stream_factory() -> Iterator[ChatStreamEvent]:
        return _stream_openai_compatible_chat_once(client, request)

    max_attempts = max(1, int(os.getenv("LLM_STREAM_MAX_ATTEMPTS", "2")))
    yield from _retry_stream_before_first_event(stream_factory, max_attempts=max_attempts)


def _stream_openai_compatible_chat_once(client: Any, request: dict[str, Any]) -> Iterator[ChatStreamEvent]:
    response = client.chat.completions.create(**request)
    fragments: dict[int, dict[str, str]] = {}
    for chunk in response:
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        delta = choice.delta
        content = getattr(delta, "content", None)
        if content:
            yield ChatStreamEvent(kind="delta", text=content)
        for tool_delta in getattr(delta, "tool_calls", None) or []:
            index = int(getattr(tool_delta, "index", 0) or 0)
            fragment = fragments.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if getattr(tool_delta, "id", None):
                fragment["id"] += str(tool_delta.id)
            function = getattr(tool_delta, "function", None)
            if function is None:
                continue
            if getattr(function, "name", None):
                fragment["name"] += str(function.name)
            if getattr(function, "arguments", None):
                fragment["arguments"] += str(function.arguments)
    if fragments:
        yield ChatStreamEvent(
            kind="tool_calls",
            tool_calls=[
                _tool_call_from_fragment(index, fragment)
                for index, fragment in sorted(fragments.items())
                if fragment.get("name")
            ],
        )


def _retry_stream_before_first_event(
    stream_factory: Callable[[], Iterator[ChatStreamEvent]],
    *,
    max_attempts: int,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[ChatStreamEvent]:
    attempts = max(1, int(max_attempts))
    for attempt_index in range(attempts):
        emitted = False
        try:
            for event in stream_factory():
                emitted = True
                yield event
            return
        except Exception as exc:
            if not _is_retryable_stream_error(exc):
                raise
            attempt_number = attempt_index + 1
            if emitted:
                raise LLMStreamInterruptedError(
                    "模型服务在流式输出途中断开；本次草稿必须丢弃，"
                    "运行时将从本回合开始前的检查点重试。",
                    partial=True,
                    attempts=attempt_number,
                ) from exc
            if attempt_number >= attempts:
                raise LLMStreamInterruptedError(
                    f"模型服务在建立流式响应时断开，已自动尝试 {attempt_number} 次仍失败；"
                    "本回合没有完成，请重试。",
                    partial=False,
                    attempts=attempt_number,
                ) from exc
            LOGGER.warning(
                "LLM stream disconnected before the first visible event; retrying (%s/%s): %s",
                attempt_number,
                attempts,
                exc,
            )
            sleep(0.35 * attempt_number)


def _is_retryable_stream_error(exc: Exception) -> bool:
    retryable_names = {
        "APIConnectionError",
        "APITimeoutError",
        "ConnectError",
        "ConnectTimeout",
        "ReadError",
        "ReadTimeout",
        "RemoteProtocolError",
    }
    retryable_fragments = (
        "connection reset",
        "connection closed",
        "peer closed connection",
        "incomplete chunked read",
        "server disconnected",
        "timed out",
    )
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in retryable_names:
            return True
        message = str(current).lower()
        if any(fragment in message for fragment in retryable_fragments):
            return True
        current = current.__cause__ or current.__context__
    return False


def _tool_call_from_fragment(index: int, fragment: dict[str, str]) -> ChatToolCall:
    arguments = _parse_json_object(fragment.get("arguments", ""))
    reason = str(arguments.pop("reason", "") or "")
    return ChatToolCall(
        call_id=fragment.get("id") or f"call_{index}",
        name=fragment.get("name", ""),
        arguments=arguments,
        reason=reason,
    )


def _parse_json_object(value: str) -> dict[str, Any]:
    if not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


