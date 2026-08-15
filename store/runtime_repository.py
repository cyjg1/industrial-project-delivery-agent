from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from agent.runtime.contracts import RuntimeEvent
from store.database import SQLiteDatabase


class RuntimeRunRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def ensure_schema(self) -> None:
        with self.database.lock, self.database.connect() as connection:
            connection.executescript(
                """
                create table if not exists runtime_runs(
                  id text primary key,
                  kind text not null,
                  project_id text not null,
                  actor_id text not null,
                  session_id text not null default '',
                  turn_id text not null default '',
                  client_request_id text,
                  status text not null,
                  stop_reason text not null default '',
                  input_payload text not null,
                  output_payload text not null default '{}',
                  checkpoint text not null default '{}',
                  recoverable integer not null default 1,
                  error_id text not null default '',
                  error_message text not null default '',
                  created_at text not null,
                  updated_at text not null,
                  started_at text not null default '',
                  completed_at text not null default ''
                );

                create unique index if not exists runtime_runs_client_request
                on runtime_runs(project_id, actor_id, client_request_id)
                where client_request_id is not null;

                create table if not exists runtime_events(
                  seq integer primary key autoincrement,
                  run_id text not null,
                  event_type text not null,
                  round_index integer not null default 0,
                  payload text not null,
                  created_at text not null,
                  foreign key(run_id) references runtime_runs(id) on delete cascade
                );

                create index if not exists runtime_events_run_seq
                on runtime_events(run_id, seq);
                """
            )
            connection.commit()

    def create_run(
        self,
        *,
        run_id: str,
        kind: str,
        project_id: str,
        actor_id: str,
        input_payload: dict[str, Any],
        session_id: str = "",
        turn_id: str = "",
        client_request_id: str = "",
        recoverable: bool = True,
    ) -> dict[str, Any]:
        if not run_id or not kind or not project_id or not actor_id:
            raise ValueError("run_id, kind, project_id and actor_id are required")
        request_key = client_request_id.strip() or None
        now = _now()
        with self.database.lock, self.database.connect() as connection:
            connection.execute("begin immediate")
            if request_key is not None:
                existing = connection.execute(
                    """
                    select * from runtime_runs
                    where project_id = ? and actor_id = ? and client_request_id = ?
                    """,
                    (project_id, actor_id, request_key),
                ).fetchone()
                if existing is not None:
                    connection.commit()
                    return _run_row(existing)
            connection.execute(
                """
                insert into runtime_runs(
                  id, kind, project_id, actor_id, session_id, turn_id,
                  client_request_id, status, input_payload, recoverable,
                  created_at, updated_at
                ) values(?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                (
                    run_id,
                    kind,
                    project_id,
                    actor_id,
                    session_id,
                    turn_id,
                    request_key,
                    _json(input_payload),
                    1 if recoverable else 0,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "select * from runtime_runs where id = ?",
                (run_id,),
            ).fetchone()
            connection.commit()
        assert row is not None
        return _run_row(row)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                "select * from runtime_runs where id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return _run_row(row)

    def list_runs(
        self,
        *,
        kind: str = "",
        statuses: tuple[str, ...] = (),
        project_id: str = "",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if kind:
            clauses.append("kind = ?")
            values.append(kind)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status in ({placeholders})")
            values.extend(statuses)
        if project_id:
            clauses.append("project_id = ?")
            values.append(project_id)
        where = f"where {' and '.join(clauses)}" if clauses else ""
        values.append(max(1, min(int(limit), 1000)))
        with self.database.connect() as connection:
            rows = connection.execute(
                f"select * from runtime_runs {where} order by created_at, id limit ?",
                tuple(values),
            ).fetchall()
        return [_run_row(row) for row in rows]

    def mark_running(self, run_id: str) -> dict[str, Any]:
        return self._update(
            run_id,
            status="running",
            started_at=_now(),
            stop_reason="",
            error_id="",
            error_message="",
        )

    def update_checkpoint(self, run_id: str, checkpoint: dict[str, Any]) -> dict[str, Any]:
        return self._update(run_id, checkpoint=_json(checkpoint))

    def complete_run(
        self,
        run_id: str,
        *,
        stop_reason: str,
        output_payload: dict[str, Any],
    ) -> dict[str, Any]:
        now = _now()
        return self._update(
            run_id,
            status="completed",
            stop_reason=stop_reason,
            output_payload=_json(output_payload),
            completed_at=now,
        )

    def complete_run_with_event(
        self,
        run_id: str,
        *,
        stop_reason: str,
        output_payload: dict[str, Any],
        event_type: str,
        event_payload: dict[str, Any],
    ) -> dict[str, Any]:
        self._terminal_with_event(
            run_id,
            status="completed",
            stop_reason=stop_reason,
            output_payload=output_payload,
            event_type=event_type,
            event_payload=event_payload,
        )
        return self.get_run(run_id)

    def fail_run(
        self,
        run_id: str,
        *,
        error_id: str,
        error_message: str,
        stop_reason: str = "runtime_failed",
    ) -> dict[str, Any]:
        return self._update(
            run_id,
            status="failed",
            stop_reason=stop_reason,
            error_id=error_id,
            error_message=error_message,
            completed_at=_now(),
        )

    def fail_run_with_event(
        self,
        run_id: str,
        *,
        error_id: str,
        error_message: str,
        event_payload: dict[str, Any],
        stop_reason: str = "runtime_failed",
    ) -> dict[str, Any]:
        self._terminal_with_event(
            run_id,
            status="failed",
            stop_reason=stop_reason,
            output_payload={},
            event_type="error",
            event_payload=event_payload,
            error_id=error_id,
            error_message=error_message,
        )
        return self.get_run(run_id)

    def append_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        round_index: int = 0,
        created_at: str = "",
    ) -> dict[str, Any]:
        with self.database.lock, self.database.connect() as connection:
            cursor = connection.execute(
                """
                insert into runtime_events(run_id, event_type, round_index, payload, created_at)
                values(?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    event_type,
                    int(round_index),
                    _json(payload),
                    created_at or _now(),
                ),
            )
            seq = int(cursor.lastrowid)
            row = connection.execute(
                "select * from runtime_events where seq = ?",
                (seq,),
            ).fetchone()
            connection.commit()
        assert row is not None
        return _event_row(row)

    def list_events(self, run_id: str, *, after_seq: int = 0) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                select * from runtime_events
                where run_id = ? and seq > ?
                order by seq
                """,
                (run_id, max(0, int(after_seq))),
            ).fetchall()
        return [_event_row(row) for row in rows]

    def recover_interrupted(self) -> list[str]:
        now = _now()
        with self.database.lock, self.database.connect() as connection:
            recoverable = connection.execute(
                "select id from runtime_runs where status = 'running' and recoverable = 1 order by created_at, id"
            ).fetchall()
            failed = connection.execute(
                "select id from runtime_runs where status = 'running' and recoverable = 0 order by created_at, id"
            ).fetchall()
            connection.execute(
                """
                update runtime_runs
                set status = 'queued', stop_reason = 'recovered_after_restart',
                    updated_at = ?, started_at = ''
                where status = 'running' and recoverable = 1
                """,
                (now,),
            )
            connection.execute(
                """
                update runtime_runs
                set status = 'failed', stop_reason = 'interrupted',
                    error_message = 'Run interrupted by process restart',
                    updated_at = ?, completed_at = ?
                where status = 'running' and recoverable = 0
                """,
                (now, now),
            )
            connection.commit()
        recoverable_ids = {str(row["id"]) for row in recoverable}
        for row in [*recoverable, *failed]:
            run_id = str(row["id"])
            self.append_event(
                run_id,
                "run_recovered" if run_id in recoverable_ids else "run_interrupted",
                {},
            )
        return sorted(recoverable_ids)

    def _update(self, run_id: str, **changes: Any) -> dict[str, Any]:
        allowed = {
            "status",
            "stop_reason",
            "output_payload",
            "checkpoint",
            "error_id",
            "error_message",
            "started_at",
            "completed_at",
        }
        if set(changes) - allowed:
            raise ValueError("Unsupported runtime run field")
        assignments = [f"{key} = ?" for key in changes]
        values = list(changes.values())
        assignments.append("updated_at = ?")
        values.append(_now())
        values.append(run_id)
        with self.database.lock, self.database.connect() as connection:
            cursor = connection.execute(
                f"update runtime_runs set {', '.join(assignments)} where id = ?",
                tuple(values),
            )
            if cursor.rowcount == 0:
                raise KeyError(run_id)
            connection.commit()
        return self.get_run(run_id)

    def _terminal_with_event(
        self,
        run_id: str,
        *,
        status: str,
        stop_reason: str,
        output_payload: dict[str, Any],
        event_type: str,
        event_payload: dict[str, Any],
        error_id: str = "",
        error_message: str = "",
    ) -> None:
        now = _now()
        with self.database.lock, self.database.connect() as connection:
            connection.execute("begin immediate")
            cursor = connection.execute(
                """
                update runtime_runs
                set status = ?, stop_reason = ?, output_payload = ?,
                    error_id = ?, error_message = ?, completed_at = ?, updated_at = ?
                where id = ?
                """,
                (
                    status,
                    stop_reason,
                    _json(output_payload),
                    error_id,
                    error_message,
                    now,
                    now,
                    run_id,
                ),
            )
            if cursor.rowcount == 0:
                connection.rollback()
                raise KeyError(run_id)
            connection.execute(
                """
                insert into runtime_events(run_id, event_type, round_index, payload, created_at)
                values(?, ?, 0, ?, ?)
                """,
                (run_id, event_type, _json(event_payload), now),
            )
            connection.commit()


class SQLiteRuntimeEventSink:
    def __init__(self, repository: RuntimeRunRepository) -> None:
        self.repository = repository

    def append(self, event: RuntimeEvent) -> None:
        self.repository.append_event(
            event.run_id,
            event.kind,
            event.payload,
            round_index=event.round_index,
            created_at=event.created_at,
        )

    def checkpoint(self, run_id: str, checkpoint: dict[str, Any]) -> None:
        self.repository.update_checkpoint(run_id, checkpoint)


class SQLiteRuntimeCheckpointSink:
    """Persists runtime state without duplicating externally translated events."""

    def __init__(self, repository: RuntimeRunRepository) -> None:
        self.repository = repository

    def append(self, event: RuntimeEvent) -> None:
        return None

    def checkpoint(self, run_id: str, checkpoint: dict[str, Any]) -> None:
        self.repository.update_checkpoint(run_id, checkpoint)


def _run_row(row: Any) -> dict[str, Any]:
    return {
        "run_id": str(row["id"]),
        "kind": str(row["kind"]),
        "project_id": str(row["project_id"]),
        "actor_id": str(row["actor_id"]),
        "session_id": str(row["session_id"]),
        "turn_id": str(row["turn_id"]),
        "client_request_id": str(row["client_request_id"] or ""),
        "status": str(row["status"]),
        "stop_reason": str(row["stop_reason"]),
        "input_payload": _loads(row["input_payload"]),
        "output_payload": _loads(row["output_payload"]),
        "checkpoint": _loads(row["checkpoint"]),
        "recoverable": bool(row["recoverable"]),
        "error_id": str(row["error_id"]),
        "error_message": str(row["error_message"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "started_at": str(row["started_at"]),
        "completed_at": str(row["completed_at"]),
    }


def _event_row(row: Any) -> dict[str, Any]:
    return {
        "seq": int(row["seq"]),
        "run_id": str(row["run_id"]),
        "event_type": str(row["event_type"]),
        "round_index": int(row["round_index"]),
        "payload": _loads(row["payload"]),
        "created_at": str(row["created_at"]),
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _loads(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
