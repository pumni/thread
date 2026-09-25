from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from threads_platform.application.ports.worker_agent import (
    LocalSessionState,
    ManagedProfileDirectory,
    WorkerAccountContext,
    WorkerLocalState,
)
from threads_platform.domain.workers import BrowserSessionState, NetworkProfile, NetworkProtocol

_SESSION_TRANSITIONS: dict[BrowserSessionState, frozenset[BrowserSessionState]] = {
    BrowserSessionState.UNINITIALIZED: frozenset(
        {
            BrowserSessionState.LOGIN_REQUIRED,
            BrowserSessionState.STARTING,
            BrowserSessionState.ERROR,
            BrowserSessionState.STOPPED,
        }
    ),
    BrowserSessionState.LOGIN_REQUIRED: frozenset(
        {BrowserSessionState.STARTING, BrowserSessionState.ERROR, BrowserSessionState.STOPPED}
    ),
    BrowserSessionState.STARTING: frozenset(
        {
            BrowserSessionState.AUTHENTICATED,
            BrowserSessionState.LOGIN_REQUIRED,
            BrowserSessionState.SESSION_EXPIRED,
            BrowserSessionState.CHALLENGE_REQUIRED,
            BrowserSessionState.ERROR,
            BrowserSessionState.STOPPED,
        }
    ),
    BrowserSessionState.AUTHENTICATED: frozenset(
        {
            BrowserSessionState.BUSY,
            BrowserSessionState.SESSION_EXPIRED,
            BrowserSessionState.CHALLENGE_REQUIRED,
            BrowserSessionState.ERROR,
            BrowserSessionState.STOPPED,
        }
    ),
    BrowserSessionState.BUSY: frozenset(
        {
            BrowserSessionState.AUTHENTICATED,
            BrowserSessionState.SESSION_EXPIRED,
            BrowserSessionState.CHALLENGE_REQUIRED,
            BrowserSessionState.ERROR,
            BrowserSessionState.STOPPED,
        }
    ),
    BrowserSessionState.SESSION_EXPIRED: frozenset(
        {
            BrowserSessionState.LOGIN_REQUIRED,
            BrowserSessionState.STARTING,
            BrowserSessionState.ERROR,
            BrowserSessionState.STOPPED,
        }
    ),
    BrowserSessionState.CHALLENGE_REQUIRED: frozenset(
        {
            BrowserSessionState.LOGIN_REQUIRED,
            BrowserSessionState.STARTING,
            BrowserSessionState.ERROR,
            BrowserSessionState.STOPPED,
        }
    ),
    BrowserSessionState.ERROR: frozenset(
        {BrowserSessionState.STARTING, BrowserSessionState.STOPPED}
    ),
    BrowserSessionState.STOPPED: frozenset({BrowserSessionState.STARTING}),
}


class InvalidSessionTransition(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class NetworkRoute:
    account_id: UUID
    protocol: NetworkProtocol
    host: str | None
    port: int | None
    credential_ref: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class BrowserSessionOpenResult:
    state: LocalSessionState
    network_route: NetworkRoute


class NetworkProfileApplication:
    """Resolves account routing metadata without retrieving proxy credentials."""

    def resolve(self, account_id: UUID, profile: NetworkProfile | None) -> NetworkRoute:
        if profile is None:
            return NetworkRoute(account_id, NetworkProtocol.DIRECT, None, None)
        if profile.account_id != account_id:
            raise ValueError("NetworkProfile belongs to a different account")
        return NetworkRoute(
            account_id,
            profile.protocol,
            profile.host,
            profile.port,
            profile.credential_ref,
        )


class ProxyCredentialProvider(Protocol):
    async def credentials_for(self, credential_ref: str) -> ProxyCredentials: ...


@dataclass(frozen=True, slots=True)
class ProxyCredentials:
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)


class BrowserSessionManager(Protocol):
    async def open(self, context: WorkerAccountContext) -> BrowserSessionOpenResult: ...

    async def transition(
        self, account_id: UUID, state: BrowserSessionState
    ) -> LocalSessionState: ...

    async def close(self, account_id: UUID) -> LocalSessionState: ...


