from __future__ import annotations

import copy
import logging
from pathlib import Path

from pydantic import BaseModel, Field

from mewcode.permissions.sandbox import PathSandbox
from mewcode.tools.base import SKIP_DIRS, Tool, ToolResult
from mewcode.tools.path_utils import resolve_tool_path, validate_glob_pattern

log = logging.getLogger(__name__)


class Params(BaseModel):
    pattern: str = Field(description="Glob pattern to match (e.g. '**/*.py')")
    path: str = Field(default=".", description="Base directory to search from")


class Glob(Tool):
    name = "Glob"
    description = "Find files matching a glob pattern, returning relative paths."
    params_model = Params
    category = "read"
    is_concurrency_safe = True


    def __init__(self, path_sandbox: PathSandbox | None = None) -> None:
        self._path_sandbox = path_sandbox or PathSandbox(str(Path.cwd()))

    def for_project_root(
        self,
        project_root: Path,
        path_sandbox: PathSandbox,
    ) -> Glob:
        configured = copy.copy(self)
        configured._path_sandbox = path_sandbox
        return configured


    async def execute(self, params: Params) -> ToolResult:
        base, path_error = resolve_tool_path(params.path, self._path_sandbox)
        if base is None:
            return ToolResult(output=path_error, is_error=True)
        if not base.exists():
            return ToolResult(output=f"Error: path not found: {params.path}", is_error=True)

        pattern_error = validate_glob_pattern(params.pattern)
        if pattern_error:
            return ToolResult(output=pattern_error, is_error=True)

        try:
            matches = sorted(
                str(p.relative_to(base))
                for p in base.glob(params.pattern)
                if (
                    p.is_file()
                    and self._path_sandbox.check(str(p))[0]
                    and not any(part in SKIP_DIRS for part in p.parts)
                )
            )
        except ValueError as exc:
            return ToolResult(output=f"Error: invalid glob pattern: {exc}", is_error=True)
        except OSError as exc:
            log.error(
                "Glob search failed: base=%s pattern=%s reason=%s",
                base,
                params.pattern,
                exc,
                exc_info=True,
            )
            return ToolResult(output=f"Error: {exc}", is_error=True)

        if not matches:
            return ToolResult(output="No files matched the pattern.")
        return ToolResult(output="\n".join(matches))

