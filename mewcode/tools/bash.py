from __future__ import annotations

import asyncio
import copy
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from mewcode.tools.base import MAX_OUTPUT_CHARS, Tool, ToolResult
from mewcode.tools.windows_job import WindowsJob

MAX_TIMEOUT = 600
_READ_CHUNK_BYTES = 8192
_TRUNCATION_MARKER = "\n...[output truncated]"

log = logging.getLogger(__name__)


class Params(BaseModel):
    command: str = Field(description="Shell command to execute")
    timeout: int = Field(
        default=120,
        gt=0,
        le=MAX_TIMEOUT,
        description="Timeout in seconds (max 600)",
    )


async def _read_stream_limited(
    stream: asyncio.StreamReader | None,
) -> tuple[bytes, bool]:
    if stream is None:
        return b"", False
    chunks: list[bytes] = []
    stored = 0
    truncated = False
    while True:
        chunk = await stream.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        remaining = MAX_OUTPUT_CHARS - stored
        if remaining > 0:
            kept = chunk[:remaining]
            chunks.append(kept)
            stored += len(kept)
        if len(chunk) > max(remaining, 0):
            truncated = True
    return b"".join(chunks), truncated


async def _terminate_process_tree(
    proc: asyncio.subprocess.Process,
    windows_job: WindowsJob | None,
) -> None:
    if proc.returncode is not None:
        if windows_job is not None:
            windows_job.close()
        return

    try:
        if windows_job is not None:
            windows_job.close()
        elif os.name == "nt":
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
            await asyncio.sleep(0.2)
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (
        OSError,
        ProcessLookupError,
        RuntimeError,
        asyncio.TimeoutError,
    ) as exc:
        log.error(
            "Failed to terminate command process tree: pid=%s reason=%s",
            proc.pid,
            exc,
            exc_info=True,
        )
        if proc.returncode is None:
            proc.kill()

    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"Command process tree did not exit after termination: pid={proc.pid}"
        ) from exc


async def _finish_readers(
    proc: asyncio.subprocess.Process,
    stdout_task: asyncio.Task[tuple[bytes, bool]],
    stderr_task: asyncio.Task[tuple[bytes, bool]],
) -> tuple[tuple[bytes, bool], tuple[bytes, bool]]:
    try:
        return await asyncio.wait_for(
            asyncio.gather(stdout_task, stderr_task),
            timeout=5,
        )
    except asyncio.TimeoutError as exc:
        stdout_task.cancel()
        stderr_task.cancel()
        await asyncio.gather(
            stdout_task,
            stderr_task,
            return_exceptions=True,
        )
        raise RuntimeError(
            f"Command pipes did not close after process termination: pid={proc.pid}"
        ) from exc


async def _cancel_readers(
    stdout_task: asyncio.Task[tuple[bytes, bool]],
    stderr_task: asyncio.Task[tuple[bytes, bool]],
) -> None:
    stdout_task.cancel()
    stderr_task.cancel()
    await asyncio.gather(
        stdout_task,
        stderr_task,
        return_exceptions=True,
    )


def _render_output(
    stdout: bytes,
    stderr: bytes,
    *,
    truncated: bool,
) -> str:
    parts: list[str] = []
    if stdout:
        parts.append(f"STDOUT:\n{stdout.decode(errors='replace')}")
    if stderr:
        parts.append(f"STDERR:\n{stderr.decode(errors='replace')}")
    if not parts:
        parts.append("(no output)")

    output = "\n".join(parts)
    if truncated or len(output) > MAX_OUTPUT_CHARS:
        keep = MAX_OUTPUT_CHARS - len(_TRUNCATION_MARKER)
        output = output[:keep] + _TRUNCATION_MARKER
    return output


class Bash(Tool):
    name = "Bash"
    description = "Execute a shell command and return stdout and stderr."
    params_model = Params
    category = "command"


    def __init__(self, work_dir: str | Path | None = None) -> None:
        self._work_dir = Path(work_dir or Path.cwd()).resolve()

    def for_project_root(
        self,
        project_root: Path,
        path_sandbox: object,
    ) -> Bash:
        configured = copy.copy(self)
        configured._work_dir = project_root
        return configured


    async def execute(self, params: Params) -> ToolResult:
        creation_options: dict[str, object] = {}
        if os.name == "nt":
            creation_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            creation_options["start_new_session"] = True

        try:
            runner_path = Path(__file__).with_name("command_runner.py")
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(runner_path),
                params.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._work_dir),
                **creation_options,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.error(
                "Command process creation failed: cwd=%s reason=%s",
                self._work_dir,
                exc,
                exc_info=True,
            )
            return ToolResult(
                output=f"Error executing command: {exc}",
                is_error=True,
            )

        windows_job = WindowsJob.attach(proc)
        try:
            if proc.stdin is None:
                raise RuntimeError(
                    f"Command runner stdin was not created: pid={proc.pid}"
                )
            proc.stdin.write(b"1")
            await proc.stdin.drain()
            proc.stdin.close()
        except BaseException:
            await _terminate_process_tree(proc, windows_job)
            raise

        stdout_task = asyncio.create_task(_read_stream_limited(proc.stdout))
        stderr_task = asyncio.create_task(_read_stream_limited(proc.stderr))
        try:
            await asyncio.wait_for(proc.wait(), timeout=params.timeout)
        except asyncio.TimeoutError:
            await _terminate_process_tree(proc, windows_job)
            await _cancel_readers(stdout_task, stderr_task)
            return ToolResult(
                output=f"Error: command timed out after {params.timeout}s",
                is_error=True,
            )
        except asyncio.CancelledError:
            await _terminate_process_tree(proc, windows_job)
            await _cancel_readers(stdout_task, stderr_task)
            raise

        (stdout, stdout_truncated), (stderr, stderr_truncated) = (
            await _finish_readers(proc, stdout_task, stderr_task)
        )
        output = _render_output(
            stdout,
            stderr,
            truncated=stdout_truncated or stderr_truncated,
        )
        if windows_job is not None:
            windows_job.close()
        return ToolResult(output=output, is_error=proc.returncode != 0)

