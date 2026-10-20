"""Tests for the built-in scheduler (v0.8 特性 #7)."""

import pytest
from datetime import datetime, timezone
from kb_mcp_lite.schema import Document
from kb_mcp_lite.store.sqlite import SqliteStore
from kb_mcp_lite.scheduler import (
    TASK_REGISTRY,
    TaskScheduler,
    JobRun,
    register_task,
)


# ---------------------------------------------------------------------------
# Task registry
# ---------------------------------------------------------------------------


class TestTaskRegistry:
    def test_all_builtin_tasks_registered(self):
        expected = {"auto-commit", "auto-embed", "auto-reindex", "doctor", "prune"}
        assert set(TASK_REGISTRY.keys()) == expected

    def test_task_protocol_compliance(self):
        for name, cls in TASK_REGISTRY.items():
            instance = cls()
            assert hasattr(instance, "name")
            assert hasattr(instance, "description")
            assert hasattr(instance, "run")
            assert instance.name == name

    def test_register_custom_task(self):
        @register_task("test-custom")
        class CustomTask:
            name = "test-custom"
            description = "test"

            def run(self, store, config):
                pass

        assert "test-custom" in TASK_REGISTRY
        del TASK_REGISTRY["test-custom"]  # cleanup


# ---------------------------------------------------------------------------
# TaskScheduler
# ---------------------------------------------------------------------------


class TestTaskScheduler:
    def test_list_tasks(self, tmp_path):
        store = SqliteStore(tmp_path / "test.db")
        scheduler = TaskScheduler(store, {})
        tasks = scheduler.list_tasks()
        names = {t["name"] for t in tasks}
        assert "auto-commit" in names
        assert "doctor" in names
        store.close()

    def test_run_task_now_unknown(self, tmp_path):
        store = SqliteStore(tmp_path / "test.db")
        scheduler = TaskScheduler(store, {})
        with pytest.raises(ValueError, match="unknown task"):
            scheduler.run_task_now("nonexistent")
        store.close()

    def test_run_task_now_doctor(self, tmp_path):
        """Doctor task should complete successfully on empty store."""
        store = SqliteStore(tmp_path / "test.db")
        scheduler = TaskScheduler(store, {})
        run = scheduler.run_task_now("doctor")
        assert run.status == "ok"
        assert run.task_name == "doctor"
        assert run.duration_ms >= 0
        store.close()

    def test_run_task_now_auto_reindex(self, tmp_path):
        """Auto-reindex task should complete on empty store."""
        store = SqliteStore(tmp_path / "test.db")
        store.add(Document(id="d1", type="test", title="D1", body="body"))
        scheduler = TaskScheduler(store, {})
        run = scheduler.run_task_now("auto-reindex")
        assert run.status == "ok"
        store.close()

    def test_run_task_now_auto_embed(self, tmp_path):
        """Auto-embed task should complete (may be no-op if no embedder)."""
        store = SqliteStore(tmp_path / "test.db")
        scheduler = TaskScheduler(store, {})
        run = scheduler.run_task_now("auto-embed")
        assert run.status == "ok"
        store.close()

    def test_history_recorded(self, tmp_path):
        """Task execution should be recorded in schedule_history."""
        store = SqliteStore(tmp_path / "test.db")
        scheduler = TaskScheduler(store, {})
        scheduler.run_task_now("doctor")
        history = scheduler.get_history()
        assert len(history) >= 1
        assert history[0]["task_name"] == "doctor"
        assert history[0]["status"] == "ok"
        store.close()

    def test_start_stop_no_apscheduler(self, tmp_path):
        """start/stop should not crash when APScheduler is not installed."""
        store = SqliteStore(tmp_path / "test.db")
        scheduler = TaskScheduler(store, {})
        # start should be a no-op (APScheduler might not be installed)
        scheduler.start()
        scheduler.stop()
        store.close()

    def test_start_with_config(self, tmp_path):
        """start with schedule config should register tasks."""
        store = SqliteStore(tmp_path / "test.db")
        config = {
            "kb": {
                "schedule": [
                    {"task": "doctor", "interval": "1h", "enabled": True},
                    {"task": "auto-embed", "interval": "5m", "enabled": False},
                ]
            }
        }
        scheduler = TaskScheduler(store, config)
        scheduler.start()
        # Should have at least 1 job (doctor), auto-embed disabled
        if scheduler._scheduler:
            jobs = scheduler._scheduler.get_jobs()
            assert len(jobs) == 1
        scheduler.stop()
        store.close()

    def test_auto_disable_after_failures(self, tmp_path):
        """Task should be auto-disabled after max_failures consecutive errors."""
        from kb_mcp_lite.scheduler import register_task

        @register_task("_test_failing")
        class FailingTask:
            name = "_test_failing"
            description = "always fails"

            def run(self, store, config):
                raise RuntimeError("boom")

        store = SqliteStore(tmp_path / "test.db")
        config = {"kb": {"schedule_max_failures": 3}}
        scheduler = TaskScheduler(store, config)

        # Run 3 times — should auto-disable on the 3rd
        for _ in range(3):
            scheduler.run_task_now("_test_failing")

        assert "_test_failing" in scheduler._disabled_tasks
        assert scheduler._consecutive_failures["_test_failing"] == 3

        # 4th run should be skipped
        run = scheduler.run_task_now("_test_failing")
        assert run.status == "skipped"

        # History should show 3 errors + 1 skipped
        history = scheduler.get_history()
        statuses = [h["status"] for h in history[:4]]
        assert statuses == ["skipped", "error", "error", "error"]

        del TASK_REGISTRY["_test_failing"]
        store.close()

    def test_enable_task_resets_failures(self, tmp_path):
        """enable_task should clear disabled state and failure count."""
        from kb_mcp_lite.scheduler import register_task

        @register_task("_test_recoverable")
        class RecoverableTask:
            name = "_test_recoverable"
            description = "fails then succeeds"

            def run(self, store, config):
                raise RuntimeError("boom")

        store = SqliteStore(tmp_path / "test.db")
        scheduler = TaskScheduler(store, {})

        # Fail 3 times
        for _ in range(3):
            scheduler.run_task_now("_test_recoverable")
        assert "_test_recoverable" in scheduler._disabled_tasks

        # Re-enable
        scheduler.enable_task("_test_recoverable")
        assert "_test_recoverable" not in scheduler._disabled_tasks
        assert scheduler._consecutive_failures["_test_recoverable"] == 0

        # Now make it succeed
        TASK_REGISTRY["_test_recoverable"].run = lambda self, store, config: None
        run = scheduler.run_task_now("_test_recoverable")
        assert run.status == "ok"

        del TASK_REGISTRY["_test_recoverable"]
        store.close()

    def test_consecutive_failures_reset_on_success(self, tmp_path):
        """Failure counter should reset to 0 on success."""
        from kb_mcp_lite.scheduler import register_task

        call_count = [0]

        @register_task("_test_flaky")
        class FlakyTask:
            name = "_test_flaky"
            description = "fails twice then succeeds"

            def run(self, store, config):
                call_count[0] += 1
                if call_count[0] <= 2:
                    raise RuntimeError("transient")

        store = SqliteStore(tmp_path / "test.db")
        scheduler = TaskScheduler(store, {})

        scheduler.run_task_now("_test_flaky")  # fail 1
        scheduler.run_task_now("_test_flaky")  # fail 2
        assert scheduler._consecutive_failures["_test_flaky"] == 2

        scheduler.run_task_now("_test_flaky")  # success → reset
        assert scheduler._consecutive_failures["_test_flaky"] == 0

        del TASK_REGISTRY["_test_flaky"]
        store.close()


