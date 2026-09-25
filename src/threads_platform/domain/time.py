from datetime import UTC, datetime


def normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


def utc_now() -> datetime:
    return datetime.now(UTC)
