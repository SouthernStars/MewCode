from __future__ import annotations

import re
from pathlib import Path, PureWindowsPath

from mewcode.permissions.sandbox import PathSandbox, PathSandboxViolation


def resolve_tool_path(
    path: str,
    sandbox: PathSandbox,
) -> tuple[Path | None, str]:
    try:
        return sandbox.resolve(path), ""
    except PathSandboxViolation as exc:
        return None, f"Error: path sandbox denied: {exc}"


def validate_glob_pattern(pattern: str) -> str:
    parts = [part for part in re.split(r"[\\/]", pattern) if part]
    if (
        Path(pattern).is_absolute()
        or PureWindowsPath(pattern).is_absolute()
        or ".." in parts
    ):
        return f"Error: glob pattern escapes the search root: {pattern}"
    return ""
