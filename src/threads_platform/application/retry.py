import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from threads_platform.domain.time import normalize_utc


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay: timedelta = timedelta(seconds=1)
    max_delay: timedelta = timedelta(minutes=2)
    jitter_ratio: float = 0.2
    jitter_source: Callable[[float, float], float] = random.uniform

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if self.base_delay <= timedelta(0) or self.max_delay < self.base_delay:
            raise ValueError("retry delays must be positive and ordered")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between zero and one")

    def next_attempt_at(
        self,
        attempt_number: int,
        now: datetime,
        deadline_at: datetime,
        *,
        retry_after: timedelta | None = None,
    ) -> datetime | None:
        if attempt_number < 1:
            raise ValueError("attempt_number must be positive")
        if attempt_number >= self.max_attempts:
            return None

        current_time = normalize_utc(now)
        deadline = normalize_utc(deadline_at)
        exponent = min(attempt_number - 1, 30)
        exponential_seconds = min(
            self.base_delay.total_seconds() * (2**exponent), self.max_delay.total_seconds()
        )
        lower_bound = exponential_seconds * (1 - self.jitter_ratio)
        upper_bound = min(
            self.max_delay.total_seconds(), exponential_seconds * (1 + self.jitter_ratio)
        )
        delay_seconds = self.jitter_source(lower_bound, upper_bound)
        if retry_after is not None:
            if retry_after > self.max_delay:
                return None
            delay_seconds = max(delay_seconds, retry_after.total_seconds())
        delay = min(delay_seconds, self.max_delay.total_seconds())
        candidate = current_time + timedelta(seconds=delay)
        if candidate >= deadline:
            return None
        return candidate
