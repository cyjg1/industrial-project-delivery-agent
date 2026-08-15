import os
import tempfile
import unittest
from unittest.mock import patch

from agent.llm_provider import get_provider
from agent.runtime import AgentRuntime
from store.sqlite_store import ProjectSQLiteStore


class ModelCatalogTest(unittest.TestCase):
    def test_catalog_uses_server_allowlist_and_keeps_default_first(self):
        from agent.model_catalog import conversation_model_catalog

        with patch.dict(
            os.environ,
            {
                "LLM_PROVIDER": "openai_compatible",
                "OPENAI_COMPAT_MODEL": "glm-5.2",
                "AGENT_CONVERSATION_MODELS": (
                    "qwen3.5-plus, glm-5.2, qwen3.5-plus, deepseek-v3.2"
                ),
            },
            clear=False,
        ):
            catalog = conversation_model_catalog()

        self.assertEqual(catalog["default_model"], "glm-5.2")
        self.assertEqual(
            [row["id"] for row in catalog["models"]],
            ["glm-5.2", "qwen3.5-plus", "deepseek-v3.2"],
        )
        self.assertEqual(catalog["provider"], "openai_compatible")

    def test_unknown_model_is_rejected_without_leaking_provider_details(self):
        from agent.model_catalog import require_conversation_model

        with patch.dict(
            os.environ,
            {
                "LLM_PROVIDER": "openai_compatible",
                "OPENAI_COMPAT_MODEL": "glm-5.2",
                "AGENT_CONVERSATION_MODELS": "glm-5.2,qwen3.5-plus",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "Conversation model is not enabled",
            ):
                require_conversation_model("unlisted-private-model")

    def test_provider_and_runtime_honor_validated_model_override(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "LLM_PROVIDER": "openai_compatible",
                "OPENAI_COMPAT_API_KEY": "test-key",
                "OPENAI_COMPAT_BASE_URL": "https://example.invalid/v1",
                "OPENAI_COMPAT_MODEL": "glm-5.2",
            },
            clear=False,
        ):
            provider = get_provider(
                "conversation",
                model_override="qwen3.5-plus",
            )
            calls: list[tuple[str, str]] = []

            def provider_factory(role: str, model: str = ""):
                calls.append((role, model))
                return provider

            runtime = AgentRuntime(
                tool_registry=None,
                store=ProjectSQLiteStore(tmpdir),
                provider_factory=provider_factory,
            )
            selected = runtime.provider(
                "conversation",
                model="qwen3.5-plus",
            )

        self.assertEqual(provider.model, "qwen3.5-plus")
        self.assertIs(selected, provider)
        self.assertEqual(calls, [("conversation", "qwen3.5-plus")])


if __name__ == "__main__":
    unittest.main()
