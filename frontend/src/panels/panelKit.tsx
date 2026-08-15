import { DatePicker, Empty } from "antd";
import dayjs, { type Dayjs } from "dayjs";
import type { CSSProperties, ReactNode } from "react";

const emptyDescriptionStyle: CSSProperties = {
  display: "block",
  maxWidth: 460,
  margin: "0 auto",
  lineHeight: 1.8,
  color: "var(--color-text-secondary)",
};

const emptyTitleStyle: CSSProperties = {
  display: "block",
  marginBottom: 2,
  color: "var(--color-text)",
};

const emptyStyle: CSSProperties = { padding: "20px 0" };

export function toDayjs(value: string | null | undefined): Dayjs | null {
  if (!value) return null;
  const parsed = dayjs(value);
  return parsed.isValid() ? parsed : null;
}

function firstDateText(value: string | string[]): string {
  return Array.isArray(value) ? value[0] || "" : value || "";
}

// 受控日期输入：已有日期显示在输入框里，而不是塞进 placeholder 冒充。
export function DateField({
  value,
  onChange,
  placeholder = "选择日期",
  allowClear = true,
  disabled,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  allowClear?: boolean;
  disabled?: boolean;
}) {
  return (
    <DatePicker
      style={{ width: "100%" }}
      value={toDayjs(value)}
      placeholder={placeholder}
      allowClear={allowClear}
      disabled={disabled}
      onChange={(_, text) => onChange(firstDateText(text))}
    />
  );
}

export function EditableCell({
  editing,
  view,
  edit,
}: {
  editing: boolean;
  view: ReactNode;
  edit: () => ReactNode;
}) {
  return <>{editing ? edit() : view}</>;
}

export function PanelEmpty({
  title,
  hint,
  action,
  compact,
}: {
  title: string;
  hint?: string;
  action?: ReactNode;
  compact?: boolean;
}) {
  return (
    <Empty
      style={emptyStyle}
      image={compact ? Empty.PRESENTED_IMAGE_SIMPLE : undefined}
      description={
        <span style={emptyDescriptionStyle}>
          <strong style={emptyTitleStyle}>{title}</strong>
          {hint}
        </span>
      }
    >
      {action}
    </Empty>
  );
}
