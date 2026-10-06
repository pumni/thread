"""Bounded foreground workflows over accepted standalone capabilities."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, NoReturn, cast
from uuid import UUID

from threads_platform.application.browser_capabilities import (
    BrowserFeedResultV1,
    BrowserTargetOpenResultV1,
)
from threads_platform.application.browser_read_semantics import (
    normalize_profile_username,
    parse_thread_ref,
)
from threads_platform.application.ports.threads import (
    DiscoveryPage,
    PublishingQuota,
    RemoteMedia,
    RemotePublicProfile,
    ReplyPage,
    ThreadsAPIError,
    ThreadsCredentialError,
)
from threads_platform.domain.discovery import DiscoverySearchMode, DiscoverySearchType
from threads_platform.standalone.api import LocalThreadsApiRuntime, StandaloneApiError
from threads_platform.standalone.mutations import (
    LocalThreadsMutationRuntime,
    PublishedTextResult,
    StandaloneMutationError,
)
from threads_platform.standalone.runtime import LocalRuntime, StandaloneRuntimeError

MAX_WORKFLOW_BYTES = 65_536
_MAX_WORKFLOW_STEPS = 32
_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_OPAQUE_ID = re.compile(r"[A-Za-z0-9._:-]{1,255}")
_SAFE_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")

WorkflowAction = Literal[
    "quota",
    "media",
    "replies",
    "conversation",
    "post_text",
    "feed",
    "profile",
    "thread",
    "public_profile",
    "profile_posts",
    "search",
    "mentions",
]
WorkflowValue = (
    PublishingQuota
    | RemoteMedia
    | RemotePublicProfile
    | ReplyPage
    | DiscoveryPage
    | PublishedTextResult
    | BrowserFeedResultV1
    | BrowserTargetOpenResultV1
)


class StandaloneWorkflowError(Exception):
    """A sanitized workflow-plan or step failure."""

    code: str
    step_index: int | None
    operation_id: UUID | None

    def __init__(
        self,
        code: str,
        step_index: int | None = None,
        operation_id: UUID | None = None,
    ) -> None:
        if _SAFE_ERROR_CODE.fullmatch(code) is None:
            raise ValueError("workflow error code must be bounded and safe")
        if step_index is not None and step_index < 1:
            raise ValueError("workflow step index must be positive")
        self.code = code
        self.step_index = step_index
        self.operation_id = operation_id
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class QuotaStep:
    pass


@dataclass(frozen=True, slots=True)
class MediaStep:
    media_id: str


@dataclass(frozen=True, slots=True)
class RepliesStep:
    thread_id: str
    after: str | None = None


@dataclass(frozen=True, slots=True)
class ConversationStep:
    thread_id: str
    after: str | None = None


@dataclass(frozen=True, slots=True)
class FeedStep:
    limit: int


@dataclass(frozen=True, slots=True)
class ProfileStep:
    username: str


@dataclass(frozen=True, slots=True)
class ThreadStep:
    thread_ref: str


@dataclass(frozen=True, slots=True)
class PublicProfileStep:
    username: str


@dataclass(frozen=True, slots=True)
class ProfilePostsStep:
    username: str
    limit: int
    after: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class SearchStep:
    query: str = field(repr=False)
    search_mode: DiscoverySearchMode
    search_type: DiscoverySearchType
    limit: int
    after: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class MentionsStep:
    limit: int
    after: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class PostTextStep:
    text: str = field(repr=False)


WorkflowStep = (
    QuotaStep
    | MediaStep
    | RepliesStep
    | ConversationStep
    | FeedStep
    | ProfileStep
    | ThreadStep
    | PublicProfileStep
    | ProfilePostsStep
    | SearchStep
    | MentionsStep
    | PostTextStep
)


@dataclass(frozen=True, slots=True)
class WorkflowPlan:
    version: int
    account: str
    steps: tuple[WorkflowStep, ...]


def is_read_only_workflow(plan: WorkflowPlan) -> bool:
    """Return whether every parsed workflow step is an approved READ capability."""

    read_only_steps = (
        QuotaStep,
        MediaStep,
        RepliesStep,
        ConversationStep,
        FeedStep,
        ProfileStep,
        ThreadStep,
        PublicProfileStep,
        ProfilePostsStep,
        SearchStep,
        MentionsStep,
    )
    return all(isinstance(step, read_only_steps) for step in plan.steps)


@dataclass(frozen=True, slots=True)
class WorkflowStepResult:
    index: int
    action: WorkflowAction
    value: WorkflowValue


def load_workflow(path: Path) -> WorkflowPlan:
    """Read and fully validate one bounded UTF-8 JSON workflow document."""

    try:
        if not path.is_file():
            raise StandaloneWorkflowError("WORKFLOW_UNAVAILABLE")
        with path.open("rb") as stream:
            contents = stream.read(MAX_WORKFLOW_BYTES + 1)
    except OSError:
        raise StandaloneWorkflowError("WORKFLOW_UNAVAILABLE") from None

    if len(contents) > MAX_WORKFLOW_BYTES:
        raise StandaloneWorkflowError("INVALID_WORKFLOW") from None
    try:
        document = contents.decode("utf-8")
    except UnicodeDecodeError:
        raise StandaloneWorkflowError("INVALID_WORKFLOW") from None

    try:
        raw_plan: object = json.loads(
            document,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_non_json_constant,
        )
    except ValueError, RecursionError:
        raise StandaloneWorkflowError("INVALID_WORKFLOW") from None
    return _parse_plan(raw_plan)


class LocalWorkflowRuntime:
    """Execute each validated step once, in order, and stop on first failure."""

    def __init__(
        self,
        api_runtime: LocalThreadsApiRuntime,
        mutation_runtime: LocalThreadsMutationRuntime,
        browser_runtime: LocalRuntime,
    ) -> None:
        self._api = api_runtime
        self._mutations = mutation_runtime
        self._browser = browser_runtime

    async def run(self, plan: WorkflowPlan) -> tuple[WorkflowStepResult, ...]:
        results: list[WorkflowStepResult] = []
        for index, step in enumerate(plan.steps, start=1):
            try:
                if isinstance(step, QuotaStep):
                    action: WorkflowAction = "quota"
                    value = await self._api.quota(plan.account)
                elif isinstance(step, MediaStep):
                    action = "media"
                    value = await self._api.media(plan.account, step.media_id)
                elif isinstance(step, RepliesStep):
                    action = "replies"
                    value = await self._api.replies(
                        plan.account,
                        step.thread_id,
                        after=step.after,
                    )
                elif isinstance(step, ConversationStep):
                    action = "conversation"
                    value = await self._api.conversation(
                        plan.account,
                        step.thread_id,
                        after=step.after,
                    )
                elif isinstance(step, FeedStep):
                    action = "feed"
                    value = await self._browser.browse_feed(plan.account, step.limit)
                elif isinstance(step, ProfileStep):
                    action = "profile"
                    value = await self._browser.open_profile(plan.account, step.username)
                elif isinstance(step, ThreadStep):
                    action = "thread"
                    value = await self._browser.open_thread(plan.account, step.thread_ref)
                elif isinstance(step, PublicProfileStep):
                    action = "public_profile"
                    value = await self._api.public_profile(plan.account, step.username)
                elif isinstance(step, ProfilePostsStep):
                    action = "profile_posts"
                    value = await self._api.profile_posts(
                        plan.account,
                        step.username,
                        after=step.after,
                        limit=step.limit,
                    )
                elif isinstance(step, SearchStep):
                    action = "search"
                    value = await self._api.search(
                        plan.account,
                        step.query,
                        search_mode=step.search_mode,
                        search_type=step.search_type,
                        after=step.after,
                        limit=step.limit,
                    )
                elif isinstance(step, MentionsStep):
                    action = "mentions"
                    value = await self._api.mentions(
                        plan.account,
                        after=step.after,
                        limit=step.limit,
                    )
                else:
                    action = "post_text"
                    value = await self._mutations.publish_text(plan.account, step.text)
            except (
                StandaloneApiError,
                StandaloneMutationError,
                StandaloneRuntimeError,
                ThreadsCredentialError,
                ThreadsAPIError,
            ) as error:
                operation_id = (
                    error.operation_id if isinstance(error, StandaloneMutationError) else None
                )
                raise StandaloneWorkflowError(error.code, index, operation_id) from None
            results.append(WorkflowStepResult(index=index, action=action, value=value))
        return tuple(results)


def _parse_plan(document: object) -> WorkflowPlan:
    if not isinstance(document, dict):
        raise StandaloneWorkflowError("INVALID_WORKFLOW")
    plan_data = cast(dict[str, object], document)
    if set(plan_data) != {"version", "account", "steps"}:
        raise StandaloneWorkflowError("INVALID_WORKFLOW")

    version = plan_data["version"]
    account = plan_data["account"]
    raw_steps = plan_data["steps"]
    if type(version) is not int or version != 1:
        raise StandaloneWorkflowError("INVALID_WORKFLOW")
    if not isinstance(account, str) or _ALIAS.fullmatch(account) is None:
        raise StandaloneWorkflowError("INVALID_WORKFLOW")
    if not isinstance(raw_steps, list):
        raise StandaloneWorkflowError("INVALID_WORKFLOW")
    step_list = cast(list[object], raw_steps)
    if not 1 <= len(step_list) <= _MAX_WORKFLOW_STEPS:
        raise StandaloneWorkflowError("INVALID_WORKFLOW")

    steps = tuple(_parse_step(raw_step) for raw_step in step_list)
    post_positions = [index for index, step in enumerate(steps) if isinstance(step, PostTextStep)]
    if len(post_positions) > 1 or (post_positions and post_positions[0] != len(steps) - 1):
        raise StandaloneWorkflowError("INVALID_WORKFLOW")
    return WorkflowPlan(version=version, account=account, steps=steps)


def _parse_step(value: object) -> WorkflowStep:
    if not isinstance(value, dict):
        raise StandaloneWorkflowError("INVALID_WORKFLOW")
    step = cast(dict[str, object], value)
    action = step.get("action")
    if not isinstance(action, str):
        raise StandaloneWorkflowError("INVALID_WORKFLOW")

    if action == "quota":
        if set(step) != {"action"}:
            raise StandaloneWorkflowError("INVALID_WORKFLOW")
        return QuotaStep()
    if action == "media":
        if set(step) != {"action", "media_id"}:
            raise StandaloneWorkflowError("INVALID_WORKFLOW")
        media_id = step["media_id"]
        if not isinstance(media_id, str) or _OPAQUE_ID.fullmatch(media_id) is None:
            raise StandaloneWorkflowError("INVALID_WORKFLOW")
        return MediaStep(media_id)
    if action in ("replies", "conversation"):
        if not _has_optional_after(step, {"action", "thread_id"}):
            raise StandaloneWorkflowError("INVALID_WORKFLOW")
        thread_id = step["thread_id"]
        if not isinstance(thread_id, str) or _OPAQUE_ID.fullmatch(thread_id) is None:
            raise StandaloneWorkflowError("INVALID_WORKFLOW")
        after = _parse_optional_cursor(step)
        if action == "replies":
            return RepliesStep(thread_id, after)
        return ConversationStep(thread_id, after)
    if action == "feed":
        if set(step) != {"action", "limit"}:
            raise StandaloneWorkflowError("INVALID_WORKFLOW")
        return FeedStep(_parse_feed_limit(step["limit"]))
    if action == "profile":
        if set(step) != {"action", "username"}:
            raise StandaloneWorkflowError("INVALID_WORKFLOW")
        username = step["username"]
        if normalize_profile_username(username) is None:
            raise StandaloneWorkflowError("INVALID_WORKFLOW")
        return ProfileStep(cast(str, username))
    if action == "thread":
        if set(step) != {"action", "thread_ref"}:
            raise StandaloneWorkflowError("INVALID_WORKFLOW")
        parsed_target = parse_thread_ref(step["thread_ref"])
        if parsed_target is None:
            raise StandaloneWorkflowError("INVALID_WORKFLOW")
        return ThreadStep(parsed_target[0])
    if action == "public_profile":
        if set(step) != {"action", "username"}:
            raise StandaloneWorkflowError("INVALID_WORKFLOW")
        username = _parse_api_lookup(step["username"], "INVALID_USERNAME")
        return PublicProfileStep(username)
    if action == "profile_posts":
        if not _has_optional_after(step, {"action", "username", "limit"}):
            raise StandaloneWorkflowError("INVALID_WORKFLOW")
        username = _parse_api_lookup(step["username"], "INVALID_USERNAME")
        limit = _parse_page_limit(step["limit"])
        after = _parse_optional_cursor(step)
        return ProfilePostsStep(username, limit, after)
    if action == "search":
        if not _has_optional_after(step, {"action", "query", "mode", "type", "limit"}):
            raise StandaloneWorkflowError("INVALID_WORKFLOW")
        query = _parse_api_lookup(step["query"], "INVALID_QUERY")
        mode_value = step["mode"]
        type_value = step["type"]
        if not isinstance(mode_value, str) or not isinstance(type_value, str):
            raise StandaloneWorkflowError("INVALID_WORKFLOW")
        try:
            search_mode = DiscoverySearchMode(mode_value)
            search_type = DiscoverySearchType(type_value)
        except ValueError:
            raise StandaloneWorkflowError("INVALID_WORKFLOW") from None
        limit = _parse_page_limit(step["limit"])
        after = _parse_optional_cursor(step)
        return SearchStep(query, search_mode, search_type, limit, after)
    if action == "mentions":
        if not _has_optional_after(step, {"action", "limit"}):
            raise StandaloneWorkflowError("INVALID_WORKFLOW")
        limit = _parse_page_limit(step["limit"])
        after = _parse_optional_cursor(step)
        return MentionsStep(limit, after)
    if action == "post_text":
        if set(step) != {"action", "text"}:
            raise StandaloneWorkflowError("INVALID_WORKFLOW")
        text = step["text"]
        if not isinstance(text, str) or not 1 <= len(text) <= 500 or not text.strip():
            raise StandaloneWorkflowError("INVALID_WORKFLOW")
        return PostTextStep(text)
    raise StandaloneWorkflowError("INVALID_WORKFLOW")


def _has_optional_after(step: dict[str, object], required: set[str]) -> bool:
    keys = set(step)
    return keys == required or keys == required | {"after"}


def _parse_optional_cursor(step: dict[str, object]) -> str | None:
    if "after" not in step:
        return None
    after = step["after"]
    if not isinstance(after, str):
        raise StandaloneWorkflowError("INVALID_WORKFLOW")
    try:
        LocalThreadsApiRuntime.validate_cursor(after)
    except StandaloneApiError:
        raise StandaloneWorkflowError("INVALID_WORKFLOW") from None
    return after


def _parse_api_lookup(value: object, code: str) -> str:
    try:
        return LocalThreadsApiRuntime.validate_lookup_text(value, code)
    except StandaloneApiError:
        raise StandaloneWorkflowError("INVALID_WORKFLOW") from None


def _parse_page_limit(value: object) -> int:
    try:
        LocalThreadsApiRuntime.validate_limit(value)
    except StandaloneApiError:
        raise StandaloneWorkflowError("INVALID_WORKFLOW") from None
    return cast(int, value)


def _parse_feed_limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 20:
        raise StandaloneWorkflowError("INVALID_WORKFLOW")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_non_json_constant(value: str) -> NoReturn:
    raise ValueError(value)
