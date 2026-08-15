from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend.main import create_app  # noqa: E402
from scripts.init_demo import initialize_demo  # noqa: E402


OUTPUT_PATH = PROJECT_ROOT / "frontend" / "src" / "demo" / "staticDemoData.json"
ACTOR_HEADER = "X-Actor-Id"
DEFAULT_ACTOR = "u_pmo"


def export_static_demo(output_path: Path = OUTPUT_PATH) -> Path:
    with tempfile.TemporaryDirectory() as store_dir, patch.dict(
        os.environ,
        {
            "LLM_PROVIDER": "",
            "MEMORY_EMBEDDING": "off",
            "PROJECT_AGENT_STORE_DIR": store_dir,
        },
        clear=False,
    ):
        initialize_demo(store_dir)
        with TestClient(create_app(store_dir)) as client:
            default_headers = {ACTOR_HEADER: DEFAULT_ACTOR}
            users_payload = get_json(client, "/api/dev/switchable-users", default_headers)
            actor_ids = [user["user_id"] for user in users_payload["users"]]
            if DEFAULT_ACTOR not in actor_ids:
                actor_ids.insert(0, DEFAULT_ACTOR)

            workspaces = {}
            sessions = {}
            for actor_id in actor_ids:
                headers = {ACTOR_HEADER: actor_id}
                workspaces[actor_id] = get_json(client, "/api/workspace", headers)
                sessions[actor_id] = get_json(client, "/api/conversation/sessions", headers)

            milestone_id = workspaces[DEFAULT_ACTOR]["milestone_workspace"]["summary"]["milestone_id"]
            month = date.today().strftime("%Y-%m")
            payload = {
                "generatedAt": date.today().isoformat(),
                "defaultActorId": DEFAULT_ACTOR,
                "workspaces": workspaces,
                "sessions": sessions,
                "switchableUsers": users_payload,
                "conversationModels": get_json(client, "/api/conversation/models", default_headers),
                "dailyWorkRecords": get_json(client, "/api/daily-work-records", default_headers),
                "fileWorkspace": get_json(client, "/api/file-workspace", default_headers),
                "projectFiles": get_json(client, "/api/project-files", default_headers),
                "projectCalendar": get_json(client, f"/api/project-calendar?month={month}", default_headers),
                "deliverables": get_json(
                    client,
                    f"/api/deliverables?milestone_id={milestone_id}",
                    default_headers,
                ),
                "skillPackages": get_json(client, "/api/skills/packages", default_headers),
            }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_path


def get_json(client: TestClient, path: str, headers: dict[str, str]) -> dict:
    response = client.get(path, headers=headers)
    response.raise_for_status()
    return response.json()


if __name__ == "__main__":
    print(export_static_demo())
