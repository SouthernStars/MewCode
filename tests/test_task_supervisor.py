from __future__ import annotations

import asyncio
import logging

import pytest

from mewcode.task_supervisor import TaskSupervisor


@pytest.mark.asyncio
async def test_supervisor_tracks_and_cancels_named_tasks() -> None:
    supervisor = TaskSupervisor(session_id="session-1", agent_id="agent-1")
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def worker() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = supervisor.create(worker(), name="scheduler.loop")
    await asyncio.wait_for(started.wait(), timeout=1)

    assert task.get_name() == "scheduler.loop"
    assert supervisor.active_names == ("scheduler.loop",)

    await supervisor.shutdown(timeout=1)

    assert cancelled.is_set()
    assert task.cancelled()
    assert supervisor.active_names == ()


@pytest.mark.asyncio
async def test_supervisor_logs_task_failures_with_runtime_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    supervisor = TaskSupervisor(session_id="session-2", agent_id="agent-2")

    async def fail() -> None:
        raise ValueError("broken background task")

    with caplog.at_level(logging.ERROR):
        task = supervisor.create(fail(), name="hooks.async")
        with pytest.raises(ValueError, match="broken background task"):
            await task
        await asyncio.sleep(0)

    assert supervisor.active_names == ()
    assert "hooks.async" in caplog.text
    assert "session-2" in caplog.text
    assert "agent-2" in caplog.text
    assert "broken background task" in caplog.text


@pytest.mark.asyncio
async def test_supervisor_rejects_new_tasks_after_shutdown() -> None:
    supervisor = TaskSupervisor()
    await supervisor.shutdown()
    coroutine = asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="shut down"):
        supervisor.create(coroutine, name="late-task")

    assert coroutine.cr_frame is None


@pytest.mark.asyncio
async def test_supervisor_shutdown_is_idempotent() -> None:
    supervisor = TaskSupervisor()

    await supervisor.shutdown()
    await supervisor.shutdown()

    assert supervisor.active_names == ()
