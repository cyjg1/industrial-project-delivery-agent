import os
import unittest
from unittest.mock import patch

from agent.runtime_provider_config import (
    RuntimeProviderConfigurationError,
    clear_runtime_provider,
    configure_runtime_provider,
    redact_runtime_error,
    runtime_provider_status,
)


RUNTIME_ENV = {
    "LLM_PROVIDER": "",
    "RUNTIME_PROVIDER_SERVICE": "",
    "OPENAI_API_KEY": "",
    "OPENAI_COMPAT_API_KEY": "",
    "DASHSCOPE_API_KEY": "",
    "GLM_API_KEY": "",
    "ALIYUN_API_KEY": "",
    "OPENAI_MODEL": "",
    "OPENAI_COMPAT_MODEL": "",
    "GLM_MODEL": "",
    "AGENT_MAIN_MODEL": "",
    "AGENT_FAST_MODEL": "",
    "AGENT_CONVERSATION_MODELS": "",
    "OPENAI_COMPAT_BASE_URL": "",
    "GLM_BASE_URL": "",
}


class RuntimeProviderConfigTest(unittest.TestCase):
    def test_config_is_process_only_and_status_never_returns_secret(self):
        secret = "competition-secret-value"
        with patch.dict(os.environ, RUNTIME_ENV, clear=False):
            status = configure_runtime_provider(
                provider="openai_compatible",
                model="demo-model",
                api_key=secret,
                base_url="https://models.example.invalid/v1",
            )

            self.assertTrue(status["configured"])
            self.assertTrue(status["api_key_configured"])
            self.assertEqual(status["persistence"], "process_memory")
            self.assertNotIn(secret, repr(status))
            self.assertEqual(os.environ["OPENAI_COMPAT_API_KEY"], secret)

            cleared = clear_runtime_provider()
            self.assertFalse(cleared["configured"])
            self.assertNotIn("OPENAI_COMPAT_API_KEY", os.environ)

    def test_existing_key_can_be_reused_without_echoing_it_to_the_browser(self):
        with patch.dict(os.environ, RUNTIME_ENV, clear=False):
            configure_runtime_provider(
                provider="glm",
                model="first-model",
                api_key="private-key",
            )
            status = configure_runtime_provider(provider="glm", model="second-model")

            self.assertEqual(status["model"], "second-model")
            self.assertTrue(status["api_key_configured"])
            self.assertNotIn("private-key", repr(status))

    def test_domestic_service_preset_is_preserved_while_using_compatible_provider(self):
        with patch.dict(os.environ, RUNTIME_ENV, clear=False):
            status = configure_runtime_provider(
                provider="volcengine_ark",
                model="reviewer-endpoint-id",
                api_key="private-key",
            )

            self.assertEqual(status["provider"], "volcengine_ark")
            self.assertEqual(os.environ["LLM_PROVIDER"], "openai_compatible")
            self.assertEqual(
                status["base_url"],
                "https://ark.cn-beijing.volces.com/api/v3",
            )

    def test_remote_plain_http_and_embedded_credentials_are_rejected(self):
        with patch.dict(os.environ, RUNTIME_ENV, clear=False):
            with self.assertRaises(RuntimeProviderConfigurationError):
                configure_runtime_provider(
                    provider="openai_compatible",
                    model="demo-model",
                    api_key="private-key",
                    base_url="http://models.example.invalid/v1",
                )
            with self.assertRaises(RuntimeProviderConfigurationError):
                configure_runtime_provider(
                    provider="openai_compatible",
                    model="demo-model",
                    api_key="private-key",
                    base_url="https://user:password@models.example.invalid/v1",
                )

    def test_loopback_http_is_allowed_for_local_model_servers(self):
        with patch.dict(os.environ, RUNTIME_ENV, clear=False):
            status = configure_runtime_provider(
                provider="openai_compatible",
                model="local-model",
                api_key="local-placeholder",
                base_url="http://127.0.0.1:11434/v1",
            )
            self.assertTrue(status["configured"])

    def test_error_redaction_removes_current_secret(self):
        with patch.dict(os.environ, RUNTIME_ENV, clear=False):
            configure_runtime_provider(
                provider="openai",
                model="demo-model",
                api_key="do-not-leak-this-value",
            )
            message = redact_runtime_error(
                RuntimeError("request failed api_key=do-not-leak-this-value")
            )
            self.assertNotIn("do-not-leak-this-value", message)
            self.assertIn("[REDACTED]", message)

    def test_unconfigured_status_is_safe(self):
        with patch.dict(os.environ, RUNTIME_ENV, clear=False):
            clear_runtime_provider()
            self.assertEqual(runtime_provider_status()["provider"], "")


if __name__ == "__main__":
    unittest.main()
