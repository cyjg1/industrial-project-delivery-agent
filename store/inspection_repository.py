from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from store.database import SQLiteDatabase


class InspectionJobRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def ensure_schema(self) -> None:
        with self.database.lock, self.database.connect() as connection:
            connection.executescript(
                """
                create table if not exists inspection_jobs(
                  id text primary key,
                  org_id text not null,
                  project_id text not null,
                  topic_id text,
                  actor_id text not null,
                  author_id text not null,
                  sensitivity text not null default 'l3',
                  trigger_type text not null,
                  entity_type text not null default '',
                  entity_id text not null default '',
                  event_id text not null default '',
                  source_ids text not null default '[]',
                  payload text not null default '{}',
                  idempotency_key text not null unique,
                  status text not null,
                  attempts integer not null default 0,
                  max_attempts integer not null default 3,
                  lease_owner text not null default '',
                  lease_expires_at text not null default '',
                  result_run_id text not null default '',
                  result text not null default '{}',
                  error text not null default '',
                  created_at text not null,
                  updated_at text not null,
                  completed_at text not null default ''
                );

                create index if not exists inspection_jobs_status_created
                on inspection_jobs(status, created_at, id);

                create table if not exists inspection_dispatch_state(
                  name text primary key,
                  value text not null,
                  updated_at text not null
                );
                """
            )
            latest_event = connection.execute(
                "select coalesce(max(rowid), 0) as value from events"
            ).fetchone()
            connection.execute(
                """
                insert or ignore into inspection_dispatch_state(name, value, updated_at)
                values('project_events', ?, ?)
                """,
                (str(int(latest_event["value"] or 0)), _now()),
            )
            connection.commit()

    def enqueue(
        self,
        *,
        org_id: str,
        project_id: str,
        actor_id: str,
        trigger_type: str,
        idempotency_key: str,
        author_id: str = "",
        topic_id: str | None = None,
        sensitivity: str = "l3",
        entity_type: str = "",
        entity_id: str = "",
        event_id: str = "",
        source_ids: list[str] | None = None,
        payload: dict[str, Any] | None = None,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        required = (org_id, project_id, actor_id, trigger_type, idempotency_key)
        if any(not str(value).strip() for value in required):
            raise ValueError(
                "org_id, project_id, actor_id, trigger_type and idempotency_key are required"
            )
        if sensitivity not in {"l1", "l2", "l3", "l4"}:
            raise ValueError(f"Unsupported inspection sensitivity: {sensitivity}")
        now = _now()
        job_id = f"inspection_{uuid4().hex}"
        with self.database.lock, self.database.connect() as connection:
            connection.execute("begin immediate")
            existing = connection.execute(
                "select * from inspection_jobs where idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                connection.commit()
                row = _job_row(existing)
                row["created"] = False
                return row
            connection.execute(
                """
                insert into inspection_jobs(
                  id, org_id, project_id, topic_id, actor_id, author_id,
                  sensitivity, trigger_type, entity_type, entity_id, event_id,
                  source_ids, payload, idempotency_key, status, max_attempts,
                  created_at, updated_at
                ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    job_id,
                    org_id,
                    project_id,
                    topic_id,
                    actor_id,
                    author_id or actor_id,
                    sensitivity,
                    trigger_type,
                    entity_type,
                    entity_id,
                    event_id,
                    _json(list(dict.fromkeys(source_ids or []))),
                    _json(payload or {}),
                    idempotency_key,
                    max(1, int(max_attempts)),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "select * from inspection_jobs where id = ?",
                (job_id,),
            ).fetchone()
            connection.commit()
        assert row is not None
        result = _job_row(row)
        result["created"] = True
        return result

    def claim_next(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 300,
    ) -> dict[str, Any] | None:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        expires = (now + timedelta(seconds=max(30, int(lease_seconds)))).isoformat()
        with self.database.lock, self.database.connect() as connection:
            connection.execute("begin immediate")
            connection.execute(
                """
                update inspection_jobs
                set status = 'queued', lease_owner = '', lease_expires_at = '',
                    error = 'Recovered expired worker lease', updated_at = ?
                where status = 'running' and lease_expires_at <> '' and lease_expires_at < ?
                """,
                (now_text, now_text),
            )
            row = connection.execute(
                """
                select * from inspection_jobs
                where status = 'queued' and attempts < max_attempts
                order by created_at, id
                limit 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                update inspection_jobs
                set status = 'running', attempts = attempts + 1,
                    lease_owner = ?, lease_expires_at = ?, error = '', updated_at = ?
                where id = ? and status = 'queued'
                """,
                (worker_id, expires, now_text, row["id"]),
            )
            claimed = connection.execute(
                "select * from inspection_jobs where id = ?",
                (row["id"],),
            ).fetchone()
            connection.commit()
        assert claimed is not None
        return _job_row(claimed)

    def complete(
        self,
        job_id: str,
        *,
        result_run_id: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        return self._update(
            job_id,
            status="completed",
            result_run_id=result_run_id,
            result=_json(result),
            error="",
            lease_owner="",
            lease_expires_at="",
            completed_at=_now(),
        )

    def fail(self, job_id: str, *, error: str) -> dict[str, Any]:
        current = self.get(job_id)
        terminal = current["attempts"] >= current["max_attempts"]
        return self._update(
            job_id,
            status="failed" if terminal else "queued",
            error=error,
            lease_owner="",
            lease_expires_at="",
            completed_at=_now() if terminal else "",
        )

    def get(self, job_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                "select * from inspection_jobs where id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return _job_row(row)

    def list(
        self,
        *,
        project_id: str = "",
        statuses: tuple[str, ...] = (),
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if project_id:
            clauses.append("project_id = ?")
            values.append(project_id)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status in ({placeholders})")
            values.extend(statuses)
        where = f"where {' and '.join(clauses)}" if clauses else ""
        values.append(max(1, min(int(limit), 1000)))
        with self.database.connect() as connection:
            rows = connection.execute(
                f"select * from inspection_jobs {where} order by created_at desc, id desc limit ?",
                tuple(values),
            ).fetchall()
        return [_job_row(row) for row in rows]

    def event_records_after(self, rowid: int, *, limit: int = 500) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                select rowid as event_rowid, * from events
                where rowid > ? order by rowid limit ?
                """,
                (max(0, int(rowid)), max(1, min(int(limit), 2000))),
            ).fetchall()
        return [
            {
                "rowid": int(row["event_rowid"]),
                "event_id": str(row["id"]),
                "entity": str(row["entity"]),
                "entity_id": str(row["entity_id"]),
                "action": str(row["action"]),
                "payload": _loads(row["payload"], {}),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def dispatch_cursor(self, name: str = "project_events") -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                "select value from inspection_dispatch_state where name = ?",
                (name,),
            ).fetchone()
        try:
            return int(row["value"]) if row is not None else 0
        except (TypeError, ValueError):
            return 0

    def set_dispatch_cursor(self, rowid: int, name: str = "project_events") -> None:
        with self.database.lock, self.database.connect() as connection:
            connection.execute(
                """
                insert into inspection_dispatch_state(name, value, updated_at)
                values(?, ?, ?)
                on conflict(name) do update set value=excluded.value, updated_at=excluded.updated_at
                """,
                (name, str(max(0, int(rowid))), _now()),
            )
            connection.commit()

    def _update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        allowed = {
            "status", "result_run_id", "result", "error", "lease_owner",
            "lease_expires_at", "completed_at",
        }
        if set(changes) - allowed:
            raise ValueError("Unsupported inspection job field")
        assignments = [f"{key} = ?" for key in changes]
        values = list(changes.values())
        assignments.append("updated_at = ?")
        values.extend((_now(), job_id))
        with self.database.lock, self.database.connect() as connection:
            cursor = connection.execute(
                f"update inspection_jobs set {', '.join(assignments)} where id = ?",
                tuple(values),
            )
            if cursor.rowcount == 0:
                raise KeyError(job_id)
            connection.commit()
        return self.get(job_id)


def _job_row(row: Any) -> dict[str, Any]:
    return {
        "job_id": str(row["id"]),
        "org_id": str(row["org_id"]),
        "project_id": str(row["project_id"]),
        "topic_id": str(row["topic_id"]) if row["topic_id"] is not None else None,
        "actor_id": str(row["actor_id"]),
        "author_id": str(row["author_id"]),
        "sensitivity": str(row["sensitivity"]),
        "trigger_type": str(row["trigger_type"]),
        "entity_type": str(row["entity_type"]),
        "entity_id": str(row["entity_id"]),
        "event_id": str(row["event_id"]),
        "source_ids": _loads(row["source_ids"], []),
        "payload": _loads(row["payload"], {}),
        "idempotency_key": str(row["idempotency_key"]),
        "status": str(row["status"]),
        "attempts": int(row["attempts"]),
        "max_attempts": int(row["max_attempts"]),
        "lease_owner": str(row["lease_owner"]),
        "lease_expires_at": str(row["lease_expires_at"]),
        "result_run_id": str(row["result_run_id"]),
        "result": _loads(row["result"], {}),
        "error": str(row["error"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "completed_at": str(row["completed_at"]),
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except (json.JSONDecodeError, TypeError):
        return default


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
