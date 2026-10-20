"""Built-in scheduled task system (v0.8 特性 #7).

Provides an APScheduler-backed task scheduler with built-in tasks for
auto-commit, auto-embed, auto-reindex, doctor, and prune.

Usage::

    from kb_mcp_lite.scheduler import TaskScheduler
    scheduler = TaskScheduler(store, config)
    scheduler.start()
    # ... later ...
    scheduler.stop()
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Task protocol and registry
# ---------------------------------------------------------------------------


@runtime_checkable
class ScheduledTask(Protocol):
    """Interface for a scheduled task."""

    name: str
    description: str

    def run(self, store: Any, config: dict) -> None: ...


@dataclass
class JobRun:
    """One execution record for a scheduled task."""

    task_name: str
    started_at: str
    finished_at: str | None = None
    status: Literal["ok", "error", "skipped"] = "ok"
    duration_ms: int = 0
    error: str | None = None
    triggered_by: str = "auto"


TASK_REGISTRY: dict[str, type[ScheduledTask]] = {}


def register_task(name: str):
    """Decorator to register a task class in the global registry."""

    def decorator(cls: type[ScheduledTask]) -> type[ScheduledTask]:
        TASK_REGISTRY[name] = cls
        return cls

    return decorator


# ---------------------------------------------------------------------------
# Built-in tasks
# ---------------------------------------------------------------------------


@register_task("auto-commit")
class AutoCommitTask:
    """Export vault changes and git commit (no push)."""

    name = "auto-commit"
    description = "导出变更并 git commit（不 push）"

    def run(self, store: Any, config: dict) -> None:
        from kb_mcp_lite.vault import VaultManager

        vm = VaultManager()
        name = vm.get_current()
        vm.commit(config.get("message", "auto-commit"), name=name)


@register_task("auto-embed")
class AutoEmbedTask:
    """Process one batch from the embedding queue."""

    name = "auto-embed"
    description = "处理 embedding 队列一个 batch"

    def run(self, store: Any, config: dict) -> None:
        max_jobs = config.get("batch_size", 20)
        store.process_embedding_queue(max_jobs=max_jobs)


@register_task("auto-reindex")
class AutoReindexTask:
    """Full FTS5 reindex."""

    name = "auto-reindex"
    description = "FTS5 全量重建索引"

    def run(self, store: Any, config: dict) -> None:
        store.reindex()


@register_task("doctor")
class DoctorTask:
    """Run health check and write audit log."""

    name = "doctor"
    description = "运行健康检查，写 audit log"

    def run(self, store: Any, config: dict) -> None:
        result = store.doctor()
        if not result.ok:
            logger.warning("doctor check reported issues: %s", result)


@register_task("prune")
class PruneTask:
    """Hard-delete documents soft-deleted more than N days ago."""

    name = "prune"
    description = "清理软删除超过 N 天的文档"

    def run(self, store: Any, config: dict) -> None:
        from datetime import timedelta

        older_than = timedelta(days=config.get("older_than_days", 30))
        store.prune(older_than=older_than)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class TaskScheduler:
    """APScheduler-backed task scheduler.

    Reads task configuration from the ``kb.schedule`` section of the
    config file and registers them with APScheduler.

    Supports three trigger types:
    - ``interval: "30m"`` — periodic
    - ``cron: "0 3 * * *"`` — cron expression
    - ``at: "2026-01-01T00:00:00"`` — one-shot
    """

    def __init__(self, store: Any, config: dict) -> None:
        self.store = store
        self.config = config
        self._scheduler: Any = None
        self._job_history: list[JobRun] = []
        self._consecutive_failures: dict[str, int] = {}
        self._disabled_tasks: set[str] = set()
        self._max_failures = config.get("kb", {}).get("schedule_max_failures", 3)

    def start(self) -> None:
        """Start the scheduler with configured tasks."""
        try:
            from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore[import-not-found]
        except ImportError:
            logger.warning(
                "APScheduler not installed; scheduled tasks disabled. "
                "Run: pip install 'kb-mcp-lite[v0.8]'"
            )
            return

        self._scheduler = BackgroundScheduler(daemon=True)
        schedule_cfg = self.config.get("kb", {}).get("schedule", [])

        for task_config in schedule_cfg:
            if not task_config.get("enabled", True):
                continue
            self._register_task(task_config)

        self._scheduler.start()
        logger.info("Scheduler started with %d tasks", len(schedule_cfg))

    def stop(self) -> None:
        """Shutdown the scheduler gracefully."""
        if self._scheduler:
            self._scheduler.shutdown(wait=True)
            self._scheduler = None

    def run_task_now(self, task_name: str) -> JobRun:
        """Manually trigger a task by name."""
        if task_name not in TASK_REGISTRY:
            raise ValueError(f"unknown task: {task_name!r}")

        task_cls = TASK_REGISTRY[task_name]
        return self._execute_task(task_cls, {}, triggered_by="manual")

    def list_tasks(self) -> list[dict[str, Any]]:
        """Return all registered tasks with their status."""
        # Get schedule config from file
        from kb_mcp_lite.config import load_config

        cfg = load_config()
        schedule_cfg = cfg.get("kb", {}).get("schedule", [])
        task_configs = {t.get("task"): t for t in schedule_cfg}

        tasks = []
        for name, cls in TASK_REGISTRY.items():
            file_config = task_configs.get(name, {})
            # Check disabled from config file (enabled=false means disabled)
            file_disabled = file_config.get("enabled") is False
            # Also check in-memory disabled set
            memory_disabled = name in self._disabled_tasks

            tasks.append(
                {
                    "name": name,
                    "description": cls.description,
                    "disabled": file_disabled or memory_disabled,
                    "consecutive_failures": self._consecutive_failures.get(name, 0),
                    "interval": file_config.get("interval"),
                    "cron": file_config.get("cron"),
                    "config": file_config,
                }
            )
        return tasks

    def get_task_config(self, task_name: str) -> dict[str, Any] | None:
        """Get configuration for a specific task from the config file."""
        schedule_cfg = self.config.get("kb", {}).get("schedule", [])
        for task_config in schedule_cfg:
            if task_config.get("task") == task_name:
                return task_config
        return None

    def update_task(self, task_name: str, updates: dict) -> bool:
        """Update an existing task configuration."""
        if task_name not in TASK_REGISTRY:
            raise ValueError(f"unknown task: {task_name!r}")

        from kb_mcp_lite.config import config_path, load_config

        cfg_path = config_path()
        cfg = load_config()

        # Ensure kb.schedule exists
        if "kb" not in cfg:
            cfg["kb"] = {}
        if "schedule" not in cfg["kb"]:
            cfg["kb"]["schedule"] = []

        # Find and update or add the task
        found = False
        for i, existing in enumerate(cfg["kb"]["schedule"]):
            if existing.get("task") == task_name:
                cfg["kb"]["schedule"][i].update(updates)
                cfg["kb"]["schedule"][i]["task"] = task_name  # Ensure task name is preserved
                found = True
                break

        # If task not found, add it
        if not found:
            new_config = {"task": task_name}
            new_config.update(updates)
            cfg["kb"]["schedule"].append(new_config)

        # Save config
        import yaml

        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(
            yaml.dump(cfg, allow_unicode=True, default_flow_style=False), encoding="utf-8"
        )
        return True

    def enable_task(self, task_name: str) -> bool:
        """Re-enable a previously auto-disabled task."""
        if task_name not in TASK_REGISTRY:
            raise ValueError(f"unknown task: {task_name!r}")
        self._disabled_tasks.discard(task_name)
        self._consecutive_failures[task_name] = 0
        return self.update_task(task_name, {"enabled": True})

    def disable_task(self, task_name: str) -> bool:
        """Disable a task."""
        if task_name not in TASK_REGISTRY:
            raise ValueError(f"unknown task: {task_name!r}")
        self._disabled_tasks.add(task_name)
        return self.update_task(task_name, {"enabled": False})

    def get_status(self) -> dict[str, Any]:
        """Return scheduler status including next run times."""
        jobs = []
        if self._scheduler:
            for job in self._scheduler.get_jobs():
                next_run = job.next_run_time
                jobs.append(
                    {
                        "id": job.id,
                        "next_run": next_run.isoformat() if next_run else None,
                    }
                )
        return {
            "running": self._scheduler is not None and self._scheduler.running,
            "jobs": jobs,
            "history_count": len(self._job_history),
        }

    def get_history(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent task execution history."""
        rows = self.store._conn.execute(
            """
            SELECT task_name, started_at, finished_at, status, duration_ms, error, triggered_by
            FROM schedule_history
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- internal -----------------------------------------------------------

    def _register_task(self, task_config: dict) -> None:
        task_name = task_config.get("task", "")
        if task_name not in TASK_REGISTRY:
            logger.warning("Unknown scheduled task: %s", task_name)
            return

        task_cls = TASK_REGISTRY[task_name]
        trigger = self._build_trigger(task_config)
        if trigger is None:
            logger.warning("Invalid trigger for task %s: %s", task_name, task_config)
            return

        self._scheduler.add_job(
            func=self._execute_task,
            trigger=trigger,
            args=[task_cls, task_config],
            id=f"kb-{task_name}",
            max_instances=1,
            coalesce=True,
        )

    def _build_trigger(self, task_config: dict) -> Any:
        """Build an APScheduler trigger from task config."""
        try:
            from apscheduler.triggers.interval import IntervalTrigger  # type: ignore[import-not-found]
            from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-not-found]
            from apscheduler.triggers.date import DateTrigger  # type: ignore[import-not-found]
        except ImportError:
            return None

        if "interval" in task_config:
            return IntervalTrigger(seconds=self._parse_interval(task_config["interval"]))
        if "cron" in task_config:
            return CronTrigger.from_crontab(task_config["cron"])
        if "at" in task_config:
            return DateTrigger(run_date=datetime.fromisoformat(task_config["at"]))
        return None

    @staticmethod
    def _parse_interval(value: str) -> int:
        """Parse interval string like '30m', '5s', '2h' to seconds."""
        value = value.strip().lower()
        if value.endswith("s"):
            return int(value[:-1])
        if value.endswith("m"):
            return int(value[:-1]) * 60
        if value.endswith("h"):
            return int(value[:-1]) * 3600
        return int(value)

    def _execute_task(
        self,
        task_cls: type[ScheduledTask],
        task_config: dict,
        triggered_by: str = "auto",
    ) -> JobRun:
        """Execute a task and record the result.

        After ``_max_failures`` consecutive failures the task is
        automatically disabled for the rest of this scheduler session.
        """
        task_name = task_cls.name

        # Skip if auto-disabled
        if task_name in self._disabled_tasks:
            now = datetime.now(timezone.utc).isoformat()
            run = JobRun(
                task_name=task_name,
                started_at=now,
                finished_at=now,
                status="skipped",
                triggered_by=triggered_by,
                error="auto-disabled after consecutive failures",
            )
            self._record_history(run)
            return run

        now = datetime.now(timezone.utc).isoformat()
        start = time.monotonic()

        try:
            task_instance = task_cls()
            task_instance.run(self.store, task_config)
            duration_ms = int((time.monotonic() - start) * 1000)
            run = JobRun(
                task_name=task_name,
                started_at=now,
                finished_at=datetime.now(timezone.utc).isoformat(),
                status="ok",
                duration_ms=duration_ms,
                triggered_by=triggered_by,
            )
            self._consecutive_failures[task_name] = 0
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            failures = self._consecutive_failures.get(task_name, 0) + 1
            self._consecutive_failures[task_name] = failures
            run = JobRun(
                task_name=task_name,
                started_at=now,
                finished_at=datetime.now(timezone.utc).isoformat(),
                status="error",
                duration_ms=duration_ms,
                error=str(exc),
                triggered_by=triggered_by,
            )
            logger.exception(
                "Scheduled task %s failed (%d/%d)", task_name, failures, self._max_failures
            )

            if failures >= self._max_failures:
                self._disabled_tasks.add(task_name)
                # Remove from APScheduler
                if self._scheduler:
                    job_id = f"kb-{task_name}"
                    try:
                        self._scheduler.remove_job(job_id)
                    except Exception:
                        pass
                logger.warning(
                    "Task %s auto-disabled after %d consecutive failures",
                    task_name,
                    failures,
                )

        self._record_history(run)
        return run

    def _record_history(self, run: JobRun) -> None:
        """Write execution record to schedule_history table."""
        try:
            self.store._conn.execute(
                """
                INSERT INTO schedule_history
                    (task_name, started_at, finished_at, status, duration_ms, error, triggered_by)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.task_name,
                    run.started_at,
                    run.finished_at,
                    run.status,
                    run.duration_ms,
                    run.error,
                    run.triggered_by,
                ),
            )
            self.store._conn.commit()
        except Exception:
            logger.debug("Failed to record schedule history", exc_info=True)
        self._job_history.append(run)
