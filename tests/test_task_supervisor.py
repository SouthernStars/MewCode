from __future__ import annotations

import asyncio
import logging

import pytest

from mewcode.agents.task_manager import TaskManager
from mewcode.hooks import Action, ActionResult, Hook, HookContext, HookEngine
from mewcode.task_supervisor import TaskSupervisor
from mewcode.workflow.tool import WorkflowParams, WorkflowTool


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


@pytest.mark.asyncio
async def test_task_manager_uses_shared_supervisor() -> None:
    supervisor = TaskSupervisor()
    manager = TaskManager(task_supervisor=supervisor)
    started = asyncio.Event()

    class FakeAgent:
        team_name = ""
        _team_manager = None
        total_input_tokens = 0
        total_output_tokens = 0

        async def run_to_completion(self, task, conversation=None):
            started.set()
            await asyncio.Event().wait()

    task_id = manager.launch(FakeAgent(), "work")
    await asyncio.wait_for(started.wait(), timeout=1)

    assert manager.task_supervisor is supervisor
    assert supervisor.active_names == (f"agent.background.{task_id}",)
    assert manager.cancel(task_id) is True
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert supervisor.active_names == ()


@pytest.mark.asyncio
async def test_async_hooks_use_shared_supervisor(monkeypatch) -> None:
    supervisor = TaskSupervisor()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_action(action, context):
        started.set()
        await release.wait()
        return ActionResult(output="done", success=True)

    monkeypatch.setattr("mewcode.hooks.engine.execute_action", slow_action)
    engine = HookEngine(
        [
            Hook(
                id="notify",
                event="post_tool_use",
                action=Action(type="command", command="ignored"),
                async_exec=True,
            )
        ],
        task_supervisor=supervisor,
    )

    await engine.run_hooks(
        "post_tool_use",
        HookContext(event_name="post_tool_use"),
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    assert supervisor.active_names == ("hook.post_tool_use.notify",)
    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert supervisor.active_names == ()


@pytest.mark.asyncio
async def test_background_workflow_uses_shared_supervisor() -> None:
    supervisor = TaskSupervisor()
    started = asyncio.Event()

    class Definition:
        name = "demo"

    class Engine:
        def list_workflows(self):
            return [Definition()]

        async def execute(self, **kwargs):
            started.set()
            await asyncio.Event().wait()

    tool = WorkflowTool(
        engine=Engine(),
        task_supervisor=supervisor,
    )

    result = await tool.execute(
        WorkflowParams(workflow_name="demo", background=True)
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    assert result.is_error is False
    assert supervisor.active_names[0].startswith("workflow.demo.")

    await supervisor.shutdown()
    assert supervisor.active_names == ()
