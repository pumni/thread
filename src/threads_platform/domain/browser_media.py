from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

MAX_BROWSER_UPLOAD_BYTES = 50 * 1024 * 1024


class BrowserMediaKind(StrEnum):
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"


BROWSER_MEDIA_EXTENSIONS: Mapping[str, BrowserMediaKind] = MappingProxyType(
    {
        ".jpg": BrowserMediaKind.IMAGE,
        ".jpeg": BrowserMediaKind.IMAGE,
        ".png": BrowserMediaKind.IMAGE,
        ".webp": BrowserMediaKind.IMAGE,
        ".mp4": BrowserMediaKind.VIDEO,
        ".mov": BrowserMediaKind.VIDEO,
    }
)
