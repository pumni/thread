from __future__ import annotations

import asyncio
import ctypes
import os
import sys
import threading
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

SERVICE_NAME = "ThreadsOperationsWorker"
SERVICE_CONTROL_STOP = 0x00000001
SERVICE_CONTROL_INTERROGATE = 0x00000004
SERVICE_CONTROL_SHUTDOWN = 0x00000005
SERVICE_WIN32_OWN_PROCESS = 0x00000010
SERVICE_STOPPED = 0x00000001
SERVICE_START_PENDING = 0x00000002
SERVICE_STOP_PENDING = 0x00000003
SERVICE_RUNNING = 0x00000004
SERVICE_ACCEPT_STOP = 0x00000001
SERVICE_ACCEPT_SHUTDOWN = 0x00000004
ERROR_CALL_NOT_IMPLEMENTED = 120
ERROR_FAILED_SERVICE_CONTROLLER_CONNECT = 1063
ERROR_SERVICE_SPECIFIC_ERROR = 1066
_STOP_WAIT_HINT_MS = 30_000
_STOP_PROGRESS_INTERVAL_SECONDS = 10

_CALLBACK_FACTORY: Any = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
_SERVICE_MAIN_CALLBACK: Any = _CALLBACK_FACTORY(
    None, ctypes.c_uint32, ctypes.POINTER(ctypes.c_wchar_p)
)
_HANDLER_EX_CALLBACK: Any = _CALLBACK_FACTORY(
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_void_p,
    ctypes.c_void_p,
)


class LocalStopSignal:
    """Thread-safe stop edge with an asyncio wake-up for the WorkerAgent loop."""

    def __init__(self) -> None:
        self._requested = threading.Event()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._async_event: asyncio.Event | None = None

    def is_set(self) -> bool:
        return self._requested.is_set()

    def request(self) -> None:
        self._requested.set()
        with self._lock:
            loop = self._loop
            event = self._async_event
        if loop is not None and event is not None:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                return

    def bind(self, loop: asyncio.AbstractEventLoop, event: asyncio.Event) -> None:
        with self._lock:
            self._loop = loop
            self._async_event = event
        if self.is_set():
            event.set()


ServiceRunner = Callable[[asyncio.Event, LocalStopSignal, Callable[[], None]], Awaitable[None]]


class WindowsSCMApi(Protocol):
    def start_dispatcher(self, table: Any) -> bool: ...

    def register_handler(self, callback: Any) -> int | None: ...

    def set_status(self, handle: int, status: Any) -> bool: ...


class _ServiceStatus(ctypes.Structure):
    _fields_ = [
        ("dwServiceType", ctypes.c_uint32),
        ("dwCurrentState", ctypes.c_uint32),
        ("dwControlsAccepted", ctypes.c_uint32),
        ("dwWin32ExitCode", ctypes.c_uint32),
        ("dwServiceSpecificExitCode", ctypes.c_uint32),
        ("dwCheckPoint", ctypes.c_uint32),
        ("dwWaitHint", ctypes.c_uint32),
    ]


class _ServiceTableEntry(ctypes.Structure):
    _fields_ = [
        ("lpServiceName", ctypes.c_wchar_p),
        ("lpServiceProc", _SERVICE_MAIN_CALLBACK),
    ]


class _WindowsSCMApi:
    def __init__(self) -> None:
        load_library = getattr(ctypes, "WinDLL", None)
        if load_library is None:
            raise RuntimeError("Windows SCM APIs are unavailable")
        advapi32 = load_library("Advapi32.dll", use_last_error=True)
        self._start_dispatcher = advapi32.StartServiceCtrlDispatcherW
        self._start_dispatcher.argtypes = [ctypes.POINTER(_ServiceTableEntry)]
        self._start_dispatcher.restype = ctypes.c_int
        self._register_handler = advapi32.RegisterServiceCtrlHandlerExW
        self._register_handler.argtypes = [
            ctypes.c_wchar_p,
            _HANDLER_EX_CALLBACK,
            ctypes.c_void_p,
        ]
        self._register_handler.restype = ctypes.c_void_p
        self._set_status = advapi32.SetServiceStatus
        self._set_status.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ServiceStatus)]
        self._set_status.restype = ctypes.c_int

    def start_dispatcher(self, table: Any) -> bool:
        return bool(self._start_dispatcher(table))

    def register_handler(self, callback: Any) -> int | None:
        handle = self._register_handler(SERVICE_NAME, callback, None)
        return int(handle) if handle else None

    def set_status(self, handle: int, status: _ServiceStatus) -> bool:
        return bool(self._set_status(handle, ctypes.byref(status)))


