from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from store.sqlite_store import ProjectSQLiteStore  # noqa: E402


STORE_DIR = PROJECT_ROOT / "data" / "store"


def main() -> None:
    store = ProjectSQLiteStore(STORE_DIR)

    store.upsert_org("org_mvp", "MVP Org")
    store.upsert_user("u_pm", "org_mvp", "Project Manager")
    store.upsert_user("u_exec", "org_mvp", "Executor")
    store.upsert_user("u_pmo", "org_mvp", "PMO")
    store.upsert_user("u_professional", "org_mvp", "Professional Lead")
    store.upsert_user("u_topic_lead", "org_mvp", "Topic Lead")

    store.upsert_project("project_mvp", "org_mvp", "MVP Project", "u_pm")
    store.upsert_project_member("project_mvp", "u_pm", "pm")
    store.upsert_project_member("project_mvp", "u_pmo", "pmo", manager_id="u_pm")
    store.upsert_project_member("project_mvp", "u_professional", "professional_lead", manager_id="u_pmo")
    store.upsert_project_member("project_mvp", "u_topic_lead", "topic_lead", manager_id="u_professional")
    store.upsert_project_member("project_mvp", "u_exec", "exec", manager_id="u_topic_lead")

    store.upsert_topic("topic_a", "project_mvp", "Topic A", "u_pm")
    store.upsert_topic_member("topic_a", "u_pm")
    store.upsert_topic_member("topic_a", "u_exec")
    store.upsert_topic_member("topic_a", "u_topic_lead")
    store.upsert_topic_member("topic_a", "u_professional")

    store.upsert_topic("topic_b", "project_mvp", "Topic B", "u_pm")
    store.upsert_topic_member("topic_b", "u_pm")
    store.upsert_topic_member("topic_b", "u_pmo")
    store.upsert_topic_member("topic_b", "u_professional")

    print(f"Seeded MVP identity data into {store.database_path}")


if __name__ == "__main__":
    main()
