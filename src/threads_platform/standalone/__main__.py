"""Command-line entry point for standalone local accounts."""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from threads_platform.application.browser_capabilities import (
    BrowserFeedResultV1,
    BrowserTargetOpenResultV1,
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
    CarouselManifest,
    CreatedReplyResult,
    LocalOperationStore,
    LocalThreadsMutationRuntime,
    StandaloneMutationError,
    load_carousel_manifest,
    validate_media_post_inputs,
    validate_reply_moderation_inputs,
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


def _format_feed_result(result: BrowserFeedResultV1) -> str:
    lines = [f"feed count={len(result.observations)} truncated={str(result.truncated).lower()}\n"]
    for item in result.observations:
        lines.append(
            f"item position={item.position} "
            f"thread_ref={_format_bounded_text(item.thread_ref, 255)} "
            f"author={_format_bounded_text(item.author_username, 30)} "
            f"text={_format_bounded_text(item.text_excerpt, 500)}\n"
        )
    return "".join(lines)


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


def _format_discovery_page(
    kind: str,
    page: DiscoveryPage,
    limit: int,
    *,
    workflow_step: int | None = None,
) -> str:
    threads = page.threads[: min(max(limit, 0), 50)]
    header = (
        f"{kind} " if workflow_step is None else f"workflow step={workflow_step} action={kind} "
    )
    thread_prefix = (
        "thread "
        if workflow_step is None
        else f"workflow step={workflow_step} action={kind} thread "
    )
    lines = [
        f"{header}count={len(threads)} "
        f"has_more={str(page.has_more).lower()} "
        f"next_cursor={_format_discovery_cursor(page.next_cursor)}\n"
    ]
    for thread in threads:
        timestamp = thread.timestamp.isoformat() if thread.timestamp is not None else None
        quote = "-" if thread.is_quote_post is None else str(thread.is_quote_post).lower()
        has_replies = "-" if thread.has_replies is None else str(thread.has_replies).lower()
        lines.append(
            f"{thread_prefix}{_format_bounded_text(thread.remote_thread_id, 255)} "
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


async def _run_moderate_reply_command(
    alias: str,
    reply_id: str,
    action: str,
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
        result = await runtime.moderate_reply(alias, reply_id, action)
    return f"moderated operation={result.operation_id} action={result.action}\n"


async def _run_media_post_command(
    args: argparse.Namespace,
    root: Path,
    store: LocalAccountStore,
    *,
    media_type: Literal["IMAGE", "VIDEO"],
) -> str:
    media_url = args.image_url if media_type == "IMAGE" else args.video_url
    validate_media_post_inputs(media_url, args.text, args.alt_text)
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
        if media_type == "IMAGE":
            result = await runtime.publish_image(
                args.alias,
                args.image_url,
                args.text,
                alt_text=args.alt_text,
            )
        else:
            result = await runtime.publish_video(
                args.alias,
                args.video_url,
                args.text,
                alt_text=args.alt_text,
            )
        return (
            f"published-{media_type.lower()} operation={result.operation_id} "
            f"media={result.media_id}\n"
        )


async def _run_carousel_post_command(
    alias: str,
    manifest: CarouselManifest,
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
        result = await runtime.publish_carousel(alias, manifest)
    return f"published-carousel operation={result.operation_id} media={result.media_id}\n"


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
        browser_runtime = LocalRuntime(root, store)
        runtime = LocalWorkflowRuntime(api_runtime, mutation_runtime, browser_runtime)
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
                    f"workflow step={result.index} action={result.action} reply {reply.reply_id} "
                    f"timestamp={_format_scalar(reply.timestamp)} "
                    f"root={_format_scalar(reply.root_post_id)} "
                    f"parent={_format_scalar(reply.replied_to_id)} "
                    f"text={_format_text(reply.text)}\n"
                )
        elif isinstance(value, BrowserTargetOpenResultV1):
            lines.append(
                f"workflow step={result.index} action={result.action} recognized "
                f"target={_format_bounded_text(value.target_ref, 255)}\n"
            )
        elif isinstance(value, BrowserFeedResultV1):
            lines.append(
                f"workflow step={result.index} action=feed count={len(value.observations)} "
                f"truncated={str(value.truncated).lower()}\n"
            )
            for item in value.observations:
                lines.append(
                    f"workflow step={result.index} action=feed item position={item.position} "
                    f"thread_ref={_format_bounded_text(item.thread_ref, 255)} "
                    f"author={_format_bounded_text(item.author_username, 30)} "
                    f"text={_format_bounded_text(item.text_excerpt, 500)}\n"
                )
        elif isinstance(value, RemotePublicProfile):
            profile = _format_public_profile(value)
            lines.append(f"workflow step={result.index} action=public_profile {profile}")
        elif isinstance(value, DiscoveryPage):
            lines.append(
                _format_discovery_page(
                    result.action,
                    value,
                    50,
                    workflow_step=result.index,
                )
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

    profile_parser = commands.add_parser("profile")
    profile_parser.add_argument("alias")
    profile_parser.add_argument("username")
    thread_parser = commands.add_parser("thread")
    thread_parser.add_argument("alias")
    thread_parser.add_argument("thread_ref")
    feed_parser = commands.add_parser("feed")
    feed_parser.add_argument("alias")
    feed_parser.add_argument("--limit", type=int, default=10)

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
    moderation_parser = commands.add_parser("moderate-reply")
    moderation_parser.add_argument("alias")
    moderation_parser.add_argument("reply_id")
    moderation_parser.add_argument("action")
    image_parser = commands.add_parser("post-image")
    image_parser.add_argument("alias")
    image_parser.add_argument("image_url")
    image_parser.add_argument("text", nargs="?")
    image_parser.add_argument("--alt-text")
    video_parser = commands.add_parser("post-video")
    video_parser.add_argument("alias")
    video_parser.add_argument("video_url")
    video_parser.add_argument("text", nargs="?")
    video_parser.add_argument("--alt-text")
    carousel_parser = commands.add_parser("post-carousel")
    carousel_parser.add_argument("alias")
    carousel_parser.add_argument("manifest")

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

        if args.command == "post-carousel":
            manifest = load_carousel_manifest(Path(args.manifest))
            root = resolve_standalone_data_root()
            store = LocalAccountStore(root)
            output = asyncio.run(_run_carousel_post_command(args.alias, manifest, root, store))
            sys.stdout.write(output)
            return 0

        if args.command == "moderate-reply":
            reply_id, action = validate_reply_moderation_inputs(args.reply_id, args.action)
            root = resolve_standalone_data_root()
            store = LocalAccountStore(root)
            store.get(args.alias)
            output = asyncio.run(
                _run_moderate_reply_command(args.alias, reply_id, action, root, store)
            )
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

        if args.command == "profile":
            result = asyncio.run(LocalRuntime(root, store).open_profile(args.alias, args.username))
            sys.stdout.write(f"profile recognized target={result.target_ref}\n")
            return 0

        if args.command == "thread":
            result = asyncio.run(LocalRuntime(root, store).open_thread(args.alias, args.thread_ref))
            sys.stdout.write(f"thread recognized target={result.target_ref}\n")
            return 0

        if args.command == "feed":
            result = asyncio.run(LocalRuntime(root, store).browse_feed(args.alias, args.limit))
            sys.stdout.write(_format_feed_result(result))
            return 0

        if args.command == "operation":
            try:
                operation_id = UUID(args.operation_id)
            except ValueError, AttributeError:
                raise StandaloneMutationError("OPERATION_NOT_FOUND") from None
            operation = LocalOperationStore(root).get(operation_id)
            child_summary = (
                f" children={len(operation.child_container_ids)}"
                if operation.kind == "POST_CAROUSEL"
                else ""
            )
            moderation_summary = (
                f" action={operation.action}" if operation.kind == "MODERATE_REPLY" else ""
            )
            sys.stdout.write(
                f"operation {operation.id} kind={operation.kind} phase={operation.phase} "
                f"container={operation.container_id or '-'} "
                f"media={operation.media_id or '-'} "
                f"outcome={operation.outcome_code or '-'}"
                f"{child_summary}{moderation_summary}\n"
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

        if args.command == "post-image":
            output = asyncio.run(_run_media_post_command(args, root, store, media_type="IMAGE"))
            sys.stdout.write(output)
            return 0

        if args.command == "post-video":
            output = asyncio.run(_run_media_post_command(args, root, store, media_type="VIDEO"))
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
