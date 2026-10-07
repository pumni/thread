"""One bounded observe-only recruitment run for the Standalone CLI."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID

from threads_platform.standalone.accounts import LocalAccount
from threads_platform.standalone.api import LocalThreadsApiRuntime
from threads_platform.standalone.nurture import NurturePresetV1
from threads_platform.standalone.nurture_conversations import (
    NurtureConversationError,
    collect_nurture_inbound,
)
from threads_platform.standalone.nurture_discovery import (
    NurtureDiscoveryError,
    NurtureDiscoverySource,
    discover_nurture_candidates,
)
from threads_platform.standalone.nurture_store import (
    NurtureAccountLock,
    NurtureRunScope,
    NurtureRunV1,
    NurtureStateError,
    NurtureStore,
    NurtureTargetStateV1,
    NurtureTargetV1,
)

_SAFE_RUNNER_CODES = frozenset(
    {
        "DISCOVERY_CONTRACT_MISMATCH",
        "DISCOVERY_SOURCE_FAILED",
        "INVALID_CURSOR",
        "INVALID_LIMIT",
        "INVALID_MEDIA_ID",
        "INVALID_NURTURE_PRESET",
        "INVALID_QUERY",
        "INVALID_SEARCH_MODE",
        "INVALID_SEARCH_TYPE",
        "INVALID_THREAD_ID",
        "INVALID_USERNAME",
        "NURTURE_BUSY",
        "NURTURE_RUN_NOT_FOUND",
        "NURTURE_STATE_CAP_REACHED",
        "NURTURE_STATE_INVALID",
        "NURTURE_TARGET_AMBIGUOUS",
        "NURTURE_TARGET_PENDING",
        "NURTURE_TARGET_RESERVED",
        "NURTURE_VERSION_UNSUPPORTED",
        "PRESET_NOT_FOUND",
        "THREADS_API_REJECTED_REQUEST",
        "THREADS_AUTHENTICATION_FAILED",
        "THREADS_CREDENTIAL_EXPIRED",
        "THREADS_CREDENTIAL_INVALID",
        "THREADS_CREDENTIAL_NOT_CONFIGURED",
        "THREADS_CREDENTIAL_SECRET_UNAVAILABLE",
        "THREADS_DOCUMENTATION_CONTRACT_MISMATCH",
        "THREADS_INVALID_REQUEST",
        "THREADS_OBJECT_NOT_FOUND",
        "THREADS_PERMISSION_DENIED",
        "THREADS_RATE_LIMITED",
        "THREADS_REAUTHORIZATION_REQUIRED",
        "THREADS_SERVER_ERROR",
        "THREADS_TRANSPORT_FAILURE",
        "RUN_FAILED",
    }
)
_SAFE_STAGE = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")


class NurtureRunnerError(Exception):
    """A bounded CLI-safe error that never carries remote or credential data."""

    def __init__(self, code: str, run_id: UUID | None = None) -> None:
        self.code = code if code in _SAFE_RUNNER_CODES else "RUN_FAILED"
        self.run_id = run_id
        super().__init__(self.code)

    def __repr__(self) -> str:
        return f"NurtureRunnerError(code={self.code}, run_id={self.run_id})"


class NurtureRunnerInterrupted(KeyboardInterrupt):
    """Keyboard interruption with a safe local receipt reference for CLI output."""

    code = "INTERRUPTED"

    def __init__(self, run_id: UUID) -> None:
        self.run_id = run_id
        super().__init__(self.code)

    def __repr__(self) -> str:
        return f"NurtureRunnerInterrupted(run_id={self.run_id})"


class NurtureRunner:
    """Compose the accepted discovery policy with local observe-only receipts."""

    def __init__(self, api: LocalThreadsApiRuntime, store: NurtureStore) -> None:
        self._api = api
        self._store = store

    async def run(
        self,
        account: LocalAccount,
        preset: NurturePresetV1,
        *,
        now: datetime | None = None,
    ) -> NurtureRunV1:
        if type(account) is not LocalAccount or type(account.id) is not UUID:
            raise NurtureRunnerError("RUN_FAILED")
        if type(preset) is not NurturePresetV1:
            raise NurtureRunnerError("PRESET_NOT_FOUND")
        captured_now = _capture_utc(now)
        owner = self._store.acquire_account_lock(account.id)
        run_id: UUID | None = None
        try:
            async with owner:
                scope = owner.start_run(preset, now=captured_now)
                run_id = scope.receipt.id
                try:
                    return await self._run_started(
                        owner,
                        scope,
                        account,
                        preset,
                        captured_now,
                    )
                except KeyboardInterrupt:
                    raise NurtureRunnerInterrupted(run_id) from None
        except NurtureRunnerError:
            raise
        except NurtureStateError as error:
            raise NurtureRunnerError(error.code, run_id) from None
        raise NurtureRunnerError("RUN_FAILED", run_id) from None

    async def _run_started(
        self,
        owner: NurtureAccountLock,
        scope: NurtureRunScope,
        account: LocalAccount,
        preset: NurturePresetV1,
        now: datetime,
    ) -> NurtureRunV1:
        run_id = scope.receipt.id
        stage = "state"
        async with scope:
            try:
                target_state = NurtureTargetStateV1(
                    version=1,
                    account_id=account.id,
                    preset_id=preset.id,
                    targets=owner.get_targets(preset),
                )
                target_by_fingerprint = {
                    target.fingerprint: target for target in target_state.targets
                }
                stage = "inbound"
                inbound = await collect_nurture_inbound(self._api, account.alias, preset)
                chosen_inbound = next(
                    (
                        candidate
                        for candidate in inbound.candidates
                        if _surface_eligible(
                            target_by_fingerprint.get(candidate.fingerprint),
                            preset.seen_cooldown_seconds,
                            now,
                        )
                    ),
                    None,
                )

                chosen_fingerprint: str | None = None
                selected_count = 0
                decision = "NO_ACTION"
                discovery_discovered_count = 0
                discovery_deduped_count = 0
                if chosen_inbound is not None:
                    chosen_fingerprint = chosen_inbound.fingerprint
                    selected_count = 1
                    decision = "INBOUND_CANDIDATE"
                else:
                    stage = "discovery"
                    discovery = await discover_nurture_candidates(
                        self._api,
                        account.alias,
                        preset,
                        target_state,
                        now,
                    )
                    discovery_discovered_count = discovery.discovered_count
                    discovery_deduped_count = discovery.deduped_count
                    mention_candidates = tuple(
                        candidate
                        for candidate in discovery.selected_candidates
                        if candidate.source_class is NurtureDiscoverySource.MENTIONS
                    )
                    remaining_candidates = tuple(
                        candidate
                        for candidate in discovery.selected_candidates
                        if candidate.source_class is not NurtureDiscoverySource.MENTIONS
                    )
                    chosen_discovery = next(
                        (
                            candidate
                            for candidate in (*mention_candidates, *remaining_candidates)
                            if _surface_eligible(
                                target_by_fingerprint.get(candidate.fingerprint),
                                preset.seen_cooldown_seconds,
                                now,
                            )
                        ),
                        None,
                    )
                    if chosen_discovery is not None:
                        chosen_fingerprint = chosen_discovery.fingerprint
                        selected_count = 1
                        decision = "OBSERVE_ONLY"
                decision_codes = [decision]
                discovered_count = inbound.discovered_count + discovery_discovered_count
                deduped_count = inbound.deduped_count + discovery_deduped_count

                stage = "receipt"
                scope.update(
                    discovered_count=discovered_count,
                    deduped_count=deduped_count,
                    selected_count=selected_count,
                    enriched_count=0,
                    replied_count=0,
                    published_count=0,
                    skipped_count=deduped_count - selected_count,
                    decision_codes=tuple(decision_codes),
                )

                if chosen_fingerprint is not None:
                    stage = "observe"
                    observed = owner.observe_target(
                        preset,
                        chosen_fingerprint,
                        run_id,
                        decision,
                        now=now,
                    )
                    previous = target_by_fingerprint.get(chosen_fingerprint)
                    if previous is not None and not _action_history_preserved(previous, observed):
                        raise NurtureStateError("NURTURE_STATE_INVALID")
                    if observed.action_state not in {"NONE", "CONFIRMED"}:
                        raise NurtureStateError("NURTURE_STATE_INVALID")

                stage = "finalize"
                return scope.finish("SUCCESS")
            except KeyboardInterrupt, asyncio.CancelledError:
                raise
            except NurtureDiscoveryError as error:
                failed_stage = _safe_stage(error.stage, "discovery")
                _finish_failed(scope, error.code, failed_stage, run_id)
                raise NurtureRunnerError(error.code, run_id) from None
            except NurtureConversationError as error:
                _finish_failed(scope, error.code, error.stage, run_id)
                raise NurtureRunnerError(error.code, run_id) from None
            except NurtureStateError as error:
                _finish_failed(scope, error.code, stage, run_id)
                raise NurtureRunnerError(error.code, run_id) from None
            except Exception:
                _finish_failed(scope, "RUN_FAILED", stage, run_id)
                raise NurtureRunnerError("RUN_FAILED", run_id) from None
        raise NurtureRunnerError("RUN_FAILED", run_id) from None


def _capture_utc(value: datetime | None) -> datetime:
    captured = datetime.now(UTC) if value is None else value
    if type(captured) is not datetime or captured.tzinfo is None:
        raise NurtureRunnerError("RUN_FAILED")
    try:
        if captured.utcoffset() is None:
            raise NurtureRunnerError("RUN_FAILED")
        return captured.astimezone(UTC)
    except OverflowError, TypeError, ValueError:
        raise NurtureRunnerError("RUN_FAILED") from None


def _surface_eligible(
    target: NurtureTargetV1 | None,
    cooldown_seconds: int,
    now: datetime,
) -> bool:
    if target is None:
        return True
    if target.action_state not in {"NONE", "CONFIRMED"}:
        return False
    try:
        last_seen_at = target.last_seen_at
        if type(last_seen_at) is not datetime or last_seen_at.tzinfo is None:
            return False
        if last_seen_at.utcoffset() is None:
            return False
        elapsed = now - last_seen_at.astimezone(UTC)
    except AttributeError, OverflowError, TypeError, ValueError:
        return False
    return elapsed >= timedelta(seconds=cooldown_seconds)


def _action_history_preserved(
    previous: NurtureTargetV1,
    observed: NurtureTargetV1,
) -> bool:
    return (
        observed.action_state == previous.action_state
        and observed.last_action_at == previous.last_action_at
        and observed.last_operation_id == previous.last_operation_id
    )


def _safe_stage(value: str, fallback: str) -> str:
    return value if type(value) is str and _SAFE_STAGE.fullmatch(value) else fallback


def _finish_failed(scope: NurtureRunScope, code: str, stage: str, run_id: UUID) -> None:
    try:
        scope.finish("FAILED", error_code=code, failed_stage=stage)
    except NurtureStateError as error:
        raise NurtureRunnerError(error.code, run_id) from None
