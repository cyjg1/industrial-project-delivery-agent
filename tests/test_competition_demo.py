import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.main import create_app
from scripts.init_demo import initialize_demo


class CompetitionDemoTest(unittest.TestCase):
    def test_synthetic_demo_initializes_idempotently_and_workspace_loads(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "LLM_PROVIDER": "",
                "PROJECT_AGENT_STORE_DIR": tmpdir,
                "MEMORY_EMBEDDING": "off",
            },
            clear=False,
        ):
            first = initialize_demo(tmpdir)
            second = initialize_demo(tmpdir)
            with TestClient(create_app(tmpdir)) as client:
                response = client.get("/api/workspace", headers={"X-Actor-Id": "u_pmo"})
                users = client.get(
                    "/api/dev/switchable-users",
                    headers={"X-Actor-Id": "u_pmo"},
                ).json()["users"]
                named_pmo_workspaces = [
                    client.get(
                        "/api/workspace",
                        headers={"X-Actor-Id": user["user_id"]},
                    ).json()
                    for user in users
                    if user["role"] == "pmo"
                ]

            self.assertTrue(first["initialized"])
            self.assertTrue(first["synthetic"])
            self.assertFalse(second["initialized"])
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertIn("星河钢铁协同平台演示项目", str(payload))
            self.assertIn("synthetic_demo", str(payload))
            self.assertEqual(len(payload["confirmation_cards"]), 3)
            self.assertEqual(
                {card["category"] for card in payload["confirmation_cards"]},
                {"task"},
            )
            self.assertTrue(named_pmo_workspaces)
            self.assertTrue(
                all(len(workspace["confirmation_cards"]) == 3 for workspace in named_pmo_workspaces)
            )

    def test_runtime_config_api_never_echoes_api_key(self):
        secret = "judge-owned-secret-value"
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"LLM_PROVIDER": "", "PROJECT_AGENT_STORE_DIR": tmpdir},
            clear=False,
        ):
            initialize_demo(tmpdir)
            with TestClient(create_app(tmpdir)) as client:
                response = client.put(
                    "/api/runtime/provider-config",
                    json={
                        "provider": "openai_compatible",
                        "model": "judge-model",
                        "api_key": secret,
                        "base_url": "https://models.example.invalid/v1",
                        "api_surface": "chat_completions",
                    },
                )
                client.delete("/api/runtime/provider-config")

            self.assertEqual(response.status_code, 200)
            self.assertNotIn(secret, response.text)
            self.assertTrue(response.json()["api_key_configured"])


if __name__ == "__main__":
    unittest.main()
