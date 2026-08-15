import json
import unittest

from pydantic import BaseModel, ConfigDict, Field

from agent.access_policy import AccessContext, User
from agent.tool_registry import (
    ToolEffect,
    ToolRegistry,
    ToolSpec,
    conversation_tool_specs,
    register_specs,
)


class DemoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=2, description="Validated demo value.")


class DemoOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    summary: str
    count: int


class _Handlers:
    def __getattr__(self, name):
        return lambda *args, **kwargs: {"summary": name}


class ToolContractUpgradeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.actor = User(id="u_exec", org_id="org_mvp", name="Executor")
        self.context = AccessContext(
            project_roles={(self.actor.id, "project_mvp"): "exec"},
            topic_members={
                "topic_a": {self.actor.id},
                "topic_b": {"u_pm"},
            },
        )

    def test_typed_input_generates_schema_and_invalid_arguments_never_reach_handler(self):
        calls: list[dict] = []
        registry = ToolRegistry(default_context=self.context)
        registry.register(
            ToolSpec(
                name="typed_demo",
                description="Use for typed validation.",
                parameters={},
                input_model=DemoInput,
                output_model=DemoOutput,
                handler=lambda actor, ctx, args: (
                    calls.append(args)
                    or {"summary": "ok", "count": 1}
                ),
                access_filter_keys=(),
            )
        )

        schema = registry.openai_tools(
            self.actor,
            project_id="project_mvp",
        )[0]["function"]["parameters"]
        result = registry.call(
            "typed_demo",
            self.actor,
            self.context,
            {"value": "x", "unexpected": True},
            project_id="project_mvp",
        )

        self.assertEqual(schema["required"], ["value"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(calls, [])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_arguments")

    def test_invalid_typed_output_is_a_structured_contract_error(self):
        registry = ToolRegistry(default_context=self.context)
        registry.register(
            ToolSpec(
                name="bad_output",
                description="Return a typed result.",
                parameters={},
                input_model=DemoInput,
                output_model=DemoOutput,
                handler=lambda actor, ctx, args: {"summary": "missing count"},
                access_filter_keys=(),
            )
        )

        result = registry.call(
            "bad_output",
            self.actor,
            self.context,
            {"value": "valid"},
            project_id="project_mvp",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_tool_result")
        self.assertNotIn("facts", result)

    def test_nested_tagged_rows_are_visibility_filtered_without_key_conventions(self):
        visible = {
            "id": "visible",
            "org_id": "org_mvp",
            "project_id": "project_mvp",
            "topic_id": "topic_a",
            "author_id": "u_pm",
            "sensitivity": "l1",
            "title": "可见",
        }
        hidden = {
            "id": "hidden",
            "org_id": "org_mvp",
            "project_id": "project_mvp",
            "topic_id": "topic_b",
            "author_id": "u_pm",
            "sensitivity": "l1",
            "title": "隐藏",
        }
        registry = ToolRegistry(default_context=self.context)
        registry.register(
            ToolSpec(
                name="nested_rows",
                description="Return nested scoped rows.",
                parameters={"type": "object", "properties": {}},
                handler=lambda actor, ctx, args: {
                    "summary": "two rows",
                    "payload": {"rows": [visible, hidden]},
                },
                access_filter_keys=(),
            )
        )

        result = registry.call(
            "nested_rows",
            self.actor,
            self.context,
            {},
            project_id="project_mvp",
        )

        self.assertEqual(
            [row["id"] for row in result["payload"]["rows"]],
            ["visible"],
        )
        self.assertNotIn("隐藏", json.dumps(result, ensure_ascii=False))
        self.assertEqual(
            result["access"]["filtered"]["$.payload.rows"],
            {"examined": 2, "returned": 1},
        )

    def test_partial_access_tags_are_rejected_instead_of_leaked(self):
        registry = ToolRegistry(default_context=self.context)
        registry.register(
            ToolSpec(
                name="partial_tags",
                description="Return an invalid scoped row.",
                parameters={"type": "object", "properties": {}},
                handler=lambda actor, ctx, args: {
                    "items": [
                        {
                            "id": "leak",
                            "project_id": "project_mvp",
                            "title": "不应泄露",
                        }
                    ]
                },
            )
        )

        result = registry.call(
            "partial_tags",
            self.actor,
            self.context,
            {},
            project_id="project_mvp",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_access_contract")
        self.assertNotIn("不应泄露", json.dumps(result, ensure_ascii=False))

    def test_partial_access_tags_without_an_entity_id_are_also_rejected(self):
        registry = ToolRegistry(default_context=self.context)
        registry.register(
            ToolSpec(
                name="partial_tags_without_id",
                description="Return an invalid nested scoped aggregate.",
                handler=lambda actor, ctx, args: {
                    "payload": {
                        "project_id": "project_other",
                        "sensitivity": "l4",
                        "secret": "不应泄露",
                    }
                },
                access_filter_keys=(),
            )
        )

        result = registry.call(
            "partial_tags_without_id",
            self.actor,
            self.context,
            {},
            project_id="project_mvp",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_access_contract")
        self.assertNotIn("不应泄露", json.dumps(result, ensure_ascii=False))

    def test_untagged_entity_list_cannot_hide_under_an_unregistered_result_key(self):
        registry = ToolRegistry(default_context=self.context)
        registry.register(
            ToolSpec(
                name="hidden_key",
                description="Return an invalid nested entity list.",
                handler=lambda actor, ctx, args: {
                    "payload": {
                        "secret_rows": [
                            {"id": "leak", "title": "无标签实体"}
                        ]
                    }
                },
                access_filter_keys=(),
            )
        )

        result = registry.call(
            "hidden_key",
            self.actor,
            self.context,
            {},
            project_id="project_mvp",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_access_contract")
        self.assertNotIn("无标签实体", json.dumps(result, ensure_ascii=False))

    def test_source_id_reference_is_not_misclassified_as_an_entity_row(self):
        registry = ToolRegistry(default_context=self.context)
        registry.register(
            ToolSpec(
                name="evidence_reference",
                description="Return a citation nested inside an authorized aggregate.",
                handler=lambda actor, ctx, args: {
                    "summary": "one citation",
                    "citations": [
                        {
                            "source_id": "source_visible",
                            "locator": "paragraph 3",
                        }
                    ],
                },
                access_filter_keys=(),
            )
        )

        result = registry.call(
            "evidence_reference",
            self.actor,
            self.context,
            {},
            project_id="project_mvp",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["citations"][0]["source_id"], "source_visible")

    def test_project_scope_conflict_is_rejected_before_handler(self):
        called = False

        def handler(actor, ctx, args):
            nonlocal called
            called = True
            return {"summary": "unsafe"}

        registry = ToolRegistry(default_context=self.context)
        registry.register(
            ToolSpec(
                name="scoped",
                description="Use in the active project only.",
                parameters={
                    "type": "object",
                    "properties": {"project_id": {"type": "string"}},
                },
                handler=handler,
                access_filter_keys=(),
            )
        )

        result = registry.call(
            "scoped",
            self.actor,
            self.context,
            {"project_id": "project_other"},
            project_id="project_mvp",
        )

        self.assertFalse(called)
        self.assertEqual(result["error"]["code"], "scope_conflict")

    def test_conversation_mutations_declare_effect_and_parallel_safety(self):
        specs = {
            spec.name: spec
            for spec in conversation_tool_specs(_Handlers())
        }

        self.assertEqual(specs["search_memory"].effect, ToolEffect.READ)
        self.assertTrue(specs["search_memory"].parallel_safe)
        for name in {
            "ingest_file",
            "archive_deliverable",
            "generate_daily_brief",
            "request_project_inspection",
            "propose_candidates",
        }:
            self.assertEqual(specs[name].effect, ToolEffect.WRITE)
            self.assertFalse(specs[name].parallel_safe)

    def test_production_search_memory_output_rejects_wrong_field_types(self):
        handlers = _Handlers()
        handlers.search_memory = lambda **kwargs: {
            "summary": "invalid",
            "items": "not-a-list",
            "degraded": "not-a-bool",
            "embedding_error": "",
            "retrieval_mode": "fts",
        }
        registry = register_specs(
            ToolRegistry(default_context=self.context),
            conversation_tool_specs(handlers),
        )

        result = registry.call(
            "search_memory",
            self.actor,
            self.context,
            {"query": "risk"},
            project_id="project_mvp",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_tool_result")


if __name__ == "__main__":
    unittest.main()
