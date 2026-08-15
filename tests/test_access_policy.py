import sqlite3
import tempfile
import unittest

from fastapi.testclient import TestClient

from agent.access_policy import (
    AccessContext,
    IdentityResolutionError,
    Taggable,
    User,
    resolve_actor,
    visible,
    visible_filter,
)
from backend.main import create_app
from store.sqlite_store import ProjectSQLiteStore


class RequestStub:
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}


class AccessPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pm = User(id="u_pm", org_id="org_main", name="PM")
        self.exec = User(id="u_exec", org_id="org_main", name="Exec")
        self.pmo = User(id="u_pmo", org_id="org_main", name="PMO")
        self.outsider = User(id="u_outsider", org_id="org_main", name="Outsider")
        self.other_org_user = User(id="u_other", org_id="org_other", name="Other")
        self.ctx = AccessContext(
            project_roles={
                ("u_pm", "project_main"): "pm",
                ("u_exec", "project_main"): "exec",
                ("u_pmo", "project_main"): "pmo",
                ("u_other", "project_other"): "pm",
            },
            topic_members={
                "topic_a": {"u_pm", "u_exec"},
                "topic_b": {"u_pm", "u_pmo"},
            },
            item_blocklists={
                "blocked_item": {"u_exec"},
            },
        )

    def item(
        self,
        item_id: str = "item",
        *,
        org_id: str = "org_main",
        project_id: str = "project_main",
        topic_id: str | None = None,
        author_id: str = "u_pm",
        sensitivity: str = "l1",
    ) -> Taggable:
        return Taggable(
            id=item_id,
            org_id=org_id,
            project_id=project_id,
            topic_id=topic_id,
            author_id=author_id,
            sensitivity=sensitivity,
        )

    def test_author_can_see_own_l3_item_even_when_role_is_not_high_enough(self):
        item = self.item(author_id="u_exec", sensitivity="l3")

        self.assertTrue(visible(self.exec, item, self.ctx))

    def test_cross_org_item_is_not_visible_to_current_org_user(self):
        item = self.item(org_id="org_other", project_id="project_other", author_id="u_other")

        self.assertFalse(visible(self.pm, item, self.ctx))

    def test_non_project_member_cannot_see_project_item(self):
        item = self.item()

        self.assertFalse(visible(self.outsider, item, self.ctx))

    def test_topic_membership_blocks_user_even_for_l1_item(self):
        item = self.item(topic_id="topic_b", sensitivity="l1")

        self.assertFalse(visible(self.exec, item, self.ctx))

    def test_explicit_owner_can_see_assigned_task_outside_topic_membership(self):
        task = {
            "id": "assigned_task",
            "org_id": "org_main",
            "project_id": "project_main",
            "topic_id": "topic_b",
            "author_id": "u_pm",
            "sensitivity": "l1",
            "owner_candidates": ["Exec"],
        }

        self.assertTrue(visible(self.exec, task, self.ctx))

    def test_management_role_sees_all_project_topics(self):
        topic_a = self.item(topic_id="topic_a", sensitivity="l1")
        topic_b = self.item(topic_id="topic_b", sensitivity="l1")

        self.assertTrue(visible(self.pm, topic_b, self.ctx))
        self.assertTrue(visible(self.pmo, topic_a, self.ctx))

    def test_sensitivity_threshold_uses_role_rank(self):
        l3 = self.item(sensitivity="l3")
        l4 = self.item(sensitivity="l4")

        self.assertFalse(visible(self.exec, l3, self.ctx))
        self.assertTrue(visible(self.pm, l3, self.ctx))
        self.assertTrue(visible(self.pm, l4, self.ctx))
        self.assertTrue(visible(self.pmo, l4, self.ctx))

    def test_pm_and_pmo_are_equivalent_management_roles(self):
        from agent.access_policy import ROLE_RANK

        self.assertEqual(ROLE_RANK["pm"], ROLE_RANK["pmo"])

    def test_blocklisted_user_cannot_see_even_if_other_checks_pass(self):
        item = self.item(item_id="blocked_item", author_id="u_pm", sensitivity="l1")

        self.assertFalse(visible(self.exec, item, self.ctx))

    def test_visible_filter_returns_only_visible_items_in_original_order(self):
        visible_one = self.item(item_id="visible_one", sensitivity="l1")
        hidden_l3 = self.item(item_id="hidden_l3", sensitivity="l3")
        visible_two = self.item(item_id="visible_two", topic_id="topic_a", sensitivity="l1")
        hidden_topic = self.item(item_id="hidden_topic", topic_id="topic_b", sensitivity="l1")

        result = visible_filter(self.exec, [visible_one, hidden_l3, visible_two, hidden_topic], self.ctx)

        self.assertEqual([item.id for item in result], ["visible_one", "visible_two"])

    def test_resolve_actor_requires_x_actor_id_header(self):
        with self.assertRaises(IdentityResolutionError) as raised:
            resolve_actor(RequestStub())

        self.assertIn("X-Actor-Id", str(raised.exception))


