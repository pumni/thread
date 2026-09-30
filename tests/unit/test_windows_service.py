import asyncio
import io
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from threads_platform.infrastructure.worker_agent.enrollment_bootstrap import (
    EnrollmentBootstrapError,
    EnrollmentBootstrapFile,
)
from threads_platform.infrastructure.worker_agent.local_state import LocalDataRoot
from threads_platform.workers import __main__ as worker_main
from threads_platform.workers import windows_service


def test_enrollment_bootstrap_is_bounded_and_removed_after_success(tmp_path: Path) -> None:
    root = LocalDataRoot(tmp_path / "service-data")
    root.prepare()
    bootstrap = EnrollmentBootstrapFile(root)
    bootstrap.path.write_bytes(b"SYNTHETIC_ENROLLMENT_CODE_V1\n")

    assert bootstrap.read_code() == "SYNTHETIC_ENROLLMENT_CODE_V1"
    bootstrap.remove_after_success()
    assert not bootstrap.path.exists()


@pytest.mark.parametrize(
    "contents",
    [b"", b"not a safe token!", b"leading space", b"SYNTHETIC\r\nCODE"],
)
def test_enrollment_bootstrap_rejects_invalid_values(tmp_path: Path, contents: bytes) -> None:
    root = LocalDataRoot(tmp_path / "invalid-service-data")
    root.prepare()
    bootstrap = EnrollmentBootstrapFile(root)
    bootstrap.path.write_bytes(contents)

    with pytest.raises(EnrollmentBootstrapError):
        bootstrap.read_code()


def test_service_data_root_is_fixed_to_program_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program_data = tmp_path / "ProgramData"
    program_data.mkdir()
    expected = program_data / "ThreadsOperations"
    monkeypatch.setenv("ProgramData", str(program_data))
    monkeypatch.setenv("THREADS_WORKER_DATA_ROOT", str(expected))
    assert worker_main.production_service_data_root().path == expected.resolve()

    monkeypatch.setenv("THREADS_WORKER_DATA_ROOT", "C:/Users/LocalService/AppData")
    with pytest.raises(RuntimeError, match="service data directory"):
        worker_main.production_service_data_root()


def test_service_check_root_must_be_isolated_under_program_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program_data = tmp_path / "ProgramData"
    program_data.mkdir()
    monkeypatch.setenv("ProgramData", str(program_data))
    monkeypatch.setenv(
        "THREADS_WORKER_SERVICE_CHECK_ROOT",
        str(program_data / "ThreadsOperations-SCMCheck-test"),
    )
    assert worker_main.service_check_data_root().path.is_relative_to(program_data.resolve())

    monkeypatch.setenv("THREADS_WORKER_SERVICE_CHECK_ROOT", str(tmp_path / "outside-data"))
    with pytest.raises(RuntimeError, match="under ProgramData"):
        worker_main.service_check_data_root()


def test_service_modes_reject_non_windows_without_loading_scm_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["threads-worker", "--windows-service"])
    monkeypatch.setattr(worker_main.os, "name", "posix")
    output = io.StringIO()
    monkeypatch.setattr(worker_main.sys, "stderr", output)

    assert worker_main.main() == 2
    assert output.getvalue() == "THREADS_WORKER_WINDOWS_SERVICE_UNSUPPORTED\n"


def test_cli_rejects_enrollment_code_arguments_without_echoing_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "SYNTHETIC_ENROLLMENT_CODE_ARGUMENT"
    monkeypatch.setattr(sys, "argv", ["threads-worker", "--enrollment-code", sentinel])
    output = io.StringIO()
    monkeypatch.setattr(worker_main.sys, "stderr", output)

    assert worker_main.main() == 2
    assert output.getvalue() == "THREADS_WORKER_INVALID_ARGUMENTS\n"
    assert sentinel not in output.getvalue()


class _FakeSCMApi:
    def __init__(self) -> None:
        self.handler: Any | None = None
        self.states: list[int] = []
        self._sent_stop = False

    def start_dispatcher(self, table: Any) -> bool:
        table[0].lpServiceProc(0, None)
        return True

    def register_handler(self, callback: Any) -> int:
        self.handler = callback
        return 9

    def set_status(
        self,
        handle: int,
        status: Any,
    ) -> bool:
        _ = handle
        self.states.append(status.dwCurrentState)
        if status.dwCurrentState == windows_service.SERVICE_RUNNING and not self._sent_stop:
            self._sent_stop = True
            handler = self.handler
            assert handler is not None
            handler(windows_service.SERVICE_CONTROL_STOP, 0, None, None)
        return True


def test_local_stop_signal_wakes_agent_loop_from_scm_thread() -> None:
    async def scenario() -> None:
        signal = windows_service.LocalStopSignal()
        stop_event = asyncio.Event()
        signal.bind(asyncio.get_running_loop(), stop_event)
        callback_thread = threading.Thread(target=signal.request)
        callback_thread.start()
        await asyncio.to_thread(callback_thread.join)

        await asyncio.wait_for(stop_event.wait(), timeout=1)
        assert signal.is_set()

    asyncio.run(scenario())


def test_scm_reports_start_run_stop_pending_and_stopped() -> None:
    api = _FakeSCMApi()

    async def runner(
        stop_event: asyncio.Event,
        _stop_signal: windows_service.LocalStopSignal,
        report_running: Callable[[], None],
    ) -> None:
        report_running()
        await stop_event.wait()

    result = windows_service.WindowsServiceHost(runner, api).run()

    assert result == 0
    assert api.states == [
        windows_service.SERVICE_START_PENDING,
        windows_service.SERVICE_RUNNING,
        windows_service.SERVICE_STOP_PENDING,
        windows_service.SERVICE_STOPPED,
    ]
