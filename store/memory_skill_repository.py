from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable

from agent.access_policy import AccessContext, User, visible, visible_filter
from store.database import SQLiteDatabase


class MemorySkillEvolutionRepository:
    """Persistence for governed runtime memory-to-skill evolution."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def ensure_schema(self) -> None:
        with self.database.lock, self.database.connect() as connection:
            connection.executescript(
                """
                create table if not exists agent_episodes(
                  id text primary key,
                  run_id text not null unique,
                  kind text not null,
                  org_id text not null,
                  project_id text not null,
                  topic_id text,
                  author_id text not null,
                  sensitivity text not null,
                  session_id text not null default '',
                  turn_id text not null default '',
                  relation text not null,
                  status text not null,
                  terminal_signal text not null default '',
                  reward_value real,
                  reward_source text not null default 'pending',
                  feedback_status text not null default 'pending',
                  feedback_note text not null default '',
                  created_at text not null,
                  closed_at text not null default '',
                  payload text not null
                );

                create index if not exists agent_episodes_project_closed
                on agent_episodes(project_id, closed_at desc, id);

                create index if not exists agent_episodes_session_closed
                on agent_episodes(session_id, closed_at desc, id);

                create table if not exists agent_trace_memories(
                  id text primary key,
                  episode_id text not null,
                  run_id text not null,
                  seq_index integer not null,
                  round_index integer not null default 0,
                  org_id text not null,
                  project_id text not null,
                  topic_id text,
                  author_id text not null,
                  sensitivity text not null,
                  state_summary text not null,
                  action_kind text not null,
                  action_name text not null,
                  action_summary text not null,
                  observation_summary text not null,
                  verification_status text not null,
                  public_reflection text not null,
                  value real,
                  evidence_event_seqs text not null,
                  created_at text not null,
                  payload text not null,
                  unique(run_id, seq_index)
                );

                create index if not exists agent_trace_memories_project_value
                on agent_trace_memories(project_id, value desc, created_at desc);

                create index if not exists agent_trace_memories_episode
                on agent_trace_memories(episode_id, seq_index);

                create table if not exists project_skill_retrievals(
                  id text primary key,
                  run_id text not null,
                  episode_id text not null default '',
                  skill_id text not null,
                  version_id text not null,
                  org_id text not null,
                  project_id text not null,
                  topic_id text,
                  author_id text not null,
                  sensitivity text not null,
                  query text not null,
                  rank integer not null,
                  relevance_score real not null,
                  retrieval_tier text not null,
                  attribution text not null default 'retrieved',
                  outcome text not null default 'pending',
                  reward_value real,
                  created_at text not null,
                  updated_at text not null,
                  payload text not null,
                  unique(run_id, skill_id, version_id)
                );

                create index if not exists project_skill_retrievals_skill_outcome
                on project_skill_retrievals(skill_id, version_id, outcome, updated_at desc);

                create index if not exists project_skill_retrievals_project_run
                on project_skill_retrievals(project_id, run_id);

                create table if not exists agent_execution_policies(
                  id text primary key,
                  signature text not null,
                  org_id text not null,
                  project_id text not null,
                  topic_id text,
                  author_id text not null,
                  sensitivity text not null,
                  status text not null,
                  trigger_text text not null,
                  procedure text not null,
                  verification text not null,
                  boundaries text not null,
                  support_episode_ids text not null,
                  counter_episode_ids text not null,
                  gain real not null,
                  stability real not null,
                  approved_by text not null default '',
                  approved_at text not null default '',
                  created_at text not null,
                  updated_at text not null,
                  payload text not null,
                  unique(project_id, signature)
                );

                create index if not exists agent_execution_policies_project_status
                on agent_execution_policies(project_id, status, stability desc, updated_at desc);

                create table if not exists environment_cognitions(
                  id text primary key,
                  signature text not null,
                  org_id text not null,
                  project_id text not null,
                  topic_id text,
                  author_id text not null,
                  sensitivity text not null,
                  status text not null,
                  entities text not null,
                  structures text not null,
                  regularities text not null,
                  constraints_json text not null,
                  support_policy_ids text not null,
                  confidence real not null,
                  approved_by text not null default '',
                  approved_at text not null default '',
                  created_at text not null,
                  updated_at text not null,
                  payload text not null,
                  unique(project_id, signature)
                );

                create index if not exists environment_cognitions_project_status
                on environment_cognitions(project_id, status, confidence desc, updated_at desc);

                create table if not exists memory_evolution_events(
                  seq integer primary key autoincrement,
                  entity_type text not null,
                  entity_id text not null,
                  action text not null,
                  actor_id text not null,
                  project_id text not null,
                  payload text not null,
                  created_at text not null
                );

                create index if not exists memory_evolution_events_entity
                on memory_evolution_events(entity_type, entity_id, seq);
                """
            )
            connection.commit()

    def upsert_episode(self, row: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        with self.database.lock, self.database.connect() as connection:
            connection.execute(
                """
                insert into agent_episodes(
                  id, run_id, kind, org_id, project_id, topic_id, author_id,
                  sensitivity, session_id, turn_id, relation, status,
                  terminal_signal, reward_value, reward_source, feedback_status,
                  feedback_note, created_at, closed_at, payload
                ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(run_id) do update set
                  status = excluded.status,
                  terminal_signal = excluded.terminal_signal,
                  reward_value = case
                    when agent_episodes.reward_source = 'human' then agent_episodes.reward_value
                    else excluded.reward_value
                  end,
                  reward_source = case
                    when agent_episodes.reward_source = 'human' then agent_episodes.reward_source
                    else excluded.reward_source
                  end,
                  closed_at = excluded.closed_at,
                  payload = excluded.payload
                """,
                (
                    row["episode_id"],
                    row["run_id"],
                    row.get("kind", "conversation"),
                    row["org_id"],
                    row["project_id"],
                    row.get("topic_id"),
                    row["author_id"],
                    row.get("sensitivity", "l2"),
                    row.get("session_id", ""),
                    row.get("turn_id", ""),
                    row["relation"],
                    row["status"],
                    row.get("terminal_signal", ""),
                    row.get("reward_value"),
                    row.get("reward_source", "pending"),
                    row.get("feedback_status", "pending"),
                    row.get("feedback_note", ""),
                    row.get("created_at") or now,
                    row.get("closed_at") or now,
                    _json(row.get("payload") or {}),
                ),
            )
            connection.commit()
        return self.get_episode_by_run(row["run_id"])

    def get_episode(self, episode_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                "select * from agent_episodes where id = ?",
                (episode_id,),
            ).fetchone()
        if row is None:
            raise KeyError(episode_id)
        return _episode_row(row)

    def get_episode_by_run(self, run_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                "select * from agent_episodes where run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return _episode_row(row)

    def list_episodes(
        self,
        *,
        project_id: str,
        session_id: str = "",
        actor: User | None = None,
        ctx: AccessContext | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses = ["project_id = ?"]
        values: list[Any] = [project_id]
        if session_id:
            clauses.append("session_id = ?")
            values.append(session_id)
        values.append(max(1, min(int(limit), 1000)))
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                select * from agent_episodes
                where {' and '.join(clauses)}
                order by closed_at desc, id desc
                limit ?
                """,
                tuple(values),
            ).fetchall()
        result = [_episode_row(row) for row in rows]
        if actor is not None and ctx is not None:
            result = visible_filter(actor, result, ctx)
        return result

    def update_episode_feedback(
        self,
        episode_id: str,
        *,
        feedback_status: str,
        feedback_note: str,
        reward_value: float,
        actor_id: str,
    ) -> dict[str, Any]:
        if feedback_status not in {"accepted", "corrected", "rejected"}:
            raise ValueError("不支持的运行反馈状态")
        with self.database.lock, self.database.connect() as connection:
            cursor = connection.execute(
                """
                update agent_episodes
                set feedback_status = ?, feedback_note = ?, reward_value = ?,
                    reward_source = 'human'
                where id = ?
                """,
                (feedback_status, feedback_note, float(reward_value), episode_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(episode_id)
            connection.execute(
                """
                update project_skill_retrievals
                set outcome = ?, reward_value = ?, updated_at = ?
                where episode_id = ?
                """,
                (feedback_status, float(reward_value), _now(), episode_id),
            )
            connection.commit()
        episode = self.get_episode(episode_id)
        self.append_event(
            "episode",
            episode_id,
            "human_feedback_recorded",
            actor_id=actor_id,
            project_id=episode["project_id"],
            payload={
                "feedback_status": feedback_status,
                "feedback_note": feedback_note,
                "reward_value": reward_value,
            },
        )
        return episode

    def upsert_trace(self, row: dict[str, Any]) -> dict[str, Any]:
        with self.database.lock, self.database.connect() as connection:
            connection.execute(
                """
                insert into agent_trace_memories(
                  id, episode_id, run_id, seq_index, round_index, org_id,
                  project_id, topic_id, author_id, sensitivity, state_summary,
                  action_kind, action_name, action_summary, observation_summary,
                  verification_status, public_reflection, value,
                  evidence_event_seqs, created_at, payload
                ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(run_id, seq_index) do update set
                  state_summary = excluded.state_summary,
                  action_kind = excluded.action_kind,
                  action_name = excluded.action_name,
                  action_summary = excluded.action_summary,
                  observation_summary = excluded.observation_summary,
                  verification_status = excluded.verification_status,
                  public_reflection = excluded.public_reflection,
                  value = excluded.value,
                  evidence_event_seqs = excluded.evidence_event_seqs,
                  payload = excluded.payload
                """,
                (
                    row["trace_id"],
                    row["episode_id"],
                    row["run_id"],
                    int(row["seq_index"]),
                    int(row.get("round_index") or 0),
                    row["org_id"],
                    row["project_id"],
                    row.get("topic_id"),
                    row["author_id"],
                    row.get("sensitivity", "l2"),
                    row["state_summary"],
                    row["action_kind"],
                    row["action_name"],
                    row["action_summary"],
                    row["observation_summary"],
                    row["verification_status"],
                    row["public_reflection"],
                    row.get("value"),
                    _json(row.get("evidence_event_seqs") or []),
                    row.get("created_at") or _now(),
                    _json(row.get("payload") or {}),
                ),
            )
            connection.commit()
        return self.get_trace(row["trace_id"])

    def get_trace(self, trace_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                "select * from agent_trace_memories where id = ?",
                (trace_id,),
            ).fetchone()
        if row is None:
            raise KeyError(trace_id)
        return _trace_row(row)

    def list_traces(
        self,
        *,
        project_id: str,
        episode_id: str = "",
        actor: User | None = None,
        ctx: AccessContext | None = None,
        min_value: float | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses = ["project_id = ?"]
        values: list[Any] = [project_id]
        if episode_id:
            clauses.append("episode_id = ?")
            values.append(episode_id)
        if min_value is not None:
            clauses.append("value >= ?")
            values.append(float(min_value))
        values.append(max(1, min(int(limit), 1000)))
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                select * from agent_trace_memories
                where {' and '.join(clauses)}
                order by value desc, created_at desc, id
                limit ?
                """,
                tuple(values),
            ).fetchall()
        result = [_trace_row(row) for row in rows]
        if actor is not None and ctx is not None:
            result = visible_filter(actor, result, ctx)
        return result

    def revalue_episode_traces(
        self,
        episode_id: str,
        *,
        terminal_reward: float,
        gamma: float,
    ) -> list[dict[str, Any]]:
        with self.database.lock, self.database.connect() as connection:
            rows = connection.execute(
                """
                select id, seq_index
                from agent_trace_memories
                where episode_id = ?
                order by seq_index
                """,
                (episode_id,),
            ).fetchall()
            count = len(rows)
            for index, row in enumerate(rows):
                value = float(terminal_reward) * (float(gamma) ** (count - index - 1))
                connection.execute(
                    "update agent_trace_memories set value = ? where id = ?",
                    (value, str(row["id"])),
                )
            connection.commit()
        if not rows:
            return []
        episode = self.get_episode(episode_id)
        return self.list_traces(
            project_id=episode["project_id"],
            episode_id=episode_id,
            limit=1000,
        )

    def record_skill_retrievals(
        self,
        *,
        run_id: str,
        query: str,
        rows: Iterable[dict[str, Any]],
        actor_id: str,
    ) -> list[dict[str, Any]]:
        now = _now()
        saved_ids: list[str] = []
        with self.database.lock, self.database.connect() as connection:
            for rank, row in enumerate(rows, start=1):
                skill_id = str(row.get("skill_id") or row.get("id") or "").strip()
                version_id = str(row.get("version_id") or "").strip()
                if not version_id and row.get("version") not in {None, ""}:
                    version_id = f"{skill_id}_v{row['version']}"
                if not skill_id or not version_id:
                    continue
                retrieval_id = _stable_id(
                    "skill_retrieval",
                    run_id,
                    skill_id,
                    version_id,
                )
                connection.execute(
                    """
                    insert into project_skill_retrievals(
                      id, run_id, episode_id, skill_id, version_id, org_id,
                      project_id, topic_id, author_id, sensitivity, query, rank,
                      relevance_score, retrieval_tier, attribution, outcome,
                      reward_value, created_at, updated_at, payload
                    ) values(?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'retrieved',
                      'pending', null, ?, ?, ?)
                    on conflict(run_id, skill_id, version_id) do update set
                      query = excluded.query,
                      rank = excluded.rank,
                      relevance_score = excluded.relevance_score,
                      updated_at = excluded.updated_at,
                      payload = excluded.payload
                    """,
                    (
                        retrieval_id,
                        run_id,
                        skill_id,
                        version_id,
                        str(row.get("org_id") or ""),
                        str(row.get("project_id") or ""),
                        row.get("topic_id"),
                        actor_id,
                        str(row.get("sensitivity") or "l2"),
                        query,
                        rank,
                        float(row.get("retrieval_score") or row.get("relevance_score") or 0.0),
                        str(row.get("retrieval_tier") or "project_skill"),
                        now,
                        now,
                        _json(
                            {
                                "name": row.get("name") or "",
                                "usage_lifecycle": (
                                    row.get("reliability") or {}
                                ).get("lifecycle", "probationary"),
                            }
                        ),
                    ),
                )
                saved_ids.append(retrieval_id)
            connection.commit()
        return [self.get_skill_retrieval(retrieval_id) for retrieval_id in saved_ids]

    def get_skill_retrieval(self, retrieval_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                "select * from project_skill_retrievals where id = ?",
                (retrieval_id,),
            ).fetchone()
        if row is None:
            raise KeyError(retrieval_id)
        return _skill_retrieval_row(row)

    def finalize_skill_retrievals(
        self,
        *,
        run_id: str,
        episode_id: str,
        outcome: str,
        reward_value: float,
    ) -> None:
        if outcome not in {"success", "failure", "accepted", "corrected", "rejected"}:
            raise ValueError("不支持的方法技能调用结果")
        with self.database.lock, self.database.connect() as connection:
            connection.execute(
                """
                update project_skill_retrievals
                set episode_id = ?, outcome = ?, reward_value = ?, updated_at = ?
                where run_id = ?
                """,
                (episode_id, outcome, float(reward_value), _now(), run_id),
            )
            connection.commit()

    def skill_reliability(self, skill_id: str, version_id: str = "") -> dict[str, Any]:
        clauses = ["skill_id = ?", "outcome <> 'pending'"]
        values: list[Any] = [skill_id]
        count_clauses = ["skill_id = ?"]
        count_values: list[Any] = [skill_id]
        if version_id:
            clauses.append("version_id = ?")
            values.append(version_id)
            count_clauses.append("version_id = ?")
            count_values.append(version_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                select outcome, episode_id, reward_value
                from project_skill_retrievals
                where {' and '.join(clauses)}
                """,
                tuple(values),
            ).fetchall()
            retrieval_count = connection.execute(
                f"""
                select count(*) as count
                from project_skill_retrievals
                where {' and '.join(count_clauses)}
                """,
                tuple(count_values),
            ).fetchone()
        successes = sum(1 for row in rows if row["outcome"] in {"success", "accepted"})
        failures = sum(1 for row in rows if row["outcome"] in {"failure", "corrected", "rejected"})
        episode_count = len({str(row["episode_id"]) for row in rows if str(row["episode_id"])})
        attempts = successes + failures
        reliability = (successes + 1.0) / (attempts + 2.0)
        lifecycle = (
            "probationary"
            if episode_count < 3
            else "active"
            if reliability >= 0.6
            else "revalidation_required"
        )
        return {
            "skill_id": skill_id,
            "version_id": version_id,
            "retrieval_count": int(retrieval_count["count"] or 0),
            "episode_count": episode_count,
            "success_count": successes,
            "failure_count": failures,
            "reliability": round(reliability, 4),
            "lifecycle": lifecycle,
            "activation_gate": {
                "minimum_distinct_episodes": 3,
                "minimum_reliability": 0.6,
                "passed": lifecycle == "active",
            },
        }

    def list_skill_reliability(
        self,
        *,
        project_id: str,
        actor: User | None = None,
        ctx: AccessContext | None = None,
    ) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                select distinct skill_id, version_id, org_id, project_id, topic_id,
                  author_id, sensitivity
                from project_skill_retrievals
                where project_id = ?
                order by skill_id, version_id
                """,
                (project_id,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            tags = {
                "id": str(row["skill_id"]),
                "org_id": str(row["org_id"]),
                "project_id": str(row["project_id"]),
                "topic_id": row["topic_id"],
                "author_id": str(row["author_id"]),
                "sensitivity": str(row["sensitivity"]),
            }
            if actor is not None and ctx is not None and not visible(actor, tags, ctx):
                continue
            result.append(
                {
                    **tags,
                    **self.skill_reliability(
                        str(row["skill_id"]),
                        str(row["version_id"]),
                    ),
                }
            )
        return result

    def upsert_policy_candidate(self, row: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        revalidated_cognition_ids: list[str] = []
        with self.database.lock, self.database.connect() as connection:
            existing = connection.execute(
                """
                select * from agent_execution_policies
                where project_id = ? and signature = ?
                """,
                (row["project_id"], row["signature"]),
            ).fetchone()
            policy_id = str(existing["id"]) if existing is not None else row["policy_id"]
            current_status = str(existing["status"]) if existing is not None else "candidate"
            if current_status == "retired":
                status = "retired"
            elif current_status == "approved" and float(row["stability"]) < 0.6:
                status = "revalidation_required"
            else:
                status = current_status
            connection.execute(
                """
                insert into agent_execution_policies(
                  id, signature, org_id, project_id, topic_id, author_id,
                  sensitivity, status, trigger_text, procedure, verification,
                  boundaries, support_episode_ids, counter_episode_ids, gain,
                  stability, approved_by, approved_at, created_at, updated_at,
                  payload
                ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '',
                  ?, ?, ?)
                on conflict(project_id, signature) do update set
                  status = excluded.status,
                  support_episode_ids = excluded.support_episode_ids,
                  counter_episode_ids = excluded.counter_episode_ids,
                  gain = excluded.gain,
                  stability = excluded.stability,
                  updated_at = excluded.updated_at,
                  payload = excluded.payload
                """,
                (
                    policy_id,
                    row["signature"],
                    row["org_id"],
                    row["project_id"],
                    row.get("topic_id"),
                    row["author_id"],
                    row.get("sensitivity", "l2"),
                    status,
                    row["trigger_text"],
                    _json(row["procedure"]),
                    _json(row["verification"]),
                    _json(row["boundaries"]),
                    _json(row["support_episode_ids"]),
                    _json(row.get("counter_episode_ids") or []),
                    float(row["gain"]),
                    float(row["stability"]),
                    row.get("created_at") or now,
                    now,
                    _json(row.get("payload") or {}),
                ),
            )
            if status in {"revalidation_required", "retired"}:
                revalidated_cognition_ids = _revalidate_supported_cognitions(
                    connection,
                    project_id=row["project_id"],
                    policy_id=policy_id,
                    now=now,
                )
            connection.commit()
        for cognition_id in revalidated_cognition_ids:
            self.append_event(
                "environment_cognition",
                cognition_id,
                "cognition_revalidation_required",
                actor_id="system",
                project_id=row["project_id"],
                payload={
                    "reason": "support_policy_no_longer_approved",
                    "support_policy_id": policy_id,
                },
            )
        return self.get_policy(policy_id)

    def get_policy(self, policy_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                "select * from agent_execution_policies where id = ?",
                (policy_id,),
            ).fetchone()
        if row is None:
            raise KeyError(policy_id)
        return _policy_row(row)

    def list_policies(
        self,
        *,
        project_id: str,
        statuses: tuple[str, ...] = (),
        actor: User | None = None,
        ctx: AccessContext | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses = ["project_id = ?"]
        values: list[Any] = [project_id]
        if statuses:
            clauses.append(f"status in ({','.join('?' for _ in statuses)})")
            values.extend(statuses)
        values.append(max(1, min(int(limit), 1000)))
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                select * from agent_execution_policies
                where {' and '.join(clauses)}
                order by stability desc, gain desc, updated_at desc, id
                limit ?
                """,
                tuple(values),
            ).fetchall()
        result = [_policy_row(row) for row in rows]
        if actor is not None and ctx is not None:
            result = visible_filter(actor, result, ctx)
        return result

    def update_policy_status(
        self,
        policy_id: str,
        *,
        status: str,
        actor_id: str,
    ) -> dict[str, Any]:
        if status not in {"approved", "retired"}:
            raise ValueError("不支持的执行策略状态")
        current = self.get_policy(policy_id)
        if status == "approved":
            if current["status"] == "retired":
                raise ValueError("已停用的执行策略不能重新批准")
            if len(set(current["support_episode_ids"])) < 2:
                raise ValueError("执行策略至少需要两次不同运行记录支撑")
            if current["stability"] < 0.6:
                raise ValueError("执行策略尚未通过稳定度门禁")
            if not current["verification"] or not current["boundaries"]:
                raise ValueError("执行策略必须包含校验条件和适用边界")
        now = _now()
        revalidated_cognition_ids: list[str] = []
        with self.database.lock, self.database.connect() as connection:
            connection.execute(
                """
                update agent_execution_policies
                set status = ?, approved_by = ?, approved_at = ?, updated_at = ?
                where id = ?
                """,
                (
                    status,
                    actor_id if status == "approved" else current["approved_by"],
                    now if status == "approved" else current["approved_at"],
                    now,
                    policy_id,
                ),
            )
            if status == "retired":
                revalidated_cognition_ids = _revalidate_supported_cognitions(
                    connection,
                    project_id=current["project_id"],
                    policy_id=policy_id,
                    now=now,
                )
            connection.commit()
        self.append_event(
            "execution_policy",
            policy_id,
            f"policy_{status}",
            actor_id=actor_id,
            project_id=current["project_id"],
            payload={"previous_status": current["status"]},
        )
        for cognition_id in revalidated_cognition_ids:
            self.append_event(
                "environment_cognition",
                cognition_id,
                "cognition_revalidation_required",
                actor_id=actor_id,
                project_id=current["project_id"],
                payload={
                    "reason": "support_policy_retired",
                    "support_policy_id": policy_id,
                },
            )
        return self.get_policy(policy_id)

    def require_policy_revalidation(
        self,
        policy_id: str,
        *,
        actor_id: str,
        reason: str,
    ) -> dict[str, Any]:
        current = self.get_policy(policy_id)
        if current["status"] != "approved":
            return current
        now = _now()
        with self.database.lock, self.database.connect() as connection:
            connection.execute(
                """
                update agent_execution_policies
                set status = 'revalidation_required', updated_at = ?
                where id = ? and status = 'approved'
                """,
                (now, policy_id),
            )
            cognition_ids = _revalidate_supported_cognitions(
                connection,
                project_id=current["project_id"],
                policy_id=policy_id,
                now=now,
            )
            connection.commit()
        self.append_event(
            "execution_policy",
            policy_id,
            "policy_revalidation_required",
            actor_id=actor_id,
            project_id=current["project_id"],
            payload={"reason": reason},
        )
        for cognition_id in cognition_ids:
            self.append_event(
                "environment_cognition",
                cognition_id,
                "cognition_revalidation_required",
                actor_id=actor_id,
                project_id=current["project_id"],
                payload={
                    "reason": "support_policy_revalidation_required",
                    "support_policy_id": policy_id,
                },
            )
        return self.get_policy(policy_id)

    def upsert_cognition_candidate(self, row: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        with self.database.lock, self.database.connect() as connection:
            existing = connection.execute(
                """
                select * from environment_cognitions
                where project_id = ? and signature = ?
                """,
                (row["project_id"], row["signature"]),
            ).fetchone()
            current_status = str(existing["status"]) if existing is not None else "candidate"
            if current_status == "retired":
                status = "retired"
            elif current_status == "approved" and float(row["confidence"]) < 0.6:
                status = "revalidation_required"
            else:
                status = current_status
            connection.execute(
                """
                insert into environment_cognitions(
                  id, signature, org_id, project_id, topic_id, author_id,
                  sensitivity, status, entities, structures, regularities,
                  constraints_json, support_policy_ids, confidence, approved_by,
                  approved_at, created_at, updated_at, payload
                ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', ?, ?, ?)
                on conflict(project_id, signature) do update set
                  support_policy_ids = excluded.support_policy_ids,
                  confidence = excluded.confidence,
                  updated_at = excluded.updated_at,
                  payload = excluded.payload
                """,
                (
                    row["cognition_id"],
                    row["signature"],
                    row["org_id"],
                    row["project_id"],
                    row.get("topic_id"),
                    row["author_id"],
                    row.get("sensitivity", "l2"),
                    status,
                    _json(row["entities"]),
                    _json(row["structures"]),
                    _json(row["regularities"]),
                    _json(row["constraints"]),
                    _json(row["support_policy_ids"]),
                    float(row["confidence"]),
                    row.get("created_at") or now,
                    now,
                    _json(row.get("payload") or {}),
                ),
            )
            connection.commit()
        return self.get_cognition(row["cognition_id"])

    def get_cognition(self, cognition_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                "select * from environment_cognitions where id = ?",
                (cognition_id,),
            ).fetchone()
        if row is None:
            raise KeyError(cognition_id)
        return _cognition_row(row)

    def list_cognitions(
        self,
        *,
        project_id: str,
        statuses: tuple[str, ...] = (),
        actor: User | None = None,
        ctx: AccessContext | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses = ["project_id = ?"]
        values: list[Any] = [project_id]
        if statuses:
            clauses.append(f"status in ({','.join('?' for _ in statuses)})")
            values.extend(statuses)
        values.append(max(1, min(int(limit), 1000)))
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                select * from environment_cognitions
                where {' and '.join(clauses)}
                order by confidence desc, updated_at desc, id
                limit ?
                """,
                tuple(values),
            ).fetchall()
        result = [_cognition_row(row) for row in rows]
        if actor is not None and ctx is not None:
            result = visible_filter(actor, result, ctx)
        return result

    def update_cognition_status(
        self,
        cognition_id: str,
        *,
        status: str,
        actor_id: str,
    ) -> dict[str, Any]:
        if status not in {"approved", "retired"}:
            raise ValueError("不支持的环境认知状态")
        current = self.get_cognition(cognition_id)
        if status == "approved":
            if current["status"] == "retired":
                raise ValueError("已停用的环境认知不能重新批准")
            if len(set(current["support_policy_ids"])) < 2:
                raise ValueError("环境认知至少需要两条执行策略支撑")
            support = [
                self.get_policy(policy_id)
                for policy_id in current["support_policy_ids"]
            ]
            if any(policy["status"] != "approved" for policy in support):
                raise ValueError("所有支撑策略都必须先经过人工批准")
            if current["confidence"] < 0.6:
                raise ValueError("环境认知尚未通过置信度门禁")
        now = _now()
        with self.database.lock, self.database.connect() as connection:
            connection.execute(
                """
                update environment_cognitions
                set status = ?, approved_by = ?, approved_at = ?, updated_at = ?
                where id = ?
                """,
                (
                    status,
                    actor_id if status == "approved" else current["approved_by"],
                    now if status == "approved" else current["approved_at"],
                    now,
                    cognition_id,
                ),
            )
            connection.commit()
        self.append_event(
            "environment_cognition",
            cognition_id,
            f"cognition_{status}",
            actor_id=actor_id,
            project_id=current["project_id"],
            payload={"previous_status": current["status"]},
        )
        return self.get_cognition(cognition_id)

    def append_event(
        self,
        entity_type: str,
        entity_id: str,
        action: str,
        *,
        actor_id: str,
        project_id: str,
        payload: dict[str, Any],
    ) -> None:
        with self.database.lock, self.database.connect() as connection:
            connection.execute(
                """
                insert into memory_evolution_events(
                  entity_type, entity_id, action, actor_id, project_id,
                  payload, created_at
                ) values(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entity_type,
                    entity_id,
                    action,
                    actor_id,
                    project_id,
                    _json(payload),
                    _now(),
                ),
            )
            connection.commit()

    def governance_counts(
        self,
        project_id: str,
        *,
        actor: User | None = None,
        ctx: AccessContext | None = None,
    ) -> dict[str, int]:
        with self.database.connect() as connection:
            episodes = connection.execute(
                """
                select id, org_id, project_id, topic_id, author_id, sensitivity
                from agent_episodes where project_id = ?
                """,
                (project_id,),
            ).fetchall()
            traces = connection.execute(
                """
                select id, org_id, project_id, topic_id, author_id, sensitivity
                from agent_trace_memories where project_id = ?
                """,
                (project_id,),
            ).fetchall()
            policy_rows = connection.execute(
                """
                select id, status, org_id, project_id, topic_id, author_id, sensitivity
                from agent_execution_policies
                where project_id = ?
                """,
                (project_id,),
            ).fetchall()
            cognition_rows = connection.execute(
                """
                select id, status, org_id, project_id, topic_id, author_id, sensitivity
                from environment_cognitions
                where project_id = ?
                """,
                (project_id,),
            ).fetchall()
        episode_rows = [dict(row) for row in episodes]
        trace_rows = [dict(row) for row in traces]
        policy_items = [dict(row) for row in policy_rows]
        cognition_items = [dict(row) for row in cognition_rows]
        if actor is not None and ctx is not None:
            episode_rows = visible_filter(actor, episode_rows, ctx)
            trace_rows = visible_filter(actor, trace_rows, ctx)
            policy_items = visible_filter(actor, policy_items, ctx)
            cognition_items = visible_filter(actor, cognition_items, ctx)
        policy_counts: dict[str, int] = {}
        for row in policy_items:
            status = str(row["status"])
            policy_counts[status] = policy_counts.get(status, 0) + 1
        cognition_counts: dict[str, int] = {}
        for row in cognition_items:
            status = str(row["status"])
            cognition_counts[status] = cognition_counts.get(status, 0) + 1
        return {
            "episode_count": len(episode_rows),
            "trace_count": len(trace_rows),
            "policy_candidate_count": policy_counts.get("candidate", 0),
            "policy_approved_count": policy_counts.get("approved", 0),
            "policy_revalidation_count": policy_counts.get(
                "revalidation_required",
                0,
            ),
            "cognition_candidate_count": cognition_counts.get("candidate", 0),
            "cognition_approved_count": cognition_counts.get("approved", 0),
            "cognition_revalidation_count": cognition_counts.get(
                "revalidation_required",
                0,
            ),
        }


def _episode_row(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "episode_id": str(row["id"]),
        "run_id": str(row["run_id"]),
        "kind": str(row["kind"]),
        **_access_tags(row),
        "session_id": str(row["session_id"]),
        "turn_id": str(row["turn_id"]),
        "relation": str(row["relation"]),
        "status": str(row["status"]),
        "terminal_signal": str(row["terminal_signal"]),
        "reward_value": (
            float(row["reward_value"]) if row["reward_value"] is not None else None
        ),
        "reward_source": str(row["reward_source"]),
        "feedback_status": str(row["feedback_status"]),
        "feedback_note": str(row["feedback_note"]),
        "created_at": str(row["created_at"]),
        "closed_at": str(row["closed_at"]),
        "payload": _loads_object(row["payload"]),
    }


def _trace_row(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "trace_id": str(row["id"]),
        "episode_id": str(row["episode_id"]),
        "run_id": str(row["run_id"]),
        "seq_index": int(row["seq_index"]),
        "round_index": int(row["round_index"]),
        **_access_tags(row),
        "state_summary": str(row["state_summary"]),
        "action_kind": str(row["action_kind"]),
        "action_name": str(row["action_name"]),
        "action_summary": str(row["action_summary"]),
        "observation_summary": str(row["observation_summary"]),
        "verification_status": str(row["verification_status"]),
        "public_reflection": str(row["public_reflection"]),
        "value": float(row["value"]) if row["value"] is not None else None,
        "evidence_event_seqs": _loads_list(row["evidence_event_seqs"]),
        "created_at": str(row["created_at"]),
        "payload": _loads_object(row["payload"]),
    }


def _skill_retrieval_row(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "retrieval_id": str(row["id"]),
        "run_id": str(row["run_id"]),
        "episode_id": str(row["episode_id"]),
        "skill_id": str(row["skill_id"]),
        "version_id": str(row["version_id"]),
        **_access_tags(row),
        "query": str(row["query"]),
        "rank": int(row["rank"]),
        "relevance_score": float(row["relevance_score"]),
        "retrieval_tier": str(row["retrieval_tier"]),
        "attribution": str(row["attribution"]),
        "outcome": str(row["outcome"]),
        "reward_value": (
            float(row["reward_value"]) if row["reward_value"] is not None else None
        ),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "payload": _loads_object(row["payload"]),
    }


def _policy_row(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "policy_id": str(row["id"]),
        "signature": str(row["signature"]),
        **_access_tags(row),
        "status": str(row["status"]),
        "trigger_text": str(row["trigger_text"]),
        "procedure": _loads_list(row["procedure"]),
        "verification": _loads_list(row["verification"]),
        "boundaries": _loads_list(row["boundaries"]),
        "support_episode_ids": _loads_list(row["support_episode_ids"]),
        "counter_episode_ids": _loads_list(row["counter_episode_ids"]),
        "gain": float(row["gain"]),
        "stability": float(row["stability"]),
        "approved_by": str(row["approved_by"]),
        "approved_at": str(row["approved_at"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "payload": _loads_object(row["payload"]),
    }


def _cognition_row(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "cognition_id": str(row["id"]),
        "signature": str(row["signature"]),
        **_access_tags(row),
        "status": str(row["status"]),
        "entities": _loads_list(row["entities"]),
        "structures": _loads_list(row["structures"]),
        "regularities": _loads_list(row["regularities"]),
        "constraints": _loads_list(row["constraints_json"]),
        "support_policy_ids": _loads_list(row["support_policy_ids"]),
        "confidence": float(row["confidence"]),
        "approved_by": str(row["approved_by"]),
        "approved_at": str(row["approved_at"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "payload": _loads_object(row["payload"]),
    }


def _access_tags(row: Any) -> dict[str, Any]:
    return {
        "org_id": str(row["org_id"]),
        "project_id": str(row["project_id"]),
        "topic_id": row["topic_id"],
        "author_id": str(row["author_id"]),
        "sensitivity": str(row["sensitivity"]),
    }


def _revalidate_supported_cognitions(
    connection: Any,
    *,
    project_id: str,
    policy_id: str,
    now: str,
) -> list[str]:
    rows = connection.execute(
        """
        select id, support_policy_ids
        from environment_cognitions
        where project_id = ? and status = 'approved'
        """,
        (project_id,),
    ).fetchall()
    cognition_ids = [
        str(row["id"])
        for row in rows
        if policy_id in _loads_list(row["support_policy_ids"])
    ]
    if cognition_ids:
        connection.executemany(
            """
            update environment_cognitions
            set status = 'revalidation_required', updated_at = ?
            where id = ? and status = 'approved'
            """,
            [(now, cognition_id) for cognition_id in cognition_ids],
        )
    return cognition_ids


def _loads_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _loads_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, *parts: str) -> str:
    import hashlib

    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"
