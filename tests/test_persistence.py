from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

import mewcode.persistence as persistence
from mewcode.persistence import (
    PersistenceError,
    UnsupportedSchemaVersionError,
    atomic_write_json,
    file_lock,
    load_versioned_json,
)


def test_atomic_write_preserves_previous_snapshot_on_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"previous": true}', encoding="utf-8")

    def fail_replace(source: str | Path, target: str | Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(persistence.os, "replace", fail_replace)

    with pytest.raises(PersistenceError, match="state snapshot"):
        atomic_write_json(path, {"next": True}, format_name="state snapshot")

    assert json.loads(path.read_text(encoding="utf-8")) == {"previous": True}
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def test_file_lock_serializes_access(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    started = threading.Event()
    acquired = threading.Event()

    def contender() -> None:
        started.set()
        with file_lock(path, format_name="state snapshot"):
            acquired.set()

    with file_lock(path, format_name="state snapshot"):
        thread = threading.Thread(target=contender)
        thread.start()
        assert started.wait(timeout=1)
        assert not acquired.wait(timeout=0.1)

    thread.join(timeout=2)
    assert not thread.is_alive()
    assert acquired.is_set()


def test_versioned_loader_runs_explicit_v0_migration(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('[{"id": "legacy"}]', encoding="utf-8")

    def migrate_v0(data: Any) -> dict[str, Any]:
        assert isinstance(data, list)
        return {"schema_version": 1, "items": data}

    loaded = load_versioned_json(
        path,
        current_version=1,
        migrations={0: migrate_v0},
        format_name="state snapshot",
    )

    assert loaded == {
        "schema_version": 1,
        "items": [{"id": "legacy"}],
    }


def test_versioned_loader_rejects_future_schema(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"schema_version": 2}', encoding="utf-8")

    with pytest.raises(UnsupportedSchemaVersionError, match="version 2"):
        load_versioned_json(
            path,
            current_version=1,
            migrations={},
            format_name="state snapshot",
        )


def test_versioned_loader_reports_corrupted_json(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("not json", encoding="utf-8")

    with pytest.raises(PersistenceError, match="state snapshot"):
        load_versioned_json(
            path,
            current_version=1,
            migrations={},
            format_name="state snapshot",
        )
