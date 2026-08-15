from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
POLICY_PATH = Path(__file__).with_name("agent_policy.json")
PROMPT_PATH = Path(__file__).with_name("project_delivery_manager.md")


def load_agent_policy(path: str | Path = POLICY_PATH) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def render_agent_instructions() -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    policy = json.dumps(load_agent_policy(), ensure_ascii=False, indent=2)
    return template.replace("{{POLICY_JSON}}", policy)
