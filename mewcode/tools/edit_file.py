from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from mewcode.permissions.sandbox import PathSandbox
from mewcode.tools.base import Tool, ToolResult
from mewcode.tools.path_utils import resolve_tool_path

if TYPE_CHECKING:
    from mewcode.cache import FileCache
    from mewcode.tools.file_state_cache import FileStateCache

log = logging.getLogger(__name__)


class Params(BaseModel):
    file_path: str = Field(description="Path to the file to edit")
    old_string: str = Field(description="The exact string to find and replace (must be unique in file)")
    new_string: str = Field(description="The replacement string")


class EditFile(Tool):
    name = "EditFile"
    description = (
        "Replace an exact string in a file. The old_string must appear exactly once in the file.\n"
        "You MUST read the file with ReadFile before editing. This tool will fail otherwise."
    )
    params_model = Params
    category = "write"


    def __init__(
        self,
        file_cache: FileCache | None = None,
        file_history: Any = None,
        file_state_cache: FileStateCache | None = None,
        path_sandbox: PathSandbox | None = None,
    ) -> None:
        self._cache = file_cache
        self.file_history = file_history
        self._state_cache = file_state_cache
        self._path_sandbox = path_sandbox or PathSandbox(str(Path.cwd()))

    def for_project_root(
        self,
        project_root: Path,
        path_sandbox: PathSandbox,
    ) -> EditFile:
        configured = copy.copy(self)
        configured._path_sandbox = path_sandbox
        return configured


    async def execute(self, params: Params) -> ToolResult:
        path, path_error = resolve_tool_path(params.file_path, self._path_sandbox)
        if path is None:
            return ToolResult(output=path_error, is_error=True)

        if self.file_history is not None:
            self.file_history.track_edit(str(path))
        if not path.exists():
            return ToolResult(output=f"Error: file not found: {params.file_path}", is_error=True)

        if self._state_cache:
            resolved = str(path.resolve())
            ok, err_msg = self._state_cache.check(resolved)
            if not ok:
                return ToolResult(output=err_msg, is_error=True)

        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            log.error(
                "EditFile read failed: path=%s reason=%s",
                path,
                exc,
                exc_info=True,
            )
            return ToolResult(output=f"Error reading file: {exc}", is_error=True)

        count = content.count(params.old_string)
        if count == 0:
            return ToolResult(output="Error: old_string not found in file", is_error=True)
        if count > 1:
            return ToolResult(
                output=f"Error: old_string found {count} times, must be unique",
                is_error=True,
            )

        new_content = content.replace(params.old_string, params.new_string, 1)
        try:
            path.write_text(new_content, encoding="utf-8")
            if self._cache:
                self._cache.invalidate(str(path.resolve()))
            if self._state_cache:
                self._state_cache.update(str(path.resolve()))
        except OSError as exc:
            log.error(
                "EditFile write failed: path=%s reason=%s",
                path,
                exc,
                exc_info=True,
            )
            return ToolResult(output=f"Error writing file: {exc}", is_error=True)

        return ToolResult(output=f"Successfully edited {params.file_path}")
