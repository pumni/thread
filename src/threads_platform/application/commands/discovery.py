from datetime import UTC, datetime
from hashlib import sha256
from typing import NoReturn
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import SecretStr

from threads_platform.application.commands.conversations import ThreadsConversationHandler
from threads_platform.application.commands.handlers import (
    CommandExecutionContext,
    CommandExecutionOutput,
)
from threads_platform.application.crm_protocol_v1 import CommandEnvelopeV1
from threads_platform.application.errors import PermanentCommandError, RetryableCommandError
from threads_platform.application.ports.repositories import DiscoveryRepository, UnitOfWorkFactory
from threads_platform.application.ports.threads import (
    DiscoveryPage,
    RemoteDiscoveryThread,
    RemoteReply,
    ReplyPage,
    ThreadsAccessTokenProvider,
    ThreadsAPI,
    ThreadsAPIError,
    ThreadsContractError,
)
from threads_platform.domain.discovery import (
    DiscoveredAuthor,
    DiscoveredThread,
    DiscoveryCampaign,
    DiscoveryCampaignStatus,
    DiscoveryEnrichmentStatus,
    DiscoveryEvidenceClass,
    DiscoveryEvidenceSource,
    DiscoveryQueryKind,
    DiscoveryRun,
    DiscoveryRunCursor,
    DiscoveryRunStatus,
    DiscoverySearchMode,
    DiscoverySearchType,
    DiscoverySourceEvidence,
    LeadCandidate,
    LeadCandidateEvidence,
    LeadCandidateStatus,
    SearchQuery,
)
from threads_platform.domain.time import utc_now

_RETRYABLE_API_CODES = frozenset(
    {"THREADS_TRANSPORT_FAILURE", "THREADS_RATE_LIMITED", "THREADS_SERVER_ERROR"}
)
_API_PAGE_SIZE = 50
_MAX_PAGES_PER_EXECUTION = 5


