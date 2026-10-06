"""Command-line entry point for standalone local accounts."""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from threads_platform.application.ports.threads import (
    DiscoveryPage,
    PublishingQuota,
    RemoteMedia,
    RemotePublicProfile,
    ReplyPage,
    ThreadsAPIError,
    ThreadsCredentialError,
)
from threads_platform.config.settings import Settings
from threads_platform.domain.discovery import DiscoverySearchMode, DiscoverySearchType
from threads_platform.infrastructure.threads_api.client import HttpThreadsAPI
from threads_platform.infrastructure.threads_api.environment_credentials import (
    EnvironmentThreadsCredentialSecretResolver,
)
from threads_platform.standalone.accounts import (
    LocalAccountStore,
    StandaloneAccountError,
    resolve_standalone_data_root,
)
from threads_platform.standalone.api import (
    LocalThreadsApiRuntime,
    StandaloneApiError,
    bind_env_credential,
    build_threads_http_client,
)
from threads_platform.standalone.mutations import (
    CreatedReplyResult,
    LocalOperationStore,
    LocalThreadsMutationRuntime,
    StandaloneMutationError,
)
from threads_platform.standalone.runtime import LocalRuntime, StandaloneRuntimeError
from threads_platform.standalone.workflows import (
    LocalWorkflowRuntime,
    StandaloneWorkflowError,
    WorkflowPlan,
    WorkflowStepResult,
    load_workflow,
)


def _format_scalar(value: int | str | None) -> str:
    return "-" if value is None else str(value)


def _format_text(value: str | None) -> str:
    if value is None:
        return "-"
    normalized = re.sub(r"\s+", " ", value).strip()
    return normalized[:500] if normalized else "-"


def _format_bounded_text(value: str | None, limit: int) -> str:
    if value is None:
        return "-"
    safe_value = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    normalized = re.sub(r"\s+", " ", safe_value).strip()
    return normalized[:limit] if normalized else "-"


def _format_https_url(value: str | None) -> str:
    if value is None or len(value) > 2048 or re.search(r"[\s\x00-\x1f\x7f]", value):
        return "-"
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError:
        return "-"
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return "-"
    return value


def _format_discovery_cursor(value: str | None) -> str:
    if value is None or not value.strip() or len(value) > 4096 or not value.isprintable():
        return "-"
    return value


def _format_public_profile(profile: RemotePublicProfile) -> str:
    return (
        f"profile id={_format_bounded_text(profile.remote_author_id, 255)} "
        f"username={_format_bounded_text(profile.username, 255)} "
        f"name={_format_bounded_text(profile.display_name, 255)} "
        f"bio={_format_bounded_text(profile.biography, 500)} "
        f"picture={_format_https_url(profile.profile_picture_url)}\n"
    )


def _format_discovery_page(kind: str, page: DiscoveryPage, limit: int) -> str:
    threads = page.threads[: min(max(limit, 0), 50)]
    lines = [
        f"{kind} count={len(threads)} "
        f"has_more={str(page.has_more).lower()} "
        f"next_cursor={_format_discovery_cursor(page.next_cursor)}\n"
    ]
    for thread in threads:
        timestamp = thread.timestamp.isoformat() if thread.timestamp is not None else None
        quote = "-" if thread.is_quote_post is None else str(thread.is_quote_post).lower()
        has_replies = "-" if thread.has_replies is None else str(thread.has_replies).lower()
        lines.append(
            f"thread {_format_bounded_text(thread.remote_thread_id, 255)} "
            f"username={_format_bounded_text(thread.username, 255)} "
            f"timestamp={_format_bounded_text(timestamp, 64)} "
            f"media_type={_format_bounded_text(thread.media_type, 80)} "
            f"quote={quote} has_replies={has_replies} "
            f"permalink={_format_https_url(thread.permalink)} "
            f"text={_format_bounded_text(thread.text, 500)}\n"
        )
    return "".join(lines)


