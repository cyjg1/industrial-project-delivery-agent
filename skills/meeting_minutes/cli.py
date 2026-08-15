from __future__ import annotations

import argparse
import json
from pathlib import Path

from skills.registry import get_project_skill


def main() -> int:
    parser = argparse.ArgumentParser(description="运行会议转写转纪要 skill。")
    parser.add_argument("--transcript", required=True, help="会议原始转写文件路径，支持 .txt 或 .docx。")
    parser.add_argument("--meeting-id", default="", help="会议编号；不填则自动生成。")
    parser.add_argument("--meeting-date", default="", help="会议日期，格式 YYYY-MM-DD。")
    parser.add_argument("--doc-nature", default="会议纪要", help="文稿性质，例如专题讨论会、周例会、复盘笔记。")
    parser.add_argument("--project-background", default="", help="项目背景、人名映射和专业术语说明。")
    parser.add_argument("--continuation-info", default="", help="上次会议延续信息。")
    parser.add_argument("--output-format", default="", help="输出格式要求。")
    parser.add_argument("--store-dir", default="", help="skill 本地存储目录；默认 data/skills/meeting_minutes。")
    parser.add_argument("--output", default="", help="纪要 Markdown 输出路径；不填则只打印摘要 JSON。")
    parser.add_argument("--print-audit", action="store_true", help="打印 prompt 审计摘要。")
    args = parser.parse_args()

    skill = get_project_skill(
        "meeting_minutes",
        store_dir=Path(args.store_dir) if args.store_dir else None,
    )
    payload = {
        "transcript_path": args.transcript,
        "meeting_id": args.meeting_id,
        "meeting_date": args.meeting_date,
        "doc_nature": args.doc_nature,
        "project_background": args.project_background,
        "continuation_info": args.continuation_info,
        "output_format": args.output_format,
    }
    result = skill.run({key: value for key, value in payload.items() if value})
    if args.output:
        Path(args.output).write_text(result["minutes_markdown"], encoding="utf-8")

    summary: dict[str, object] = {
        "skill_name": result["skill_name"],
        "meeting_id": result["meeting_id"],
        "model_used": result["model_used"],
        "tokens_estimated": result["tokens_estimated"],
        "correction_candidate_count": len(result["asr_correction_candidates"]),
        "methodology_candidate_count": len(result["methodology_candidates"]),
        "stored_files": result["stored_files"],
    }
    if args.print_audit:
        summary["prompt_audit"] = result["prompt_audit"]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

