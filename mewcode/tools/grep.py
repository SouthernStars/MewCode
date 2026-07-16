from __future__ import annotations

import copy
import logging
import re
from pathlib import Path

from pydantic import BaseModel, Field

from mewcode.permissions.sandbox import PathSandbox
from mewcode.tools.base import SKIP_DIRS, Tool, ToolResult
from mewcode.tools.path_utils import resolve_tool_path, validate_glob_pattern

log = logging.getLogger(__name__)


class Params(BaseModel):
    pattern: str = Field(description="Regex pattern to search for")
    path: str = Field(default=".", description="Base directory to search from")
    include: str = Field(default="", description="Glob filter for filenames (e.g. '*.py')")


class Grep(Tool):
    name = "Grep"
    description = "Search file contents using a regex pattern, returning file:line:content matches."
    params_model = Params
    category = "read"
    is_concurrency_safe = True


    def __init__(self, path_sandbox: PathSandbox | None = None) -> None:
        self._path_sandbox = path_sandbox or PathSandbox(str(Path.cwd()))

    def for_project_root(
        self,
        project_root: Path,
        path_sandbox: PathSandbox,
    ) -> Grep:
        configured = copy.copy(self)
        configured._path_sandbox = path_sandbox
        return configured


    async def execute(self, params: Params) -> ToolResult:
        base, path_error = resolve_tool_path(params.path, self._path_sandbox)
        if base is None:
            return ToolResult(output=path_error, is_error=True)
        if not base.exists():
            return ToolResult(output=f"Error: path not found: {params.path}", is_error=True)

        try:
            regex = re.compile(params.pattern)
        except re.error as e:
            return ToolResult(output=f"Error: invalid regex: {e}", is_error=True)

        glob_pattern = params.include if params.include else "**/*"
        if not glob_pattern.startswith("**/"):
            glob_pattern = "**/" + glob_pattern
        pattern_error = validate_glob_pattern(glob_pattern)
        if pattern_error:
            return ToolResult(output=pattern_error, is_error=True)

        results: list[str] = []
        for file_path in sorted(base.glob(glob_pattern)):
            if not file_path.is_file():
                continue
            if not self._path_sandbox.check(str(file_path))[0]:
                continue
            if any(part in SKIP_DIRS for part in file_path.parts):
                continue
            try:
                text = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError as exc:
                log.error(
                    "Grep skipped unreadable file: path=%s reason=%s",
                    file_path,
                    exc,
                    exc_info=True,
                )
                continue
            for line_num, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    rel = file_path.relative_to(base)
                    results.append(f"{rel}:{line_num}:{line}")

        if not results:
            return ToolResult(output="No matches found.")
        return ToolResult(output="\n".join(results))