async def _run_api_command(args: argparse.Namespace, store: LocalAccountStore) -> str:
    settings = Settings()
    async with build_threads_http_client(settings) as client:
        api = HttpThreadsAPI(client)
        secret_resolver = EnvironmentThreadsCredentialSecretResolver()
        runtime = LocalThreadsApiRuntime(store, api, secret_resolver)

        if args.api_command == "quota":
            quota = await runtime.quota(args.alias)
            return (
                f"quota usage={_format_scalar(quota.usage)} "
                f"total={_format_scalar(quota.total)} "
                f"reply_usage={_format_scalar(quota.reply_usage)} "
                f"reply_total={_format_scalar(quota.reply_total)}\n"
            )
        if args.api_command == "media":
            media = await runtime.media(args.alias, args.media_id)
            return (
                f"media {media.media_id}\n"
                f"timestamp {_format_scalar(media.published_at)}\n"
                f"permalink {_format_scalar(media.permalink)}\n"
                f"text {_format_text(media.text)}\n"
            )

        if args.api_command == "public-profile":
            profile = await runtime.public_profile(args.alias, args.username)
            return _format_public_profile(profile)
        if args.api_command == "profile-posts":
            page = await runtime.profile_posts(
                args.alias,
                args.username,
                after=args.after,
                limit=args.limit,
            )
            return _format_discovery_page("profile-posts", page, args.limit)
        if args.api_command == "search":
            page = await runtime.search(
                args.alias,
                args.query,
                search_mode=DiscoverySearchMode(args.mode.upper()),
                search_type=DiscoverySearchType(args.type.upper()),
                after=args.after,
                limit=args.limit,
            )
            return _format_discovery_page("search", page, args.limit)
        if args.api_command == "mentions":
            page = await runtime.mentions(args.alias, after=args.after, limit=args.limit)
            return _format_discovery_page("mentions", page, args.limit)

        if args.api_command == "replies":
            page = await runtime.replies(args.alias, args.thread_id, after=args.after)
            label = "replies"
        else:
            page = await runtime.conversation(args.alias, args.thread_id, after=args.after)
            label = "conversation"
        lines = [
            f"{label} count={len(page.replies)} "
            f"has_more={str(page.has_more).lower()} "
            f"next_cursor={_format_scalar(page.next_cursor)}\n"
        ]
        for reply in page.replies:
            lines.append(
                f"reply {reply.reply_id} "
                f"timestamp={_format_scalar(reply.timestamp)} "
                f"root={_format_scalar(reply.root_post_id)} "
                f"parent={_format_scalar(reply.replied_to_id)} "
                f"text={_format_text(reply.text)}\n"
            )
        return "".join(lines)


async def _run_post_command(
    args: argparse.Namespace,
    root: Path,
    store: LocalAccountStore,
) -> str:
    settings = Settings()
    async with build_threads_http_client(settings) as client:
        api = HttpThreadsAPI(client)
        secret_resolver = EnvironmentThreadsCredentialSecretResolver()
        operations = LocalOperationStore(root)
        runtime = LocalThreadsMutationRuntime(
            root,
            store,
            api,
            secret_resolver,
            operations,
        )
        result = await runtime.publish_text(args.alias, args.text)
        return f"published operation={result.operation_id} media={result.media_id}\n"


async def _run_reply_command(
    args: argparse.Namespace,
    root: Path,
    store: LocalAccountStore,
) -> str:
    settings = Settings()
    async with build_threads_http_client(settings) as client:
        api = HttpThreadsAPI(client)
        secret_resolver = EnvironmentThreadsCredentialSecretResolver()
        runtime = LocalThreadsMutationRuntime(
            root,
            store,
            api,
            secret_resolver,
            LocalOperationStore(root),
        )
        result: CreatedReplyResult = await runtime.create_reply(
            args.alias,
            args.thread_id,
            args.text,
            parent_reply_id=args.parent_reply_id,
        )
        return f"replied operation={result.operation_id} reply={result.reply_id}\n"


