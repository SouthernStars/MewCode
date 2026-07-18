from __future__ import annotations

import ctypes
import logging
import os
from ctypes import wintypes
from typing import Any

log = logging.getLogger(__name__)

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class WindowsJob:
    def __init__(self, handle: int) -> None:
        self._handle = handle

    @classmethod
    def attach(cls, process: Any) -> WindowsJob | None:
        if os.name != "nt":
            return None

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            log.error(
                "Failed to create Windows Job Object: pid=%s winerror=%s",
                process.pid,
                ctypes.get_last_error(),
            )
            return None

        job = cls(handle)
        info = _ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if not kernel32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            error = ctypes.get_last_error()
            job.close()
            log.error(
                "Failed to configure Windows Job Object: pid=%s winerror=%s",
                process.pid,
                error,
            )
            return None

        transport = getattr(process, "_transport", None)
        subprocess_handle = (
            transport.get_extra_info("subprocess")
            if transport is not None
            else None
        )
        process_handle = getattr(subprocess_handle, "_handle", None)
        if process_handle is None or not kernel32.AssignProcessToJobObject(
            handle,
            process_handle,
        ):
            error = ctypes.get_last_error()
            job.close()
            log.error(
                "Failed to assign command to Windows Job Object: "
                "pid=%s winerror=%s",
                process.pid,
                error,
            )
            return None

        return job

    def close(self) -> None:
        if not self._handle:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        if not kernel32.CloseHandle(self._handle):
            error = ctypes.get_last_error()
            self._handle = 0
            raise OSError(error, "Failed to close Windows Job Object")
        self._handle = 0

    def __enter__(self) -> WindowsJob:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
