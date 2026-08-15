import {
  BookOutlined,
  BugOutlined,
  CalendarOutlined,
  CheckSquareOutlined,
  DashboardOutlined,
  FolderOutlined,
  InboxOutlined,
  MessageOutlined,
  ProfileOutlined,
  TeamOutlined,
} from "@ant-design/icons";
import { Badge, Tooltip } from "antd";
import type { ReactNode } from "react";
import { brand } from "../theme";
import type { ActiveView } from "../lib/ui";

type IconRailProps = {
  activeView: ActiveView;
  reviewCount: number;
  canViewDashboard: boolean;
  canViewPeople: boolean;
  canReview: boolean;
  onViewChange: (view: ActiveView) => void;
  onDebugOpen: () => void;
};

type RailItem = {
  id: ActiveView;
  label: string;
  icon: ReactNode;
};

const RAIL_ITEMS: RailItem[] = [
  { id: "chat", label: "对话", icon: <MessageOutlined /> },
  { id: "progress", label: "项目进度", icon: <DashboardOutlined /> },
  { id: "calendar", label: "项目日历", icon: <CalendarOutlined /> },
  { id: "threeLists", label: "三清单", icon: <ProfileOutlined /> },
  { id: "tasks", label: "任务", icon: <CheckSquareOutlined /> },
  { id: "knowledge", label: "知识沉淀", icon: <BookOutlined /> },
  { id: "people", label: "人员", icon: <TeamOutlined /> },
  { id: "review", label: "待确认", icon: <InboxOutlined /> },
];

RAIL_ITEMS.splice(6, 0, { id: "files", label: "项目文件", icon: <FolderOutlined /> });

export function IconRail({
  activeView,
  reviewCount,
  canViewDashboard,
  canViewPeople,
  canReview,
  onViewChange,
  onDebugOpen,
}: IconRailProps) {
  const items = RAIL_ITEMS.filter((item) => (
    (item.id !== "progress" || canViewDashboard)
    && (item.id !== "people" || canViewPeople)
    && (item.id !== "review" || canReview)
  ));

  return (
    <aside className="icon-rail" aria-label="主导航">
      <button className="rail-logo" type="button" onClick={() => onViewChange("chat")} aria-label={`${brand.name}，回到对话`}>
        <BrandMark />
      </button>

      <nav className="rail-actions" aria-label="功能导航">
        {items.map((item) => (
          <Tooltip key={item.id} title={item.label} placement="right">
            <button
              className={`rail-button ${activeView === item.id ? "active" : ""}`}
              type="button"
              aria-current={activeView === item.id ? "page" : undefined}
              onClick={() => onViewChange(item.id)}
              aria-label={item.id === "review" ? `${item.label}，${reviewCount} 条` : item.label}
            >
              {item.id === "review" ? (
                <Badge count={reviewCount} size="small" offset={[2, -2]}>
                  {item.icon}
                </Badge>
              ) : item.icon}
              <span className="rail-button-label">{item.label}</span>
            </button>
          </Tooltip>
        ))}
      </nav>

      <div className="rail-bottom">
        <Tooltip title="调试与审计" placement="right">
          <button className="rail-button" type="button" onClick={onDebugOpen} aria-label="调试与审计">
            <BugOutlined />
            <span className="rail-button-label">调试</span>
          </button>
        </Tooltip>
        <small className="rail-version">v{brand.version}</small>
      </div>
    </aside>
  );
}

function BrandMark() {
  return (
    <svg viewBox="0 0 32 32" role="img" aria-hidden="true" focusable="false" width="22" height="22">
      <path
        d="M8 16.8 13.4 22 24 10.6"
        fill="none"
        stroke="currentColor"
        strokeWidth="3.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
