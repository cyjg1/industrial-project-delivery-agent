from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent.access_policy import User
from agent.project_files import ProjectFileError, ProjectFileService
from store.sqlite_store import ProjectSQLiteStore


class ProjectFileServiceTest(unittest.TestCase):
    def test_project_document_is_classified_and_indexed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ProjectSQLiteStore(Path(temp_dir))
            service = ProjectFileService(store)
            actor = User(id="u_test", org_id="org_test", name="测试用户")

            folder = service.create_folder(
                project_id="project_test",
                parent="",
                name="需求资料",
            )
            item, job = service.upload(
                project_id="project_test",
                actor=actor,
                folder=folder,
                upload_type="project_document",
                filename="需求说明.txt",
                content="这是项目需求和验收标准。".encode("utf-8"),
            )

            self.assertIsNone(job)
            self.assertEqual("需求资料", item["document_category"])
            self.assertEqual("已建立索引", item["index_status"])
            self.assertEqual(1, service.workspace(project_id="project_test")["summary"]["file_count"])
            self.assertEqual("project_document", store.get_source(item["source_id"])["kind"])

    def test_folder_path_cannot_escape_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ProjectFileService(ProjectSQLiteStore(Path(temp_dir)))
            with self.assertRaises(ProjectFileError):
                service.create_folder(
                    project_id="project_test",
                    parent="../outside",
                    name="资料",
                )


if __name__ == "__main__":
    unittest.main()
