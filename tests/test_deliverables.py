from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent.access_policy import User
from agent.deliverables import (
    DeliverableService,
    DeliverableUpload,
    DeliverableValidationError,
)
from backend.main import create_app
from store.sqlite_store import ProjectSQLiteStore


class DeliverableServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = ProjectSQLiteStore(self.tmpdir.name)
        self.store.upsert_org("org_main", "Main")
        self.store.upsert_org("org_other", "Other")
        self.store.upsert_user("u_pm", "org_main", "PM")
        self.store.upsert_user("u_exec", "org_main", "Exec")
        self.store.upsert_user("u_out", "org_other", "Out")
        self.store.upsert_project("project_main", "org_main", "Project", "u_pm")
        self.store.upsert_project_member("project_main", "u_pm", "pm")
        self.store.upsert_project_member("project_main", "u_exec", "exec", manager_id="u_pm")
        self.pm = User("u_pm", "org_main", "PM")
        self.exec = User("u_exec", "org_main", "Exec")
        self.out = User("u_out", "org_other", "Out")
        self.service = DeliverableService(self.store)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_pm_creates_dynamic_requirement_linked_to_milestone(self):
        row = self._create_requirement(title="用户自由配置的接口设计成果")

        self.assertEqual(row["title"], "用户自由配置的接口设计成果")
        self.assertEqual(row["milestone_id"], "milestone_alpha")
        self.assertEqual(row["project_id"], "project_main")
        self.assertEqual(row["author_id"], "u_pm")
        self.assertEqual(row["sensitivity"], "l1")
        self.assertEqual(row["status"], "expected")
        self.assertEqual(row["current_version"], 0)

    def test_non_management_actor_cannot_create_or_edit_requirement(self):
        with self.assertRaises(PermissionError):
            self.service.create_requirement(
                actor=self.exec,
                access_context=self.store.access_context_for_actor("u_exec"),
                project_id="project_main",
                milestone_id="milestone_alpha",
                title="执行人自行添加",
                type_label="自由类型",
                acceptance_criteria="人工验收",
            )

        row = self._create_requirement()
        with self.assertRaises(PermissionError):
            self.service.update_requirement(
                row["deliverable_id"],
                actor=self.exec,
                access_context=self.store.access_context_for_actor("u_exec"),
                patch={"title": "越权修改"},
            )

    def test_visible_member_submission_creates_immutable_hashed_version(self):
        row = self._create_requirement()
        formal = b"formal deliverable bytes"
        process = b"process evidence bytes"

        submitted = self.service.submit_version(
            row["deliverable_id"],
            actor=self.exec,
            access_context=self.store.access_context_for_actor("u_exec"),
            files=[
                DeliverableUpload("正式成果.docx", formal, "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "formal"),
                DeliverableUpload("评审过程.txt", process, "text/plain", "process"),
            ],
            note="第一次提交",
        )

        self.assertEqual(submitted["version"], 1)
        self.assertEqual(submitted["submitted_by"], "u_exec")
        self.assertEqual([file["artifact_role"] for file in submitted["files"]], ["formal", "process"])
        for file_row, expected in zip(submitted["files"], (formal, process)):
            self.assertEqual(file_row["content_hash"], hashlib.sha256(expected).hexdigest())
            self.assertNotIn(str(Path(self.tmpdir.name)), file_row["relative_path"])
            resolved = self.service.resolve_download(
                file_row["file_id"],
                actor=self.exec,
                access_context=self.store.access_context_for_actor("u_exec"),
            )
            self.assertEqual(resolved.read_bytes(), expected)
            self.assertIn(
                f"artifacts/deliverables/project_main/{row['deliverable_id']}/v1",
                resolved.as_posix(),
            )

        listed = self.service.list_requirements(
            actor=self.exec,
            access_context=self.store.access_context_for_actor("u_exec"),
            project_id="project_main",
        )
        self.assertEqual(listed[0]["current_version"], 1)
        self.assertEqual(listed[0]["status"], "submitted")
        self.assertEqual(len(listed[0]["versions"][0]["files"]), 2)

    def test_each_submission_increments_version_without_mutating_previous_bytes(self):
        row = self._create_requirement()
        first = self.service.submit_version(
            row["deliverable_id"],
            actor=self.exec,
            access_context=self.store.access_context_for_actor("u_exec"),
            files=[DeliverableUpload("成果.txt", b"v1", "text/plain", "formal")],
        )
        first_path = self.service.resolve_download(
            first["files"][0]["file_id"],
            actor=self.pm,
            access_context=self.store.access_context_for_actor("u_pm"),
        )
        second = self.service.submit_version(
            row["deliverable_id"],
            actor=self.exec,
            access_context=self.store.access_context_for_actor("u_exec"),
            files=[DeliverableUpload("成果.txt", b"v2", "text/plain", "formal")],
        )

        self.assertEqual(second["version"], 2)
        self.assertEqual(first_path.read_bytes(), b"v1")
        self.assertNotEqual(first["files"][0]["relative_path"], second["files"][0]["relative_path"])

    def test_concurrent_submissions_reserve_distinct_versions_and_keep_every_file(self):
        row = self._create_requirement()
        barrier = Barrier(4)

        def submit(index: int):
            content = f"concurrent-{index}".encode("utf-8")
            barrier.wait()
            version = self.service.submit_version(
                row["deliverable_id"],
                actor=self.exec,
                access_context=self.store.access_context_for_actor("u_exec"),
                files=[DeliverableUpload("成果.txt", content, "text/plain", "formal")],
            )
            return version, content

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(submit, range(4)))

        self.assertEqual({version["version"] for version, _ in results}, {1, 2, 3, 4})
        for version, expected in results:
            resolved = self.service.resolve_download(
                version["files"][0]["file_id"],
                actor=self.pm,
                access_context=self.store.access_context_for_actor("u_pm"),
            )
            self.assertEqual(resolved.read_bytes(), expected)

    def test_scope_revocation_between_precheck_and_reservation_rejects_submission(self):
        row = self._create_requirement()
        original_reserve = self.store.reserve_deliverable_version_record

        def revoke_then_reserve(*args, **kwargs):
            self.service.update_requirement(
                row["deliverable_id"],
                actor=self.pm,
                access_context=self.store.access_context_for_actor("u_pm"),
                patch={"sensitivity": "l4"},
            )
            return original_reserve(*args, **kwargs)

        with patch.object(
            self.store,
            "reserve_deliverable_version_record",
            side_effect=revoke_then_reserve,
        ):
            with self.assertRaises(PermissionError):
                self.service.submit_version(
                    row["deliverable_id"],
                    actor=self.exec,
                    access_context=self.store.access_context_for_actor("u_exec"),
                    files=[DeliverableUpload("成果.txt", b"must not commit", "text/plain", "formal")],
                )

        self.assertEqual(self.store.list_deliverable_versions(row["deliverable_id"]), [])

    def test_atomic_audit_failure_rolls_back_metadata_and_removes_uncommitted_file(self):
        row = self._create_requirement()

        with patch.object(
            self.store,
            "_insert_event",
            side_effect=RuntimeError("audit unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
                self.service.submit_version(
                    row["deliverable_id"],
                    actor=self.exec,
                    access_context=self.store.access_context_for_actor("u_exec"),
                    files=[DeliverableUpload("成果.txt", b"uncommitted", "text/plain", "formal")],
                )

        self.assertEqual(self.store.list_deliverable_versions(row["deliverable_id"]), [])
        artifact_root = Path(self.tmpdir.name) / "artifacts" / "deliverables"
        self.assertEqual(
            [path for path in artifact_root.rglob("*") if path.is_file()]
            if artifact_root.exists() else [],
            [],
        )

    def test_exception_after_commit_never_deletes_committed_artifact(self):
        row = self._create_requirement()
        original_complete = self.store.complete_deliverable_version_record

        def commit_then_raise(*args, **kwargs):
            original_complete(*args, **kwargs)
            raise RuntimeError("response failed after commit")

        with patch.object(
            self.store,
            "complete_deliverable_version_record",
            side_effect=commit_then_raise,
        ):
            with self.assertRaisesRegex(RuntimeError, "response failed after commit"):
                self.service.submit_version(
                    row["deliverable_id"],
                    actor=self.exec,
                    access_context=self.store.access_context_for_actor("u_exec"),
                    files=[DeliverableUpload("成果.txt", b"committed", "text/plain", "formal")],
                )

        submitted = self.store.list_deliverable_versions(row["deliverable_id"])
        self.assertEqual(len(submitted), 1)
        stored_path = Path(self.tmpdir.name) / submitted[0]["files"][0]["relative_path"]
        self.assertEqual(stored_path.read_bytes(), b"committed")

    def test_requirement_scope_change_revokes_access_to_existing_versions_and_files(self):
        row = self._create_requirement()
        submitted = self.service.submit_version(
            row["deliverable_id"],
            actor=self.exec,
            access_context=self.store.access_context_for_actor("u_exec"),
            files=[DeliverableUpload("成果.txt", b"classified later", "text/plain", "formal")],
        )
        file_id = submitted["files"][0]["file_id"]
        self.assertTrue(self.service.resolve_download(
            file_id,
            actor=self.exec,
            access_context=self.store.access_context_for_actor("u_exec"),
        ).is_file())

        self.service.update_requirement(
            row["deliverable_id"],
            actor=self.pm,
            access_context=self.store.access_context_for_actor("u_pm"),
            patch={"sensitivity": "l4"},
        )

        visible = self.service.list_requirements(
            actor=self.exec,
            access_context=self.store.access_context_for_actor("u_exec"),
            project_id="project_main",
        )
        self.assertEqual(visible, [])
        with self.assertRaises(PermissionError):
            self.service.resolve_download(
                file_id,
                actor=self.exec,
                access_context=self.store.access_context_for_actor("u_exec"),
            )

    def test_invalid_role_empty_files_and_outsider_download_fail_closed(self):
        row = self._create_requirement()
        with self.assertRaises(DeliverableValidationError):
            self.service.submit_version(
                row["deliverable_id"],
                actor=self.exec,
                access_context=self.store.access_context_for_actor("u_exec"),
                files=[],
            )
        with self.assertRaises(DeliverableValidationError):
            self.service.submit_version(
                row["deliverable_id"],
                actor=self.exec,
                access_context=self.store.access_context_for_actor("u_exec"),
                files=[DeliverableUpload("成果.txt", b"x", "text/plain", "unknown")],
            )
        submitted = self.service.submit_version(
            row["deliverable_id"],
            actor=self.exec,
            access_context=self.store.access_context_for_actor("u_exec"),
            files=[DeliverableUpload("成果.txt", b"x", "text/plain", "formal")],
        )
        with self.assertRaises(PermissionError):
            self.service.resolve_download(
                submitted["files"][0]["file_id"],
                actor=self.out,
                access_context=self.store.access_context_for_actor("u_out"),
            )

    def test_oversized_deliverable_is_rejected_before_a_version_is_reserved(self):
        row = self._create_requirement()
        with patch.dict(os.environ, {"DELIVERABLE_MAX_FILE_BYTES": "4"}, clear=False):
            with self.assertRaisesRegex(DeliverableValidationError, "exceeds configured limit"):
                self.service.submit_version(
                    row["deliverable_id"],
                    actor=self.exec,
                    access_context=self.store.access_context_for_actor("u_exec"),
                    files=[DeliverableUpload("large.bin", b"12345", "application/octet-stream", "formal")],
                )
        self.assertEqual(self.store.list_deliverable_versions(row["deliverable_id"]), [])

    def test_aggregate_deliverable_bytes_are_bounded_before_version_reservation(self):
        row = self._create_requirement()
        with patch.dict(
            os.environ,
            {
                "DELIVERABLE_MAX_FILE_BYTES": "10",
                "DELIVERABLE_MAX_TOTAL_BYTES": "6",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(DeliverableValidationError, "total limit"):
                self.service.submit_version(
                    row["deliverable_id"],
                    actor=self.exec,
                    access_context=self.store.access_context_for_actor("u_exec"),
                    files=[
                        DeliverableUpload("part-a.txt", b"1234", "text/plain", "formal"),
                        DeliverableUpload("part-b.txt", b"5678", "text/plain", "formal"),
                    ],
                )
        self.assertEqual(self.store.list_deliverable_versions(row["deliverable_id"]), [])

    def test_api_stops_reading_an_oversized_upload_and_returns_413(self):
        with patch.dict(os.environ, {"DELIVERABLE_MAX_FILE_BYTES": "4"}, clear=False):
            with TestClient(create_app(store_dir=self.tmpdir.name)) as client:
                created = client.post(
                    "/api/deliverables",
                    headers={"X-Actor-Id": "u_pm"},
                    json={"title": "限额验证"},
                )
                deliverable_id = created.json()["deliverable"]["deliverable_id"]
                response = client.post(
                    f"/api/deliverables/{deliverable_id}/versions",
                    headers={"X-Actor-Id": "u_exec"},
                    files={"files": ("large.bin", b"12345", "application/octet-stream")},
                )

        self.assertEqual(response.status_code, 413)
        self.assertIn("configured limit", response.text)

    def test_api_rejects_more_than_twenty_files_before_version_creation(self):
        with TestClient(create_app(store_dir=self.tmpdir.name)) as client:
            created = client.post(
                "/api/deliverables",
                headers={"X-Actor-Id": "u_pm"},
                json={"title": "文件数量限制"},
            )
            deliverable_id = created.json()["deliverable"]["deliverable_id"]
            response = client.post(
                f"/api/deliverables/{deliverable_id}/versions",
                headers={"X-Actor-Id": "u_exec"},
                files=[
                    ("files", (f"part-{index}.txt", b"x", "text/plain"))
                    for index in range(21)
                ],
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("20", response.text)
        self.assertEqual(self.store.list_deliverable_versions(deliverable_id), [])

    def test_conversation_upload_uses_the_same_streaming_byte_limit(self):
        with patch.dict(os.environ, {"DELIVERABLE_MAX_FILE_BYTES": "4"}, clear=False):
            with TestClient(create_app(store_dir=self.tmpdir.name)) as client:
                response = client.post(
                    "/api/conversation",
                    headers={"X-Actor-Id": "u_exec"},
                    data={"message": "归档这个文件", "input_kind": "document"},
                    files={"files": ("large.txt", b"12345", "text/plain")},
                )

        self.assertEqual(response.status_code, 413)
        self.assertIn("configured limit", response.text)

    def test_http_uploads_enforce_aggregate_bytes_for_deliverables_and_conversation(self):
        limits = {
            "DELIVERABLE_MAX_FILE_BYTES": "10",
            "DELIVERABLE_MAX_TOTAL_BYTES": "6",
        }
        with patch.dict(os.environ, limits, clear=False):
            with TestClient(create_app(store_dir=self.tmpdir.name)) as client:
                created = client.post(
                    "/api/deliverables",
                    headers={"X-Actor-Id": "u_pm"},
                    json={"title": "请求总量限制"},
                )
                deliverable_id = created.json()["deliverable"]["deliverable_id"]
                upload_parts = [
                    ("files", ("part-a.txt", b"1234", "text/plain")),
                    ("files", ("part-b.txt", b"5678", "text/plain")),
                ]
                deliverable_response = client.post(
                    f"/api/deliverables/{deliverable_id}/versions",
                    headers={"X-Actor-Id": "u_exec"},
                    files=upload_parts,
                )
                conversation_response = client.post(
                    "/api/conversation",
                    headers={"X-Actor-Id": "u_exec"},
                    data={"message": "归档这些文件", "input_kind": "document"},
                    files=upload_parts,
                )

        self.assertEqual(deliverable_response.status_code, 413)
        self.assertIn("total limit", deliverable_response.text)
        self.assertEqual(conversation_response.status_code, 413)
        self.assertIn("total limit", conversation_response.text)
        self.assertEqual(self.store.list_deliverable_versions(deliverable_id), [])

    def test_api_binds_current_milestone_and_never_exposes_local_file_path(self):
        with TestClient(create_app(store_dir=self.tmpdir.name)) as client:
            created = client.post(
                "/api/deliverables",
                headers={"X-Actor-Id": "u_pm"},
                json={
                    "title": "接口设计成果",
                    "type_label": "项目自定义",
                    "acceptance_criteria": "评审通过",
                    "due_date": "2026-09-30",
                    "required": True,
                },
            )
            deliverable_id = created.json().get("deliverable", {}).get("deliverable_id", "")
            exec_list = client.get("/api/deliverables", headers={"X-Actor-Id": "u_exec"})
            forbidden = client.patch(
                f"/api/deliverables/{deliverable_id}",
                headers={"X-Actor-Id": "u_exec"},
                json={"title": "越权修改"},
            )
            submitted = client.post(
                f"/api/deliverables/{deliverable_id}/versions",
                headers={"X-Actor-Id": "u_exec"},
                data={"artifact_roles": '["formal"]', "note": "对话外直接提交验证"},
                files={"files": ("成果.txt", b"api deliverable", "text/plain")},
            )
            file_id = submitted.json().get("version", {}).get("files", [{}])[0].get("file_id", "")
            download = client.get(
                f"/api/deliverable-files/{file_id}/download",
                headers={"X-Actor-Id": "u_exec"},
            )

        self.assertEqual(created.status_code, 201)
        self.assertTrue(deliverable_id)
        self.assertTrue(created.json()["deliverable"]["milestone_id"])
        self.assertEqual(exec_list.status_code, 200)
        self.assertEqual(len(exec_list.json()["deliverables"]), 1)
        self.assertNotIn(str(Path(self.tmpdir.name)), exec_list.text)
        self.assertNotIn("relative_path", exec_list.text)
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(submitted.status_code, 201)
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, b"api deliverable")

    def _create_requirement(self, title: str = "阶段成果"):
        return self.service.create_requirement(
            actor=self.pm,
            access_context=self.store.access_context_for_actor("u_pm"),
            project_id="project_main",
            milestone_id="milestone_alpha",
            title=title,
            type_label="由项目经理定义",
            acceptance_criteria="评审记录和正式文件齐全",
            due_date="2026-09-30",
            required=True,
        )


if __name__ == "__main__":
    unittest.main()
