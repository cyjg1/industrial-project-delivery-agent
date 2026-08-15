from __future__ import annotations

from typing import Protocol

from agent.runtime.contracts import RuntimeEvent


class RuntimeEventSink(Protocol):
    def append(self, event: RuntimeEvent) -> None:
        ...

    def checkpoint(self, run_id: str, checkpoint: dict) -> None:
        ...


class NullRuntimeEventSink:
    def append(self, event: RuntimeEvent) -> None:
        return None

    def checkpoint(self, run_id: str, checkpoint: dict) -> None:
        return None