class LocalBrowserSessionManager:
    """Tracks engine-neutral session lifecycle and bounded local reservations."""

    def __init__(
        self,
        worker_id: UUID,
        max_browser_sessions: int,
        state_store: WorkerLocalState,
        profile_resolver: ManagedProfileDirectory,
        *,
        network_profiles: NetworkProfileApplication | None = None,
    ) -> None:
        if max_browser_sessions < 1:
            raise ValueError("max_browser_sessions must be positive")
        self._worker_id = worker_id
        self._max_browser_sessions = max_browser_sessions
        self._state_store = state_store
        self._profile_resolver = profile_resolver
        self._network_profiles = network_profiles or NetworkProfileApplication()

    async def open(self, context: WorkerAccountContext) -> BrowserSessionOpenResult:
        if context.worker_id != self._worker_id:
            raise ValueError("account is assigned to another worker")
        previous = self._state_store.get_session(context.account_id)
        if (
            previous is not None
            and previous.state
            not in {
                BrowserSessionState.ERROR,
                BrowserSessionState.SESSION_EXPIRED,
                BrowserSessionState.STOPPED,
            }
            and previous.profile_ref != context.profile_ref
        ):
            raise ValueError("active account session cannot be moved to another profile")
        route = self._network_profiles.resolve(context.account_id, context.network_profile)
        self._profile_resolver.ensure_profile(
            self._worker_id, context.account_id, context.profile_ref
        )

        if previous is not None and previous.state not in {
            BrowserSessionState.ERROR,
            BrowserSessionState.SESSION_EXPIRED,
            BrowserSessionState.STOPPED,
        }:
            self._state_store.reserve_session(
                worker_id=self._worker_id,
                account_id=context.account_id,
                profile_ref=context.profile_ref,
                session_id=previous.session_id,
                maximum=self._max_browser_sessions,
            )
            return BrowserSessionOpenResult(previous, route)

        session_id = (
            previous.session_id
            if previous is not None and previous.profile_ref == context.profile_ref
            else uuid4()
        )
        self._state_store.reserve_session(
            worker_id=self._worker_id,
            account_id=context.account_id,
            profile_ref=context.profile_ref,
            session_id=session_id,
            maximum=self._max_browser_sessions,
        )
        starting = self._transition_from(
            context.account_id,
            context.profile_ref,
            session_id,
            BrowserSessionState.STARTING,
        )
        # C3-01 has no browser engine, so opening a logical session always requests operator login.
        login_required = self._transition_from(
            context.account_id,
            context.profile_ref,
            starting.session_id,
            BrowserSessionState.LOGIN_REQUIRED,
        )
        return BrowserSessionOpenResult(login_required, route)

    async def transition(self, account_id: UUID, state: BrowserSessionState) -> LocalSessionState:
        current = self._state_store.get_session(account_id)
        if current is None:
            raise ValueError("browser session is not initialized")
        if state not in _SESSION_TRANSITIONS[current.state]:
            raise InvalidSessionTransition(
                f"{current.state.value} cannot transition to {state.value}"
            )
        changed = self._state_store.record_session_state(
            worker_id=self._worker_id,
            account_id=account_id,
            profile_ref=current.profile_ref,
            session_id=current.session_id,
            state=state,
            updated_at=datetime.now(UTC),
        )
        if state is BrowserSessionState.STOPPED:
            self._state_store.release_session(account_id, current.session_id)
        return changed

    async def close(self, account_id: UUID) -> LocalSessionState:
        return await self.transition(account_id, BrowserSessionState.STOPPED)

    def _transition_from(
        self,
        account_id: UUID,
        profile_ref: str,
        session_id: UUID,
        target: BrowserSessionState,
    ) -> LocalSessionState:
        current = self._state_store.get_session(account_id)
        if current is not None and target not in _SESSION_TRANSITIONS[current.state]:
            raise InvalidSessionTransition(
                f"{current.state.value} cannot transition to {target.value}"
            )
        return self._state_store.record_session_state(
            worker_id=self._worker_id,
            account_id=account_id,
            profile_ref=profile_ref,
            session_id=session_id,
            state=target,
            updated_at=datetime.now(UTC),
        )
