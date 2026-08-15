import {
  CalendarOutlined,
  CheckCircleFilled,
  ClockCircleOutlined,
  LeftOutlined,
  RightOutlined,
  TeamOutlined,
} from "@ant-design/icons";
import { Button, Empty, Modal, Segmented, Skeleton, Tag, Tooltip, message } from "antd";
import { useEffect, useMemo, useState } from "react";
import { loadProjectCalendar } from "../api/client";
import { taskTitle } from "../lib/ui";
import type { ProjectCalendarEvent, ProjectCalendarResponse } from "../types";
import "./ProjectCalendar.css";

type CalendarScope = "all" | "mine";

type ProjectCalendarPanelProps = {
  currentActorId: string;
};

const WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

export function ProjectCalendarPanel({ currentActorId }: ProjectCalendarPanelProps) {
  const [month, setMonth] = useState(currentMonth());
  const [scope, setScope] = useState<CalendarScope>("mine");
  const [calendar, setCalendar] = useState<ProjectCalendarResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<ProjectCalendarEvent | null>(null);

  useEffect(() => {
    let active = true;
    setLoading(true);
    loadProjectCalendar(month)
      .then((payload) => {
        if (active) setCalendar(payload);
      })
      .catch((reason) => {
        if (active) message.error(reason instanceof Error ? reason.message : "项目日历加载失败");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [month, currentActorId]);

  const days = useMemo(() => calendarDays(month), [month]);
  const visibleEvents = useMemo(
    () => (calendar?.events || []).filter((event) => scope === "all" || event.is_related),
    [calendar, scope],
  );
  const eventsByDate = useMemo(() => {
    const grouped = new Map<string, ProjectCalendarEvent[]>();
    visibleEvents.forEach((event) => {
      grouped.set(event.date, [...(grouped.get(event.date) || []), event]);
    });
    return grouped;
  }, [visibleEvents]);
  const today = localDateKey(new Date());

  return (
    <section className="project-calendar" aria-label="项目日历">
      <div className="calendar-toolbar">
        <div className="calendar-navigation">
          <Button onClick={() => setMonth(shiftMonth(month, -1))} icon={<LeftOutlined />} aria-label="上个月" />
          <Button onClick={() => setMonth(currentMonth())}>今天</Button>
          <Button onClick={() => setMonth(shiftMonth(month, 1))} icon={<RightOutlined />} aria-label="下个月" />
        </div>
        <h2>{monthTitle(month)}</h2>
        <Segmented
          value={scope}
          onChange={(value) => setScope(value as CalendarScope)}
          options={[
            { label: "全部", value: "all" },
            { label: `与我有关${calendar?.actor.name ? ` · ${calendar.actor.name}` : ""}`, value: "mine" },
          ]}
        />
      </div>

      <div className="calendar-subbar">
        <div className="calendar-legend">
          <span><i className="calendar-dot meeting" />会议</span>
          <span><i className="calendar-dot deadline" />任务截止</span>
        </div>
        <span className="calendar-count">
          本月 {visibleEvents.filter((event) => event.event_type === "meeting").length} 场会议，
          {visibleEvents.filter((event) => event.event_type === "task_deadline").length} 项截止
        </span>
      </div>

      {loading && !calendar ? (
        <div className="calendar-loading"><Skeleton active paragraph={{ rows: 8 }} /></div>
      ) : (
        <div className="calendar-grid-wrap">
          <div
            className="calendar-grid"
            role="grid"
            aria-label={`${monthTitle(month)}日程`}
            style={{ gridTemplateRows: `30px repeat(${days.length / 7}, minmax(0, 1fr))` }}
          >
            {WEEKDAYS.map((weekday, index) => (
              <div className={`calendar-weekday ${index > 4 ? "weekend" : ""}`} role="columnheader" key={weekday}>
                {weekday}
              </div>
            ))}
            {days.map((day) => {
              const key = localDateKey(day);
              const inMonth = key.startsWith(month);
              const dayEvents = eventsByDate.get(key) || [];
              return (
                <div
                  className={`calendar-day ${inMonth ? "" : "outside"} ${key === today ? "today" : ""}`}
                  role="gridcell"
                  aria-label={inMonth ? `${key}，${dayEvents.length}项日程` : undefined}
                  aria-hidden={!inMonth}
                  key={key}
                >
                  {inMonth ? (
                    <>
                      <div className="calendar-date-row">
                        <span className="calendar-date-number">{day.getDate()}</span>
                        {key === today ? <span className="calendar-today-label">今天</span> : null}
                      </div>
                      <div className="calendar-day-events">
                        {dayEvents.map((event) => (
                          <Tooltip
                            key={event.event_id}
                            title={event.event_type === "task_deadline" ? taskTitle(event.title) : event.title}
                            mouseEnterDelay={0.5}
                          >
                            <button
                              type="button"
                              className={`calendar-event ${event.event_type}`}
                              onClick={() => setSelected(event)}
                            >
                              {event.event_type === "meeting" ? (
                                <span className="calendar-event-kind">{event.time_label}</span>
                              ) : null}
                              <span className="calendar-event-title">
                                {event.event_type === "task_deadline" ? taskTitle(event.title) : event.title}
                              </span>
                              {event.event_type === "meeting" && event.is_related ? (
                                <span className="calendar-related-mark" aria-label="与我有关" />
                              ) : null}
                            </button>
                          </Tooltip>
                        ))}
                      </div>
                    </>
                  ) : null}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {!loading && visibleEvents.length === 0 ? (
        <div className="calendar-empty">
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={scope === "mine" ? "本月没有与你有关的日程" : "本月暂无日程"} />
        </div>
      ) : null}

      <Modal
        open={Boolean(selected)}
        footer={null}
        onCancel={() => setSelected(null)}
        title={selected?.event_type === "meeting" ? "会议详情" : "任务截止详情"}
        width={520}
      >
        {selected ? <CalendarEventDetail event={selected} /> : null}
      </Modal>
    </section>
  );
}

function CalendarEventDetail({ event }: { event: ProjectCalendarEvent }) {
  return (
    <div className="calendar-event-detail">
      <div className={`calendar-detail-icon ${event.event_type}`}>
        {event.event_type === "meeting" ? <CalendarOutlined /> : <CheckCircleFilled />}
      </div>
      <div>
        <h3>{event.title}</h3>
        <p><ClockCircleOutlined /> {formatFullDate(event.date)} · {event.time_label}</p>
        {event.owner_names.length ? <p><TeamOutlined /> {event.owner_names.join("、")}</p> : null}
        {event.deliverable ? <p><strong>交付物：</strong>{event.deliverable}</p> : null}
        {event.description ? <p className="calendar-detail-description">{event.description}</p> : null}
        <Tag color={event.is_related ? "blue" : "default"}>{event.is_related ? "与我有关" : "项目日程"}</Tag>
      </div>
    </div>
  );
}

function currentMonth() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

function shiftMonth(month: string, offset: number) {
  const [year, monthNumber] = month.split("-").map(Number);
  const shifted = new Date(year, monthNumber - 1 + offset, 1);
  return `${shifted.getFullYear()}-${String(shifted.getMonth() + 1).padStart(2, "0")}`;
}

function calendarDays(month: string) {
  const [year, monthNumber] = month.split("-").map(Number);
  const first = new Date(year, monthNumber - 1, 1);
  const mondayOffset = (first.getDay() + 6) % 7;
  const start = new Date(year, monthNumber - 1, 1 - mondayOffset);
  const daysInMonth = new Date(year, monthNumber, 0).getDate();
  const visibleDayCount = Math.ceil((mondayOffset + daysInMonth) / 7) * 7;
  return Array.from(
    { length: visibleDayCount },
    (_, index) => new Date(start.getFullYear(), start.getMonth(), start.getDate() + index),
  );
}

function localDateKey(value: Date) {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
}

function monthTitle(month: string) {
  const [year, monthNumber] = month.split("-").map(Number);
  return `${year}年${monthNumber}月`;
}

function formatFullDate(dateKey: string) {
  const [year, month, day] = dateKey.split("-").map(Number);
  return `${year}年${month}月${day}日`;
}
