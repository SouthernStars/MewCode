"""Shared execution context for Agent, Team, and Workflow orchestration."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    work_dir: str
    session_id: str = ""
    run_id: str = ""
    agent_id: str = ""
    parent_agent_id: str = ""
    task_supervisor: Any = None
    event_bus: Any = None
    trace_manager: Any = None
    permission_checker: Any = None

    def child(
        self,
        *,
        run_id: str | None = None,
        agent_id: str | None = None,
        parent_agent_id: str | None = None,
    ) -> ExecutionContext:
        return replace(
            self,
            run_id=self.run_id if run_id is None else run_id,
            agent_id=self.agent_id if agent_id is None else agent_id,
            parent_agent_id=(
                self.parent_agent_id
                if parent_agent_id is None
                else parent_agent_id
            ),
        )
