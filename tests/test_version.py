from __future__ import annotations

import re
from pathlib import Path

from mewcode.__main__ import _version


def test_cli_version_matches_project_metadata() -> None:
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    match = re.search(r'^version = "([^"]+)"$', pyproject.read_text(encoding="utf-8"), re.MULTILINE)
    assert match is not None
    assert _version() == match.group(1)
