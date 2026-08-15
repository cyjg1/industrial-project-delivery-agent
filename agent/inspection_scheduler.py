from __future__ import annotations

from typing import Any, Callable


INSPECTION_JOB_ID = "daily_proactive_project_inspection"


class InspectionScheduler:
    def __init__(
        self,
        job_func: Callable[[], Any],
        *,
        hour: int = 23,
        minute: int = 40,
        timezone_name: str = "Asia/Shanghai",
    ) -> None:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
        except Exception as exc:
            raise RuntimeError(
                "apscheduler is required for proactive inspection scheduling; install requirements before starting the API."
            ) from exc
        self.timezone_name = timezone_name
        self.hour = int(hour)
        self.minute = int(minute)
        self.scheduler = BackgroundScheduler(timezone=timezone_name)
        self.scheduler.add_job(
            job_func,
            "cron",
            id=INSPECTION_JOB_ID,
            hour=self.hour,
            minute=self.minute,
            replace_existing=True,
        )

    def start(self) -> None:
        if not self.scheduler.running:
            self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def status(self) -> dict[str, Any]:
        job = self.scheduler.get_job(INSPECTION_JOB_ID)
        next_run = getattr(job, "next_run_time", None) if job else None
        return {
            "enabled": True,
            "running": bool(self.scheduler.running),
            "job_id": INSPECTION_JOB_ID,
            "hour": self.hour,
            "minute": self.minute,
            "timezone": self.timezone_name,
            "next_run_time": next_run.isoformat() if next_run else "",
        }