# ---------------------------------------------------------------------------
# Migration 0008
# ---------------------------------------------------------------------------


class TestScheduleHistoryMigration:
    def test_schedule_history_table_exists(self, tmp_path):
        """Migration 0008 should create schedule_history table."""
        store = SqliteStore(tmp_path / "test.db")
        rows = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schedule_history'"
        ).fetchall()
        assert len(rows) == 1
        store.close()

    def test_insert_and_query(self, tmp_path):
        """Can insert and query schedule_history records."""
        store = SqliteStore(tmp_path / "test.db")
        now = datetime.now(timezone.utc).isoformat()
        store._conn.execute(
            """
            INSERT INTO schedule_history (task_name, started_at, finished_at, status, duration_ms, triggered_by)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("test-task", now, now, "ok", 100, "manual"),
        )
        store._conn.commit()
        rows = store._conn.execute("SELECT * FROM schedule_history").fetchall()
        assert len(rows) == 1
        assert dict(rows[0])["task_name"] == "test-task"
        store.close()


# ---------------------------------------------------------------------------
# JobRun dataclass
# ---------------------------------------------------------------------------


class TestJobRun:
    def test_defaults(self):
        run = JobRun(task_name="t", started_at="2026-01-01T00:00:00")
        assert run.status == "ok"
        assert run.duration_ms == 0
        assert run.error is None
        assert run.triggered_by == "auto"
