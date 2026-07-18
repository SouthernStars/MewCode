from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from mewcode.permissions import PathSandbox
from mewcode.tools import create_default_registry
from mewcode.tools.bash import Bash, MAX_OUTPUT_CHARS, Params as BashParams
from mewcode.tools.glob import Glob, Params as GlobParams
from mewcode.tools.read_file import Params as ReadParams
from mewcode.tools.write_file import Params as WriteParams


def _shell_command(*args: str) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(list(args))
    return shlex.join(args)


def _process_exists(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _force_kill(pid: int) -> None:
    if not _process_exists(pid):
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


@pytest.mark.asyncio
async def test_default_file_tools_resolve_relative_paths_from_project_root(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "input.txt").write_text("project data", encoding="utf-8")
    registry = create_default_registry(project_root=project)

    read_tool = registry.get("ReadFile")
    write_tool = registry.get("WriteFile")
    assert read_tool is not None
    assert write_tool is not None

    read_result = await read_tool.execute(ReadParams(file_path="input.txt"))
    write_result = await write_tool.execute(
        WriteParams(file_path="nested/output.txt", content="saved")
    )

    assert not read_result.is_error
    assert "project data" in read_result.output
    assert not write_result.is_error
    assert (project / "nested" / "output.txt").read_text(encoding="utf-8") == "saved"


@pytest.mark.asyncio
async def test_file_tool_rechecks_sandbox_when_called_directly(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    registry = create_default_registry(project_root=project)
    write_tool = registry.get("WriteFile")
    assert write_tool is not None

    result = await write_tool.execute(
        WriteParams(file_path=str(outside), content="must not be written")
    )

    assert result.is_error
    assert "sandbox" in result.output.lower()
    assert not outside.exists()


@pytest.mark.asyncio
async def test_registry_project_views_do_not_rebind_parent_tools(
    tmp_path: Path,
) -> None:
    parent_root = tmp_path / "parent"
    child_root = tmp_path / "child"
    parent_root.mkdir()
    child_root.mkdir()
    parent_registry = create_default_registry(project_root=parent_root)
    child_registry = parent_registry.for_project_root(child_root)
    parent_write = parent_registry.get("WriteFile")
    child_write = child_registry.get("WriteFile")
    assert parent_write is not None
    assert child_write is not None
    assert parent_write is not child_write

    parent_result = await parent_write.execute(
        WriteParams(file_path="result.txt", content="parent")
    )
    child_result = await child_write.execute(
        WriteParams(file_path="result.txt", content="child")
    )

    assert not parent_result.is_error
    assert not child_result.is_error
    assert (parent_root / "result.txt").read_text(encoding="utf-8") == "parent"
    assert (child_root / "result.txt").read_text(encoding="utf-8") == "child"


@pytest.mark.asyncio
async def test_glob_rejects_parent_traversal_pattern(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    sandbox = PathSandbox(str(project))
    tool = Glob(path_sandbox=sandbox)

    result = await tool.execute(GlobParams(path=".", pattern="../*.txt"))

    assert result.is_error
    assert "pattern" in result.output.lower()


@pytest.mark.asyncio
async def test_bash_runs_in_project_root_and_caps_output(tmp_path: Path) -> None:
    script = tmp_path / "output.py"
    script.write_text(
        "import os\n"
        "print(os.getcwd())\n"
        f"print('x' * {MAX_OUTPUT_CHARS * 2})\n",
        encoding="utf-8",
    )
    tool = Bash(work_dir=tmp_path)

    result = await tool.execute(
        BashParams(command=_shell_command(sys.executable, str(script)))
    )

    assert not result.is_error
    assert str(tmp_path.resolve()) in result.output
    assert "truncated" in result.output.lower()
    assert len(result.output) <= MAX_OUTPUT_CHARS + 100


@pytest.mark.asyncio
async def test_bash_timeout_kills_descendant_process(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    script = tmp_path / "spawn_child.py"
    script.write_text(
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    tool = Bash(work_dir=tmp_path)
    child_pid: int | None = None

    try:
        result = await tool.execute(
            BashParams(
                command=_shell_command(
                    sys.executable,
                    str(script),
                    str(pid_file),
                ),
                # Leave enough startup budget for the runner, shell, and two
                # Python processes on loaded Windows CI hosts.
                timeout=5,
            )
        )
        assert result.is_error
        assert "timed out" in result.output.lower()
        assert pid_file.exists()
        child_pid = int(pid_file.read_text(encoding="utf-8"))

        for _ in range(20):
            if not _process_exists(child_pid):
                break
            await asyncio.sleep(0.05)
        assert not _process_exists(child_pid)
    finally:
        if child_pid is not None:
            _force_kill(child_pid)


@pytest.mark.asyncio
async def test_bash_cancellation_kills_descendant_process(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "cancelled-child.pid"
    script = tmp_path / "spawn_cancelled_child.py"
    script.write_text(
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    tool = Bash(work_dir=tmp_path)
    task = asyncio.create_task(
        tool.execute(
            BashParams(
                command=_shell_command(
                    sys.executable,
                    str(script),
                    str(pid_file),
                ),
                timeout=60,
            )
        )
    )
    child_pid: int | None = None

    try:
        for _ in range(40):
            if pid_file.exists():
                break
            await asyncio.sleep(0.05)
        assert pid_file.exists()
        child_pid = int(pid_file.read_text(encoding="utf-8"))

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        for _ in range(20):
            if not _process_exists(child_pid):
                break
            await asyncio.sleep(0.05)
        assert not _process_exists(child_pid)
    finally:
        if not task.done():
            task.cancel()
        if child_pid is not None:
            _force_kill(child_pid)
