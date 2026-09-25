SUPPORTED_WORKER_PROTOCOL_MIN = 1
SUPPORTED_WORKER_PROTOCOL_MAX = 2
SUPPORTED_CAPABILITY_SCHEMA_VERSION = 1


def is_worker_protocol_supported(
    protocol_version: int | None,
    capabilities_schema_version: int | None,
) -> bool:
    return (
        protocol_version is not None
        and SUPPORTED_WORKER_PROTOCOL_MIN <= protocol_version <= SUPPORTED_WORKER_PROTOCOL_MAX
        and capabilities_schema_version == SUPPORTED_CAPABILITY_SCHEMA_VERSION
    )
