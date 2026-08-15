from __future__ import annotations

from typing import Any

from agent.access_policy import User
from agent.tool_registry import ToolRegistry


class McpServerAdapter:
    """Placeholder adapter for exposing ToolSpec objects as MCP tools.

    This class does not implement the MCP wire protocol yet. The intended
    mapping is deliberately thin: registry tools on the explicit `mcp` surface
    become descriptors shaped as {name, description, inputSchema}. Protocol transport,
    sessions, and invocation framing belong in a later adapter layer.
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def tool_descriptions(self, actor: User) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "inputSchema": spec.parameters,
            }
            for spec in self.registry.external_specs(actor)
        ]
