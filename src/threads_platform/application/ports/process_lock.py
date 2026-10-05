from typing import Protocol


class ProcessAlreadyRunning(RuntimeError):
    pass


class ProcessLock(Protocol):
    @property
    def held(self) -> bool: ...

    def acquire(self) -> None: ...

    def release(self) -> None: ...
