"""Command-line entry point for standalone local accounts."""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections.abc import Sequence

from threads_platform.application.ports.threads import (
    ThreadsAPIError,
    ThreadsCredentialError,
)
from threads_platform.config.settings import Settings
from threads_platform.infrastructure.threads_api.client import HttpThreadsAPI
from threads_platform.infrastructure.threads_api.credentials import (
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
from threads_platform.standalone.runtime import LocalRuntime, StandaloneRuntimeError


def _format_scalar(value: int | str | None) -> str:
    return "-" if value is None else str(value)


def _format_text(value: str | None) -> str:
    if value is None:
        return "-"
    normalized = re.sub(r"\s+", " ", value).strip()
    return normalized[:500] if normalized else "-"


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
    for name in ("replies", "conversation"):
        page_parser = api_commands.add_parser(name)
        page_parser.add_argument("alias")
        page_parser.add_argument("thread_id")
        page_parser.add_argument("--after")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
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

        sys.stdout.write(asyncio.run(_run_api_command(args, store)))
        return 0
    except (
        StandaloneAccountError,
        StandaloneRuntimeError,
        StandaloneApiError,
        ThreadsCredentialError,
        ThreadsAPIError,
    ) as error:
        sys.stderr.write(f"ERROR {error.code}\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("ERROR INTERRUPTED\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
