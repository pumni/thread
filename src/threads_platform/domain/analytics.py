from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from threads_platform.domain.time import normalize_utc, utc_now


@dataclass(slots=True)
class InsightSnapshot:
    account_id: UUID
    entity_type: str
    entity_id: str
    metric: str
    value: Decimal
    captured_at: datetime = field(default_factory=utc_now)
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if not all((self.entity_type.strip(), self.entity_id.strip(), self.metric.strip())):
            raise ValueError("insight identity fields must not be empty")
        self.captured_at = normalize_utc(self.captured_at)
