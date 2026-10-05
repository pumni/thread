"""Bounded foreground workflows over accepted standalone capabilities."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, NoReturn, cast
from uuid import UUID

from threads_platform.application.ports.threads import (
    PublishingQuota,
    RemoteMedia,
    ReplyPage,
    ThreadsAPIError,
    ThreadsCredentialError,
)
from threads_platform.standalone.api import LocalThreadsApiRuntime, StandaloneApiError
from threads_platform.standalone.mutations import (
    LocalThreadsMutationRuntime,
    PublishedTextResult,
    StandaloneMutationError,
)

_MAX_WORKFLOW_BYTES = 65_536
_MAX_WORKFLOW_STEPS = 32
_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_OPAQUE_ID = re.compile(r"[A-Za-z0-9._:-]{1,255}")
_SAFE_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")

WorkflowAction = Literal["quota", "media", "replies", "conversation", "post_text"]
WorkflowValue = PublishingQuota | RemoteMedia | ReplyPage | PublishedTextResult


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
class PostTextStep:
    text: str = field(repr=False)


WorkflowStep = QuotaStep | MediaStep | RepliesStep | ConversationStep | PostTextStep


@dataclass(frozen=True, slots=True)
class WorkflowPlan:
    version: int
    account: str
    steps: tuple[WorkflowStep, ...]


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
            contents = stream.read(_MAX_WORKFLOW_BYTES + 1)
    except OSError:
        raise StandaloneWorkflowError("WORKFLOW_UNAVAILABLE") from None

    if len(contents) > _MAX_WORKFLOW_BYTES:
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
    ) -> None:
        self._api = api_runtime
        self._mutations = mutation_runtime

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
                else:
                    action = "post_text"
                    value = await self._mutations.publish_text(plan.account, step.text)
            except (
                StandaloneApiError,
                StandaloneMutationError,
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
        if set(step) not in (
            {"action", "thread_id"},
            {"action", "thread_id", "after"},
        ):
            raise StandaloneWorkflowError("INVALID_WORKFLOW")
        thread_id = step["thread_id"]
        if not isinstance(thread_id, str) or _OPAQUE_ID.fullmatch(thread_id) is None:
            raise StandaloneWorkflowError("INVALID_WORKFLOW")
        after: str | None = None
        if "after" in step:
            after_value = step["after"]
            if (
                not isinstance(after_value, str)
                or not 1 <= len(after_value) <= 4096
                or not after_value.strip()
                or "\r" in after_value
                or "\n" in after_value
            ):
                raise StandaloneWorkflowError("INVALID_WORKFLOW")
            after = after_value
        if action == "replies":
            return RepliesStep(thread_id, after)
        return ConversationStep(thread_id, after)
    if action == "post_text":
        if set(step) != {"action", "text"}:
            raise StandaloneWorkflowError("INVALID_WORKFLOW")
        text = step["text"]
        if not isinstance(text, str) or not 1 <= len(text) <= 500 or not text.strip():
            raise StandaloneWorkflowError("INVALID_WORKFLOW")
        return PostTextStep(text)
    raise StandaloneWorkflowError("INVALID_WORKFLOW")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_non_json_constant(value: str) -> NoReturn:
    raise ValueError(value)
