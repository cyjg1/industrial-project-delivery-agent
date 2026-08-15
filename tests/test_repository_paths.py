import tempfile
import unittest
from os import environ
from pathlib import Path
from unittest.mock import patch

from agent.repository_paths import (
    REPOSITORY_ROOT,
    normalize_repository_paths,
    portable_repository_path,
    resolve_repository_path,
)
from agent.project_people import project_people_asset_path
from store.sqlite_store import ProjectSQLiteStore


class RepositoryPathsTest(unittest.TestCase):
    def test_repository_files_are_stored_relative_and_resolved_from_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            source = root / "data" / "sources" / "meeting.md"

            portable = portable_repository_path(source, root=root)
            resolved = resolve_repository_path(portable, root=root)

        self.assertEqual(portable, "data/sources/meeting.md")
        self.assertEqual(resolved, source)

    def test_external_absolute_path_is_not_rewritten_as_a_fake_repository_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            external = root.parent / "external" / "meeting.md"

            portable = portable_repository_path(external, root=root)

        self.assertEqual(portable, str(external))

    def test_nested_repository_paths_are_normalized_without_touching_external_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            external = str(root.parent / "external-source.md")
            payload = {
                "workspace": {
                    "directory": str(root / "data" / "store" / "memory"),
                    "files": [str(root / "data" / "store" / "memory" / "index.json")],
                },
                "external": external,
            }

            normalized = normalize_repository_paths(payload, root=root)

        self.assertEqual(normalized["workspace"]["directory"], "data/store/memory")
        self.assertEqual(normalized["workspace"]["files"], ["data/store/memory/index.json"])
        self.assertEqual(normalized["external"], external)

    def test_non_path_business_text_is_never_normalized(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            repository_text = str(root / "data" / "sources" / "meeting.md")
            payload = {
                "title": repository_text,
                "note": "~definitely-not-a-local-user/file",
                "summary": repository_text,
                "path": repository_text,
            }

            normalized = normalize_repository_paths(payload, root=root)

        self.assertEqual(normalized["title"], repository_text)
        self.assertEqual(normalized["note"], "~definitely-not-a-local-user/file")
        self.assertEqual(normalized["summary"], repository_text)
        self.assertEqual(normalized["path"], "data/sources/meeting.md")

    def test_storage_file_manifest_normalizes_its_named_paths(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        payload = {
            "storage_files": {
                "database": str(root / "data" / "store" / "project.db"),
                "vault_dir": str(root / "data" / "vault"),
                "summary": str(root / "data" / "store" / "memory" / "summary.json"),
                "events": str(root / "data" / "store" / "memory" / "events.jsonl"),
            }
        }

        normalized = normalize_repository_paths(payload, root=root)

        self.assertEqual(normalized["storage_files"]["database"], "data/store/project.db")
        self.assertEqual(normalized["storage_files"]["vault_dir"], "data/vault")
        self.assertEqual(normalized["storage_files"]["summary"], "data/store/memory/summary.json")
        self.assertEqual(normalized["storage_files"]["events"], "data/store/memory/events.jsonl")

        stored = normalize_repository_paths(
            {
                "stored_files": {
                    "lexicon": str(root / "data" / "skills" / "meeting_minutes" / "lexicon.json"),
                    "runs": str(root / "data" / "skills" / "meeting_minutes" / "runs.jsonl"),
                }
            },
            root=root,
        )
        self.assertEqual(stored["stored_files"]["lexicon"], "data/skills/meeting_minutes/lexicon.json")
        self.assertEqual(stored["stored_files"]["runs"], "data/skills/meeting_minutes/runs.jsonl")

    def test_explicit_root_aliases_round_trip_independent_directories(self):
        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as output_tmp:
            source_root = Path(source_tmp).resolve()
            output_root = Path(output_tmp).resolve()
            payload = {
                "source_path": str(source_root / "meeting.md"),
                "artifact_path": str(output_root / "result.json"),
            }
            roots = {
                "PROJECT_AGENT_SOURCE_DIR": source_root,
                "PROJECT_AGENT_OUTPUT_DIR": output_root,
            }

            normalized = normalize_repository_paths(payload, configured_roots=roots)
            source = resolve_repository_path(normalized["source_path"], configured_roots=roots)
            artifact = resolve_repository_path(normalized["artifact_path"], configured_roots=roots)

        self.assertEqual(normalized["source_path"], "${PROJECT_AGENT_SOURCE_DIR}/meeting.md")
        self.assertEqual(normalized["artifact_path"], "${PROJECT_AGENT_OUTPUT_DIR}/result.json")
        self.assertEqual(source, source_root / "meeting.md")
        self.assertEqual(artifact, output_root / "result.json")

    def test_nested_root_alias_uses_the_most_specific_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_root = Path(tmpdir).resolve() / "source"
            output_root = source_root / "output"
            payload = {"artifact_path": str(output_root / "result.json")}

            normalized = normalize_repository_paths(
                payload,
                configured_roots={
                    "PROJECT_AGENT_SOURCE_DIR": source_root,
                    "PROJECT_AGENT_OUTPUT_DIR": output_root,
                },
            )

        self.assertEqual(
            normalized["artifact_path"],
            "${PROJECT_AGENT_OUTPUT_DIR}/result.json",
        )

    def test_store_connections_enable_full_secure_delete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(Path(tmpdir) / "store")
            with store._connect() as conn:
                secure_delete = conn.execute("pragma secure_delete").fetchone()[0]

        self.assertEqual(secure_delete, 1)

    def test_configured_external_root_is_stored_as_environment_reference(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vault = Path(tmpdir).resolve() / "obsidian"
            source = vault / "meetings" / "minutes.md"
            with patch.dict(environ, {"OBSIDIAN_VAULT_PATH": str(vault)}, clear=False):
                normalized = normalize_repository_paths({"path": str(source)})
                resolved = resolve_repository_path(normalized["path"])

        self.assertEqual(normalized["path"], "${OBSIDIAN_VAULT_PATH}/meetings/minutes.md")
        self.assertEqual(resolved, source)

    def test_path_resolution_rejects_unapproved_variables_and_parent_traversal(self):
        with self.assertRaises(ValueError):
            resolve_repository_path("${HOME}/.ssh/id_rsa")
        with self.assertRaises(ValueError):
            resolve_repository_path("../../outside")
        with tempfile.TemporaryDirectory() as tmpdir:
            vault = Path(tmpdir).resolve() / "obsidian"
            with patch.dict(environ, {"OBSIDIAN_VAULT_PATH": str(vault)}, clear=False):
                with self.assertRaises(ValueError):
                    resolve_repository_path("${OBSIDIAN_VAULT_PATH}/../outside")

    def test_caller_can_authorize_a_managed_runtime_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir).resolve()
            source = runtime_root / "uploads" / "meeting.md"

            with self.assertRaises(ValueError):
                resolve_repository_path(source)

            resolved = resolve_repository_path(source, allowed_roots=(runtime_root,))

        self.assertEqual(resolved, source)

    def test_people_asset_direct_path_resolves_from_repository_root(self):
        class Store:
            def list_sources(self):
                return [
                    {
                        "id": "people_source",
                        "kind": "people_structure",
                        "project_id": "project_mvp",
                        "payload": {"people_asset_path": "data/assets/people_structure.json"},
                    }
                ]

        path = project_people_asset_path(Store(), "project_mvp")

        self.assertEqual(path, REPOSITORY_ROOT / "data/assets/people_structure.json")
        self.assertTrue(path.is_file())

    def test_source_store_normalizes_repository_absolute_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(Path(tmpdir) / "store")
            repository_file = Path.cwd() / "data" / "sources" / "meeting.md"

            store.save_source(
                {
                    "doc_id": "portable_source",
                    "title": "Portable source",
                    "curated_source": {"path": str(repository_file), "status": "matched"},
                    "raw_source": {"path": "", "status": "raw_source_pending"},
                }
            )
            source = store.get_source("portable_source")

        self.assertEqual(source["path"], "data/sources/meeting.md")
        self.assertEqual(source["payload"]["curated_source"]["path"], "data/sources/meeting.md")

    def test_message_metadata_persistence_preserves_non_path_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(Path(tmpdir) / "store")
            session = store.ensure_session(title="Path safety")
            repository_text = str(REPOSITORY_ROOT / "data" / "sources" / "meeting.md")
            store.append_session_message(
                session["session_id"],
                "assistant",
                "done",
                metadata={
                    "summary": repository_text,
                    "note": "~definitely-not-a-local-user/file",
                    "path": repository_text,
                },
            )

            metadata = store.list_session_messages(session["session_id"])[0]["metadata"]

        self.assertEqual(metadata["summary"], repository_text)
        self.assertEqual(metadata["note"], "~definitely-not-a-local-user/file")
        self.assertEqual(metadata["path"], "data/sources/meeting.md")


if __name__ == "__main__":
    unittest.main()
