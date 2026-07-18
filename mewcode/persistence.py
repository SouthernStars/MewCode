"""Shared contracts for durable file-backed state."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

Migration = Callable[[Any], dict[str, Any]]
log = logging.getLogger(__name__)


class PersistenceError(RuntimeError):
    """A durable state file could not be read, validated, or written."""


class UnsupportedSchemaVersionError(PersistenceError):
    """A state file was produced by a newer, unsupported schema."""


def _lock_path(path: Path) -> Path:
    return Path(f"{path}.lock")


@contextmanager
def file_lock(
    path: str | Path,
    *,
    format_name: str,
) -> Iterator[None]:
    """Hold an exclusive cross-process lock associated with ``path``."""

    target = Path(path)
    lock_path = _lock_path(target)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = open(lock_path, "a+b")  # noqa: SIM115
    except OSError as exc:
        raise PersistenceError(
            f"Failed to open lock for {format_name} at {target}: {exc}"
        ) from exc

    try:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            except OSError as exc:
                raise PersistenceError(
                    f"Failed to acquire lock for {format_name} at {target}: {exc}"
                ) from exc
            try:
                yield
            finally:
                lock_file.seek(0)
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError as exc:
                    raise PersistenceError(
                        f"Failed to release lock for {format_name} at "
                        f"{target}: {exc}"
                    ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            except OSError as exc:
                raise PersistenceError(
                    f"Failed to acquire lock for {format_name} at {target}: {exc}"
                ) from exc
            try:
                yield
            finally:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                except OSError as exc:
                    raise PersistenceError(
                        f"Failed to release lock for {format_name} at "
                        f"{target}: {exc}"
                    ) from exc
    finally:
        lock_file.close()


def _fsync_parent_directory(path: Path) -> None:
    if os.name == "nt":
        return
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def atomic_write_text(
    path: str | Path,
    content: str,
    *,
    format_name: str,
) -> None:
    """Flush a same-directory temporary file and atomically replace ``path``."""

    target = Path(path)
    temp_path: Path | None = None
    file_descriptor = -1
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, raw_temp_path = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        temp_path = Path(raw_temp_path)
        with os.fdopen(
            file_descriptor,
            "w",
            encoding="utf-8",
            newline="\n",
        ) as temp_file:
            file_descriptor = -1
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, target)
        temp_path = None
        _fsync_parent_directory(target)
    except OSError as exc:
        raise PersistenceError(
            f"Failed to atomically write {format_name} at {target}: {exc}"
        ) from exc
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError as cleanup_error:
                log.error(
                    "Failed to remove temporary %s file after write failure: "
                    "path=%s reason=%s",
                    format_name,
                    temp_path,
                    cleanup_error,
                    exc_info=True,
                )


def atomic_write_json(
    path: str | Path,
    data: Any,
    *,
    format_name: str,
) -> None:
    try:
        content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    except (TypeError, ValueError) as exc:
        raise PersistenceError(
            f"Failed to serialize {format_name} for {Path(path)}: {exc}"
        ) from exc
    atomic_write_text(path, content, format_name=format_name)


def append_jsonl_record(
    path: str | Path,
    data: Any,
    *,
    format_name: str,
) -> None:
    """Append one JSONL record with a durable flush under the file lock."""

    target = Path(path)
    try:
        line = json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n"
    except (TypeError, ValueError) as exc:
        raise PersistenceError(
            f"Failed to serialize {format_name} for {target}: {exc}"
        ) from exc

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(target, format_name=format_name):
            with open(target, "a", encoding="utf-8", newline="") as stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())
    except OSError as exc:
        raise PersistenceError(
            f"Failed to append {format_name} at {target}: {exc}"
        ) from exc


def read_jsonl_records(
    path: str | Path,
    *,
    format_name: str,
) -> list[Any]:
    """Read JSONL fail-fast, recovering only an incomplete final line.

    A malformed interior line (or a malformed final line terminated by a
    newline) is treated as corruption.  A final unterminated malformed line
    is assumed to be a crash-interrupted append and is truncated.
    """

    target = Path(path)
    if not target.exists():
        return []

    try:
        with file_lock(target, format_name=format_name):
            raw = target.read_bytes()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PersistenceError(
                    f"Failed to decode {format_name} at {target}: {exc}"
                ) from exc

            records: list[Any] = []
            offset = 0
            chunks = text.splitlines(keepends=True)
            for index, chunk in enumerate(chunks):
                has_newline = chunk.endswith(("\n", "\r"))
                line = chunk.strip()
                next_offset = offset + len(chunk.encode("utf-8"))
                offset = next_offset
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    is_final = index == len(chunks) - 1
                    if is_final and not has_newline:
                        try:
                            with open(target, "r+b") as stream:
                                stream.truncate(next_offset - len(chunk.encode("utf-8")))
                                stream.flush()
                                os.fsync(stream.fileno())
                        except OSError as truncate_error:
                            raise PersistenceError(
                                f"Failed to recover incomplete {format_name} at "
                                f"{target}: {truncate_error}"
                            ) from truncate_error
                        break
                    raise PersistenceError(
                        f"Corrupted {format_name} line {index + 1} at {target}: {exc}"
                    ) from exc
            return records
    except PersistenceError:
        raise
    except OSError as exc:
        raise PersistenceError(
            f"Failed to read {format_name} at {target}: {exc}"
        ) from exc


def _read_json(path: Path, *, format_name: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PersistenceError(
            f"Failed to read {format_name} at {path}: {exc}"
        ) from exc


def read_versioned_json(
    path: str | Path,
    *,
    current_version: int,
    migrations: Mapping[int, Migration],
    format_name: str,
) -> dict[str, Any]:
    """Read an immutable snapshot or one synchronized by the caller."""

    target = Path(path)
    data = _read_json(target, format_name=format_name)

    if isinstance(data, dict) and "schema_version" in data:
        version = data["schema_version"]
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise PersistenceError(
                f"Invalid schema_version for {format_name} at {target}: "
                f"{version!r}"
            )
    else:
        version = 0

    if version > current_version:
        raise UnsupportedSchemaVersionError(
            f"Unsupported {format_name} schema version {version} at {target}; "
            f"this runtime supports up to version {current_version}"
        )

    while version < current_version:
        migration = migrations.get(version)
        if migration is None:
            raise PersistenceError(
                f"Missing {format_name} migration from schema version "
                f"{version} at {target}"
            )
        try:
            data = migration(data)
        except (KeyError, TypeError, ValueError) as exc:
            raise PersistenceError(
                f"Failed to migrate {format_name} from schema version "
                f"{version} at {target}: {exc}"
            ) from exc
        next_version = data.get("schema_version")
        if next_version != version + 1:
            raise PersistenceError(
                f"Invalid {format_name} migration from schema version "
                f"{version} at {target}: produced version {next_version!r}"
            )
        version = next_version

    if not isinstance(data, dict):
        raise PersistenceError(
            f"Invalid {format_name} root at {target}: expected JSON object"
        )
    return data


def load_versioned_json(
    path: str | Path,
    *,
    current_version: int,
    migrations: Mapping[int, Migration],
    format_name: str,
) -> dict[str, Any]:
    """Read and migrate a JSON snapshot under its cross-process lock."""

    target = Path(path)
    with file_lock(target, format_name=format_name):
        return read_versioned_json(
            target,
            current_version=current_version,
            migrations=migrations,
            format_name=format_name,
        )
