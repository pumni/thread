"""One bounded observe-only or explicitly approved reply run for Standalone Nurture."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from threads_platform.application.ports.threads import PublishingQuota
from threads_platform.standalone.accounts import LocalAccount
from threads_platform.standalone.api import LocalThreadsApiRuntime
from threads_platform.standalone.mutations import (
    CreatedReplyResult,
    LocalThreadsMutationRuntime,
    StandaloneMutationError,
)
from threads_platform.standalone.nurture import NurturePresetV1
from threads_platform.standalone.nurture_conversations import (
    NurtureConversationError,
    NurtureInboundCandidate,
    collect_nurture_inbound,
)
from threads_platform.standalone.nurture_discovery import (
    NurtureCandidate,
    NurtureDiscoveryError,
    NurtureDiscoverySource,
    discover_nurture_candidates,
)
from threads_platform.standalone.nurture_draft import NurtureDraft
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
        "ACCOUNT_BUSY",
        "DISCOVERY_CONTRACT_MISMATCH",
        "DISCOVERY_SOURCE_FAILED",
        "INVALID_CURSOR",
        "INVALID_LIMIT",
        "INVALID_MEDIA_ID",
        "INVALID_NURTURE_DRAFT",
        "INVALID_NURTURE_PRESET",
        "INVALID_QUERY",
        "INVALID_REPLY_ID",
        "INVALID_REPLY_TEXT",
        "INVALID_SEARCH_MODE",
        "INVALID_SEARCH_TYPE",
        "INVALID_THREAD_ID",
        "INVALID_USERNAME",
        "LOCAL_OPERATION_UNAVAILABLE",
        "NURTURE_BUSY",
        "NURTURE_DRAFT_TARGET_MISMATCH",
        "NURTURE_REPLY_APPLY_DISABLED",
        "NURTURE_RUN_NOT_FOUND",
        "NURTURE_STATE_CAP_REACHED",
        "NURTURE_STATE_INVALID",
        "NURTURE_TARGET_AMBIGUOUS",
        "NURTURE_TARGET_PENDING",
        "NURTURE_TARGET_RESERVED",
        "NURTURE_VERSION_UNSUPPORTED",
        "OPERATION_CANCELLED",
        "OPERATION_STATE_INVALID",
        "PRESET_NOT_FOUND",
        "PUBLISH_OUTCOME_AMBIGUOUS",
        "RUN_FAILED",
        "THREADS_API_REJECTED_REQUEST",
        "THREADS_AUTHENTICATION_FAILED",
        "THREADS_CONTAINER_ERROR",
        "THREADS_CONTAINER_EXPIRED",
        "THREADS_CONTAINER_PROCESSING_TIMEOUT",
        "THREADS_CREDENTIAL_EXPIRED",
        "THREADS_CREDENTIAL_INVALID",
        "THREADS_CREDENTIAL_NOT_CONFIGURED",
        "THREADS_CREDENTIAL_SECRET_UNAVAILABLE",
        "THREADS_DOCUMENTATION_CONTRACT_MISMATCH",
        "THREADS_INVALID_REQUEST",
        "THREADS_OBJECT_NOT_FOUND",
        "THREADS_PERMISSION_DENIED",
        "THREADS_PUBLISHING_QUOTA_REACHED",
        "THREADS_RATE_LIMITED",
        "THREADS_REAUTHORIZATION_REQUIRED",
        "THREADS_REPLY_QUOTA_REACHED",
        "THREADS_SERVER_ERROR",
        "THREADS_TRANSPORT_FAILURE",
    }
)
_SAFE_STAGE = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")


@dataclass(frozen=True, slots=True, repr=False)
class NurtureRunResult:
    """A receipt plus the safe fingerprint selected in this invocation."""

    receipt: NurtureRunV1
    target_fingerprint: str | None

    def __repr__(self) -> str:
        return (
            "NurtureRunResult("
            f"run_id={self.receipt.id}, outcome={self.receipt.outcome}, "
            f"target_fingerprint={self.target_fingerprint or '-'})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class _NurtureSelection:
    candidate: NurtureInboundCandidate | NurtureCandidate | None
    target_by_fingerprint: dict[str, NurtureTargetV1]
    discovered_count: int
    deduped_count: int


class NurtureRunnerError(Exception):
    """A bounded CLI-safe error that never carries remote or credential data."""

    def __init__(
        self,
        code: str,
        run_id: UUID | None = None,
        operation_id: UUID | None = None,
    ) -> None:
        self.code = code if code in _SAFE_RUNNER_CODES else "RUN_FAILED"
        self.run_id = run_id
        self.operation_id = operation_id
        super().__init__(self.code)

    def __repr__(self) -> str:
        operation = f", operation_id={self.operation_id}" if self.operation_id else ""
        return f"NurtureRunnerError(code={self.code}, run_id={self.run_id}{operation})"


class NurtureRunnerInterrupted(KeyboardInterrupt):
    """Keyboard interruption with a safe local receipt reference for CLI output."""

    code = "INTERRUPTED"

    def __init__(self, run_id: UUID) -> None:
        self.run_id = run_id
        super().__init__(self.code)

    def __repr__(self) -> str:
        return f"NurtureRunnerInterrupted(run_id={self.run_id})"


class NurtureRunner:
    """Compose accepted discovery policy with local receipts and optional explicit apply."""

    def __init__(self, api: LocalThreadsApiRuntime, store: NurtureStore) -> None:
        self._api = api
        self._store = store

    async def run(
        self,
        account: LocalAccount,
        preset: NurturePresetV1,
        *,
        now: datetime | None = None,
        apply_requested: bool = False,
        draft: NurtureDraft | None = None,
        mutations: LocalThreadsMutationRuntime | None = None,
    ) -> NurtureRunV1:
        return (
            await self.run_with_selection(
                account,
                preset,
                now=now,
                apply_requested=apply_requested,
                draft=draft,
                mutations=mutations,
            )
        ).receipt

    async def run_with_selection(
        self,
        account: LocalAccount,
        preset: NurturePresetV1,
        *,
        now: datetime | None = None,
        apply_requested: bool = False,
        draft: NurtureDraft | None = None,
        mutations: LocalThreadsMutationRuntime | None = None,
    ) -> NurtureRunResult:
        if type(account) is not LocalAccount or type(account.id) is not UUID:
            raise NurtureRunnerError("RUN_FAILED")
        if type(preset) is not NurturePresetV1:
            raise NurtureRunnerError("PRESET_NOT_FOUND")
        if type(apply_requested) is not bool:
            raise NurtureRunnerError("RUN_FAILED")
        if draft is not None and type(draft) is not NurtureDraft:
            raise NurtureRunnerError("INVALID_NURTURE_DRAFT")
        if (draft is not None) != (mutations is not None) or (
            draft is not None and not apply_requested
        ):
            raise NurtureRunnerError("INVALID_NURTURE_DRAFT")

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
                        apply_requested=apply_requested,
                        draft=draft,
                        mutations=mutations,
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
        *,
        apply_requested: bool,
        draft: NurtureDraft | None,
        mutations: LocalThreadsMutationRuntime | None,
    ) -> NurtureRunResult:
        run_id = scope.receipt.id
        stage = "selection"
        selection: _NurtureSelection | None = None
        async with scope:
            try:
                selection = await self._select_current_candidate(
                    owner,
                    account,
                    preset,
                    now,
                    draft,
                )
                chosen = selection.candidate
                selected_count = 1 if chosen is not None else 0
                decision = _selection_decision(chosen, apply_requested, draft)
                stage = "receipt"
                scope.update(
                    discovered_count=selection.discovered_count,
                    deduped_count=selection.deduped_count,
                    selected_count=selected_count,
                    enriched_count=0,
                    replied_count=0,
                    published_count=0,
                    skipped_count=selection.deduped_count - selected_count,
                    decision_codes=(decision,),
                )

                if chosen is None:
                    return NurtureRunResult(scope.finish("SUCCESS", now=now), None)

                selected_fingerprint = chosen.fingerprint
                if draft is not None:
                    if selected_fingerprint != draft.target_fingerprint or mutations is None:
                        raise NurtureRunnerError("NURTURE_DRAFT_TARGET_MISMATCH")
                    thread_id, parent_reply_id = _mutation_target(chosen)
                    stage = "quota"
                    quota = await self._api.quota(account.alias)
                    if _reply_quota_exhausted(quota):
                        raise NurtureRunnerError("THREADS_REPLY_QUOTA_REACHED")
                    stage = "reserve"
                    owner.reserve_target(
                        preset,
                        selected_fingerprint,
                        run_id,
                        "REPLY_APPLY",
                        now=now,
                    )
                    stage = "publish"
                    result = await mutations.create_reply(
                        account.alias,
                        thread_id,
                        draft.text,
                        parent_reply_id=parent_reply_id,
                    )
                    if (
                        type(result) is not CreatedReplyResult
                        or type(result.operation_id) is not UUID
                        or result.operation_id.version != 4
                    ):
                        raise NurtureRunnerError("RUN_FAILED")

                    stage = "confirm"
                    owner.complete_target_action(
                        preset,
                        selected_fingerprint,
                        run_id,
                        "CONFIRMED",
                        result.operation_id,
                        now=now,
                    )
                    scope.link_operation(result.operation_id)
                    scope.update(replied_count=1, decision_codes=("REPLY_APPLIED",))
                    return NurtureRunResult(scope.finish("SUCCESS", now=now), selected_fingerprint)

                stage = "observe"
                observed = owner.observe_target(
                    preset,
                    selected_fingerprint,
                    run_id,
                    decision,
                    now=now,
                )
                previous = selection.target_by_fingerprint.get(selected_fingerprint)
                if previous is not None and not _action_history_preserved(previous, observed):
                    raise NurtureStateError("NURTURE_STATE_INVALID")
                if observed.action_state not in {"NONE", "CONFIRMED"}:
                    raise NurtureStateError("NURTURE_STATE_INVALID")

                return NurtureRunResult(scope.finish("SUCCESS", now=now), selected_fingerprint)
            except KeyboardInterrupt, asyncio.CancelledError:
                raise
            except NurtureDiscoveryError as error:
                failed_stage = _safe_stage(error.stage, "discovery")
                _finish_failed(scope, error.code, failed_stage, run_id)
                raise NurtureRunnerError(error.code, run_id) from None
            except NurtureConversationError as error:
                _finish_failed(scope, error.code, error.stage, run_id)
                raise NurtureRunnerError(error.code, run_id) from None
            except StandaloneMutationError as error:
                if (
                    error.code == "PUBLISH_OUTCOME_AMBIGUOUS"
                    and type(error.operation_id) is UUID
                    and error.operation_id.version == 4
                ):
                    _best_effort_ambiguous(
                        owner,
                        scope,
                        preset,
                        None
                        if selection is None or selection.candidate is None
                        else selection.candidate.fingerprint,
                        run_id,
                        error.operation_id,
                        now,
                    )
                    raise NurtureRunnerError(
                        "PUBLISH_OUTCOME_AMBIGUOUS", run_id, error.operation_id
                    ) from None
                code = _safe_code(error.code)
                _finish_failed(scope, code, stage, run_id)
                raise NurtureRunnerError(code, run_id) from None
            except NurtureRunnerError as error:
                _finish_failed(scope, error.code, stage, run_id)
                raise NurtureRunnerError(error.code, run_id, error.operation_id) from None
            except NurtureStateError as error:
                _finish_failed(scope, error.code, stage, run_id)
                raise NurtureRunnerError(error.code, run_id) from None
            except Exception as error:
                code = _safe_code(_exception_code(error))
                _finish_failed(scope, code, stage, run_id)
                raise NurtureRunnerError(code, run_id) from None
        raise NurtureRunnerError("RUN_FAILED", run_id) from None

    async def _select_current_candidate(
        self,
        owner: NurtureAccountLock,
        account: LocalAccount,
        preset: NurturePresetV1,
        now: datetime,
        draft: NurtureDraft | None,
    ) -> _NurtureSelection:
        state = NurtureTargetStateV1(
            version=1,
            account_id=account.id,
            preset_id=preset.id,
            targets=owner.get_targets(preset),
        )
        targets = {target.fingerprint: target for target in state.targets}
        bound_target = None if draft is None else targets.get(draft.target_fingerprint)
        if draft is not None:
            if not preset.engagement_enabled or preset.max_replies_per_explicit_run < 1:
                raise NurtureRunnerError("NURTURE_REPLY_APPLY_DISABLED")
            if bound_target is not None and bound_target.action_state != "NONE":
                raise NurtureRunnerError("NURTURE_DRAFT_TARGET_MISMATCH")

        inbound = await collect_nurture_inbound(self._api, account.alias, preset)
        chosen = next(
            (
                candidate
                for candidate in inbound.candidates
                if _candidate_surface_eligible(
                    candidate.fingerprint,
                    targets.get(candidate.fingerprint),
                    preset.seen_cooldown_seconds,
                    now,
                    draft,
                )
            ),
            None,
        )
        discovered_count = inbound.discovered_count
        deduped_count = inbound.deduped_count
        if chosen is not None:
            if draft is not None and chosen.fingerprint != draft.target_fingerprint:
                raise NurtureRunnerError("NURTURE_DRAFT_TARGET_MISMATCH")
        else:
            discovery = await discover_nurture_candidates(
                self._api,
                account.alias,
                preset,
                _discovery_state_for_draft(state, bound_target, draft),
                now,
            )
            discovered_count += discovery.discovered_count
            deduped_count += discovery.deduped_count
            mentions = tuple(
                candidate
                for candidate in discovery.selected_candidates
                if candidate.source_class is NurtureDiscoverySource.MENTIONS
            )
            remaining = tuple(
                candidate
                for candidate in discovery.selected_candidates
                if candidate.source_class is not NurtureDiscoverySource.MENTIONS
            )
            chosen = next(
                (
                    candidate
                    for candidate in (*mentions, *remaining)
                    if _candidate_surface_eligible(
                        candidate.fingerprint,
                        targets.get(candidate.fingerprint),
                        preset.seen_cooldown_seconds,
                        now,
                        draft,
                    )
                ),
                None,
            )
            if draft is not None and (
                chosen is None or chosen.fingerprint != draft.target_fingerprint
            ):
                raise NurtureRunnerError("NURTURE_DRAFT_TARGET_MISMATCH")
        return _NurtureSelection(chosen, targets, discovered_count, deduped_count)


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


def _candidate_surface_eligible(
    fingerprint: str,
    target: NurtureTargetV1 | None,
    cooldown_seconds: int,
    now: datetime,
    draft: NurtureDraft | None,
) -> bool:
    if (
        draft is not None
        and fingerprint == draft.target_fingerprint
        and _can_bypass_seen_cooldown(target, now)
    ):
        return True
    return _surface_eligible(target, cooldown_seconds, now)


def _can_bypass_seen_cooldown(target: NurtureTargetV1 | None, now: datetime) -> bool:
    if target is None:
        return True
    if target.action_state != "NONE":
        return False
    try:
        last_seen_at = target.last_seen_at
        if type(last_seen_at) is not datetime or last_seen_at.tzinfo is None:
            return False
        if last_seen_at.utcoffset() is None:
            return False
        return now >= last_seen_at.astimezone(UTC)
    except AttributeError, OverflowError, TypeError, ValueError:
        return False


def _selection_decision(
    candidate: NurtureInboundCandidate | NurtureCandidate | None,
    apply_requested: bool,
    draft: NurtureDraft | None,
) -> str:
    if candidate is None:
        return "NO_ACTION"
    if draft is not None:
        return "REPLY_APPLY"
    if apply_requested:
        return "REPLY_RECOMMENDED_NO_DRAFT"
    if isinstance(candidate, NurtureInboundCandidate):
        return "INBOUND_CANDIDATE"
    return "OBSERVE_ONLY"


def _discovery_state_for_draft(
    state: NurtureTargetStateV1,
    bound_target: NurtureTargetV1 | None,
    draft: NurtureDraft | None,
) -> NurtureTargetStateV1:
    if draft is None or bound_target is None or bound_target.action_state != "NONE":
        return state
    return NurtureTargetStateV1(
        version=state.version,
        account_id=state.account_id,
        preset_id=state.preset_id,
        targets=tuple(
            target for target in state.targets if target.fingerprint != draft.target_fingerprint
        ),
    )


def _mutation_target(
    candidate: NurtureInboundCandidate | NurtureCandidate,
) -> tuple[str, str | None]:
    if isinstance(candidate, NurtureInboundCandidate):
        if not candidate.root_thread_id or not candidate.reply_id:
            raise NurtureRunnerError("DISCOVERY_CONTRACT_MISMATCH")
        return candidate.root_thread_id, candidate.reply_id
    if not candidate.remote_thread_id:
        raise NurtureRunnerError("DISCOVERY_CONTRACT_MISMATCH")
    return candidate.remote_thread_id, None


def _reply_quota_exhausted(quota: object) -> bool:
    if type(quota) is not PublishingQuota:
        raise NurtureRunnerError("THREADS_DOCUMENTATION_CONTRACT_MISMATCH")
    usage = quota.reply_usage
    total = quota.reply_total
    for value in (usage, total):
        if value is not None and (type(value) is not int or value < 0):
            raise NurtureRunnerError("THREADS_DOCUMENTATION_CONTRACT_MISMATCH")
    return usage is not None and total is not None and usage >= total


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


def _safe_code(value: object) -> str:
    return value if type(value) is str and value in _SAFE_RUNNER_CODES else "RUN_FAILED"


def _exception_code(error: Exception) -> object:
    try:
        return getattr(error, "code", None)
    except Exception:
        return None


def _finish_failed(scope: NurtureRunScope, code: str, stage: str, run_id: UUID) -> None:
    try:
        scope.finish("FAILED", error_code=code, failed_stage=stage)
    except NurtureStateError as error:
        raise NurtureRunnerError(error.code, run_id) from None


def _best_effort_ambiguous(
    owner: NurtureAccountLock,
    scope: NurtureRunScope,
    preset: NurturePresetV1,
    fingerprint: str | None,
    run_id: UUID,
    operation_id: UUID,
    now: datetime,
) -> None:
    if fingerprint is not None:
        try:
            owner.complete_target_action(
                preset,
                fingerprint,
                run_id,
                "AMBIGUOUS",
                operation_id,
                now=now,
            )
        except Exception:
            pass
    try:
        scope.link_operation(operation_id)
    except Exception:
        pass
    try:
        scope.finish(
            "AMBIGUOUS",
            now=now,
            error_code="PUBLISH_OUTCOME_AMBIGUOUS",
            failed_stage="publish",
        )
    except Exception:
        pass