class AccessStoreSchemaTest(unittest.TestCase):
    def test_store_creates_identity_tables_with_org_scoped_memberships(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            conn = sqlite3.connect(store.database_path)
            try:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "select name from sqlite_master where type='table'"
                    ).fetchall()
                }
                self.assertTrue({"orgs", "users", "projects", "project_members", "topics", "topic_members"} <= tables)
                project_member_columns = {row[1] for row in conn.execute("pragma table_info(project_members)").fetchall()}
                topic_columns = {row[1] for row in conn.execute("pragma table_info(topics)").fetchall()}
                topic_member_columns = {row[1] for row in conn.execute("pragma table_info(topic_members)").fetchall()}
            finally:
                conn.close()

        self.assertTrue({"id", "org_id", "project_id", "user_id", "role"} <= project_member_columns)
        self.assertTrue({"org_id", "project_id", "created_by"} <= topic_columns)
        self.assertTrue({"org_id", "project_id", "topic_id", "user_id"} <= topic_member_columns)

    def test_store_can_persist_and_load_access_user(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            store.upsert_org("org_main", "Main Org")
            store.upsert_user("u_pm", "org_main", "Project Manager", feishu_id="fs_pm")

            user = store.get_access_user("u_pm")

        self.assertEqual(user, {"id": "u_pm", "org_id": "org_main", "name": "Project Manager", "feishu_id": "fs_pm"})


class AccessIdentityEndpointTest(unittest.TestCase):
    def test_identity_actor_endpoint_requires_x_actor_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            client = TestClient(create_app(store_dir=tmpdir))

            response = client.get("/api/identity/actor")

        self.assertEqual(response.status_code, 401)
        self.assertIn("X-Actor-Id", response.json()["detail"])

    def test_identity_actor_endpoint_resolves_known_user_from_header(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            store.upsert_org("org_main", "Main Org")
            store.upsert_user("u_pm", "org_main", "Project Manager")
            client = TestClient(create_app(store_dir=tmpdir))

            response = client.get("/api/identity/actor", headers={"X-Actor-Id": "u_pm"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"id": "u_pm", "org_id": "org_main", "name": "Project Manager", "feishu_id": ""})

    def test_switchable_users_lists_concrete_project_members_with_topics(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            store.upsert_org("org_main", "Main Org")
            store.upsert_user("u_pm", "org_main", "Project Manager")
            store.upsert_user("u_exec", "org_main", "Executor One")
            store.upsert_user("u_other", "org_main", "Other Project User")
            store.upsert_project("project_main", "org_main", "Main Project", "u_pm")
            store.upsert_project("project_other", "org_main", "Other Project", "u_other")
            store.upsert_project_member("project_main", "u_pm", "pm")
            store.upsert_project_member("project_main", "u_exec", "exec")
            store.upsert_project_member("project_other", "u_other", "pm")
            store.upsert_topic("topic_a", "project_main", "Topic A", "u_pm")
            store.upsert_topic("topic_b", "project_main", "Topic B", "u_pm")
            store.upsert_topic_member("topic_a", "u_exec")
            store.upsert_topic_member("topic_b", "u_exec")
            client = TestClient(create_app(store_dir=tmpdir))

            response = client.get(
                "/api/dev/switchable-users",
                headers={"X-Actor-Id": "u_pm"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["project_id"], "project_main")
        self.assertEqual(
            response.json()["users"],
            [
                {
                    "user_id": "u_pm",
                    "org_id": "org_main",
                    "name": "Project Manager",
                    "role": "pm",
                    "topics": [],
                },
                {
                    "user_id": "u_exec",
                    "org_id": "org_main",
                    "name": "Executor One",
                    "role": "exec",
                    "topics": [
                        {"topic_id": "topic_a", "name": "Topic A"},
                        {"topic_id": "topic_b", "name": "Topic B"},
                    ],
                },
            ],
        )

    def test_switchable_users_prefers_people_directory_over_role_seed_accounts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            store.upsert_org("org_main", "Main Org")
            store.upsert_user("u_pmo", "org_main", "PMO Seed")
            store.upsert_project("project_main", "org_main", "Main Project", "u_pmo")
            store.upsert_project_member("project_main", "u_pmo", "pmo")
            entities = [
                {
                    "person_id": "person_manager",
                    "name": "具体用户甲",
                    "identity_status": "confirmed",
                    "source_id": "source_people",
                    "author_id": "u_pmo",
                    "sensitivity": "l1",
                },
                {
                    "person_id": "person_lead",
                    "name": "具体用户乙",
                    "identity_status": "confirmed",
                    "source_id": "source_people",
                    "author_id": "u_pmo",
                    "sensitivity": "l1",
                },
            ]
            assignments = [
                {
                    "assignment_id": "assignment_manager",
                    "person_id": "person_manager",
                    "group": "PMO",
                    "role": "项目经理",
                    "path": "项目 / PMO / 具体用户甲",
                    "responsibility_note": "",
                    "source_id": "source_people",
                    "author_id": "u_pmo",
                    "sensitivity": "l1",
                },
                {
                    "assignment_id": "assignment_lead",
                    "person_id": "person_lead",
                    "group": "铁区组",
                    "role": "技术负责人",
                    "path": "项目 / 铁区组 / 具体用户乙",
                    "responsibility_note": "",
                    "source_id": "source_people",
                    "author_id": "u_pmo",
                    "sensitivity": "l1",
                },
            ]
            store.replace_people_directory(
                org_id="org_main",
                project_id="project_main",
                entities=entities,
                assignments=assignments,
            )
            client = TestClient(create_app(store_dir=tmpdir))

            response = client.get(
                "/api/dev/switchable-users",
                headers={"X-Actor-Id": "u_pmo"},
            )
            virtual_user = store.get_access_user("person_lead")
            virtual_role = store.access_context_for_actor("person_lead").role_of(
                User(id="person_lead", org_id="org_main", name="具体用户乙"),
                "project_main",
            )
            virtual_project = store.default_project_id_for_actor("person_lead", "org_main")

        self.assertEqual(response.status_code, 200)
        users = response.json()["users"]
        self.assertEqual([user["user_id"] for user in users], ["person_manager", "person_lead"])
        self.assertEqual([user["name"] for user in users], ["具体用户甲", "具体用户乙"])
        self.assertEqual([user["role"] for user in users], ["pmo", "topic_lead"])
        self.assertEqual(users[1]["topics"], [{"topic_id": "people_group:铁区组", "name": "铁区组"}])
        self.assertEqual(virtual_user["name"], "具体用户乙")
        self.assertEqual(virtual_role, "topic_lead")
        self.assertEqual(virtual_project, "project_main")


if __name__ == "__main__":
    unittest.main()
