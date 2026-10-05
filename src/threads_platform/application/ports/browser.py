from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID

BROWSER_FEED_ORIGIN = "https://www.threads.com"
BROWSER_FEED_CANDIDATE_BOUND = 100
_BROWSER_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class BrowserAdapterError(RuntimeError):
    def __init__(self, code: str) -> None:
        if not _BROWSER_ERROR_CODE.fullmatch(code):
            raise ValueError("browser adapter error code must be bounded and safe")
        super().__init__(code)
        self.code = code


class BrowserContractError(BrowserAdapterError):
    def __init__(self) -> None:
        super().__init__("BROWSER_CONTRACT_MISMATCH")


class LocatorNotFound(BrowserAdapterError):
    def __init__(self) -> None:
        super().__init__("BROWSER_REQUIRED_MARKER_NOT_FOUND")


class SessionExpired(BrowserAdapterError):
    def __init__(self) -> None:
        super().__init__("SESSION_EXPIRED")


class ChallengeDetected(BrowserAdapterError):
    def __init__(self) -> None:
        super().__init__("CHALLENGE_REQUIRED")


class RemoteSessionStateUncertain(BrowserAdapterError):
    def __init__(self) -> None:
        super().__init__("REMOTE_STATE_UNCERTAIN")


class NavigationTimeout(BrowserAdapterError):
    def __init__(self) -> None:
        super().__init__("BROWSER_NAVIGATION_TIMEOUT")


class MediaUploadFailed(BrowserAdapterError):
    def __init__(self) -> None:
        super().__init__("MEDIA_UPLOAD_FAILED")


class UnsupportedUIState(BrowserAdapterError):
    def __init__(self) -> None:
        super().__init__("UNSUPPORTED_UI_STATE")


class BrowserProcessCrashed(BrowserAdapterError):
    def __init__(self) -> None:
        super().__init__("BROWSER_PROCESS_CRASHED")


class BrowserRuntimeUnavailable(BrowserAdapterError):
    def __init__(self, code: str = "BROWSER_RUNTIME_UNAVAILABLE") -> None:
        super().__init__(code)


class BrowserNetworkRouteUnsupported(BrowserAdapterError):
    def __init__(self) -> None:
        super().__init__("BROWSER_NETWORK_ROUTE_UNSUPPORTED")


@dataclass(frozen=True, slots=True)
class BrowserSurface:
    contract_id: str | None
    contract_version: str | None
    session_state: str | None
    required_root_present: bool


@dataclass(frozen=True, slots=True)
class FeedAncestorObservation:
    """Bounded semantic evidence extracted from one permalink ancestor."""

    hrefs: tuple[str, ...]
    text_regions: tuple[str, ...]
    links_truncated: bool = False
    text_regions_truncated: bool = False


@dataclass(frozen=True, slots=True)
class FeedCandidateObservation:
    """A post permalink and its nearest-first bounded ancestor evidence."""

    permalink_href: str
    ancestors: tuple[FeedAncestorObservation, ...]


class BrowserNetworkProtocol(StrEnum):
    DIRECT = "DIRECT"
    HTTP = "HTTP"
    HTTPS = "HTTPS"
    SOCKS5 = "SOCKS5"


@dataclass(frozen=True, slots=True)
class BrowserNetworkRoute:
    protocol: BrowserNetworkProtocol
    host: str | None
    port: int | None


@dataclass(frozen=True, slots=True)
class BrowserProxyCredentials:
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class BrowserLaunchRequest:
    profile_directory: Path
    network_route: BrowserNetworkRoute
    proxy_credentials: BrowserProxyCredentials | None = field(default=None, repr=False)
    headless: bool = False


class BrowserEngineSession(Protocol):
    async def navigate(self, url: str, *, allowed_origins: frozenset[str]) -> None: ...

    async def close(self) -> None: ...


class BrowserFeedEngineSession(BrowserEngineSession, Protocol):
    async def collect_feed_candidates(
        self, *, ancestor_bound: int
    ) -> tuple[FeedCandidateObservation, ...]: ...

    async def scroll_feed(self) -> None: ...


class BrowserThreadOpenEngineSession(BrowserEngineSession, Protocol):
    async def verify_thread_target(
        self, *, target_ref: str, author_username: str, ancestor_bound: int
    ) -> None: ...


class BrowserProfileOpenEngineSession(BrowserEngineSession, Protocol):
    async def verify_profile_target(self, *, target_ref: str, ancestor_bound: int) -> None: ...


@dataclass(frozen=True, slots=True)
class PreparedMediaComposer:
    """Opaque, process-local handle for one verified composer dialog."""

    token: UUID


class BrowserMediaEngineSession(BrowserEngineSession, Protocol):
    async def prepare_media_composer(self) -> PreparedMediaComposer | None: ...

    async def stage_local_media(self, composer: PreparedMediaComposer, file_path: Path) -> None: ...

    async def discard_media_composer(self, composer: PreparedMediaComposer) -> None: ...


class BrowserEngine(Protocol):
    async def open(self, request: BrowserLaunchRequest) -> BrowserEngineSession: ...
