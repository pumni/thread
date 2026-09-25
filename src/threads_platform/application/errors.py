from datetime import timedelta


class CommandInputError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class IdempotencyConflict(ValueError):
    """A command id was reused with content that differs from its first receipt."""


class CommandNotFound(LookupError):
    """No inbox entry exists for the requested command id."""


class ExecutionLeaseLost(RuntimeError):
    """The current worker no longer owns the durable command execution lease."""


class RetryableCommandError(Exception):
    def __init__(self, code: str, retry_after: timedelta | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


class PermanentCommandError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class CRMUnavailable(RetryableCommandError):
    def __init__(self, retry_after: timedelta | None = None) -> None:
        super().__init__("CRM_UNAVAILABLE", retry_after)
