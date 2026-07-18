"""Cron 任务持久化存储。

任务存储在 .mewcode/scheduled_tasks.json 文件中。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mewcode.persistence import (
    PersistenceError,
    atomic_write_json,
    file_lock,
    load_versioned_json,
)
from mewcode.scheduler.cron import CronExpression

SCHEDULED_TASKS_FILE = ".mewcode/scheduled_tasks.json"
CRON_SCHEMA_VERSION = 1


@dataclass
class CronJob:
    """一个调度任务。"""

    id: str
    """唯一标识（uuid）。"""

    cron: str
    """Cron 表达式字符串。"""

    prompt: str
    """触发时注入的提示词。"""

    recurring: bool = True
    """是否是周期性任务（false = 一次性）。"""

    durable: bool = False
    """是否持久化到磁盘（false = 重启后消失）。"""

    created_at: str = ""
    """创建时间 ISO 格式。"""

    last_fired_at: str = ""
    """上次触发时间 ISO 格式。"""

    fired: bool = False
    """一次性任务是否已触发（触发后不再激活）。"""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "cron": self.cron,
            "prompt": self.prompt,
            "recurring": self.recurring,
            "durable": self.durable,
            "created_at": self.created_at,
            "last_fired_at": self.last_fired_at,
            "fired": self.fired,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CronJob:
        return cls(
            id=data["id"],
            cron=data["cron"],
            prompt=data["prompt"],
            recurring=data.get("recurring", True),
            durable=data.get("durable", False),
            created_at=data.get("created_at", ""),
            last_fired_at=data.get("last_fired_at", ""),
            fired=data.get("fired", False),
        )

    def get_next_fire(self, after: datetime | None = None) -> datetime | None:
        """计算下一次触发时间。"""
        try:
            expr = CronExpression.parse(self.cron)
            return expr.next_fire(after)
        except Exception:
            return None


class CronStore:
    """Cron 任务的持久化存储。"""

    def __init__(self, work_dir: str) -> None:
        self._work_dir = work_dir
        self._file_path = Path(work_dir) / SCHEDULED_TASKS_FILE
        self._jobs: dict[str, CronJob] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add(self, job: CronJob) -> None:
        """添加任务。"""
        self._ensure_loaded()
        if not job.created_at:
            job.created_at = _now_iso()
        self._jobs[job.id] = job
        if job.durable:
            self._save()

    def remove(self, job_id: str) -> bool:
        """删除任务。

        Returns:
            是否成功删除。
        """
        self._ensure_loaded()
        if job_id in self._jobs:
            del self._jobs[job_id]
            self._save()
            return True
        return False

    def get(self, job_id: str) -> CronJob | None:
        """获取任务。"""
        self._ensure_loaded()
        return self._jobs.get(job_id)

    def list(self) -> list[CronJob]:
        """列出所有活跃任务。

        不包括一次性且已触发的任务。
        """
        self._ensure_loaded()
        return [
            j for j in self._jobs.values()
            if not (not j.recurring and j.fired)
        ]

    def list_all(self) -> list[CronJob]:
        """列出所有任务（包括已触发的一次性任务）。"""
        self._ensure_loaded()
        return list(self._jobs.values())

    def get_due(self, now: datetime | None = None) -> list[CronJob]:
        """获取所有到期的活跃任务。

        Args:
            now: 当前时间（None = 当前 UTC 时间）。

        Returns:
            到期任务列表。
        """
        if now is None:
            now = datetime.now(timezone.utc)

        due: list[CronJob] = []
        for job in self.list():
            reference = job.last_fired_at or job.created_at
            next_fire = job.get_next_fire(
                after=_parse_iso(reference) if reference else now
            )
            if next_fire is not None and next_fire <= now:
                due.append(job)
        return due

    def mark_fired(self, job_id: str, fired_at: str | None = None) -> None:
        """标记任务已触发。"""
        self._ensure_loaded()
        job = self._jobs.get(job_id)
        if job is None:
            return
        job.last_fired_at = fired_at or _now_iso()
        if not job.recurring:
            job.fired = True
        if job.durable:
            self._save()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _save(self) -> None:
        """保存到磁盘。"""
        durable_jobs = [j for j in self._jobs.values() if j.durable]
        data = {
            "schema_version": CRON_SCHEMA_VERSION,
            "jobs": [j.to_dict() for j in durable_jobs],
        }

        with file_lock(self._file_path, format_name="cron task snapshot"):
            atomic_write_json(
                self._file_path,
                data,
                format_name="cron task snapshot",
            )

    def _load(self) -> None:
        """从磁盘加载。"""
        if not self._file_path.exists():
            self._jobs = {}
            self._loaded = True
            return

        data = load_versioned_json(
            self._file_path,
            current_version=CRON_SCHEMA_VERSION,
            migrations={0: _migrate_cron_v0},
            format_name="cron task snapshot",
        )
        jobs = data.get("jobs")
        if not isinstance(jobs, list):
            raise PersistenceError(
                f"Invalid cron task snapshot at {self._file_path}: "
                "'jobs' must be a JSON array"
            )

        self._jobs = {}
        for index, item in enumerate(jobs):
            if not isinstance(item, dict):
                raise PersistenceError(
                    f"Invalid cron task snapshot at {self._file_path}: "
                    f"jobs[{index}] must be a JSON object"
                )
            try:
                job = CronJob.from_dict(item)
            except (KeyError, TypeError, ValueError) as exc:
                raise PersistenceError(
                    f"Invalid cron task snapshot at {self._file_path}: "
                    f"jobs[{index}] is invalid: {exc}"
                ) from exc
            if job.id in self._jobs:
                raise PersistenceError(
                    f"Invalid cron task snapshot at {self._file_path}: "
                    f"duplicate job id {job.id!r}"
                )
            self._jobs[job.id] = job

        self._loaded = True

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._load()

    def reload(self) -> None:
        """重新加载（用于外部修改了文件）。"""
        self._loaded = False
        self._load()


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _migrate_cron_v0(data: Any) -> dict[str, Any]:
    if not isinstance(data, list):
        raise TypeError("legacy cron task snapshot root must be a JSON array")
    return {"schema_version": 1, "jobs": data}


def _parse_iso(iso_str: str) -> datetime:
    """解析 ISO 时间字符串。"""
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
