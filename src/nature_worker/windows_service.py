# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Windows Service Control Manager: control a service, or run this process as one.

Native APIs through ctypes, so no extra dependency is installed. Creating and
configuring the service is the installer's job (``sc.exe``); this module only
queries, starts and stops it, and hosts the service process itself.
"""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable

STOPPED, START_PENDING, STOP_PENDING, RUNNING = 1, 2, 3, 4
STATE_NAMES = {1: "stopped", 2: "start_pending", 3: "stop_pending", 4: "running",
               5: "continue_pending", 6: "pause_pending", 7: "paused"}
ERROR_SERVICE_DOES_NOT_EXIST = 1060
ERROR_SERVICE_ALREADY_RUNNING = 1056
ERROR_SERVICE_NOT_ACTIVE = 1062
ERROR_FAILED_SERVICE_CONTROLLER_CONNECT = 1063
ERROR_SERVICE_SPECIFIC_ERROR = 1066


class ServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class ServiceStatus:
    state: int
    win32_exit: int = 0
    service_exit: int = 0

    @property
    def name(self) -> str:
        return STATE_NAMES.get(self.state, f"state-{self.state}")


def _api():
    import ctypes
    from ctypes import wintypes

    class SERVICE_STATUS(ctypes.Structure):
        _fields_ = [("dwServiceType", wintypes.DWORD), ("dwCurrentState", wintypes.DWORD),
                    ("dwControlsAccepted", wintypes.DWORD), ("dwWin32ExitCode", wintypes.DWORD),
                    ("dwServiceSpecificExitCode", wintypes.DWORD), ("dwCheckPoint", wintypes.DWORD),
                    ("dwWaitHint", wintypes.DWORD)]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    advapi32.OpenSCManagerW.restype = wintypes.HANDLE
    advapi32.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
    advapi32.OpenServiceW.restype = wintypes.HANDLE
    advapi32.CloseServiceHandle.argtypes = [wintypes.HANDLE]
    advapi32.QueryServiceStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(SERVICE_STATUS)]
    advapi32.StartServiceW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p]
    advapi32.ControlService.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(SERVICE_STATUS)]
    return ctypes, wintypes, advapi32, SERVICE_STATUS


class _Handle:
    def __init__(self, name: str, access: int):
        if sys.platform != "win32":
            raise ServiceError("Windows services require Windows")
        self.ctypes, _, self.api, self.STATUS = _api()
        self.scm = self.api.OpenSCManagerW(None, None, 0x0001)  # SC_MANAGER_CONNECT
        if not self.scm:
            raise ServiceError(f"Cannot open the Service Control Manager ({self.ctypes.get_last_error()})")
        self.svc = self.api.OpenServiceW(self.scm, name, access)
        self.error = 0 if self.svc else self.ctypes.get_last_error()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        if self.svc:
            self.api.CloseServiceHandle(self.svc)
        self.api.CloseServiceHandle(self.scm)


def query(name: str) -> ServiceStatus | None:
    """The service's status, or None when it is not installed."""
    with _Handle(name, 0x0004) as h:  # SERVICE_QUERY_STATUS
        if not h.svc:
            if h.error == ERROR_SERVICE_DOES_NOT_EXIST:
                return None
            raise ServiceError(f"Cannot open service {name} ({h.error})")
        status = h.STATUS()
        if not h.api.QueryServiceStatus(h.svc, h.ctypes.byref(status)):
            raise ServiceError(f"Cannot query service {name} ({h.ctypes.get_last_error()})")
        return ServiceStatus(status.dwCurrentState, status.dwWin32ExitCode, status.dwServiceSpecificExitCode)


def wait_for(name: str, state: int, timeout: float) -> ServiceStatus | None:
    deadline = time.monotonic() + timeout
    while True:
        status = query(name)
        if status is None or status.state == state or time.monotonic() > deadline:
            return status
        time.sleep(0.5)


def start(name: str, timeout: float = 60) -> ServiceStatus:
    with _Handle(name, 0x0004 | 0x0010) as h:  # QUERY_STATUS | SERVICE_START
        if not h.svc:
            raise ServiceError(f"Service {name} is not installed" if h.error == ERROR_SERVICE_DOES_NOT_EXIST
                               else f"Cannot open service {name} ({h.error})")
        if not h.api.StartServiceW(h.svc, 0, None):
            error = h.ctypes.get_last_error()
            if error != ERROR_SERVICE_ALREADY_RUNNING:
                raise ServiceError(f"Service {name} could not start (error {error})")
    status = wait_for(name, RUNNING, timeout)
    if status is None or status.state != RUNNING:
        detail = f"{status.name}, exit {status.win32_exit}/{status.service_exit}" if status else "removed"
        raise ServiceError(f"Service {name} did not reach running ({detail})")
    return status


