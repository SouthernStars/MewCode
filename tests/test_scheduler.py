import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from mewcode.scheduler.cron import CronExpression, CronParseError
from mewcode.scheduler.runtime import SchedulerRuntime
from mewcode.scheduler.store import CronJob, CronStore
from mewcode.scheduler.wakeup import WakeupScheduler
from mewcode.observability import EventType, RuntimeEventBus


def test_cron_parsing_and_next_fire() -> None:
    start = datetime(2026, 7, 13, 8, 59, tzinfo=timezone.utc)
    weekdays = CronExpression.parse("0 9 * * 1-5")
    every_five = CronExpression.parse("*/5 * * * *")

    assert weekdays.next_fire(start) == datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)
    first = every_five.next_fire(start)
    second = every_five.next_fire(first)
    assert second - first == timedelta(minutes=5)


def test_invalid_cron_fails_clearly() -> None:
    with pytest.raises(CronParseError):
        CronExpression.parse("invalid")


def test_store_persists_durable_jobs_and_detects_due_jobs(tmp_path: Path) -> None:
    now = datetime(2026, 7, 15, 12, 2, tzinfo=timezone.utc)
    store = CronStore(str(tmp_path))
    due = CronJob(
        id="due",
        cron="* * * * *",
        prompt="run now",
        durable=True,
        created_at=(now - timedelta(minutes=2)).isoformat(),
    )
    future = CronJob(
        id="future",
        cron="0 13 * * *",
        prompt="later",
        durable=True,
        created_at=now.isoformat(),
    )
    store.add(due)
    store.add(future)

    assert [job.id for job in store.get_due(now)] == ["due"]
    restored = CronStore(str(tmp_path))
    assert {job.id for job in restored.list()} == {"due", "future"}

    snapshot = json.loads(
        (tmp_path / ".mewcode" / "scheduled_tasks.json").read_text(
            encoding="utf-8"
        )
    )
    assert snapshot["schema_version"] == 1
    assert {job["id"] for job in snapshot["jobs"]} == {"due", "future"}


def test_store_migrates_legacy_array_and_rejects_corruption(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".mewcode" / "scheduled_tasks.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            [
                {
                    "id": "legacy",
                    "cron": "* * * * *",
                    "prompt": "run",
                    "durable": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    assert CronStore(str(tmp_path)).get("legacy") is not None

    path.write_text("not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="cron task snapshot"):
        CronStore(str(tmp_path)).list()


@pytest.mark.asyncio
async def test_runtime_fires_cron_and_wakeup(tmp_path: Path) -> None:
    store = CronStore(str(tmp_path))
    cron_job = CronJob(id="cron", cron="* * * * *", prompt="cron prompt")
    store.add(cron_job)
    wakeups = WakeupScheduler()
    wakeup = wakeups.schedule(60, "check", "wakeup prompt")
    fired = []
    events = []
    event_bus = RuntimeEventBus()
    event_bus.subscribe(events.append)
    runtime = SchedulerRuntime(
        store,
        wakeups,
        on_fire=fired.append,
        event_bus=event_bus,
    )

    with patch.object(store, "get_due", return_value=[cron_job]), patch.object(
        wakeups, "get_due", return_value=[wakeup]
    ):
        await runtime._check_and_fire()

    assert [job.prompt for job in fired] == ["cron prompt", "wakeup prompt"]
    assert wakeups.list_all() == []
    assert [event.event_type for event in events] == [
        EventType.SCHEDULER_TRIGGER,
        EventType.SCHEDULER_TRIGGER,
    ]


@pytest.mark.asyncio
async def test_runtime_start_and_shutdown(tmp_path: Path) -> None:
    runtime = SchedulerRuntime(CronStore(str(tmp_path)))
    await runtime.start()
    assert runtime._running is True
    assert runtime._task is not None
    assert runtime._task.get_name() == "scheduler.loop"
    await runtime.shutdown()
    assert runtime._running is False
    assert runtime._task is None