async def _run_workflow_command(
    plan: WorkflowPlan,
    root: Path,
    store: LocalAccountStore,
) -> str:
    settings = Settings()
    async with build_threads_http_client(settings) as client:
        api = HttpThreadsAPI(client)
        secret_resolver = EnvironmentThreadsCredentialSecretResolver()
        api_runtime = LocalThreadsApiRuntime(store, api, secret_resolver)
        mutation_runtime = LocalThreadsMutationRuntime(
            root,
            store,
            api,
            secret_resolver,
            LocalOperationStore(root),
        )
        runtime = LocalWorkflowRuntime(api_runtime, mutation_runtime)
        results = await runtime.run(plan)
    return _format_workflow_results(results)


def _format_workflow_results(results: tuple[WorkflowStepResult, ...]) -> str:
    lines: list[str] = []
    for result in results:
        value = result.value
        if isinstance(value, PublishingQuota):
            lines.append(
                f"workflow step={result.index} action=quota "
                f"usage={_format_scalar(value.usage)} total={_format_scalar(value.total)} "
                f"reply_usage={_format_scalar(value.reply_usage)} "
                f"reply_total={_format_scalar(value.reply_total)}\n"
            )
        elif isinstance(value, RemoteMedia):
            lines.append(
                f"workflow step={result.index} action=media id={value.media_id} "
                f"timestamp={_format_scalar(value.published_at)} "
                f"permalink={_format_scalar(value.permalink)} "
                f"text={_format_text(value.text)}\n"
            )
        elif isinstance(value, ReplyPage):
            lines.append(
                f"workflow step={result.index} action={result.action} "
                f"count={len(value.replies)} has_more={str(value.has_more).lower()} "
                f"next_cursor={_format_scalar(value.next_cursor)}\n"
            )
            for reply in value.replies:
                lines.append(
                    f"workflow step={result.index} reply {reply.reply_id} "
                    f"timestamp={_format_scalar(reply.timestamp)} "
                    f"root={_format_scalar(reply.root_post_id)} "
                    f"parent={_format_scalar(reply.replied_to_id)} "
                    f"text={_format_text(reply.text)}\n"
                )
        else:
            assert result.action == "post_text"
            lines.append(
                f"workflow step={result.index} action=post_text published "
                f"operation={value.operation_id} media={value.media_id}\n"
            )
    lines.append(f"workflow completed steps={len(results)}\n")
    return "".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="threads-local")
    commands = parser.add_subparsers(dest="command", required=True)
    account_parser = commands.add_parser("account")
    account_commands = account_parser.add_subparsers(dest="account_command", required=True)
    add_parser = account_commands.add_parser("add")
    add_parser.add_argument("alias")
    account_commands.add_parser("list")
    login_parser = account_commands.add_parser("login")
    login_parser.add_argument("alias")
    credential_parser = account_commands.add_parser("credential")
    credential_commands = credential_parser.add_subparsers(dest="credential_command", required=True)
    set_env_parser = credential_commands.add_parser("set-env")
    set_env_parser.add_argument("alias")
    set_env_parser.add_argument("variable_name")

    api_parser = commands.add_parser("api")
    api_commands = api_parser.add_subparsers(dest="api_command", required=True)
    quota_parser = api_commands.add_parser("quota")
    quota_parser.add_argument("alias")
    media_parser = api_commands.add_parser("media")
    media_parser.add_argument("alias")
    media_parser.add_argument("media_id")
    public_profile_parser = api_commands.add_parser("public-profile")
    public_profile_parser.add_argument("alias")
    public_profile_parser.add_argument("username")
    for name in ("profile-posts", "search", "mentions"):
        page_parser = api_commands.add_parser(name)
        page_parser.add_argument("alias")
        if name == "profile-posts":
            page_parser.add_argument("username")
        elif name == "search":
            page_parser.add_argument("query")
            page_parser.add_argument("--mode", choices=("keyword", "tag"), required=True)
            page_parser.add_argument("--type", choices=("top", "recent"), required=True)
        page_parser.add_argument("--after")
        page_parser.add_argument("--limit", type=int, default=25)
    for name in ("replies", "conversation"):
        page_parser = api_commands.add_parser(name)
        page_parser.add_argument("alias")
        page_parser.add_argument("thread_id")
        page_parser.add_argument("--after")

    post_parser = commands.add_parser("post")
    post_parser.add_argument("alias")
    post_parser.add_argument("text")
    reply_parser = commands.add_parser("reply")
    reply_parser.add_argument("alias")
    reply_parser.add_argument("thread_id")
    reply_parser.add_argument("text")
    reply_parser.add_argument("--parent-reply-id")

    workflow_parser = commands.add_parser("workflow")
    workflow_commands = workflow_parser.add_subparsers(dest="workflow_command", required=True)
    run_workflow_parser = workflow_commands.add_parser("run")
    run_workflow_parser.add_argument("file")

    operation_parser = commands.add_parser("operation")
    operation_commands = operation_parser.add_subparsers(dest="operation_command", required=True)
    show_parser = operation_commands.add_parser("show")
    show_parser.add_argument("operation_id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "workflow":
            plan = load_workflow(Path(args.file))
            root = resolve_standalone_data_root()
            store = LocalAccountStore(root)
            store.get(plan.account)
            output = asyncio.run(_run_workflow_command(plan, root, store))
            sys.stdout.write(output)
            return 0

        root = resolve_standalone_data_root()
        store = LocalAccountStore(root)
        if args.command == "account":
            if args.account_command == "add":
                account = store.add(args.alias)
                sys.stdout.write(f"added {account.alias} {account.id}\n")
                return 0
            if args.account_command == "login":
                runtime = LocalRuntime(root, store)
                asyncio.run(runtime.login(args.alias))
                sys.stdout.write(f"login browser closed {args.alias}\n")
                return 0
            if args.account_command == "credential":
                bind_env_credential(store, args.alias, args.variable_name)
                sys.stdout.write(f"credential configured {args.alias}\n")
                return 0
            for account in store.list():
                sys.stdout.write(f"{account.alias} {account.id}\n")
            return 0

        if args.command == "operation":
            try:
                operation_id = UUID(args.operation_id)
            except ValueError, AttributeError:
                raise StandaloneMutationError("OPERATION_NOT_FOUND") from None
            operation = LocalOperationStore(root).get(operation_id)
            sys.stdout.write(
                f"operation {operation.id} kind={operation.kind} phase={operation.phase} "
                f"container={operation.container_id or '-'} "
                f"media={operation.media_id or '-'} "
                f"outcome={operation.outcome_code or '-'}\n"
            )
            return 0

        if args.command == "post":
            output = asyncio.run(_run_post_command(args, root, store))
            sys.stdout.write(output)
            return 0

        if args.command == "reply":
            output = asyncio.run(_run_reply_command(args, root, store))
            sys.stdout.write(output)
            return 0

        sys.stdout.write(asyncio.run(_run_api_command(args, store)))
        return 0
    except StandaloneWorkflowError as error:
        suffix = f" step={error.step_index}" if error.step_index is not None else ""
        if error.operation_id is not None:
            suffix += f" operation={error.operation_id}"
        sys.stderr.write(f"ERROR {error.code}{suffix}\n")
        return 1
    except (
        StandaloneAccountError,
        StandaloneRuntimeError,
        StandaloneApiError,
        StandaloneMutationError,
        ThreadsCredentialError,
        ThreadsAPIError,
    ) as error:
        operation_id = getattr(error, "operation_id", None)
        suffix = f" operation={operation_id}" if operation_id is not None else ""
        sys.stderr.write(f"ERROR {error.code}{suffix}\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("ERROR INTERRUPTED\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
