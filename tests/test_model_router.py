import os
import unittest
from unittest.mock import patch

from agent.llm_provider import get_provider
from agent.model_router import resolve_model_route


class ModelRouterTest(unittest.TestCase):
    def test_roles_use_main_fast_and_explicit_overrides(self):
        with patch.dict(os.environ, {
            "LLM_PROVIDER": "openai_compatible",
            "OPENAI_COMPAT_API_KEY": "test-key",
            "OPENAI_COMPAT_MODEL": "configured-main",
            "AGENT_MAIN_MODEL": "main-role",
            "AGENT_FAST_MODEL": "fast-role",
            "AGENT_EXTRACTION_MODEL": "extract-role",
        }, clear=True):
            conversation = resolve_model_route("conversation")
            extraction = resolve_model_route("extraction")
            verification = resolve_model_route("verification")

        self.assertEqual(conversation.model, "main-role")
        self.assertEqual(extraction.model, "extract-role")
        self.assertEqual(verification.model, "fast-role")

    def test_fast_roles_fall_back_to_configured_main_model(self):
        with patch.dict(os.environ, {
            "LLM_PROVIDER": "openai_compatible",
            "OPENAI_COMPAT_MODEL": "configured-main",
        }, clear=True):
            self.assertEqual(resolve_model_route("conversation").model, "configured-main")
            self.assertEqual(resolve_model_route("extraction").model, "configured-main")
            self.assertEqual(resolve_model_route("verification").model, "configured-main")

    def test_get_provider_uses_role_model_on_shared_compatible_endpoint(self):
        with patch.dict(os.environ, {
            "LLM_PROVIDER": "openai_compatible",
            "OPENAI_COMPAT_API_KEY": "test-key",
            "OPENAI_COMPAT_BASE_URL": "https://example.invalid/v1",
            "OPENAI_COMPAT_MODEL": "configured-main",
            "AGENT_EXTRACTION_MODEL": "extract-role",
        }, clear=True):
            provider = get_provider(role="extraction")

        self.assertEqual(provider.model, "extract-role")
        self.assertEqual(provider.base_url, "https://example.invalid/v1")

    def test_real_provider_requires_a_configured_model(self):
        with patch.dict(os.environ, {
            "LLM_PROVIDER": "openai_compatible",
            "OPENAI_COMPAT_API_KEY": "test-key",
        }, clear=True):
            with self.assertRaisesRegex(RuntimeError, "model"):
                resolve_model_route("conversation")


if __name__ == "__main__":
    unittest.main()
