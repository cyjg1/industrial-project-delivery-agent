from __future__ import annotations

import json
from dataclasses import asdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agent.runtime.contracts import (
    PlannerTurn,
    SelectedToolStep,
    ToolCallRequest,
)


@dataclass
class RuntimeState:
    run_id: str
    domain_state: Any
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    plan: list[SelectedToolStep] = field(default_factory=list)
    model_turns: list[PlannerTurn] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    call_counts: dict[str, int] = field(default_factory=dict)
    final_text: str = ""
    stop_reason: str = ""
    completed: bool = False
    next_round: int = 1
    tool_call_count: int = 0
    verification_rejections: int = 0
    verification_repair_pending: bool = False

    @property
    def round_index(self) -> int:
        return len(self.model_turns)

    def checkpoint(
        self,
        *,
        stage: str,
        next_round: int,
        inflight_tool: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return _json_safe(
            {
                "checkpoint_version": 1,
                "run_id": self.run_id,
                "stage": stage,
                "next_round": max(1, int(next_round)),
                "messages": self.messages,
                "plan": [asdict(step) for step in self.plan],
                "model_turns": [
                    {
                        "text": turn.text,
                        "provider": turn.provider,
                        "model": turn.model,
                        "tool_calls": [asdict(call) for call in turn.tool_calls],
                    }
                    for turn in self.model_turns
                ],
                "tool_results": self.tool_results,
                "call_counts": self.call_counts,
                "final_text": self.final_text,
                "tool_call_count": self.tool_call_count,
                "verification_rejections": self.verification_rejections,
                "verification_repair_pending": self.verification_repair_pending,
                "inflight_tool": inflight_tool or {},
            }
        )

    @classmethod
    def restore(
        cls,
        *,
        checkpoint: dict[str, Any],
        run_id: str,
        domain_state: Any,
        tools: list[dict[str, Any]],
    ) -> "RuntimeState":
        if int(checkpoint.get("checkpoint_version") or 0) != 1:
            raise ValueError("Unsupported runtime checkpoint version")
        checkpoint_run_id = str(checkpoint.get("run_id") or "")
        if checkpoint_run_id and checkpoint_run_id != run_id:
            raise ValueError("Runtime checkpoint belongs to another run")
        if checkpoint.get("stage") == "tool_inflight":
            tool = dict(checkpoint.get("inflight_tool") or {})
            raise RuntimeError(
                "Interrupted tool execution requires explicit reconciliation "
                f"before retry: {tool.get('name') or 'unknown_tool'}"
            )
        state = cls(
            run_id=run_id,
            domain_state=domain_state,
            messages=list(checkpoint.get("messages") or []),
            tools=list(tools),
            plan=[
                SelectedToolStep(
                    step_id=str(step.get("step_id") or ""),
                    tool_name=str(step.get("tool_name") or ""),
                    reason=str(step.get("reason") or ""),
                    arguments=dict(step.get("arguments") or {}),
                )
                for step in checkpoint.get("plan") or []
            ],
            model_turns=[
                PlannerTurn(
                    text=str(turn.get("text") or ""),
                    provider=str(turn.get("provider") or ""),
                    model=str(turn.get("model") or ""),
                    tool_calls=tuple(
                        ToolCallRequest(
                            call_id=str(call.get("call_id") or ""),
                            name=str(call.get("name") or ""),
                            arguments=dict(call.get("arguments") or {}),
                            reason=str(call.get("reason") or ""),
                        )
                        for call in turn.get("tool_calls") or []
                    ),
                )
                for turn in checkpoint.get("model_turns") or []
            ],
            tool_results=list(checkpoint.get("tool_results") or []),
            call_counts={
                str(key): int(value)
                for key, value in dict(checkpoint.get("call_counts") or {}).items()
            },
            final_text=str(checkpoint.get("final_text") or ""),
            stop_reason=(
                "model_completed"
                if checkpoint.get("stage") == "completed"
                else ""
            ),
            completed=checkpoint.get("stage") == "completed",
            next_round=max(1, int(checkpoint.get("next_round") or 1)),
            tool_call_count=max(0, int(checkpoint.get("tool_call_count") or 0)),
            verification_rejections=max(
                0,
                int(checkpoint.get("verification_rejections") or 0),
            ),
            verification_repair_pending=bool(
                checkpoint.get("verification_repair_pending", False)
            ),
        )
        return state


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=_json_default))


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if hasattr(value, "__dict__"):
        return {
            key: item
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return str(value)