def stop(name: str, timeout: float = 60) -> None:
    with _Handle(name, 0x0004 | 0x0020) as h:  # QUERY_STATUS | SERVICE_STOP
        if not h.svc:
            if h.error == ERROR_SERVICE_DOES_NOT_EXIST:
                return
            raise ServiceError(f"Cannot open service {name} ({h.error})")
        status = h.STATUS()
        if not h.api.ControlService(h.svc, 1, h.ctypes.byref(status)):  # SERVICE_CONTROL_STOP
            error = h.ctypes.get_last_error()
            if error != ERROR_SERVICE_NOT_ACTIVE:
                raise ServiceError(f"Service {name} could not be stopped (error {error})")
    status = wait_for(name, STOPPED, timeout)
    if status is not None and status.state != STOPPED:
        raise ServiceError(f"Service {name} did not stop within {int(timeout)} s ({status.name})")


# -- hosting: this process is the service ---------------------------------------------

class ServiceHost:
    """Runs ``body(stop_requested, ready)`` as the service ``name``.

    ``body`` blocks while the service works and returns an exit code (0 = clean).
    ``stop_requested`` is set when the SCM asks the service to stop; ``body``
    calls ``ready()`` once it really answers, so RUNNING is never reported early.
    """

    def __init__(self, name: str, body: Callable[[threading.Event, Callable[[], None]], int]):
        self.name = name
        self.body = body
        self.stop_requested = threading.Event()
        self.exit_code = 0

    def dispatch(self) -> int:
        """Hand this process to the SCM; returns after the service stopped.
        Raises ServiceError when the process was not started by the SCM."""
        if sys.platform != "win32":
            raise ServiceError("Windows services require Windows")
        ctypes, wintypes, advapi32, STATUS = _api()
        main_type = ctypes.WINFUNCTYPE(None, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR))
        handler_type = ctypes.WINFUNCTYPE(wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                          ctypes.c_void_p, ctypes.c_void_p)

        class ENTRY(ctypes.Structure):
            _fields_ = [("lpServiceName", wintypes.LPWSTR), ("lpServiceProc", main_type)]

        advapi32.RegisterServiceCtrlHandlerExW.argtypes = [wintypes.LPCWSTR, handler_type, ctypes.c_void_p]
        advapi32.RegisterServiceCtrlHandlerExW.restype = wintypes.HANDLE
        advapi32.SetServiceStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(STATUS)]
        state = {"handle": None, "checkpoint": 0}
        lock = threading.Lock()

        def report(current: int, wait_hint: int = 0, win32_exit: int = 0, service_exit: int = 0) -> None:
            with lock:
                status = STATUS()
                status.dwServiceType = 0x10  # SERVICE_WIN32_OWN_PROCESS
                status.dwCurrentState = current
                status.dwControlsAccepted = 0 if current in (START_PENDING, STOPPED) else 0x1 | 0x4
                status.dwWin32ExitCode = win32_exit
                status.dwServiceSpecificExitCode = service_exit
                pending = current in (START_PENDING, STOP_PENDING)
                state["checkpoint"] = state["checkpoint"] + 1 if pending else 0
                status.dwCheckPoint = state["checkpoint"]
                status.dwWaitHint = wait_hint
                advapi32.SetServiceStatus(state["handle"], ctypes.byref(status))

        def handler(control, _event, _data, _context):
            if control in (1, 5, 0x0F):  # STOP, SHUTDOWN, PRESHUTDOWN
                report(STOP_PENDING, 30000)
                self.stop_requested.set()
            return 0  # NO_ERROR (also answers INTERROGATE)

        def service_main(_argc, _argv):
            state["handle"] = advapi32.RegisterServiceCtrlHandlerExW(self.name, handler_cb, None)
            if not state["handle"]:
                return
            report(START_PENDING, 60000)
            try:
                code = self.body(self.stop_requested, lambda: report(RUNNING))
            except BaseException:  # never let an exception escape into the SCM thread
                import traceback
                traceback.print_exc()
                code = 1
            self.exit_code = code
            if code:
                report(STOPPED, win32_exit=ERROR_SERVICE_SPECIFIC_ERROR, service_exit=code)
            else:
                report(STOPPED)

        handler_cb = handler_type(handler)
        main_cb = main_type(service_main)
        table = (ENTRY * 2)()
        table[0].lpServiceName = self.name
        table[0].lpServiceProc = main_cb
        advapi32.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(ENTRY)]
        if not advapi32.StartServiceCtrlDispatcherW(table):
            error = ctypes.get_last_error()
            if error == ERROR_FAILED_SERVICE_CONTROLLER_CONNECT:
                raise ServiceError("Not started by the Service Control Manager. Start the service with "
                                   f"`sc.exe start {self.name}` instead.")
            raise ServiceError(f"StartServiceCtrlDispatcher failed ({error})")
        return self.exit_code
