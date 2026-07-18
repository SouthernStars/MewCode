from __future__ import annotations

import subprocess
import sys


def main() -> int:
    if len(sys.argv) != 2:
        raise RuntimeError(
            f"command_runner expected one command argument, got {len(sys.argv) - 1}"
        )

    # The parent releases this one-byte gate only after attaching this process
    # to its Windows Job Object (or establishing the POSIX process group).
    gate = sys.stdin.buffer.read(1)
    if gate != b"1":
        raise RuntimeError("command_runner gate closed before command start")

    process = subprocess.Popen(sys.argv[1], shell=True)
    return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
