from __future__ import annotations

from mewcode.execution_context import ExecutionContext


def test_child_context_preserves_services_and_updates_identity() -> None:
    supervisor = object()
    bus = object()
    context = ExecutionContext(
        work_dir="/repo",
        session_id="session-1",
        agent_id="agent-1",
        task_supervisor=supervisor,
        event_bus=bus,
    )

    child = context.child(run_id="run-1", agent_id="agent-2", parent_agent_id="agent-1")

    assert child.work_dir == context.work_dir
    assert child.session_id == context.session_id
    assert child.run_id == "run-1"
    assert child.agent_id == "agent-2"
    assert child.parent_agent_id == "agent-1"
    assert child.task_supervisor is supervisor
    assert child.event_bus is bus
