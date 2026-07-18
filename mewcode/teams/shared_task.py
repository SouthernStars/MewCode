from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from mewcode.persistence import (
    PersistenceError,
    atomic_write_json,
    file_lock,
    read_versioned_json,
)

SHARED_TASK_SCHEMA_VERSION = 1

@dataclass
class SharedTask:
    id: str
    title: str
    description: str = ""
    status: str = "pending"  # pending | in_progress | completed | blocked
    assignee: str = ""
    blocks: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    created_by: str = ""


    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SharedTask:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class SharedTaskStore:


    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._next_id = 1
        self._tasks: dict[str, SharedTask] = {}
        self._load()

    def _load(self) -> None:
        with file_lock(self._path, format_name="shared task snapshot"):
            self._load_unlocked()

    def _load_unlocked(self) -> None:
        self._next_id = 1
        self._tasks = {}
        if not self._path.exists():
            return
        data = read_versioned_json(
            self._path,
            current_version=SHARED_TASK_SCHEMA_VERSION,
            migrations={0: _migrate_shared_tasks_v0},
            format_name="shared task snapshot",
        )
        next_id = data.get("next_id")
        tasks = data.get("tasks")
        if isinstance(next_id, bool) or not isinstance(next_id, int) or next_id < 1:
            raise PersistenceError(
                f"Invalid shared task snapshot at {self._path}: "
                f"next_id must be a positive integer, got {next_id!r}"
            )
        if not isinstance(tasks, list):
            raise PersistenceError(
                f"Invalid shared task snapshot at {self._path}: "
                "tasks must be a JSON array"
            )
        self._next_id = next_id
        for index, item in enumerate(tasks):
            if not isinstance(item, dict):
                raise PersistenceError(
                    f"Invalid shared task snapshot at {self._path}: "
                    f"tasks[{index}] must be a JSON object"
                )
            try:
                task = SharedTask.from_dict(item)
            except (KeyError, TypeError, ValueError) as exc:
                raise PersistenceError(
                    f"Invalid shared task snapshot at {self._path}: "
                    f"tasks[{index}] is invalid: {exc}"
                ) from exc
            if task.id in self._tasks:
                raise PersistenceError(
                    f"Invalid shared task snapshot at {self._path}: "
                    f"duplicate task id {task.id!r}"
                )
            self._tasks[task.id] = task

    def _save_unlocked(self) -> None:
        data = {
            "schema_version": SHARED_TASK_SCHEMA_VERSION,
            "next_id": self._next_id,
            "tasks": [t.to_dict() for t in self._tasks.values()],
        }
        atomic_write_json(
            self._path,
            data,
            format_name="shared task snapshot",
        )

    def create(
        self,
        title: str,
        description: str = "",
        assignee: str = "",
        blocks: list[str] | None = None,
        blocked_by: list[str] | None = None,
        created_by: str = "",
    ) -> SharedTask:
        with file_lock(self._path, format_name="shared task snapshot"):
            self._load_unlocked()
            task_id = str(self._next_id)
            self._next_id += 1
            task = SharedTask(
                id=task_id,
                title=title,
                description=description,
                assignee=assignee,
                blocks=blocks or [],
                blocked_by=blocked_by or [],
                created_by=created_by,
            )
            self._tasks[task_id] = task
            self._save_unlocked()
            return task

    def get(self, task_id: str) -> SharedTask | None:
        self._load()
        return self._tasks.get(task_id)


    def list_tasks(
        self,
        status: str | None = None,
        assignee: str | None = None,
    ) -> list[SharedTask]:
        self._load()
        result = list(self._tasks.values())
        if status:
            result = [t for t in result if t.status == status]
        if assignee:
            result = [t for t in result if t.assignee == assignee]
        return result


    def update(
        self,
        task_id: str,
        status: str | None = None,
        assignee: str | None = None,
        description: str | None = None,
        add_blocks: list[str] | None = None,
        add_blocked_by: list[str] | None = None,
    ) -> SharedTask | None:
        with file_lock(self._path, format_name="shared task snapshot"):
            self._load_unlocked()
            task = self._tasks.get(task_id)
            if task is None:
                return None
            if status is not None:
                task.status = status
            if assignee is not None:
                task.assignee = assignee
            if description is not None:
                task.description = description
            if add_blocks:
                for bid in add_blocks:
                    if bid not in task.blocks:
                        task.blocks.append(bid)
            if add_blocked_by:
                for bid in add_blocked_by:
                    if bid not in task.blocked_by:
                        task.blocked_by.append(bid)
            self._save_unlocked()
            return task

    def init_empty(self) -> None:
        with file_lock(self._path, format_name="shared task snapshot"):
            self._tasks.clear()
            self._next_id = 1
            self._save_unlocked()


def _migrate_shared_tasks_v0(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise TypeError("legacy shared task snapshot root must be a JSON object")
    return {**data, "schema_version": 1}