class ThreadsDiscoveryHandler:
    def __init__(
        self,
        api: ThreadsAPI,
        token_provider: ThreadsAccessTokenProvider,
        unit_of_work_factory: UnitOfWorkFactory,
    ) -> None:
        self._api = api
        self._token_provider = token_provider
        self._unit_of_work_factory = unit_of_work_factory

    async def execute(
        self, command: CommandEnvelopeV1, context: CommandExecutionContext
    ) -> CommandExecutionOutput:
        del context
        if command.command_type == "threads.discovery.create_campaign":
            return await self._create_campaign(command)
        if command.command_type == "threads.discovery.complete_campaign":
            return await self._complete_campaign(command)
        if command.command_type == "threads.discovery.lead_status":
            return await self._change_lead_status(command)

        token = await self._token_provider.get_access_token(command.account_id)
        if command.command_type == "threads.discovery.resume":
            return await self._resume(command, token)
        query = self._query_from_command(command)
        run, query = await self._start_run(command, query)
        if run.status is DiscoveryRunStatus.SUCCEEDED:
            return await self._with_profile_candidate(self._run_output(run), run, query)
        if query.kind is DiscoveryQueryKind.CONVERSATION:
            return await self._run_conversation(token, run, query)
        if query.kind is DiscoveryQueryKind.PROFILE and not run.profile_lookup_complete:
            await self._lookup_profile(token, run, query)
        max_pages = min(self._max_pages(command), _MAX_PAGES_PER_EXECUTION)
        output = await self._run_discovery_pages(command, token, run, query, max_pages)
        return await self._with_profile_candidate(output, run, query)

    async def _create_campaign(self, command: CommandEnvelopeV1) -> CommandExecutionOutput:
        payload = command.payload.model_dump(mode="python")
        campaign_id = uuid5(
            NAMESPACE_URL,
            f"threads.discovery.campaign:{command.account_id}:{command.command_id}",
        )
        async with self._unit_of_work_factory() as unit_of_work:
            existing = await unit_of_work.discovery.get_campaign(campaign_id)
            if existing is None:
                campaign = DiscoveryCampaign(
                    id=campaign_id,
                    account_id=command.account_id,
                    name=str(payload["name"]),
                )
                await unit_of_work.discovery.add_campaign(campaign)
        return CommandExecutionOutput(result={"campaign_id": str(campaign_id), "status": "ACTIVE"})

    async def _complete_campaign(self, command: CommandEnvelopeV1) -> CommandExecutionOutput:
        payload = command.payload.model_dump(mode="python")
        campaign_id = UUID(str(payload["campaign_id"]))
        async with self._unit_of_work_factory() as unit_of_work:
            campaign = await unit_of_work.discovery.get_campaign_for_update(campaign_id)
            if campaign is None or campaign.account_id != command.account_id:
                raise PermanentCommandError("DISCOVERY_CAMPAIGN_NOT_FOUND")
            campaign.status = DiscoveryCampaignStatus.COMPLETE
            campaign.updated_at = utc_now()
            await unit_of_work.discovery.update_campaign(campaign)
        return CommandExecutionOutput(
            result={"campaign_id": str(campaign_id), "status": "COMPLETE"}
        )

    async def _change_lead_status(self, command: CommandEnvelopeV1) -> CommandExecutionOutput:
        payload = command.payload.model_dump(mode="python")
        candidate_id = UUID(str(payload["candidate_id"]))
        async with self._unit_of_work_factory() as unit_of_work:
            repository = unit_of_work.discovery
            candidate = await repository.get_candidate_for_update(candidate_id)
            if candidate is None or candidate.account_id != command.account_id:
                raise PermanentCommandError("LEAD_CANDIDATE_NOT_FOUND")
            if await repository.has_candidate_transition(command.command_id):
                return CommandExecutionOutput(
                    result={"candidate_id": str(candidate.id), "status": candidate.status.value}
                )
            try:
                transition = candidate.transition(
                    LeadCandidateStatus(str(payload["target_status"])),
                    command_id=command.command_id,
                    reason_code=str(payload["reason_code"]),
                    occurred_at=utc_now(),
                )
            except ValueError as error:
                raise PermanentCommandError("LEAD_STATUS_TRANSITION_REJECTED") from error
            await repository.update_candidate(candidate)
            await repository.add_candidate_transition(transition)
        return CommandExecutionOutput(
            result={"candidate_id": str(candidate_id), "status": candidate.status.value}
        )

    async def _resume(self, command: CommandEnvelopeV1, token: SecretStr) -> CommandExecutionOutput:
        payload = command.payload.model_dump(mode="python")
        run_id = UUID(str(payload["run_id"]))
        async with self._unit_of_work_factory() as unit_of_work:
            run = await unit_of_work.discovery.get_run_for_update(run_id)
            if run is None or run.account_id != command.account_id:
                raise PermanentCommandError("DISCOVERY_RUN_NOT_FOUND")
            query = await unit_of_work.discovery.get_query(run.query_id)
            if query is None:
                raise PermanentCommandError("DISCOVERY_QUERY_NOT_FOUND")
            campaign = await unit_of_work.discovery.get_campaign(run.campaign_id)
            if campaign is None or campaign.status is not DiscoveryCampaignStatus.ACTIVE:
                raise PermanentCommandError("DISCOVERY_CAMPAIGN_NOT_ACTIVE")
            if run.status is DiscoveryRunStatus.SUCCEEDED:
                succeeded_run = run
            else:
                succeeded_run = None
            if run.status is DiscoveryRunStatus.FAILED_FINAL:
                raise PermanentCommandError(run.error_code or "DISCOVERY_RUN_FAILED")
            if succeeded_run is None:
                run.status = DiscoveryRunStatus.RUNNING
                run.error_code = None
                run.finished_at = None
                run.updated_at = utc_now()
                await unit_of_work.discovery.update_run(run)
        if succeeded_run is not None:
            return await self._with_profile_candidate(self._run_output(succeeded_run), run, query)
        if query.kind is DiscoveryQueryKind.PROFILE and not run.profile_lookup_complete:
            await self._lookup_profile(token, run, query)
        if query.kind is DiscoveryQueryKind.CONVERSATION:
            return await self._run_conversation(
                token,
                run,
                query,
                min(int(payload["max_pages"]), _MAX_PAGES_PER_EXECUTION),
            )
        output = await self._run_discovery_pages(
            command,
            token,
            run,
            query,
            min(int(payload["max_pages"]), _MAX_PAGES_PER_EXECUTION),
        )
        return await self._with_profile_candidate(output, run, query)

    async def _start_run(
        self, command: CommandEnvelopeV1, query: SearchQuery
    ) -> tuple[DiscoveryRun, SearchQuery]:
        async with self._unit_of_work_factory() as unit_of_work:
            repository = unit_of_work.discovery
            campaign = await repository.get_campaign_for_update(query.campaign_id)
            if campaign is None or campaign.account_id != command.account_id:
                raise PermanentCommandError("DISCOVERY_CAMPAIGN_NOT_FOUND")
            if campaign.status is not DiscoveryCampaignStatus.ACTIVE:
                raise PermanentCommandError("DISCOVERY_CAMPAIGN_NOT_ACTIVE")
            query = await repository.get_or_create_query(query)
            run = await repository.get_run_by_command_id(command.command_id)
            if run is None:
                run = DiscoveryRun(
                    id=uuid5(NAMESPACE_URL, f"threads.discovery.run:{command.command_id}"),
                    account_id=command.account_id,
                    campaign_id=campaign.id,
                    query_id=query.id,
                    command_id=command.command_id,
                    profile_lookup_complete=query.kind is not DiscoveryQueryKind.PROFILE,
                )
                if not await repository.add_run_if_absent(run):
                    run = await repository.get_run_by_command_id(command.command_id)
                    if run is None:
                        raise RuntimeError("discovery run disappeared after insert conflict")
            elif run.query_id != query.id or run.account_id != command.account_id:
                raise PermanentCommandError("DISCOVERY_RUN_IDEMPOTENCY_CONFLICT")
            elif run.status is DiscoveryRunStatus.FAILED_RETRYABLE:
                run.status = DiscoveryRunStatus.RUNNING
                run.error_code = None
                run.finished_at = None
                run.updated_at = utc_now()
                await repository.update_run(run)
            elif run.status is DiscoveryRunStatus.FAILED_FINAL:
                raise PermanentCommandError(run.error_code or "DISCOVERY_RUN_FAILED")
        return run, query

    async def _lookup_profile(
        self, token: SecretStr, run: DiscoveryRun, query: SearchQuery
    ) -> None:
        if query.username is None:
            raise PermanentCommandError("DISCOVERY_PROFILE_QUERY_INVALID")
        try:
            profile = await self._api.get_public_profile(token, query.username)
        except ThreadsAPIError as error:
            await self._mark_api_failure(run.id, error)
            self._raise_api_error(error)
        observed_at = utc_now()
        async with self._unit_of_work_factory() as unit_of_work:
            repository = unit_of_work.discovery
            current = await repository.get_run_for_update(run.id)
            if current is None:
                raise PermanentCommandError("DISCOVERY_RUN_NOT_FOUND")
            if current.profile_lookup_complete:
                return
            try:
                profile_author = DiscoveredAuthor(
                    remote_author_id=profile.remote_author_id,
                    username=profile.username,
                    display_name=profile.display_name,
                    biography=profile.biography,
                    profile_picture_url=profile.profile_picture_url,
                    enrichment_status=(
                        DiscoveryEnrichmentStatus.ENRICHED
                        if any(
                            (
                                profile.display_name,
                                profile.biography,
                                profile.profile_picture_url,
                            )
                        )
                        else DiscoveryEnrichmentStatus.ENRICHMENT_NEEDED
                    ),
                    last_enriched_at=observed_at,
                    updated_at=observed_at,
                )
            except ValueError:
                raise PermanentCommandError("THREADS_DOCUMENTATION_CONTRACT_MISMATCH") from None
            author = await repository.upsert_author(profile_author)
            await repository.attach_author_to_threads_by_username(author.username, author.id)
            evidence = DiscoverySourceEvidence(
                id=uuid5(NAMESPACE_URL, f"{run.id}:profile:{author.id}"),
                run_id=run.id,
                source=DiscoveryEvidenceSource.PUBLIC_PROFILE_LOOKUP,
                author_id=author.id,
                page_number=0,
                observed_at=observed_at,
                evidence_class=DiscoveryEvidenceClass.DOCUMENTATION_CONTRACT,
            )
            await repository.add_evidence(evidence)
            candidate = LeadCandidate(
                account_id=run.account_id,
                author_id=author.id,
                created_at=observed_at,
                updated_at=observed_at,
            )
            await repository.add_candidate_if_absent(candidate)
            candidate = await repository.get_candidate_by_author(run.account_id, author.id)
            if candidate is None:
                raise RuntimeError("lead candidate disappeared after insert")
            for evidence_id in await repository.list_evidence_for_author(
                candidate.account_id, author.id
            ):
                await repository.add_candidate_evidence(
                    LeadCandidateEvidence(
                        id=uuid5(NAMESPACE_URL, f"{candidate.id}:{evidence_id}"),
                        candidate_id=candidate.id,
                        evidence_id=evidence_id,
                    )
                )
            current.profile_lookup_complete = True
            current.updated_at = observed_at
            await repository.update_run(current)

    async def _run_discovery_pages(
        self,
        command: CommandEnvelopeV1,
        token: SecretStr,
        run: DiscoveryRun,
        query: SearchQuery,
        max_pages: int,
    ) -> CommandExecutionOutput:
        pages_this_execution = 0
        while pages_this_execution < max_pages:
            async with self._unit_of_work_factory() as unit_of_work:
                current = await unit_of_work.discovery.get_run(run.id)
            if current is None:
                raise PermanentCommandError("DISCOVERY_RUN_NOT_FOUND")
            if current.status is DiscoveryRunStatus.SUCCEEDED:
                return self._run_output(current)
            if current.status is DiscoveryRunStatus.FAILED_FINAL:
                raise PermanentCommandError(current.error_code or "DISCOVERY_RUN_FAILED")
            if current.status is DiscoveryRunStatus.FAILED_RETRYABLE:
                raise RetryableCommandError(current.error_code or "DISCOVERY_RUN_RETRYABLE")
            if query.kind is DiscoveryQueryKind.PROFILE:
                if query.username is None:
                    raise PermanentCommandError("DISCOVERY_PROFILE_QUERY_INVALID")
                try:
                    page = await self._api.get_profile_posts(
                        token, query.username, after=current.cursor, limit=_API_PAGE_SIZE
                    )
                except ThreadsAPIError as error:
                    await self._mark_api_failure(current.id, error)
                    self._raise_api_error(error)
            elif query.kind is DiscoveryQueryKind.MENTIONS:
                try:
                    page = await self._api.get_mentions(
                        token,
                        after=current.cursor,
                        since=query.since,
                        until=query.until,
                        limit=_API_PAGE_SIZE,
                    )
                except ThreadsAPIError as error:
                    await self._mark_api_failure(current.id, error)
                    self._raise_api_error(error)
            elif query.kind is DiscoveryQueryKind.SEARCH:
                if (
                    query.query_text is None
                    or query.search_mode is None
                    or query.search_type is None
                ):
                    raise PermanentCommandError("DISCOVERY_SEARCH_QUERY_INVALID")
                try:
                    page = await self._api.search_threads(
                        token,
                        query.query_text,
                        search_mode=query.search_mode,
                        search_type=query.search_type,
                        after=current.cursor,
                        since=query.since,
                        until=query.until,
                        limit=_API_PAGE_SIZE,
                    )
                except ThreadsAPIError as error:
                    await self._mark_api_failure(current.id, error)
                    self._raise_api_error(error)
            else:
                raise PermanentCommandError("DISCOVERY_QUERY_KIND_UNSUPPORTED")
            persisted = await self._persist_discovery_page(current, query, page)
            if persisted is None:
                continue
            pages_this_execution += 1
            run = persisted
            if run.status is DiscoveryRunStatus.FAILED_FINAL:
                raise PermanentCommandError(run.error_code or "DISCOVERY_RUN_FAILED")
            if run.status is DiscoveryRunStatus.FAILED_RETRYABLE:
                raise RetryableCommandError(run.error_code or "DISCOVERY_RUN_RETRYABLE")
            if run.status is DiscoveryRunStatus.SUCCEEDED:
                return self._run_output(run)
        async with self._unit_of_work_factory() as unit_of_work:
            latest = await unit_of_work.discovery.get_run_for_update(run.id)
            if latest is not None and latest.status is DiscoveryRunStatus.RUNNING:
                latest.status = DiscoveryRunStatus.PAUSED
                latest.updated_at = utc_now()
                await unit_of_work.discovery.update_run(latest)
                run = latest
            elif latest is not None:
                run = latest
        return self._run_output(run)

    async def _persist_discovery_page(
        self,
        expected: DiscoveryRun,
        query: SearchQuery,
        page: DiscoveryPage,
    ) -> DiscoveryRun | None:
        now = utc_now()
        source = self._evidence_source(query)
        async with self._unit_of_work_factory() as unit_of_work:
            repository = unit_of_work.discovery
            run = await repository.get_run_for_update(expected.id)
            if run is None:
                raise PermanentCommandError("DISCOVERY_RUN_NOT_FOUND")
            if run.cursor != expected.cursor or run.pages_processed != expected.pages_processed:
                return None
            if run.status in {
                DiscoveryRunStatus.SUCCEEDED,
                DiscoveryRunStatus.FAILED_RETRYABLE,
                DiscoveryRunStatus.FAILED_FINAL,
            }:
                return run
            if page.next_cursor is not None and len(page.next_cursor) > 4096:
                run.status = DiscoveryRunStatus.FAILED_FINAL
                run.error_code = "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"
                run.finished_at = now
                run.updated_at = now
                await repository.update_run(run)
                return run
            if page.has_more and (page.next_cursor is None or page.next_cursor == run.cursor):
                run.status = DiscoveryRunStatus.FAILED_FINAL
                run.error_code = "DISCOVERY_CURSOR_STALLED"
                run.finished_at = now
                run.updated_at = now
                await repository.update_run(run)
                return run
            page_number = run.pages_processed + 1
            if page.has_more and page.next_cursor is not None:
                inserted = await repository.add_run_cursor(
                    DiscoveryRunCursor(
                        run_id=run.id,
                        cursor_digest=sha256(page.next_cursor.encode("utf-8")).hexdigest(),
                        page_number=page_number,
                    )
                )
                if not inserted:
                    run.status = DiscoveryRunStatus.FAILED_FINAL
                    run.error_code = "DISCOVERY_CURSOR_STALLED"
                    run.finished_at = now
                    run.updated_at = now
                    await repository.update_run(run)
                    return run
            for remote in page.threads:
                await self._persist_discovered_thread(
                    repository, run, remote, source, page_number, now
                )
            run.cursor = page.next_cursor
            run.pages_processed = page_number
            run.items_processed += len(page.threads)
            run.status = (
                DiscoveryRunStatus.RUNNING if page.has_more else DiscoveryRunStatus.SUCCEEDED
            )
            run.finished_at = None if page.has_more else now
            run.error_code = None
            run.updated_at = now
            await repository.update_run(run)
            return run

    async def _persist_discovered_thread(
        self,
        repository: DiscoveryRepository,
        run: DiscoveryRun,
        remote: RemoteDiscoveryThread,
        source: DiscoveryEvidenceSource,
        page_number: int,
        observed_at: datetime,
    ) -> None:
        author = None
        try:
            if remote.author_remote_id and remote.username:
                author = await repository.upsert_author(
                    DiscoveredAuthor(
                        remote_author_id=remote.author_remote_id,
                        username=remote.username,
                        updated_at=observed_at,
                    )
                )
            elif remote.username:
                author = await repository.get_author_by_username(remote.username)
            discovered_thread = DiscoveredThread(
                remote_thread_id=remote.remote_thread_id,
                author_id=author.id if author is not None else None,
                username=remote.username,
                text=remote.text,
                permalink=remote.permalink,
                media_type=remote.media_type,
                remote_created_at=remote.timestamp,
                is_quote_post=remote.is_quote_post,
                has_replies=remote.has_replies,
                enrichment_status=(
                    DiscoveryEnrichmentStatus.ENRICHED
                    if remote.text is not None or remote.permalink is not None
                    else DiscoveryEnrichmentStatus.ENRICHMENT_NEEDED
                ),
                updated_at=observed_at,
            )
        except ValueError:
            raise PermanentCommandError("THREADS_DOCUMENTATION_CONTRACT_MISMATCH") from None
        thread = await repository.upsert_thread(discovered_thread)
        thread_evidence = DiscoverySourceEvidence(
            id=uuid5(NAMESPACE_URL, f"{run.id}:{page_number}:thread:{thread.id}"),
            run_id=run.id,
            source=source,
            thread_id=thread.id,
            page_number=page_number,
            observed_at=observed_at,
        )
        await repository.add_evidence(thread_evidence)
        if author is not None:
            author_evidence = DiscoverySourceEvidence(
                id=uuid5(NAMESPACE_URL, f"{run.id}:{page_number}:author:{author.id}"),
                run_id=run.id,
                source=source,
                author_id=author.id,
                page_number=page_number,
                observed_at=observed_at,
            )
            await repository.add_evidence(author_evidence)
            candidate = await repository.get_candidate_by_author(run.account_id, author.id)
            if candidate is not None:
                for item in (thread_evidence, author_evidence):
                    await repository.add_candidate_evidence(
                        LeadCandidateEvidence(
                            id=uuid5(NAMESPACE_URL, f"{candidate.id}:{item.id}"),
                            candidate_id=candidate.id,
                            evidence_id=item.id,
                        )
                    )

    async def _run_conversation(
        self,
        token: SecretStr,
        run: DiscoveryRun,
        query: SearchQuery,
        max_pages: int = 3,
    ) -> CommandExecutionOutput:
        if query.thread_remote_id is None:
            raise PermanentCommandError("DISCOVERY_CONVERSATION_QUERY_INVALID")
        async with self._unit_of_work_factory() as unit_of_work:
            root = await unit_of_work.discovery.get_thread_by_remote_id(query.thread_remote_id)
        if root is None:
            try:
                remote_root = await self._api.get_media(token, query.thread_remote_id)
            except ThreadsAPIError as error:
                await self._mark_api_failure(run.id, error)
                self._raise_api_error(error)
            remote_timestamp = self._parse_timestamp(remote_root.published_at)
            now = utc_now()
            async with self._unit_of_work_factory() as unit_of_work:
                try:
                    discovered_root = DiscoveredThread(
                        remote_thread_id=remote_root.media_id,
                        text=remote_root.text,
                        permalink=remote_root.permalink,
                        remote_created_at=remote_timestamp,
                        updated_at=now,
                    )
                except ValueError:
                    raise PermanentCommandError("THREADS_DOCUMENTATION_CONTRACT_MISMATCH") from None
                root = await unit_of_work.discovery.upsert_thread(discovered_root)
                await unit_of_work.discovery.add_evidence(
                    DiscoverySourceEvidence(
                        run_id=run.id,
                        source=DiscoveryEvidenceSource.CONVERSATION,
                        thread_id=root.id,
                        page_number=0,
                        observed_at=now,
                    )
                )
        pages_this_execution = 0
        while pages_this_execution < max_pages:
            async with self._unit_of_work_factory() as unit_of_work:
                current = await unit_of_work.discovery.get_run(run.id)
            if current is None:
                raise PermanentCommandError("DISCOVERY_RUN_NOT_FOUND")
            if current.status is DiscoveryRunStatus.SUCCEEDED:
                return self._run_output(current)
            if current.status is DiscoveryRunStatus.FAILED_FINAL:
                raise PermanentCommandError(current.error_code or "DISCOVERY_RUN_FAILED")
            if current.status is DiscoveryRunStatus.FAILED_RETRYABLE:
                raise RetryableCommandError(current.error_code or "DISCOVERY_RUN_RETRYABLE")
            try:
                page = await self._api.get_conversation(
                    token, query.thread_remote_id, current.cursor
                )
            except ThreadsAPIError as error:
                await self._mark_api_failure(current.id, error)
                self._raise_api_error(error)
            persisted = await self._persist_conversation_page(
                current, root, page, query.thread_remote_id
            )
            if persisted is None:
                continue
            pages_this_execution += 1
            run = persisted
            if run.status is DiscoveryRunStatus.FAILED_FINAL:
                raise PermanentCommandError(run.error_code or "DISCOVERY_RUN_FAILED")
            if run.status is DiscoveryRunStatus.FAILED_RETRYABLE:
                raise RetryableCommandError(run.error_code or "DISCOVERY_RUN_RETRYABLE")
            if run.status is DiscoveryRunStatus.SUCCEEDED:
                output = self._run_output(run)
                output.result["missing_parent_skipped"] = run.items_skipped
                return output
        async with self._unit_of_work_factory() as unit_of_work:
            latest = await unit_of_work.discovery.get_run_for_update(run.id)
            if latest is not None and latest.status is DiscoveryRunStatus.RUNNING:
                latest.status = DiscoveryRunStatus.PAUSED
                latest.updated_at = utc_now()
                await unit_of_work.discovery.update_run(latest)
                run = latest
            elif latest is not None:
                run = latest
        output = self._run_output(run)
        output.result["missing_parent_skipped"] = run.items_skipped
        return output

    async def _persist_conversation_page(
        self,
        expected: DiscoveryRun,
        root: DiscoveredThread,
        page: ReplyPage,
        root_remote_id: str,
    ) -> DiscoveryRun | None:
        now = utc_now()
        async with self._unit_of_work_factory() as unit_of_work:
            repository = unit_of_work.discovery
            run = await repository.get_run_for_update(expected.id)
            if run is None:
                raise PermanentCommandError("DISCOVERY_RUN_NOT_FOUND")
            if run.cursor != expected.cursor or run.pages_processed != expected.pages_processed:
                return None
            if run.status in {
                DiscoveryRunStatus.SUCCEEDED,
                DiscoveryRunStatus.FAILED_RETRYABLE,
                DiscoveryRunStatus.FAILED_FINAL,
            }:
                return run
            if page.next_cursor is not None and len(page.next_cursor) > 4096:
                run.status = DiscoveryRunStatus.FAILED_FINAL
                run.error_code = "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"
                run.finished_at = now
                run.updated_at = now
                await repository.update_run(run)
                return run
            if page.has_more and (page.next_cursor is None or page.next_cursor == run.cursor):
                run.status = DiscoveryRunStatus.FAILED_FINAL
                run.error_code = "DISCOVERY_CURSOR_STALLED"
                run.finished_at = now
                run.updated_at = now
                await repository.update_run(run)
                return run
            page_number = run.pages_processed + 1
            if page.has_more and page.next_cursor is not None:
                inserted = await repository.add_run_cursor(
                    DiscoveryRunCursor(
                        run_id=run.id,
                        cursor_digest=sha256(page.next_cursor.encode("utf-8")).hexdigest(),
                        page_number=page_number,
                    )
                )
                if not inserted:
                    run.status = DiscoveryRunStatus.FAILED_FINAL
                    run.error_code = "DISCOVERY_CURSOR_STALLED"
                    run.finished_at = now
                    run.updated_at = now
                    await repository.update_run(run)
                    return run
            existing_replies = await unit_of_work.replies.list_for_discovered_thread(
                run.account_id, root.id
            )
            existing_by_id = {reply.threads_reply_id: reply for reply in existing_replies}
            remote_by_id: dict[str, RemoteReply] = {}
            for reply in page.replies:
                remote_by_id.setdefault(reply.reply_id, reply)
            new_replies, skipped = ThreadsConversationHandler.map_replies(
                run.account_id,
                None,
                remote_by_id,
                existing_by_id,
                root_remote_id=root_remote_id,
                discovered_thread_id=root.id,
            )
            for reply in new_replies:
                await unit_of_work.replies.add_if_absent(reply)
            await repository.add_evidence(
                DiscoverySourceEvidence(
                    id=uuid5(NAMESPACE_URL, f"{run.id}:{page_number}:conversation:{root.id}"),
                    run_id=run.id,
                    source=DiscoveryEvidenceSource.CONVERSATION,
                    thread_id=root.id,
                    page_number=page_number,
                    observed_at=now,
                )
            )
            root.conversation_status = (
                DiscoveryEnrichmentStatus.ENRICHED
                if not page.has_more
                else DiscoveryEnrichmentStatus.ENRICHMENT_NEEDED
            )
            root.updated_at = now
            await repository.update_thread(root)
            run.cursor = page.next_cursor
            run.pages_processed = page_number
            run.items_processed += len(page.replies)
            run.items_skipped += skipped
            run.status = (
                DiscoveryRunStatus.RUNNING if page.has_more else DiscoveryRunStatus.SUCCEEDED
            )
            run.finished_at = None if page.has_more else now
            run.error_code = None
            run.updated_at = now
            await repository.update_run(run)
            return run

    async def _mark_api_failure(self, run_id: UUID, error: ThreadsAPIError) -> None:
        status = (
            DiscoveryRunStatus.FAILED_RETRYABLE
            if error.code in _RETRYABLE_API_CODES
            else DiscoveryRunStatus.FAILED_FINAL
        )
        async with self._unit_of_work_factory() as unit_of_work:
            run = await unit_of_work.discovery.get_run_for_update(run_id)
            if run is None:
                return
            if run.status in {
                DiscoveryRunStatus.SUCCEEDED,
                DiscoveryRunStatus.FAILED_FINAL,
            }:
                return
            run.status = status
            run.error_code = error.code
            run.updated_at = utc_now()
            run.finished_at = (
                None if status is DiscoveryRunStatus.FAILED_RETRYABLE else run.updated_at
            )
            await unit_of_work.discovery.update_run(run)

    def _query_from_command(self, command: CommandEnvelopeV1) -> SearchQuery:
        payload = command.payload.model_dump(mode="python")
        campaign_id = UUID(str(payload["campaign_id"]))
        try:
            if command.command_type == "threads.discovery.search":
                return SearchQuery(
                    campaign_id=campaign_id,
                    kind=DiscoveryQueryKind.SEARCH,
                    query_text=str(payload["query"]),
                    search_mode=DiscoverySearchMode(str(payload["search_mode"])),
                    search_type=DiscoverySearchType(str(payload["search_type"])),
                    since=payload.get("since"),  # type: ignore[arg-type]
                    until=payload.get("until"),  # type: ignore[arg-type]
                )
            if command.command_type == "threads.discovery.profile":
                return SearchQuery(
                    campaign_id=campaign_id,
                    kind=DiscoveryQueryKind.PROFILE,
                    username=str(payload["username"]),
                )
            if command.command_type == "threads.discovery.mentions":
                return SearchQuery(
                    campaign_id=campaign_id,
                    kind=DiscoveryQueryKind.MENTIONS,
                    since=payload.get("since"),  # type: ignore[arg-type]
                    until=payload.get("until"),  # type: ignore[arg-type]
                )
            if command.command_type == "threads.discovery.conversation":
                return SearchQuery(
                    campaign_id=campaign_id,
                    kind=DiscoveryQueryKind.CONVERSATION,
                    thread_remote_id=str(payload["thread_remote_id"]),
                )
        except (TypeError, ValueError) as error:
            raise PermanentCommandError("DISCOVERY_QUERY_INVALID") from error
        raise PermanentCommandError("UNKNOWN_DISCOVERY_COMMAND")

    @staticmethod
    def _max_pages(command: CommandEnvelopeV1) -> int:
        payload = command.payload.model_dump(mode="python")
        return int(payload.get("max_pages", 3))

    @staticmethod
    def _evidence_source(query: SearchQuery) -> DiscoveryEvidenceSource:
        if query.kind is DiscoveryQueryKind.SEARCH:
            return (
                DiscoveryEvidenceSource.TAG_SEARCH
                if query.search_mode is DiscoverySearchMode.TAG
                else DiscoveryEvidenceSource.KEYWORD_SEARCH
            )
        if query.kind is DiscoveryQueryKind.PROFILE:
            return DiscoveryEvidenceSource.PROFILE_POSTS
        if query.kind is DiscoveryQueryKind.MENTIONS:
            return DiscoveryEvidenceSource.MENTIONS
        return DiscoveryEvidenceSource.CONVERSATION

    @staticmethod
    def _run_output(run: DiscoveryRun) -> CommandExecutionOutput:
        return CommandExecutionOutput(
            result={
                "run_id": str(run.id),
                "status": run.status.value,
                "pages_processed": run.pages_processed,
                "items_processed": run.items_processed,
                "items_skipped": run.items_skipped,
                "resume_required": run.status is DiscoveryRunStatus.PAUSED,
                "evidence_class": DiscoveryEvidenceClass.DOCUMENTATION_CONTRACT.value,
            }
        )

    async def _with_profile_candidate(
        self,
        output: CommandExecutionOutput,
        run: DiscoveryRun,
        query: SearchQuery,
    ) -> CommandExecutionOutput:
        if query.kind is not DiscoveryQueryKind.PROFILE:
            return output
        async with self._unit_of_work_factory() as unit_of_work:
            author = await unit_of_work.discovery.get_profile_author_for_run(run.id)
            if author is None:
                return output
            candidate = await unit_of_work.discovery.get_candidate_by_author(
                run.account_id, author.id
            )
        if candidate is not None:
            output.result["lead_candidate_id"] = str(candidate.id)
        return output

    @staticmethod
    def _parse_timestamp(value: str | None) -> datetime | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise PermanentCommandError("THREADS_DOCUMENTATION_CONTRACT_MISMATCH") from error
        if parsed.tzinfo is None:
            raise PermanentCommandError("THREADS_DOCUMENTATION_CONTRACT_MISMATCH")
        return parsed.astimezone(UTC)

    @staticmethod
    def _raise_api_error(error: ThreadsAPIError) -> NoReturn:
        if error.code in _RETRYABLE_API_CODES:
            raise RetryableCommandError(error.code, error.retry_after) from error
        if isinstance(error, ThreadsContractError):
            raise PermanentCommandError(error.code) from error
        raise PermanentCommandError(error.code) from error
