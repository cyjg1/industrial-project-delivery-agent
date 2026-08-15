from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run isolated off/governed retrieval evaluations and compare machine checks. "
            "Semantic quality always remains pending human review."
        )
    )
    parser.add_argument(
        "--store",
        default=str(PROJECT_ROOT / "data" / "store"),
    )
    parser.add_argument(
        "--questions",
        default=str(
            PROJECT_ROOT
            / "docs"
            / "test_reports"
            / "memory_retrieval_question_set.md"
        ),
    )
    parser.add_argument(
        "--output",
        default=str(
            PROJECT_ROOT
            / "docs"
            / "test_reports"
            / "memory_skill_ab_evaluation.json"
        ),
    )
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--actor-id", default="u_pm")
    parser.add_argument("--model", default="")
    parser.add_argument("--max-rounds", type=int, default=8)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="memory-skill-ab-") as tmpdir:
        reports: dict[str, dict[str, Any]] = {}
        for mode in ("off", "governed"):
            output = Path(tmpdir) / f"{mode}.json"
            command = [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "evaluate_agent_end_to_end.py"),
                "--store",
                str(Path(args.store).resolve()),
                "--questions",
                str(Path(args.questions).resolve()),
                "--output",
                str(output),
                "--actor-id",
                args.actor_id,
                "--max-rounds",
                str(args.max_rounds),
                "--memory-skill-mode",
                mode,
            ]
            if args.model:
                command.extend(["--model", args.model])
            for case_id in args.case_ids or []:
                command.extend(["--case", case_id])
            subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                check=True,
                text=True,
            )
            reports[mode] = json.loads(output.read_text(encoding="utf-8"))

    comparison = compare_reports(reports["off"], reports["governed"])
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_store": str(Path(args.store).resolve()),
        "model": args.model or "configured_default",
        "semantic_quality_auto_passed": False,
        "human_judgement": "pending",
        "causal_attribution_allowed": False,
        "variants": {
            mode: {
                "run_count": row.get("run_count", 0),
                "machine_pass_count": row.get("machine_pass_count", 0),
            }
            for mode, row in reports.items()
        },
        "comparison": comparison,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(output_path),
                "case_count": len(comparison),
                "machine_pass_delta": (
                    report["variants"]["governed"]["machine_pass_count"]
                    - report["variants"]["off"]["machine_pass_count"]
                ),
                "semantic_quality": "pending_human_review",
            },
            ensure_ascii=False,
        )
    )
    return 0


def compare_reports(
    off: dict[str, Any],
    governed: dict[str, Any],
) -> list[dict[str, Any]]:
    off_rows = {
        _result_key(row): row
        for row in off.get("results") or []
        if isinstance(row, dict)
    }
    governed_rows = {
        _result_key(row): row
        for row in governed.get("results") or []
        if isinstance(row, dict)
    }
    rows: list[dict[str, Any]] = []
    for key in sorted(set(off_rows) | set(governed_rows)):
        baseline = off_rows.get(key, {})
        treatment = governed_rows.get(key, {})
        treatment_trace = dict(treatment.get("memory_skill_retrieval") or {})
        treatment_exposed = any(
            int(treatment_trace.get(field) or 0) > 0
            for field in ("policy_hits", "cognition_hits", "trace_hits")
        )
        rows.append(
            {
                "case_id": key[0],
                "actor_id": key[1],
                "off": _variant_summary(baseline),
                "governed": _variant_summary(treatment),
                "machine_pass_delta": (
                    int(treatment.get("machine_passed") is True)
                    - int(baseline.get("machine_passed") is True)
                ),
                "round_delta": (
                    int(treatment.get("round_count") or 0)
                    - int(baseline.get("round_count") or 0)
                ),
                "memory_treatment_exposed": treatment_exposed,
                "causal_attribution_allowed": False,
                "requires_human_semantic_review": True,
            }
        )
    return rows


def _result_key(row: dict[str, Any]) -> tuple[str, str]:
    return (
        str(row.get("case_id") or ""),
        str(row.get("actor_id") or ""),
    )


def _variant_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": row.get("status") or "missing",
        "machine_passed": bool(row.get("machine_passed")),
        "round_count": int(row.get("round_count") or 0),
        "actual_tools": list(row.get("actual_tools") or []),
        "retrieval_trace": dict(row.get("memory_skill_retrieval") or {}),
    }


if __name__ == "__main__":
    raise SystemExit(main())
