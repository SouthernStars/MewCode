from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


class TaskSupervisor:
    """Own and observe background tasks for one Runtime instance."""

    def __init__(
        self,
        *,
        session_id: str = "",
        agent_id: str = "",
    ) -> None:
        self.session_id = session_id
        self.agent_id = agent_id
        self._tasks: dict[asyncio.Task[Any], str] = {}
        self._accepting = True

    @property
    def active_names(self) -> tuple[str, ...]:
        return tuple(self._tasks.values())

    def create(
        self,
        coroutine: Coroutine[Any, Any, T],
        *,
        name: str,
    ) -> asyncio.Task[T]:
        if not self._accepting:
            coroutine.close()
            raise RuntimeError(
                f"TaskSupervisor is shut down; cannot create task '{name}'"
            )

        task = asyncio.create_task(coroutine, name=name)
        self._tasks[task] = name
        task.add_done_callback(self._on_done)
        return task

    def cancel(self, task: asyncio.Task[Any]) -> bool:
        if task not in self._tasks or task.done():
            return False
        task.cancel()
        return True

    async def shutdown(self, *, timeout: float = 3.0) -> None:
        self._accepting = False
        current = asyncio.current_task()
        tasks = [
            task
            for task in self._tasks
            if task is not current and not task.done()
        ]
        if not tasks:
            return

        for task in tasks:
            task.cancel()

        _done, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            names = [
                self._tasks.get(task, task.get_name())
                for task in pending
            ]
            log.error(
                "Background tasks did not stop within %.1fs: names=%s "
                "session_id=%s agent_id=%s",
                timeout,
                names,
                self.session_id,
                self.agent_id,
            )
            raise RuntimeError(
                "TaskSupervisor shutdown timed out: "
                f"tasks={names} timeout={timeout}"
            )

    def _on_done(self, task: asyncio.Task[Any]) -> None:
        name = self._tasks.pop(task, task.get_name())
        if task.cancelled():
            return
        exception = task.exception()
        if exception is None:
            return
        log.error(
            "Background task failed: name=%s session_id=%s agent_id=%s: %s",
            name,
            self.session_id,
            self.agent_id,
            exception,
            exc_info=(
                type(exception),
                exception,
                exception.__traceback__,
            ),
        )
