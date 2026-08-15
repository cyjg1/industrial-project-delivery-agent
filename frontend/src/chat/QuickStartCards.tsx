import { ArrowRightOutlined } from "@ant-design/icons";
import { useRef } from "react";
import type { ActiveView } from "../lib/ui";
import type { Workspace } from "../types";

type QuickStartCardsProps = {
  workspace: Workspace;
  canViewProgress: boolean;
  canReview: boolean;
  onViewChange: (view: ActiveView) => void;
  onMeetingFilesSelected: (files: File[]) => void;
};

type QuickStartCard = {
  key: string;
  title: string;
  detail: string;
  onSelect: () => void;
};

export function QuickStartCards({
  workspace,
  canViewProgress,
  canReview,
  onViewChange,
  onMeetingFilesSelected,
}: QuickStartCardsProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);

  function openMeetingUpload() {
    fileInputRef.current?.click();
  }

  const cards = buildCards(workspace, { canViewProgress, canReview }, onViewChange, openMeetingUpload);
  if (!cards.length) return null;

  return (
    <section className="quick-start" aria-label="快捷入口">
      <span className="quick-start-head">可以先从这几件事开始</span>
      <input
        ref={fileInputRef}
        className="visually-hidden-input"
        type="file"
        multiple
        accept=".md,.markdown,.txt,.docx"
        onChange={(event) => {
          const files = Array.from(event.target.files || []);
          if (files.length) onMeetingFilesSelected(files);
          event.target.value = "";
        }}
      />
      <div className="quick-start-grid">
        {cards.map((card) => (
          <button key={card.key} type="button" className="quick-start-card" onClick={card.onSelect}>
            <span className="quick-start-title">
              {card.title}
              <ArrowRightOutlined aria-hidden="true" />
            </span>
            <span className="quick-start-detail">{card.detail}</span>
          </button>
        ))}
      </div>
    </section>
  );
}

function buildCards(
  workspace: Workspace,
  gates: { canViewProgress: boolean; canReview: boolean },
  onViewChange: (view: ActiveView) => void,
  focusComposer: () => void,
): QuickStartCard[] {
  const dashboard = workspace.progress_dashboard.summary;
  const memory = workspace.memory;
  const threeLists = workspace.three_lists.summary;
  const reviewCount = workspace.confirmation_cards.length;
  const sourceTotal = workspace.inputs.source_counts.total;
  const cards: QuickStartCard[] = [];

  if (gates.canViewProgress) {
    cards.push({
      key: "progress",
      title: "看今天的项目状况",
      detail: `任务 ${dashboard.task_count} 项 · 板块 ${dashboard.board_count} 个 · 缺进度填报 ${dashboard.missing_progress_count} 项`,
      onSelect: () => onViewChange("progress"),
    });
  }

  if (gates.canReview) {
    cards.push(reviewCount > 0
      ? {
          key: "review",
          title: "处理待确认候选",
          detail: `待确认 ${reviewCount} 条 · 确认后才会进入正式任务与三清单`,
          onSelect: () => onViewChange("review"),
        }
      : {
          key: "review-empty",
          title: "上传并生成候选",
          detail: "当前待确认 0 条",
          onSelect: focusComposer,
        });
  }

  cards.push({
    key: "upload",
    title: "上传会议纪要",
    detail: `已归档 ${sourceTotal} 份 · 原始转写待补 ${workspace.inputs.source_counts.raw_pending} 份`,
    onSelect: focusComposer,
  });

  cards.push({
    key: "knowledge",
    title: "看知识沉淀情况",
    detail: `方法 ${threeLists.method_count} 条 · 已确认记忆 ${memory.confirmed_count} 条 · 候选 ${memory.candidate_count} 条`,
    onSelect: () => onViewChange("knowledge"),
  });

  return cards;
}
