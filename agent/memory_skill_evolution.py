from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from agent.access_policy import AccessContext, ROLE_RANK, User
from store.sqlite_store import ProjectSQLiteStore


TRACE_SCHEMA_VERSION = "msce-l1-v1"
POLICY_SCHEMA_VERSION = "msce-l2-v1"
COGNITION_SCHEMA_VERSION = "msce-l3-v1"
DEFAULT_GAMMA = 0.9
MIN_POLICY_EPISODES = 2
MIN_ACTIVE_SKILL_EPISODES = 3
RELIABILITY_THRESHOLD = 0.6

_CORRECTION_MARKERS = (
    "不对",
    "不是这样",
    "错了",
    "纠正",
    "改成",
    "重新做",
    "理解错",
    "correction",
    "incorrect",
    "wrong",
)
_FOLLOW_UP_MARKERS = (
    "继续",
    "然后",
    "再看",
    "补充",
    "上面",
    "刚才",
    "前面",
    "接着",
    "follow up",
    "continue",
)
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(sk-[a-z0-9_-]{12,}|api[_-]?key\s*[:=]\s*\S+)"),
    re.compile(r"(?i)\b(password|passwd|token|secret)\s*[:=]\s*\S+"),
)


class MemorySkillEvolutionService:
    """Builds governed L1/L2/L3 artifacts from observable runtime evidence."""

    def __init__(self, store: ProjectSQLiteStore) -> None:
        self.store = store
        self.repository = store.memory_skill_evolution

    def close_runtime_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.runtime_runs.get_run(run_id)
        if run["status"] not in {"completed", "failed", "cancelled"}:
            raise ValueError("只有已经结束的运行才能归档为运行经验")
        actor_row = self.store.get_access_user(run["actor_id"]) or {}
        org_id = str(actor_row.get("org_id") or "org_mvp")
        message = str(run.get("input_payload", {}).get("message") or "")
        previous = self.repository.list_episodes(
            project_id=run["project_id"],
            session_id=run["session_id"],
            limit=50,
        )
        previous = [row for row in previous if row["run_id"] != run_id]
        relation = classify_episode_relation(
            message,
            previous_message=(
                str(previous[0].get("payload", {}).get("task_summary") or "")
                if previous
                else ""
            ),
            has_previous=bool(previous),
        )
        verification = _verification_payload(run)
        reward_value, reward_source = _terminal_reward(run, verification)
        tool_steps = _tool_steps(run)
        tool_sequence = _tool_sequence(tool_steps)
        plan_signature = _plan_signature(tool_sequence)
        task_terms = _task_terms(message)
        episode_id = _stable_id("episode", run_id)
        episode = self.repository.upsert_episode(
            {
                "episode_id": episode_id,
                "run_id": run_id,
                "kind": run["kind"],
                "org_id": org_id,
                "project_id": run["project_id"],
                "topic_id": None,
                "author_id": run["actor_id"],
                "sensitivity": "l2",
                "session_id": run["session_id"],
                "turn_id": run["turn_id"],
                "relation": relation,
                "status": run["status"],
                "terminal_signal": run["stop_reason"],
                "reward_value": reward_value,
                "reward_source": reward_source,
                "created_at": run["created_at"],
                "closed_at": run["completed_at"] or run["updated_at"],
                "payload": {
                    "trace_schema_version": TRACE_SCHEMA_VERSION,
                    "task_summary": _bounded(message, 240),
                    "task_terms": task_terms,
                    "tool_sequence": tool_sequence,
                    "plan_signature": plan_signature,
                    "verification": _compact_verification(verification),
                    "observable_round_count": len(_rounds(run)),
                    "no_hidden_reasoning_persisted": True,
                },
            }
        )
        events = self.store.runtime_runs.list_events(run_id)
        traces = self._normalize_traces(
            run,
            episode=episode,
            events=events,
            reward_value=reward_value,
        )
        retrieval_outcome = "success" if reward_value >= 0.5 else "failure"
        self.repository.finalize_skill_retrievals(
            run_id=run_id,
            episode_id=episode_id,
            outcome=retrieval_outcome,
            reward_value=reward_value,
        )
        if relation == "correction" and previous:
            prior = previous[0]
            self.repository.update_episode_feedback(
                prior["episode_id"],
                feedback_status="corrected",
                feedback_note=(
                    "后续用户消息明确纠正了本次运行结论；"
                    "该纠正已作为更高权重的人工反馈保存。"
                ),
                reward_value=-0.75,
                actor_id=run["actor_id"],
            )
            self.repository.revalue_episode_traces(
                prior["episode_id"],
                terminal_reward=-0.75,
                gamma=self.gamma,
            )
        evolution = self.evolve_project(run["project_id"])
        self.repository.append_event(
            "episode",
            episode_id,
            "episode_closed",
            actor_id=run["actor_id"],
            project_id=run["project_id"],
            payload={
                "relation": relation,
                "reward_value": reward_value,
                "reward_source": reward_source,
                "trace_count": len(traces),
                "policy_candidates": evolution["policy_candidate_count"],
                "cognition_candidates": evolution["cognition_candidate_count"],
            },
        )
        return {
            "episode": episode,
            "traces": traces,
            "evolution": evolution,
        }

    @property
    def gamma(self) -> float:
        raw = str(os.getenv("MEMORY_SKILL_VALUE_GAMMA", DEFAULT_GAMMA))
        try:
            value = float(raw)
        except ValueError:
            value = DEFAULT_GAMMA
        return max(0.0, min(value, 1.0))

    def record_project_skill_retrievals(
        self,
        *,
        run_id: str,
        query: str,
        rows: Iterable[dict[str, Any]],
        actor_id: str,
    ) -> list[dict[str, Any]]:
        if not run_id:
            return []
        return self.repository.record_skill_retrievals(
            run_id=run_id,
            query=_bounded(query, 500),
            rows=rows,
            actor_id=actor_id,
        )

    def feedback_episode(
        self,
        episode_id: str,
        *,
        feedback_status: str,
        feedback_note: str,
        actor: User,
        ctx: AccessContext,
    ) -> dict[str, Any]:
        episode = self.repository.get_episode(episode_id)
        role = ctx.role_of(actor, episode["project_id"])
        if actor.id != episode["author_id"] and ROLE_RANK.get(role, -1) < ROLE_RANK["pmo"]:
            raise PermissionError("只有本次运行发起人或 PM/PMO 可以提交反馈")
        reward = {
            "accepted": 1.0,
            "corrected": -0.75,
            "rejected": -1.0,
        }.get(feedback_status)
        if reward is None:
            raise ValueError("反馈状态只能是接受、纠正或驳回")
        updated = self.repository.update_episode_feedback(
            episode_id,
            feedback_status=feedback_status,
            feedback_note=_bounded(feedback_note, 1000),
            reward_value=reward,
            actor_id=actor.id,
        )
        self.repository.revalue_episode_traces(
            episode_id,
            terminal_reward=reward,
            gamma=self.gamma,
        )
        self.evolve_project(episode["project_id"])
        return updated

    def evolve_project(self, project_id: str) -> dict[str, Any]:
        episodes = self.repository.list_episodes(project_id=project_id, limit=1000)
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for episode in episodes:
            signature = str(episode.get("payload", {}).get("plan_signature") or "")
            tool_sequence = episode.get("payload", {}).get("tool_sequence") or []
            if not signature or not tool_sequence:
                continue
            groups[signature].append(episode)

        policies: list[dict[str, Any]] = []
        evidence_eligible_policy_ids: set[str] = set()
        for signature, grouped in groups.items():
            support = [
                row
                for row in grouped
                if row["status"] == "completed"
                and float(row.get("reward_value") or 0.0) >= 0.5
                and row["feedback_status"] not in {"corrected", "rejected"}
            ]
            if len({row["episode_id"] for row in support}) < MIN_POLICY_EPISODES:
                continue
            counters = [
                row
                for row in grouped
                if float(row.get("reward_value") or 0.0) < 0
                or row["feedback_status"] in {"corrected", "rejected"}
            ]
            first = support[0]
            sequence = [
                str(value)
                for value in first.get("payload", {}).get("tool_sequence") or []
                if str(value)
            ]
            positive_mean = _mean(
                float(row.get("reward_value") or 0.0) for row in support
            )
            counter_penalty = abs(
                _mean(float(row.get("reward_value") or 0.0) for row in counters)
            )
            if counters:
                counter_penalty *= min(1.0, len(counters) / max(1, len(support)))
            stability = len(support) / (len(support) + len(counters))
            common_terms = _common_task_terms(support)
            policy_id = _stable_id("execution_policy", project_id, signature)
            evidence_eligible_policy_ids.add(policy_id)
            policies.append(
                self.repository.upsert_policy_candidate(
                    {
                        "policy_id": policy_id,
                        "signature": signature,
                        "org_id": first["org_id"],
                        "project_id": project_id,
                        "topic_id": first.get("topic_id"),
                        "author_id": "system",
                        "sensitivity": _max_sensitivity(grouped),
                        "trigger_text": (
                            "在多次已验证运行中重复出现的工具执行模式"
                            + (
                                f"；相关任务词：{', '.join(common_terms[:8])}"
                                if common_terms
                                else ""
                            )
                        ),
                        "procedure": [
                            {
                                "step": index,
                                "action": tool_name,
                                "done_when": (
                                    "注册工具返回 ok=true 和可核查证据，"
                                    "或明确返回证据缺口。"
                                ),
                            }
                            for index, tool_name in enumerate(sequence, start=1)
                        ],
                        "verification": [
                            "引用的每个工具都存在于统一工具注册表。",
                            "最终答案校验器没有发现无依据的具体主张。",
                            "明确且可处理的未解决缺口可以作为有效结束结果。",
                        ],
                        "boundaries": [
                            "该策略不授予任何工具调用权限。",
                            "该策略不能确认候选、发布项目方法技能或发送消息。",
                            "只能使用当前项目和当前用户可见范围内的证据。",
                        ],
                        "support_episode_ids": [
                            row["episode_id"] for row in support
                        ],
                        "counter_episode_ids": [
                            row["episode_id"] for row in counters
                        ],
                        "gain": round(positive_mean - counter_penalty, 4),
                        "stability": round(stability, 4),
                        "payload": {
                            "schema_version": POLICY_SCHEMA_VERSION,
                            "tool_sequence": sequence,
                            "induction": "deterministic_observable_trace_grouping",
                            "minimum_distinct_episodes": MIN_POLICY_EPISODES,
                            "human_approval_required": True,
                        },
                    }
                )
            )
        for existing in self.repository.list_policies(
            project_id=project_id,
            statuses=("approved",),
            limit=1000,
        ):
            if existing["policy_id"] in evidence_eligible_policy_ids:
                continue
            self.repository.require_policy_revalidation(
                existing["policy_id"],
                actor_id="system",
                reason="支撑运行记录已不足最低门槛",
            )
        cognitions = self._induce_environment_cognition(project_id, policies)
        return {
            "project_id": project_id,
            "policy_candidate_count": sum(
                1 for row in policies if row["status"] == "candidate"
            ),
            "policy_count": len(policies),
            "cognition_candidate_count": sum(
                1 for row in cognitions if row["status"] == "candidate"
            ),
            "cognition_count": len(cognitions),
        }

    def retrieve_supplement(
        self,
        query: str,
        *,
        actor: User,
        ctx: AccessContext,
        project_id: str,
        project_skill_count: int,
        limit: int = 5,
    ) -> dict[str, Any]:
        mode = str(os.getenv("MEMORY_SKILL_RETRIEVAL_MODE", "governed")).strip().lower()
        trace = {
            "mode": mode,
            "tiers": [
                "published_project_skill",
                "approved_execution_policy",
                "approved_environment_cognition",
                "high_value_l1_trace_fallback",
            ],
            "project_skill_hits": project_skill_count,
            "policy_hits": 0,
            "cognition_hits": 0,
            "trace_hits": 0,
            "fallback_reason": "",
        }
        if mode == "off":
            trace["fallback_reason"] = "memory_skill_retrieval_disabled"
            return {
                "policy_items": [],
                "cognition_items": [],
                "trace_items": [],
                "retrieval_trace": trace,
            }
        if project_skill_count:
            trace["fallback_reason"] = "published_project_skill_hit"
            return {
                "policy_items": [],
                "cognition_items": [],
                "trace_items": [],
                "retrieval_trace": trace,
            }

        terms = _query_terms(query)
        policies = self.repository.list_policies(
            project_id=project_id,
            statuses=("approved",),
            actor=actor,
            ctx=ctx,
            limit=100,
        )
        policy_items = _rank_rows(
            policies,
            terms,
            text=lambda row: " ".join(
                [
                    row["trigger_text"],
                    " ".join(
                        str(step.get("action") or "")
                        for step in row["procedure"]
                        if isinstance(step, dict)
                    ),
                ]
            ),
            quality=lambda row: float(row["stability"]) + float(row["gain"]) * 0.2,
            limit=limit,
        )
        cognitions = self.repository.list_cognitions(
            project_id=project_id,
            statuses=("approved",),
            actor=actor,
            ctx=ctx,
            limit=100,
        )
        cognition_items = _rank_rows(
            cognitions,
            terms,
            text=lambda row: " ".join(
                [
                    *[str(value) for value in row["entities"]],
                    *[str(value) for value in row["regularities"]],
                    *[str(value) for value in row["constraints"]],
                ]
            ),
            quality=lambda row: float(row["confidence"]),
            limit=limit,
        )
        trace["policy_hits"] = len(policy_items)
        trace["cognition_hits"] = len(cognition_items)
        trace_items: list[dict[str, Any]] = []
        if not policy_items and not cognition_items:
            high_value = self.repository.list_traces(
                project_id=project_id,
                actor=actor,
                ctx=ctx,
                min_value=0.5,
                limit=100,
            )
            ranked_traces = _rank_rows(
                high_value,
                terms,
                text=lambda row: " ".join(
                    [
                        row["state_summary"],
                        row["action_name"],
                        row["observation_summary"],
                    ]
                ),
                quality=lambda row: float(row.get("value") or 0.0),
                limit=limit,
            )
            trace_items = [
                {
                    key: value
                    for key, value in row.items()
                    if key
                    not in {
                        "payload",
                        "evidence_event_seqs",
                    }
                }
                for row in ranked_traces
            ]
            trace["trace_hits"] = len(trace_items)
            trace["fallback_reason"] = (
                "no_approved_higher_tier_match"
                if trace_items
                else "no_governed_memory_match"
            )
        else:
            trace["fallback_reason"] = "approved_governance_match"
        return {
            "policy_items": policy_items,
            "cognition_items": cognition_items,
            "trace_items": trace_items,
            "retrieval_trace": trace,
        }

    def snapshot(
        self,
        *,
        actor: User,
        ctx: AccessContext,
        project_id: str,
    ) -> dict[str, Any]:
        episodes = self.repository.list_episodes(
            project_id=project_id,
            actor=actor,
            ctx=ctx,
            limit=20,
        )
        traces = self.repository.list_traces(
            project_id=project_id,
            actor=actor,
            ctx=ctx,
            limit=200,
        )
        policies = self.repository.list_policies(
            project_id=project_id,
            actor=actor,
            ctx=ctx,
            limit=100,
        )
        cognitions = self.repository.list_cognitions(
            project_id=project_id,
            actor=actor,
            ctx=ctx,
            limit=100,
        )
        reliability = self.repository.list_skill_reliability(
            project_id=project_id,
            actor=actor,
            ctx=ctx,
        )
        for row in reliability:
            try:
                skill = self.store.get_project_skill(row["skill_id"])
                row["name"] = skill["name"]
            except KeyError:
                row["name"] = row["skill_id"]
        counts = self.repository.governance_counts(
            project_id,
            actor=actor,
            ctx=ctx,
        )
        return {
            "summary": {
                **counts,
                "active_skill_count": sum(
                    1 for row in reliability if row["lifecycle"] == "active"
                ),
                "probationary_skill_count": sum(
                    1 for row in reliability if row["lifecycle"] == "probationary"
                ),
            },
            "recent_episodes": episodes,
            "policies": policies,
            "cognitions": cognitions,
            "skill_reliability": reliability,
            "governance": {
                "business_method_lane": (
                    "会议证据 -> 人工确认方法 -> 压力测试 -> 人工发布项目方法技能"
                ),
                "runtime_learning_lane": (
                    "运行事件 -> L1 轨迹 -> L2 候选 -> L3 候选 -> 人工批准"
                ),
                "candidate_instructions_are_not_active": True,
                "human_publish_boundary": True,
                "minimum_policy_episodes": MIN_POLICY_EPISODES,
                "minimum_active_skill_episodes": MIN_ACTIVE_SKILL_EPISODES,
                "reliability_threshold": RELIABILITY_THRESHOLD,
                "hidden_chain_of_thought_persisted": False,
            },
        }

    def approve_policy(
        self,
        policy_id: str,
        *,
        actor: User,
        ctx: AccessContext,
    ) -> dict[str, Any]:
        policy = self.repository.get_policy(policy_id)
        self._require_manager(actor, ctx, policy["project_id"])
        updated = self.repository.update_policy_status(
            policy_id,
            status="approved",
            actor_id=actor.id,
        )
        self._induce_environment_cognition(
            policy["project_id"],
            self.repository.list_policies(project_id=policy["project_id"], limit=1000),
        )
        return updated

    def retire_policy(
        self,
        policy_id: str,
        *,
        actor: User,
        ctx: AccessContext,
    ) -> dict[str, Any]:
        policy = self.repository.get_policy(policy_id)
        self._require_manager(actor, ctx, policy["project_id"])
        return self.repository.update_policy_status(
            policy_id,
            status="retired",
            actor_id=actor.id,
        )

    def approve_cognition(
        self,
        cognition_id: str,
        *,
        actor: User,
        ctx: AccessContext,
    ) -> dict[str, Any]:
        cognition = self.repository.get_cognition(cognition_id)
        self._require_manager(actor, ctx, cognition["project_id"])
        return self.repository.update_cognition_status(
            cognition_id,
            status="approved",
            actor_id=actor.id,
        )

    def retire_cognition(
        self,
        cognition_id: str,
        *,
        actor: User,
        ctx: AccessContext,
    ) -> dict[str, Any]:
        cognition = self.repository.get_cognition(cognition_id)
        self._require_manager(actor, ctx, cognition["project_id"])
        return self.repository.update_cognition_status(
            cognition_id,
            status="retired",
            actor_id=actor.id,
        )

    def _normalize_traces(
        self,
        run: dict[str, Any],
        *,
        episode: dict[str, Any],
        events: list[dict[str, Any]],
        reward_value: float,
    ) -> list[dict[str, Any]]:
        rounds = _rounds(run)
        if not rounds:
            rounds = _rounds_from_tool_steps(_tool_steps(run))
        if not rounds:
            rounds = [
                {
                    "round_index": 1,
                    "tool_calls": [],
                    "tool_results": [],
                    "verification_errors": [],
                    "status": run["status"],
                }
            ]
        values = [
            reward_value * (self.gamma ** (len(rounds) - index - 1))
            for index in range(len(rounds))
        ]
        previous_observation = ""
        traces: list[dict[str, Any]] = []
        for index, round_row in enumerate(rounds, start=1):
            round_index = int(round_row.get("round_index") or index)
            tool_calls = [
                row for row in round_row.get("tool_calls") or [] if isinstance(row, dict)
            ]
            tool_results = [
                row for row in round_row.get("tool_results") or [] if isinstance(row, dict)
            ]
            tool_names = [
                str(row.get("name") or row.get("tool_name") or "")
                for row in tool_calls
                if str(row.get("name") or row.get("tool_name") or "")
            ]
            if not tool_names:
                tool_names = [
                    str(row.get("name") or row.get("tool_name") or "")
                    for row in tool_results
                    if str(row.get("name") or row.get("tool_name") or "")
                ]
            observation = "; ".join(
                _bounded(
                    str(
                        row.get("summary")
                        or (row.get("result") or {}).get("summary")
                        or row.get("status")
                        or ""
                    ),
                    240,
                )
                for row in tool_results
                if isinstance(row, dict)
            )
            verification_errors = [
                str(value)
                for value in round_row.get("verification_errors") or []
                if str(value)
            ]
            is_last = index == len(rounds)
            verification_status = (
                "rejected"
                if verification_errors
                else "passed"
                if is_last and reward_value >= 0.5
                else "observed"
            )
            reflection = (
                "确定性校验器拒绝了无依据输出："
                + "；".join(_bounded(value, 160) for value in verification_errors)
                if verification_errors
                else (
                    "可观察工具证据与结束校验均已通过。"
                    if is_last and reward_value >= 0.5
                    else "已记录可观察运行步骤，未保存隐藏推理过程。"
                )
            )
            event_seqs = [
                int(event["seq"])
                for event in events
                if int(
                    event.get("round_index")
                    or event.get("payload", {}).get("round_index")
                    or 0
                )
                == round_index
            ]
            action_name = " -> ".join(tool_names) if tool_names else "final_response"
            action_kind = "tool_sequence" if tool_names else "answer"
            state_summary = (
                str(episode.get("payload", {}).get("task_summary") or "")
                if index == 1
                else _bounded(
                    f"基于上一条可观察结果继续：{previous_observation}",
                    500,
                )
            )
            trace_id = _stable_id("trace", run["run_id"], str(index))
            trace = self.repository.upsert_trace(
                {
                    "trace_id": trace_id,
                    "episode_id": episode["episode_id"],
                    "run_id": run["run_id"],
                    "seq_index": index,
                    "round_index": round_index,
                    "org_id": episode["org_id"],
                    "project_id": episode["project_id"],
                    "topic_id": episode.get("topic_id"),
                    "author_id": episode["author_id"],
                    "sensitivity": episode["sensitivity"],
                    "state_summary": _bounded(state_summary, 500),
                    "action_kind": action_kind,
                    "action_name": _bounded(action_name, 300),
                    "action_summary": (
                        f"选择了注册工具：{', '.join(tool_names)}"
                        if tool_names
                        else "生成最终回答并交由确定性校验。"
                    ),
                    "observation_summary": _bounded(
                        observation
                        or (
                            "本轮没有工具观察结果；结束状态为 "
                            + str(round_row.get("status") or run["status"])
                        ),
                        700,
                    ),
                    "verification_status": verification_status,
                    "public_reflection": _bounded(reflection, 700),
                    "value": round(values[index - 1], 6),
                    "evidence_event_seqs": event_seqs,
                    "created_at": run["completed_at"] or run["updated_at"],
                    "payload": {
                        "schema_version": TRACE_SCHEMA_VERSION,
                        "tool_names": tool_names,
                        "observable_only": True,
                        "hidden_chain_of_thought_persisted": False,
                    },
                }
            )
            traces.append(trace)
            previous_observation = trace["observation_summary"]
        return traces

    def _induce_environment_cognition(
        self,
        project_id: str,
        policies: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        eligible = [
            row
            for row in policies
            if row["status"] in {"candidate", "approved"}
            and row["stability"] >= RELIABILITY_THRESHOLD
            and len(set(row["support_episode_ids"])) >= MIN_POLICY_EPISODES
        ]
        if len(eligible) < 2:
            return []
        selected = sorted(
            eligible,
            key=lambda row: (-row["stability"], -row["gain"], row["policy_id"]),
        )[:8]
        signatures = sorted(row["signature"] for row in selected)
        signature = hashlib.sha256(
            json.dumps(signatures, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:24]
        first = selected[0]
        tool_sequences = [
            [
                str(step.get("action") or "")
                for step in row["procedure"]
                if isinstance(step, dict) and str(step.get("action") or "")
            ]
            for row in selected
        ]
        entities = sorted({tool for sequence in tool_sequences for tool in sequence})
        cognition_id = _stable_id("environment_cognition", project_id, signature)
        cognition = self.repository.upsert_cognition_candidate(
            {
                "cognition_id": cognition_id,
                "signature": signature,
                "org_id": first["org_id"],
                "project_id": project_id,
                "topic_id": None,
                "author_id": "system",
                "sensitivity": _max_sensitivity(selected),
                "entities": entities,
                "structures": [
                    {
                        "policy_id": row["policy_id"],
                        "tool_sequence": sequence,
                    }
                    for row, sequence in zip(selected, tool_sequences)
                ],
                "regularities": [
                    (
                        f"多次已验证运行反复出现策略 {row['policy_id']}，"
                        f"对应工具序列为 {' -> '.join(sequence)}。"
                    )
                    for row, sequence in zip(selected, tool_sequences)
                ],
                "constraints": sorted(
                    {
                        str(boundary)
                        for row in selected
                        for boundary in row["boundaries"]
                        if str(boundary)
                    }
                ),
                "support_policy_ids": [row["policy_id"] for row in selected],
                "confidence": round(
                    _mean(float(row["stability"]) for row in selected),
                    4,
                ),
                "payload": {
                    "schema_version": COGNITION_SCHEMA_VERSION,
                    "declarative_only": True,
                    "contains_imperative_instructions": False,
                    "human_approval_required": True,
                },
            }
        )
        return [cognition]

    @staticmethod
    def _require_manager(actor: User, ctx: AccessContext, project_id: str) -> None:
        role = ctx.role_of(actor, project_id)
        if ROLE_RANK.get(role, -1) < ROLE_RANK["pmo"]:
            raise PermissionError("运行经验治理需要 PM 或 PMO 权限")


def classify_episode_relation(
    message: str,
    *,
    previous_message: str = "",
    has_previous: bool = False,
) -> str:
    normalized = str(message or "").strip().lower()
    if has_previous and any(marker in normalized for marker in _CORRECTION_MARKERS):
        return "correction"
    if not has_previous:
        return "new_task"
    if any(marker in normalized for marker in _FOLLOW_UP_MARKERS):
        return "follow_up"
    current_terms = set(_task_terms(normalized))
    previous_terms = set(_task_terms(previous_message))
    union = current_terms | previous_terms
    overlap = len(current_terms & previous_terms) / max(1, len(union))
    return "follow_up" if overlap >= 0.12 else "new_task"


def _terminal_reward(
    run: dict[str, Any],
    verification: dict[str, Any],
) -> tuple[float, str]:
    if run["status"] != "completed":
        return -1.0, "runtime_status"
    if verification.get("checked"):
        failed = bool(
            verification.get("unsupported")
            or verification.get("invalid_fact_ids")
        )
        return (-1.0 if failed else 1.0), "deterministic_verifier"
    return 0.5, "runtime_status"


def _verification_payload(run: dict[str, Any]) -> dict[str, Any]:
    output = run.get("output_payload") or {}
    value = output.get("verification")
    return dict(value) if isinstance(value, dict) else {}


def _compact_verification(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "checked": bool(value.get("checked")),
        "unsupported_count": len(value.get("unsupported") or []),
        "invalid_fact_id_count": len(value.get("invalid_fact_ids") or []),
        "citation_coverage": value.get("citation_coverage") or {},
    }


def _tool_steps(run: dict[str, Any]) -> list[dict[str, Any]]:
    rows = (run.get("output_payload") or {}).get("tool_steps") or []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _rounds(run: dict[str, Any]) -> list[dict[str, Any]]:
    debug = (run.get("output_payload") or {}).get("debug") or {}
    rows = debug.get("rounds") or []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _rounds_from_tool_steps(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row.get("round_index") or 1)].append(row)
    return [
        {
            "round_index": round_index,
            "tool_calls": [
                {
                    "name": row.get("tool_name") or "",
                    "arguments": row.get("arguments") or {},
                    "reason": row.get("reason") or "",
                }
                for row in grouped[round_index]
            ],
            "tool_results": grouped[round_index],
            "verification_errors": [],
            "status": "completed",
        }
        for round_index in sorted(grouped)
    ]


def _tool_sequence(rows: list[dict[str, Any]]) -> list[str]:
    result: list[str] = []
    for row in rows:
        name = str(row.get("tool_name") or row.get("name") or "").strip()
        if not name:
            continue
        result.append(name)
    return result


def _plan_signature(tool_sequence: list[str]) -> str:
    if not tool_sequence:
        return ""
    return hashlib.sha256(
        json.dumps(tool_sequence, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:24]


def _task_terms(value: str) -> list[str]:
    normalized = _redact(str(value or "").lower())
    latin = re.findall(r"[a-z0-9_]{2,}", normalized)
    chinese_chunks = re.findall(r"[\u4e00-\u9fff]{2,}", normalized)
    chinese: list[str] = []
    for chunk in chinese_chunks:
        chinese.extend(
            chunk[index : index + 2]
            for index in range(max(0, len(chunk) - 1))
        )
    stop = {
        "一下",
        "这个",
        "那个",
        "然后",
        "现在",
        "可以",
        "需要",
        "我们",
        "你们",
        "什么",
        "怎么",
        "the",
        "and",
        "for",
        "with",
    }
    return _ordered_unique(
        term for term in [*latin, *chinese] if term not in stop
    )[:40]


def _common_task_terms(episodes: list[dict[str, Any]]) -> list[str]:
    counts: dict[str, int] = defaultdict(int)
    for episode in episodes:
        for term in set(episode.get("payload", {}).get("task_terms") or []):
            counts[str(term)] += 1
    threshold = max(2, math.ceil(len(episodes) * 0.5))
    return sorted(
        (term for term, count in counts.items() if count >= threshold),
        key=lambda term: (-counts[term], term),
    )


def _rank_rows(
    rows: list[dict[str, Any]],
    terms: list[str],
    *,
    text: Any,
    quality: Any,
    limit: int,
) -> list[dict[str, Any]]:
    ranked: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        haystack = str(text(row) or "").lower()
        relevance = sum(1 for term in terms if term.lower() in haystack)
        if terms and relevance == 0:
            continue
        score = relevance + float(quality(row))
        ranked.append((score, {**row, "retrieval_score": round(score, 4)}))
    ranked.sort(
        key=lambda pair: (
            -pair[0],
            str(pair[1].get("id") or ""),
        )
    )
    return [row for _, row in ranked[: max(1, min(int(limit), 10))]]


def _query_terms(value: str) -> list[str]:
    return _task_terms(value)[:20]


def _mean(values: Iterable[float]) -> float:
    rows = list(values)
    return sum(rows) / len(rows) if rows else 0.0


def _max_sensitivity(rows: Iterable[dict[str, Any]]) -> str:
    order = {"l1": 1, "l2": 2, "l3": 3, "l4": 4}
    values = [str(row.get("sensitivity") or "l2") for row in rows]
    return max(values or ["l2"], key=lambda value: order.get(value, 2))


def _bounded(value: str, limit: int) -> str:
    cleaned = " ".join(_redact(str(value or "")).split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)].rstrip() + "…"


def _redact(value: str) -> str:
    result = value
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result


def _ordered_unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