class WindowsServiceHost:
    def __init__(self, runner: ServiceRunner, api: WindowsSCMApi) -> None:
        self._runner = runner
        self._api = api
        self._service_handle: int | None = None
        self._status_lock = threading.RLock()
        self._current_state = SERVICE_START_PENDING
        self._checkpoint = 0
        self._exit_code = 0
        self._stop_signal = LocalStopSignal()
        self._async_stop_event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._service_main_callback: Any = _SERVICE_MAIN_CALLBACK(self._service_main)
        self._handler_callback: Any = _HANDLER_EX_CALLBACK(self._handle_control)

    def run(self) -> int:
        table = (_ServiceTableEntry * 2)()
        table[0] = _ServiceTableEntry(SERVICE_NAME, self._service_main_callback)
        table[1] = _ServiceTableEntry(None, _SERVICE_MAIN_CALLBACK())
        if not self._api.start_dispatcher(table):
            last_error = _last_error()
            if last_error == ERROR_FAILED_SERVICE_CONTROLLER_CONNECT:
                print("THREADS_WORKER_SERVICE_REQUIRES_SCM", file=sys.stderr)
                return 2
            print("THREADS_WORKER_SCM_START_FAILED", file=sys.stderr)
            return 1
        return self._exit_code

    def _service_main(self, _argc: int, _argv: object) -> None:
        try:
            self._service_handle = self._api.register_handler(self._handler_callback)
            if self._service_handle is None:
                self._exit_code = 1
                return
            self._report_state(SERVICE_START_PENDING, checkpoint=1, wait_hint_ms=30_000)
            asyncio.run(self._serve())
        except Exception:
            self._exit_code = 1
        finally:
            if self._service_handle is not None:
                self._report_state(
                    SERVICE_STOPPED,
                    checkpoint=0,
                    wait_hint_ms=0,
                    exit_code=self._exit_code,
                )

    async def _serve(self) -> None:
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        self._loop = loop
        self._async_stop_event = stop_event
        self._stop_signal.bind(loop, stop_event)

        async def run_worker() -> None:
            await self._runner(stop_event, self._stop_signal, self._report_running)

        runner_task: asyncio.Task[None] = asyncio.create_task(run_worker())
        while not runner_task.done():
            done, _ = await asyncio.wait({runner_task}, timeout=_STOP_PROGRESS_INTERVAL_SECONDS)
            if not done and self._stop_signal.is_set():
                self._report_stop_progress()
        await runner_task

    def _handle_control(
        self,
        control: int,
        _event_type: int,
        _event_data: int | None,
        _context: int | None,
    ) -> int:
        if control in {SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN}:
            self._stop_signal.request()
            self._report_stop_progress()
            return 0
        if control == SERVICE_CONTROL_INTERROGATE:
            self._report_current_state()
            return 0
        return ERROR_CALL_NOT_IMPLEMENTED

    def _report_running(self) -> None:
        if self._stop_signal.is_set():
            self._report_stop_progress()
            return
        self._report_state(
            SERVICE_RUNNING,
            checkpoint=0,
            wait_hint_ms=0,
            controls_accepted=SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN,
        )

    def _report_stop_progress(self) -> None:
        with self._status_lock:
            if self._current_state == SERVICE_STOPPED:
                return
            self._checkpoint = (
                1 if self._current_state != SERVICE_STOP_PENDING else self._checkpoint + 1
            )
            checkpoint = self._checkpoint
            self._report_state(
                SERVICE_STOP_PENDING,
                checkpoint=checkpoint,
                wait_hint_ms=_STOP_WAIT_HINT_MS,
            )

    def _report_current_state(self) -> None:
        with self._status_lock:
            state = self._current_state
            checkpoint = (
                self._checkpoint if state in {SERVICE_START_PENDING, SERVICE_STOP_PENDING} else 0
            )
        self._report_state(
            state,
            checkpoint=checkpoint,
            wait_hint_ms=(30_000 if state in {SERVICE_START_PENDING, SERVICE_STOP_PENDING} else 0),
            controls_accepted=(
                SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN if state == SERVICE_RUNNING else 0
            ),
        )

    def _report_state(
        self,
        state: int,
        *,
        checkpoint: int,
        wait_hint_ms: int,
        controls_accepted: int = 0,
        exit_code: int = 0,
    ) -> None:
        handle = self._service_handle
        if handle is None:
            return
        status = _ServiceStatus(
            SERVICE_WIN32_OWN_PROCESS,
            state,
            controls_accepted,
            0 if exit_code == 0 else ERROR_SERVICE_SPECIFIC_ERROR,
            exit_code,
            checkpoint,
            wait_hint_ms,
        )
        with self._status_lock:
            if self._current_state == SERVICE_STOPPED:
                return
            if self._current_state == SERVICE_STOP_PENDING and state in {
                SERVICE_START_PENDING,
                SERVICE_RUNNING,
            }:
                state = SERVICE_STOP_PENDING
                checkpoint = max(self._checkpoint, 1)
                wait_hint_ms = _STOP_WAIT_HINT_MS
                controls_accepted = 0
                status.dwCurrentState = state
                status.dwControlsAccepted = controls_accepted
                status.dwCheckPoint = checkpoint
                status.dwWaitHint = wait_hint_ms
            elif state == SERVICE_RUNNING and self._stop_signal.is_set():
                state = SERVICE_STOP_PENDING
                checkpoint = max(self._checkpoint, 1)
                wait_hint_ms = _STOP_WAIT_HINT_MS
                controls_accepted = 0
                status.dwCurrentState = state
                status.dwControlsAccepted = controls_accepted
                status.dwCheckPoint = checkpoint
                status.dwWaitHint = wait_hint_ms
            self._current_state = state
            self._checkpoint = checkpoint
            succeeded = self._api.set_status(handle, status)
        if not succeeded:
            self._exit_code = 1


def run_windows_service(runner: ServiceRunner) -> int:
    if os.name != "nt":
        print("THREADS_WORKER_WINDOWS_SERVICE_UNSUPPORTED", file=sys.stderr)
        return 2
    try:
        return WindowsServiceHost(runner, _WindowsSCMApi()).run()
    except Exception:
        print("THREADS_WORKER_SCM_START_FAILED", file=sys.stderr)
        return 1


def _last_error() -> int:
    get_last_error = getattr(ctypes, "get_last_error", None)
    return int(get_last_error()) if get_last_error is not None else 0
