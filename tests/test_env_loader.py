import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.env_loader import load_env_file


class EnvLoaderTest(unittest.TestCase):
    def test_load_env_file_sets_values_without_overriding_existing_environment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "\n".join([
                    "# local secrets",
                    "LLM_PROVIDER=openai_compatible",
                    "OPENAI_COMPAT_MODEL=glm-5.2",
                    "OPENAI_COMPAT_API_KEY=sk-from-file",
                    "QUOTED_VALUE=\"quoted text\"",
                    "MALFORMED",
                ]),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"OPENAI_COMPAT_API_KEY": "sk-existing"}, clear=True):
                loaded = load_env_file(env_path)

                self.assertEqual(os.environ["LLM_PROVIDER"], "openai_compatible")
                self.assertEqual(os.environ["OPENAI_COMPAT_MODEL"], "glm-5.2")
                self.assertEqual(os.environ["OPENAI_COMPAT_API_KEY"], "sk-existing")
                self.assertEqual(os.environ["QUOTED_VALUE"], "quoted text")
                self.assertEqual(loaded["OPENAI_COMPAT_API_KEY"], "sk-existing")


if __name__ == "__main__":
    unittest.main()
