from __future__ import annotations

import sys
from pathlib import Path

from mewcode.__main__ import _doctor, _init_project, _version


def test_version_is_package_value() -> None:
    assert _version()


def test_init_creates_minimal_project_files(tmp_path: Path) -> None:
    _init_project(tmp_path)

    config = tmp_path / ".mewcode" / "config.yaml"
    assert config.exists()
    assert "providers:" in config.read_text(encoding="utf-8")
    assert (tmp_path / ".gitignore").exists()


def test_doctor_reports_missing_api_key(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _init_project(tmp_path)
    exit_code = _doctor(tmp_path)

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "API Key/default" in output
    assert sys.version_info >= (3, 11)
