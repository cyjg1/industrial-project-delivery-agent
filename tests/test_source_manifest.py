import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ingestion.source_manifest import (
    build_default_manifest,
    discover_obsidian_sources,
    obsidian_sync_status,
    read_curated_text,
    register_uploaded_meeting_note,
)


class SourceManifestTest(unittest.TestCase):
    def test_public_default_has_no_built_in_private_sources(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "PROJECT_AGENT_UPLOAD_ROOT": tmpdir,
                "PROJECT_AGENT_UPLOAD_MANIFEST": str(Path(tmpdir) / "manifest.json"),
                "OBSIDIAN_VAULT_PATH": "",
            },
            clear=False,
        ):
            self.assertEqual(build_default_manifest(), [])

    def test_uploaded_meeting_note_is_content_addressed(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "PROJECT_AGENT_UPLOAD_ROOT": tmpdir,
                "PROJECT_AGENT_UPLOAD_MANIFEST": str(Path(tmpdir) / "manifest.json"),
                "OBSIDIAN_VAULT_PATH": "",
            },
            clear=False,
        ):
            source = register_uploaded_meeting_note(
                "demo-meeting.md",
                "# 虚构项目周会\n接口样例和验收口径仍需确认。".encode("utf-8"),
                title="虚构项目周会",
                meeting_date="2026-08-01",
                topic="接口联调",
                access_tags={
                    "org_id": "org_demo",
                    "project_id": "project_demo",
                    "author_id": "u_demo_pm",
                    "sensitivity": "l1",
                },
            )
            manifest = build_default_manifest()
            persisted = json.loads(
                (Path(tmpdir) / "manifest.json").read_text(encoding="utf-8")
            )["sources"][0]

            self.assertEqual([row.doc_id for row in manifest], [source.doc_id])
            self.assertIn("验收口径", read_curated_text(source))
            self.assertIn("${PROJECT_AGENT_UPLOAD_ROOT}", persisted["curated_source"])
            self.assertEqual(source.tag_origin, "actor_ingest")

    def test_same_filename_keeps_immutable_versions(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "PROJECT_AGENT_UPLOAD_ROOT": tmpdir,
                "PROJECT_AGENT_UPLOAD_MANIFEST": str(Path(tmpdir) / "manifest.json"),
            },
            clear=False,
        ):
            first = register_uploaded_meeting_note(
                "weekly.txt", b"first synthetic transcript", input_kind="transcript"
            )
            second = register_uploaded_meeting_note(
                "weekly.txt", b"second synthetic transcript", input_kind="transcript"
            )

            self.assertEqual(first.doc_id, second.doc_id)
            self.assertNotEqual(first.raw_source.path, second.raw_source.path)
            self.assertTrue(Path(first.raw_source.path or "").exists())
            self.assertTrue(Path(second.raw_source.path or "").exists())

    def test_unsupported_transcript_format_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"PROJECT_AGENT_UPLOAD_ROOT": tmpdir},
            clear=False,
        ):
            with self.assertRaisesRegex(ValueError, "Unsupported transcript format"):
                register_uploaded_meeting_note(
                    "meeting.wav", b"not audio", input_kind="transcript"
                )

    def test_external_markdown_is_opt_in(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vault = Path(tmpdir) / "vault"
            vault.mkdir()
            (vault / "20260801_demo.md").write_text(
                "# 外部演示会议\n这是用户主动配置的外部资料。",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"OBSIDIAN_VAULT_PATH": str(vault)}, clear=False):
                sources = discover_obsidian_sources()
                status = obsidian_sync_status()

            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0].meeting_date, "2026-08-01")
            self.assertTrue(status["configured"])


if __name__ == "__main__":
    unittest.main()

