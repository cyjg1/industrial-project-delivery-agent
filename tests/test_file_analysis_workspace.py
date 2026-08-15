import json
import tempfile
import unittest
from pathlib import Path

from agent.file_analysis_workspace import FileAnalysisWorkspace


class FileAnalysisWorkspaceTest(unittest.TestCase):
    @unittest.skip("semantic extraction requires a reviewer-supplied model API")
    def test_source_folder_steps_write_outputs_without_confirmation_gate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_dir = root / "source"
            output_dir = root / "output"
            source_dir.mkdir()
            (source_dir / "20260701_meeting.md").write_text(
                "# 7月1日会议纪要\n\n参会人：项目负责人。\n\n项目负责人需要确认上传即运行，交付物是验证记录。",
                encoding="utf-8",
            )

            workspace = FileAnalysisWorkspace(source_dir=source_dir, output_dir=output_dir)
            initial = workspace.status()

            self.assertEqual(initial["source_dir"], str(source_dir))
            self.assertEqual(initial["output_dir"], str(output_dir))
            self.assertEqual(initial["steps"][0]["step_id"], "01_source_manifest")
            self.assertFalse(initial["steps"][0]["locked"])
            self.assertFalse(initial["steps"][1]["locked"])

            first = workspace.run_step("01_source_manifest")

            self.assertEqual(first["status"], "ready_for_confirmation")
            self.assertTrue((output_dir / "01_source_manifest.json").exists())
            self.assertTrue((output_dir / "01_source_manifest.md").exists())
            manifest = json.loads((output_dir / "01_source_manifest.json").read_text(encoding="utf-8"))
            state = json.loads((output_dir / "analysis_state.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["source_dir"], "${PROJECT_AGENT_SOURCE_DIR}")
            self.assertEqual(
                state["steps"]["01_source_manifest"]["artifact_paths"],
                [
                    "${PROJECT_AGENT_OUTPUT_DIR}/01_source_manifest.json",
                    "${PROJECT_AGENT_OUTPUT_DIR}/01_source_manifest.md",
                ],
            )
            self.assertIn("01 读取 source 文件夹", workspace.status()["steps"][0]["artifact_preview"])
            self.assertEqual(manifest["source_files"][0]["name"], "20260701_meeting.md")
            self.assertEqual(manifest["source_files"][0]["detected_kind"], "会议材料")
            self.assertEqual(first["summary"]["kind_counts"]["会议材料"], 1)

            confirmed = workspace.confirm_step("01_source_manifest", notes="材料清单确认")

            self.assertEqual(confirmed["steps"][0]["status"], "confirmed")
            self.assertFalse(confirmed["steps"][1]["locked"])
            second = workspace.run_step("02_extract_candidates")
            self.assertGreater(second["summary"]["candidate_count"], 0)
            self.assertTrue((output_dir / "02_extract_candidates.json").exists())

    def test_independent_source_and_output_roots_remain_portable(self):
        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as output_tmp:
            source_dir = Path(source_tmp).resolve()
            output_dir = Path(output_tmp).resolve()
            (source_dir / "meeting.md").write_text("# 会议纪要\n\n确认验收口径。", encoding="utf-8")
            workspace = FileAnalysisWorkspace(source_dir=source_dir, output_dir=output_dir)

            workspace.run_step("01_source_manifest")
            manifest = json.loads((output_dir / "01_source_manifest.json").read_text(encoding="utf-8"))
            state = json.loads((output_dir / "analysis_state.json").read_text(encoding="utf-8"))

            self.assertEqual(manifest["source_dir"], "${PROJECT_AGENT_SOURCE_DIR}")
            self.assertEqual(
                state["steps"]["01_source_manifest"]["artifact_paths"][0],
                "${PROJECT_AGENT_OUTPUT_DIR}/01_source_manifest.json",
            )
            self.assertIn("01 读取 source 文件夹", workspace.status()["steps"][0]["artifact_preview"])


if __name__ == "__main__":
    unittest.main()
