import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from agent.access_policy import AccessContext, User
from agent.schemas import CandidateStatus, EvidenceRef, InspectionItem
from store.sqlite_store import ProjectSQLiteStore


def _item(item_id: str, title: str, description: str) -> InspectionItem:
    return InspectionItem(
        item_id=item_id,
        category="things",
        title=title,
        description=description,
        evidence_refs=[
            EvidenceRef(
                source_doc_id="meeting_runtime_upgrade",
                source_kind="curated_source",
                locator="line:1",
                quote=description,
            )
        ],
        status=CandidateStatus.CONFIRMED,
        org_id="org_mvp",
        project_id="project_mvp",
        author_id="u_pm",
        sensitivity="l1",
    )


class MemoryRuntimeUpgradeTest(unittest.TestCase):
    def test_default_hybrid_candidate_pool_is_bounded_to_fifty(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "MEMORY_EMBEDDING": "on",
                "MEMORY_EMBEDDING_MODEL": "test-embedding",
                "GLM_API_KEY": "test-key",
            },
            clear=False,
        ):
            store = ProjectSQLiteStore(tmpdir)
            store.save_item(
                _item("candidate_budget", "候选池预算", "用于验证检索预算。"),
                materialize=False,
            )
            with patch(
                "store.sqlite_store._search_item_matches",
                return_value=[],
            ) as lexical_search, patch.object(
                store,
                "_semantic_item_matches",
                return_value=[],
            ) as semantic_search:
                store.search_memory("预算验证", limit=10)

        self.assertEqual(lexical_search.call_args.kwargs["limit"], 50)
        self.assertEqual(semantic_search.call_args.kwargs["limit"], 50)

    def test_search_diagnostics_expose_hybrid_retrieval_counts_and_access_scope(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "MEMORY_EMBEDDING": "on",
                "MEMORY_EMBEDDING_MODEL": "test-embedding",
                "GLM_API_KEY": "test-key",
            },
            clear=False,
        ):
            store = ProjectSQLiteStore(tmpdir)
            store.save_item(
                _item(
                    "lexical_match",
                    "单据拆分规则",
                    "字段来源和去向需要逐项确认。",
                ),
                materialize=False,
            )
            store.save_item(
                _item(
                    "semantic_match",
                    "票证切割策略",
                    "按财务对象归并后切割并完成验收。",
                ),
                materialize=False,
            )

            def fake_embed(texts: list[str]) -> list[list[float]]:
                return [
                    [1.0, 0.0]
                    if "单据拆分" in text or "票证切割策略" in text
                    else [0.0, 1.0]
                    for text in texts
                ]

            store.process_embedding_batch(
                worker_id="test-worker",
                batch_size=10,
                embedder=fake_embed,
            )
            actor = User(id="u_pm", org_id="org_mvp", name="Project Manager")
            context = AccessContext(
                project_roles={(actor.id, "project_mvp"): "pm"},
            )
            diagnostics: dict[str, object] = {}
            with patch("store.sqlite_store._embed_texts", side_effect=fake_embed):
                rows = store.search_memory(
                    "单据拆分",
                    {"project_id": "project_mvp"},
                    actor=actor,
                    access_context=context,
                    diagnostics=diagnostics,
                )

        self.assertEqual(diagnostics["query"], "单据拆分")
        self.assertEqual(diagnostics["retrieval_mode"], "fts_semantic_union")
        self.assertTrue(diagnostics["access_scope_applied"])
        self.assertEqual(diagnostics["visible_candidate_count"], 2)
        self.assertGreaterEqual(diagnostics["fts_candidate_count"], 1)
        self.assertEqual(diagnostics["semantic_candidate_count"], 2)
        self.assertEqual(diagnostics["union_candidate_count"], 2)
        self.assertEqual(diagnostics["thread_collapsed_count"], 2)
        self.assertEqual(diagnostics["returned_count"], len(rows))
        self.assertEqual(diagnostics["embedding_model"], "test-embedding")
        self.assertEqual(diagnostics["embedding_index"]["indexed"], 2)
        self.assertTrue(diagnostics["embedding_index"]["complete"])
        self.assertEqual(
            diagnostics["filters_applied"],
            ["project_id=project_mvp", "status=confirmed"],
        )
        self.assertFalse(diagnostics["degraded"])
        self.assertEqual(diagnostics["degradation_reasons"], [])

    def test_incomplete_embedding_index_is_explicitly_degraded_but_fts_still_returns(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "MEMORY_EMBEDDING": "on",
                "MEMORY_EMBEDDING_MODEL": "test-embedding",
                "GLM_API_KEY": "test-key",
            },
            clear=False,
        ):
            store = ProjectSQLiteStore(tmpdir)
            store.save_item(
                _item(
                    "pending_index",
                    "单据拆分规则",
                    "字段来源和去向需要逐项确认。",
                ),
                materialize=False,
            )
            diagnostics: dict[str, object] = {}
            with patch(
                "store.sqlite_store._embed_texts",
                return_value=[[1.0, 0.0]],
            ):
                rows = store.search_memory(
                    "单据拆分",
                    diagnostics=diagnostics,
                )

        self.assertEqual(rows[0]["item_id"], "pending_index")
        self.assertTrue(diagnostics["degraded"])
        self.assertEqual(diagnostics["embedding_error"], "")
        self.assertIn(
            "embedding_index_incomplete",
            diagnostics["degradation_reasons"],
        )
        self.assertEqual(diagnostics["semantic_candidate_count"], 0)
        self.assertEqual(diagnostics["embedding_index"]["pending"], 1)

    def test_hybrid_search_unions_semantic_only_candidates_with_fts_candidates(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"MEMORY_EMBEDDING": "on", "GLM_API_KEY": "test-key"},
            clear=False,
        ):
            store = ProjectSQLiteStore(tmpdir)

            def fake_embed(texts: list[str]) -> list[list[float]]:
                vectors: list[list[float]] = []
                for text in texts:
                    if text == "单据拆分":
                        vectors.append([1.0, 0.0])
                    elif "票证切割策略" in text:
                        vectors.append([1.0, 0.0])
                    else:
                        vectors.append([0.0, 1.0])
                return vectors

            with patch("store.sqlite_store._embed_texts", side_effect=fake_embed):
                store.save_item(
                    _item(
                        "lexical_match",
                        "单据拆分规则",
                        "字段来源和去向需要逐项确认。",
                    ),
                    materialize=False,
                )
                store.save_item(
                    _item(
                        "semantic_only",
                        "票证切割策略",
                        "按财务对象归并后切割并完成验收。",
                    ),
                    materialize=False,
                )
                store.process_embedding_batch(
                    worker_id="test-worker",
                    batch_size=10,
                    embedder=fake_embed,
                )
                results = store.search_memory("单据拆分", limit=10)

        result_by_id = {row["item_id"]: row for row in results}
        self.assertIn("lexical_match", result_by_id)
        self.assertIn("semantic_only", result_by_id)
        self.assertIn("semantic", result_by_id["semantic_only"]["match_reasons"])
        self.assertEqual(result_by_id["semantic_only"]["lexical_score"], 0.0)

    def test_embedding_failure_is_explicit_even_when_fts_has_no_matches(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"MEMORY_EMBEDDING": "on", "GLM_API_KEY": "test-key"},
            clear=False,
        ):
            store = ProjectSQLiteStore(tmpdir)
            with patch.dict(
                os.environ,
                {"MEMORY_EMBEDDING": "off"},
                clear=False,
            ):
                store.save_item(
                    _item(
                        "unrelated",
                        "质量门禁",
                        "验收前完成编码校验。",
                    ),
                    materialize=False,
                )
            diagnostics = {}
            with patch(
                "store.sqlite_store._embed_texts",
                side_effect=RuntimeError("embedding endpoint unavailable"),
            ):
                rows = store.search_memory(
                    "票据拆分",
                    diagnostics=diagnostics,
                )

        self.assertEqual(rows, [])
        self.assertTrue(diagnostics["degraded"])
        self.assertEqual(diagnostics["retrieval_mode"], "fts_semantic_union")
        self.assertIn("embedding endpoint unavailable", diagnostics["embedding_error"])

    def test_source_text_is_chunked_with_access_tags_and_searchable_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_path = root / "meeting.md"
            source_path.write_text(
                "# 标准层专题会\n\n"
                "标准层必须先统一编码口径，再完成字段映射和质量门禁。\n\n"
                "上线前由项目经理确认验收样例和回退方案。\n",
                encoding="utf-8",
            )
            store = ProjectSQLiteStore(root / "store")
            store.save_source(
                {
                    "doc_id": "meeting_runtime_upgrade",
                    "title": "标准层专题会",
                    "meeting_date": "2026-07-23",
                    "curated_source": {
                        "source_type": "markdown",
                        "path": str(source_path),
                        "status": "matched",
                    },
                    "raw_source": {
                        "source_type": "transcript",
                        "path": "",
                        "status": "raw_source_pending",
                    },
                    "tags": ["标准层"],
                    "org_id": "org_mvp",
                    "project_id": "project_mvp",
                    "topic_id": "topic_a",
                    "author_id": "u_pm",
                    "sensitivity": "l1",
                },
                materialize=False,
            )
            hidden_path = root / "hidden.md"
            hidden_path.write_text(
                "隐藏专题也讨论了编码口径，但执行成员不应召回。",
                encoding="utf-8",
            )
            store.save_source(
                {
                    "doc_id": "meeting_hidden_topic",
                    "title": "隐藏专题会",
                    "meeting_date": "2026-07-23",
                    "curated_source": {
                        "source_type": "markdown",
                        "path": str(hidden_path),
                        "status": "matched",
                    },
                    "org_id": "org_mvp",
                    "project_id": "project_mvp",
                    "topic_id": "topic_b",
                    "author_id": "u_pm",
                    "sensitivity": "l1",
                },
                materialize=False,
            )
            actor = User(id="u_exec", org_id="org_mvp", name="Executor")
            context = AccessContext(
                project_roles={(actor.id, "project_mvp"): "exec"},
                topic_members={
                    "topic_a": {actor.id},
                    "topic_b": {"u_pm"},
                },
            )

            with patch(
                "store.sqlite_store._source_chunk_like_score",
                side_effect=AssertionError("FTS hits must not trigger a Python full scan"),
            ), patch(
                "store.sqlite_store._source_chunk_highlight",
                side_effect=AssertionError("FTS hits must use SQLite snippet()"),
            ):
                results = store.search_source_evidence(
                    "编码口径",
                    actor=actor,
                    access_context=context,
                    project_id="project_mvp",
                    limit=5,
                )
            with patch("store.sqlite_store._fts_match_expressions", return_value=[]):
                fallback_results = store.search_source_evidence(
                    "编码口径",
                    actor=actor,
                    access_context=context,
                    project_id="project_mvp",
                    limit=5,
                )
            with closing(sqlite3.connect(store.database_path)) as conn:
                row = conn.execute(
                    """
                    select org_id, project_id, topic_id, author_id, sensitivity
                    from source_chunks
                    where source_id = ?
                    """,
                    ("meeting_runtime_upgrade",),
                ).fetchone()

        self.assertTrue(results)
        self.assertEqual(
            {result["source_id"] for result in results},
            {"meeting_runtime_upgrade"},
        )
        self.assertEqual(results[0]["source_id"], "meeting_runtime_upgrade")
        self.assertIn("<mark>编码口径</mark>", results[0]["highlight"])
        self.assertNotIn("编</mark> <mark>码", results[0]["highlight"])
        self.assertTrue(results[0]["locator"].startswith("chars:"))
        self.assertEqual(
            fallback_results[0]["match_reasons"],
            ["like"],
        )
        self.assertEqual(
            row,
            ("org_mvp", "project_mvp", "topic_a", "u_pm", "l1"),
        )

    def test_existing_source_without_chunk_rows_is_backfilled_once_on_store_open(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_path = root / "historical.md"
            source_path.write_text(
                "历史会议确认质量门禁必须覆盖编码、映射和验收样例。",
                encoding="utf-8",
            )
            store_dir = root / "store"
            store = ProjectSQLiteStore(store_dir)
            store.save_source(
                {
                    "doc_id": "historical_source",
                    "title": "历史质量会议",
                    "meeting_date": "2026-07-01",
                    "curated_source": {
                        "source_type": "markdown",
                        "path": str(source_path),
                        "status": "matched",
                    },
                    "raw_source": {
                        "source_type": "transcript",
                        "path": "",
                        "status": "raw_source_pending",
                    },
                    "org_id": "org_mvp",
                    "project_id": "project_mvp",
                    "topic_id": None,
                    "author_id": "u_pm",
                    "sensitivity": "l1",
                },
                materialize=False,
            )
            with closing(sqlite3.connect(store.database_path)) as conn:
                conn.execute(
                    "delete from source_chunks_fts where source_id = ?",
                    ("historical_source",),
                )
                conn.execute(
                    "delete from source_chunks where source_id = ?",
                    ("historical_source",),
                )
                conn.execute(
                    "delete from source_chunk_index_status where source_id = ?",
                    ("historical_source",),
                )
                conn.commit()

            reopened = ProjectSQLiteStore(store_dir)
            with closing(sqlite3.connect(reopened.database_path)) as conn:
                chunk_count = conn.execute(
                    "select count(*) from source_chunks where source_id = ?",
                    ("historical_source",),
                ).fetchone()[0]
                status = conn.execute(
                    """
                    select status, chunk_count
                    from source_chunk_index_status
                    where source_id = ?
                    """,
                    ("historical_source",),
                ).fetchone()

        self.assertGreater(chunk_count, 0)
        self.assertEqual(status, ("indexed", chunk_count))

    def test_source_reindex_does_not_leave_stale_chunks_after_path_becomes_invalid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_path = root / "meeting.md"
            source_path.write_text("标准层需要统一编码口径。", encoding="utf-8")
            store = ProjectSQLiteStore(root / "store")
            source = {
                "doc_id": "source_reindexed",
                "title": "标准层会议",
                "meeting_date": "2026-07-23",
                "curated_source": {
                    "source_type": "markdown",
                    "path": str(source_path),
                    "status": "matched",
                },
                "org_id": "org_mvp",
                "project_id": "project_mvp",
                "topic_id": None,
                "author_id": "u_pm",
                "sensitivity": "l1",
            }
            store.save_source(source, materialize=False)
            source["curated_source"] = {
                "source_type": "markdown",
                "path": str(root / "deleted.md"),
                "status": "missing",
            }
            store.save_source(source, materialize=False)
            with closing(sqlite3.connect(store.database_path)) as conn:
                chunk_count = conn.execute(
                    "select count(*) from source_chunks where source_id = ?",
                    ("source_reindexed",),
                ).fetchone()[0]
                status = conn.execute(
                    """
                    select status, reason
                    from source_chunk_index_status
                    where source_id = ?
                    """,
                    ("source_reindexed",),
                ).fetchone()

        self.assertEqual(chunk_count, 0)
        self.assertEqual(status, ("skipped", "source_file_missing"))

    def test_context_budget_is_token_based_and_retrieval_is_opt_in(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            store.save_item(
                _item(
                    "context_memory",
                    "不应自动注入的项目记忆",
                    "详细项目事实应该由模型选择 search_memory 工具后读取。",
                ),
                materialize=False,
            )

            context = store.read_context(
                "conversation",
                "不应自动注入的项目记忆",
                token_budget=160,
                project_id="project_mvp",
                include_retrieval=False,
            )

        self.assertEqual(context["budget_unit"], "tokens")
        self.assertEqual(context["token_counter"], "cl100k_base")
        self.assertLessEqual(context["token_count"], 160)
        self.assertEqual(context["retrieval_items"], [])
        self.assertEqual(context["budgets"]["retrieval_tokens"], 0)
        self.assertNotIn("详细项目事实应该由模型选择", context["content"])

    def test_role_workspace_is_packed_inside_the_same_project_context_budget(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            context = store.read_context(
                "conversation",
                "项目进度",
                token_budget=220,
                project_id="project_mvp",
                include_retrieval=False,
                additional_context={
                    "role_daily_workspace": {
                        "risk_items": [
                            {
                                "title": f"风险 {index}",
                                "description": "需要跨专题协调并确认责任人。" * 20,
                            }
                            for index in range(20)
                        ]
                    }
                },
            )

        self.assertLessEqual(context["token_count"], 220)
        self.assertGreater(context["budgets"]["additional_tokens"], 0)
        self.assertIn("角色工作上下文", context["content"])


if __name__ == "__main__":
    unittest.main()
