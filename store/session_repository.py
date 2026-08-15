from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from agent.repository_paths import normalize_repository_paths
from store.database import SQLiteDatabase


EventWriter = Callable[[str, str, str, dict[str, Any]], None]


class SessionRepository:
    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        event_writer: EventWriter | None = None,
    ) -> None:
        self.database = database
        self.event_writer = event_writer

    def ensure_schema(self) -> None:
        with self.database.lock, self.database.connect() as connection:
            connection.executescript(
                """
                create table if not exists sessions(
                  id text primary key,
                  project_id text not null default 'project_mvp',
                  actor_id text not null default 'u_pm',
                  title text not null,
                  created_at text not null,
                  updated_at text not null,
                  archived integer not null default 0,
                  title_edited_by_human integer not null default 0
                );

                create table if not exists messages(
                  id text primary key,
                  session_id text not null,
                  role text not null,
                  content text not null,
                  tool_calls text,
                  created_at text not null,
                  metadata text,
                  turn_id text not null default '',
                  run_id text not null default '',
                  state text not null default 'committed',
                  foreign key(session_id) references sessions(id) on delete cascade
                );
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("pragma table_info(messages)").fetchall()
            }
            for name, definition in (
                ("turn_id", "text not null default ''"),
                ("run_id", "text not null default ''"),
                ("state", "text not null default 'committed'"),
            ):
                if name not in columns:
                    connection.execute(f"alter table messages add column {name} {definition}")
            connection.execute(
                "create index if not exists messages_session_turn on messages(session_id, turn_id, created_at, id)"
            )
            connection.commit()

    def ensure_session(
        self,
        *,
        session_id: str | None = None,
        title: str = "",
        project_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        with self.database.lock, self.database.connect() as connection:
            if session_id:
                row = connection.execute(
                    "select * from sessions where id = ?",
                    (session_id,),
                ).fetchone()
                if row is not None:
                    return _session_row(
                        row,
                        message_count=self._message_count(connection, session_id),
                    )
            now = _now()
            new_id = session_id or f"session_{uuid4().hex[:12]}"
            connection.execute(
                """
                insert into sessions(
                  id, project_id, actor_id, title, created_at, updated_at,
                  archived, title_edited_by_human
                ) values(?, ?, ?, ?, ?, ?, 0, 0)
                """,
                (new_id, project_id, actor_id, _session_title(title), now, now),
            )
            connection.commit()
        return self.get_session(new_id)

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        metadata: dict[str, Any] | None = None,
        turn_id: str = "",
        run_id: str = "",
        state: str = "committed",
    ) -> dict[str, Any]:
        if state not in {"running", "committed", "failed", "cancelled"}:
            raise ValueError(f"Unsupported message state: {state}")
        with self.database.lock, self.database.connect() as connection:
            session = connection.execute(
                "select * from sessions where id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise KeyError(session_id)
            existing_count = self._message_count(connection, session_id)
            now = _now()
            message_id = f"msg_{uuid4().hex[:12]}"
            active_metadata = dict(metadata or {})
            connection.execute(
                """
                insert into messages(
                  id, session_id, role, content, tool_calls, created_at, metadata,
                  turn_id, run_id, state
                ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    session_id,
                    role,
                    content,
                    _json(active_metadata.get("tool_steps", [])),
                    now,
                    _json(active_metadata),
                    turn_id,
                    run_id,
                    state,
                ),
            )
            next_title = str(session["title"])
            if (
                role == "user"
                and existing_count == 0
                and not bool(session["title_edited_by_human"])
            ):
                next_title = _session_title(content)
            connection.execute(
                "update sessions set title = ?, updated_at = ? where id = ?",
                (next_title, now, session_id),
            )
            connection.commit()
        return {
            "record_type": "message",
            "message_id": message_id,
            "session_id": session_id,
            "role": role,
            "content": content,
            "created_at": now,
            "metadata": active_metadata,
            "turn_id": turn_id,
            "run_id": run_id,
            "state": state,
        }

    def update_turn_state(self, turn_id: str, state: str) -> int:
        if not turn_id:
            raise ValueError("turn_id is required")
        if state not in {"running", "committed", "failed", "cancelled"}:
            raise ValueError(f"Unsupported message state: {state}")
        with self.database.lock, self.database.connect() as connection:
            cursor = connection.execute(
                "update messages set state = ? where turn_id = ?",
                (state, turn_id),
            )
            connection.commit()
        return int(cursor.rowcount)

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                "select * from sessions where id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            return _session_row(
                row,
                message_count=self._message_count(connection, session_id),
            )

    def list_sessions(
        self,
        *,
        project_id: str | None = None,
        actor_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[str] = []
        if project_id is not None:
            clauses.append("s.project_id = ?")
            values.append(project_id)
        if actor_id is not None:
            clauses.append("s.actor_id = ?")
            values.append(actor_id)
        where = f"where {' and '.join(clauses)}" if clauses else ""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                select s.*, count(m.id) as message_count
                from sessions s
                left join messages m on m.session_id = s.id
                {where}
                group by s.id
                order by s.archived asc, s.updated_at desc, s.created_at desc
                """,
                tuple(values),
            ).fetchall()
        return [
            _session_row(row, message_count=int(row["message_count"] or 0))
            for row in rows
        ]

    def list_messages(self, session_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "select * from messages where session_id = ? order by created_at, id",
                (session_id,),
            ).fetchall()
        return [_message_row(row) for row in rows]

    def list_model_history(
        self,
        session_id: str,
        *,
        current_turn_id: str = "",
    ) -> list[dict[str, Any]]:
        return [
            message
            for message in self.list_messages(session_id)
            if message["state"] == "committed"
            or bool(current_turn_id and message["turn_id"] == current_turn_id)
        ]

    def rename_session(self, session_id: str, title: str) -> dict[str, Any]:
        cleaned = _session_title(title)
        with self.database.lock, self.database.connect() as connection:
            cursor = connection.execute(
                """
                update sessions
                set title = ?, title_edited_by_human = 1, updated_at = ?
                where id = ?
                """,
                (cleaned, _now(), session_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(session_id)
            connection.commit()
        self._event("session", session_id, "session_renamed", {"title": cleaned})
        return self.get_session(session_id)

    def archive_session(self, session_id: str, archived: bool = True) -> dict[str, Any]:
        with self.database.lock, self.database.connect() as connection:
            cursor = connection.execute(
                "update sessions set archived = ?, updated_at = ? where id = ?",
                (1 if archived else 0, _now(), session_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(session_id)
            connection.commit()
        self._event(
            "session",
            session_id,
            "session_archived" if archived else "session_restored",
            {"archived": archived},
        )
        return self.get_session(session_id)

    def delete_session(self, session_id: str) -> None:
        current = self.get_session(session_id)
        with self.database.lock, self.database.connect() as connection:
            connection.execute("delete from messages where session_id = ?", (session_id,))
            connection.execute("delete from sessions where id = ?", (session_id,))
            connection.commit()
        self._event("session", session_id, "session_deleted", current)

    def _message_count(self, connection: Any, session_id: str) -> int:
        row = connection.execute(
            "select count(*) as value from messages where session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row["value"] or 0)

    def _event(
        self,
        entity: str,
        entity_id: str,
        action: str,
        payload: dict[str, Any],
    ) -> None:
        if self.event_writer is not None:
            self.event_writer(entity, entity_id, action, payload)


def _session_row(row: Any, *, message_count: int) -> dict[str, Any]:
    return {
        "record_type": "session",
        "session_id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "actor_id": str(row["actor_id"]),
        "title": str(row["title"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "message_count": int(message_count),
        "archived": bool(row["archived"]),
    }


def _message_row(row: Any) -> dict[str, Any]:
    return {
        "record_type": "message",
        "message_id": str(row["id"]),
        "session_id": str(row["session_id"]),
        "role": str(row["role"]),
        "content": str(row["content"]),
        "created_at": str(row["created_at"]),
        "metadata": _loads(row["metadata"]),
        "turn_id": str(row["turn_id"] or ""),
        "run_id": str(row["run_id"] or ""),
        "state": str(row["state"] or "committed"),
    }


def _session_title(value: str) -> str:
    cleaned = " ".join(str(value or "").strip().split())
    return cleaned[:28] or "未命名会话"


def _json(value: Any) -> str:
    return json.dumps(
        normalize_repository_paths(value),
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _loads(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
