from typing import Protocol

from threads_platform.application.crm_protocol_v1 import CRMCommandResultV1


class CRMResultSink(Protocol):
    async def deliver_result(self, result: CRMCommandResultV1) -> str | None: ...
