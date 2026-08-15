import json
import sqlite3
import tempfile
import unittest

from agent.access_policy import User
from agent.project_skills import ProjectSkillService
from agent.schemas import CandidateStatus, EvidenceRef, InspectionItem
from agent.tool_registry import ToolRegistry, ToolSpec
from store.sqlite_store import ProjectSQLiteStore


class SkillProvider:
    name = "scripted"

    def __init__(self, *, tool_names=None, fail_negative=False):
        self.tool_names = tool_names or ["search_memory"]
        self.fail_negative = fail_negative
        self.calls = []

    def complete_json(self, prompt: str) -> str:
        self.calls.append(prompt)
        if "PRESSURE_TEST" in prompt:
            payload = json.loads(prompt.split("PRESSURE_TEST=", 1)[1])
            selected = "查询已经确认" not in payload["input"]
            if self.fail_negative and not selected:
                selected = True
            return json.dumps({"selected": selected, "reason": "scripted selector"})
        return json.dumps(
            {
                "name": "标准层闭环推进",
                "description": "在标准层口径不一致时建立可验收闭环。",
                "when_to_use": "需要统一标准层对象、口径、责任和验收时使用。",
                "when_not_to_use": ["只需查询一个已确认字段值时"],
                "steps": [
                    {"instruction": "列出对象与口径差异", "done_when": "每个差异有来源和责任人"},
                    {"instruction": "组织业主确认并回写", "done_when": "形成已确认版本和变更记录"},
                ],
                "stop_conditions": ["所有差异已确认并有版本记录", "缺少业主授权时暂停并升级"],
                "boundaries": ["不替代权限校验", "不虚构业主结论"],
                "anti_patterns": ["只复述会议原话，不形成可验收结论"],
                "tool_names": self.tool_names,
                "pressure_tests": [
                    {"name": "标准层口径冲突", "input": "标准层字段口径冲突怎么闭环", "should_trigger": True},
                    {"name": "单值查询", "input": "查询已经确认的物料编码", "should_trigger": False},
                ],
            },
            ensure_ascii=False,
        )


class ProjectSkillServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = ProjectSQLiteStore(self.tmpdir.name)
        self.store.upsert_org("org_main", "Main")
        self.store.upsert_user("u_pm", "org_main", "PM")
        self.store.upsert_user("u_exec", "org_main", "Exec")
        self.store.upsert_project("project_main", "org_main", "Project", "u_pm")
        self.store.upsert_project_member("project_main", "u_pm", "pm")
        self.store.upsert_project_member("project_main", "u_exec", "exec")
        self.store.upsert_topic("topic_private", "project_main", "Private", "u_pm")
        self.store.upsert_topic_member("topic_private", "u_pm")
        self.pm = User("u_pm", "org_main", "PM")
        self.exec = User("u_exec", "org_main", "Exec")
        self.pm_ctx = self.store.access_context_for_actor("u_pm")
        self.exec_ctx = self.store.access_context_for_actor("u_exec")
        self.registry = ToolRegistry(default_context=self.pm_ctx)
        self.registry.register(ToolSpec(
            name="search_memory",
            description="Search authorized memory.",
            parameters={"type": "object", "properties": {}},
            handler=lambda actor, ctx, args: {"items": []},
        ))

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_weak_confirmed_method_is_saved_as_blocked_candidate_without_model_call(self):
        method = self._method("method_weak", evidence_count=1, structured=False)
        self.store.save_item(method)
        provider = SkillProvider()

        result = ProjectSkillService(self.store, self.registry, provider=provider).compile_method(
            method.item_id, self.pm, self.pm_ctx
        )

        self.assertEqual(result["status"], "candidate")
        self.assertFalse(result["readiness"]["ready"])
        self.assertIn("independent_evidence", result["readiness"]["failed_checks"])
        self.assertIn("reasoning_chain", result["readiness"]["failed_checks"])
        self.assertEqual(provider.calls, [])

    def test_valid_method_requires_real_pressure_results_before_publish(self):
        method = self._method("method_valid")
        self.store.save_item(method)
        provider = SkillProvider()
        service = ProjectSkillService(self.store, self.registry, provider=provider)

        compiled = service.compile_method(method.item_id, self.pm, self.pm_ctx)
        with self.assertRaisesRegex(ValueError, "pressure tests"):
            service.publish(compiled["skill_id"], self.pm, self.pm_ctx)
        tested = service.run_pressure_tests(compiled["skill_id"], self.pm, self.pm_ctx)
        published = service.publish(compiled["skill_id"], self.pm, self.pm_ctx)

        self.assertTrue(compiled["readiness"]["ready"])
        self.assertTrue(tested["passed"])
        self.assertTrue(all(row["payload"].get("reason") for row in tested["pressure_tests"]))
        self.assertEqual(published["status"], "published")
        self.assertIn("---", published["body_markdown"])
        self.assertEqual(published["tool_names"], ["search_memory"])
        pressure_prompts = [prompt for prompt in provider.calls if "PRESSURE_TEST=" in prompt]
        self.assertEqual(len(pressure_prompts), 2)
        self.assertTrue(all("should_trigger" not in prompt for prompt in pressure_prompts))
        pressure_payloads = [json.loads(prompt.split("PRESSURE_TEST=", 1)[1]) for prompt in pressure_prompts]
        self.assertTrue(all(set(payload) == {"skill", "input"} for payload in pressure_payloads))
        with self.assertRaisesRegex(ValueError, "immutable"):
            service.run_pressure_tests(compiled["skill_id"], self.pm, self.pm_ctx)

    def test_unknown_registry_tool_blocks_candidate_and_cannot_be_published(self):
        method = self._method("method_bad_tool")
        self.store.save_item(method)
        service = ProjectSkillService(self.store, self.registry, provider=SkillProvider(tool_names=["delete_everything"]))

        compiled = service.compile_method(method.item_id, self.pm, self.pm_ctx)

        self.assertFalse(compiled["readiness"]["ready"])
        self.assertIn("registered_tools", compiled["readiness"]["failed_checks"])
        with self.assertRaisesRegex(ValueError, "readiness"):
            service.publish(compiled["skill_id"], self.pm, self.pm_ctx)

    def test_published_version_is_immutable_and_hidden_topic_stays_hidden(self):
        method = self._method("method_private", topic_id="topic_private", sensitivity="l1")
        self.store.save_item(method)
        provider = SkillProvider()
        service = ProjectSkillService(self.store, self.registry, provider=provider)
        compiled = service.compile_method(method.item_id, self.pm, self.pm_ctx)
        service.run_pressure_tests(compiled["skill_id"], self.pm, self.pm_ctx)
        first = service.publish(compiled["skill_id"], self.pm, self.pm_ctx)
        second = service.compile_method(method.item_id, self.pm, self.pm_ctx)

        pm_rows = service.list_skills(self.pm, self.pm_ctx, project_id="project_main")
        exec_rows = service.list_skills(self.exec, self.exec_ctx, project_id="project_main")

        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 2)
        self.assertEqual(pm_rows[0]["active_version_id"], first["version_id"])
        self.assertEqual(exec_rows, [])

    def test_stricter_candidate_scope_does_not_leak_through_visible_active_version(self):
        method = self._method("method_scope_change")
        self.store.save_item(method)
        service = ProjectSkillService(self.store, self.registry, provider=SkillProvider())
        first = service.compile_method(method.item_id, self.pm, self.pm_ctx)
        service.run_pressure_tests(first["skill_id"], self.pm, self.pm_ctx)
        service.publish(first["skill_id"], self.pm, self.pm_ctx)

        tightened = self._method(
            method.item_id,
            topic_id="topic_private",
            sensitivity="l3",
        )
        self.store.save_item(tightened)
        second = service.compile_method(tightened.item_id, self.pm, self.pm_ctx)
        exec_before_publish = service.list_skills(
            self.exec,
            self.exec_ctx,
            project_id="project_main",
        )

        self.assertEqual(second["sensitivity"], "l3")
        self.assertEqual(len(exec_before_publish), 1)
        self.assertIsNone(exec_before_publish[0]["latest_version"])
        self.assertEqual(exec_before_publish[0]["active_version"]["sensitivity"], "l1")

        service.run_pressure_tests(second["skill_id"], self.pm, self.pm_ctx)
        service.publish(second["skill_id"], self.pm, self.pm_ctx)
        exec_after_publish = service.list_skills(
            self.exec,
            self.exec_ctx,
            project_id="project_main",
        )
        self.assertEqual(exec_after_publish, [])

    def test_every_skill_table_row_carries_access_tags(self):
        method = self._method("method_tags")
        self.store.save_item(method)
        service = ProjectSkillService(self.store, self.registry, provider=SkillProvider())
        compiled = service.compile_method(method.item_id, self.pm, self.pm_ctx)
        service.run_pressure_tests(compiled["skill_id"], self.pm, self.pm_ctx)

        conn = sqlite3.connect(self.store.database_path)
        try:
            for table in ("project_skills", "project_skill_versions", "project_skill_evidence", "project_skill_tests"):
                count = conn.execute(
                    f"select count(*) from {table} where org_id is null or project_id is null or author_id is null or sensitivity is null"
                ).fetchone()[0]
                self.assertEqual(count, 0, table)
        finally:
            conn.close()

    def test_skill_author_is_compiler_while_source_method_author_is_preserved(self):
        method = self._method("method_exec_authored", author_id="u_exec")
        self.store.save_item(method)

        compiled = ProjectSkillService(
            self.store,
            self.registry,
            provider=SkillProvider(),
        ).compile_method(method.item_id, self.pm, self.pm_ctx)

        self.assertEqual(compiled["author_id"], "u_pm")
        self.assertEqual(compiled["payload"]["source_method_author_id"], "u_exec")
        self.assertEqual(compiled["payload"]["source_method_id"], method.item_id)

    def _method(
        self,
        item_id,
        *,
        evidence_count=2,
        structured=True,
        topic_id=None,
        sensitivity="l1",
        author_id="u_pm",
    ):
        evidence = [
            EvidenceRef(
                source_doc_id=f"meeting_{index}",
                source_kind="curated_source",
                locator=f"第 {index} 段",
                quote=f"标准层口径需要形成第 {index} 份确认记录。",
                raw_source_doc_id=f"meeting_{index}_raw",
                raw_locator=f"raw:第 {index} 段",
                evidence_level="raw_traceable",
            )
            for index in range(1, evidence_count + 1)
        ]
        return InspectionItem(
            item_id=item_id,
            category="method",
            title="标准层闭环推进",
            description="统一对象、口径、责任和版本。",
            evidence_refs=evidence,
            status=CandidateStatus.CONFIRMED,
            business_goal="标准层结论可执行、可追溯" if structured else "",
            principles=["口径有来源", "变更有版本"] if structured else [],
            reasoning_chain=["先识别差异", "再确认并回写"] if structured else [],
            applicable_scope="标准层对象和字段口径协同" if structured else "",
            org_id="org_main",
            project_id="project_main",
            topic_id=topic_id,
            author_id=author_id,
            sensitivity=sensitivity,
            tag_origin="human_confirmed",
        )


if __name__ == "__main__":
    unittest.main()
