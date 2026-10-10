# SPLIT-00 extraction manifest

## Baseline and scope

- Frozen source: `pumni/thread@0dfbd36ff3756f8a68edcd6cc0bd083f6f3570a1`.
- Confirmed before branch creation: `origin/main` resolved to the frozen SHA; the source worktree was clean. This inventory records source facts only at that commit.
- Authorization: issue #243, coordinator comment `6093444290`, parent #242. This checkpoint authorizes documentation only.
- No destination repository or code extraction is created by this manifest. `src/threads_local/` is a provisional package namespace for ownership discussion, not an approved repository name.
- The source repository is public. The exact baseline contains no root `LICENSE`, `COPYING`, `NOTICE`, or SPDX license metadata in `pyproject.toml`; only third-party notices for Windows desktop packaging were found. Public visibility alone does not establish permission to copy first-party code. Resolve provenance and license approval before extraction.

Use these dispositions:

- **MUST_COPY**: needed to preserve current Standalone behavior or its safety boundary.
- **REFACTOR_MINIMAL**: preserve the named contract while moving or narrowing ownership to remove a measured coupling.
- **REJECT**: outside the Standalone slice, topology-specific to distributed operation, persistent database lifecycle, or disallowed by ADR-0008.

Destination owners are proposed placeholders. A later authorized extraction must choose the final package and repository without changing these ownership boundaries.

## Standalone source inventory

Every Python file directly under `src/threads_platform/standalone/` at the frozen baseline is listed below.

| Source path | Disposition | Proposed destination owner | Current responsibility / preserved contract |
| --- | --- | --- | --- |
| `src/threads_platform/standalone/__init__.py` | MUST_COPY | `threads_local.standalone` package marker | Preserve package import behavior; currently adds no runtime service. |
| `src/threads_platform/standalone/__main__.py` | MUST_COPY | `threads_local.standalone.__main__` | `threads-local` CLI parser, command dispatch, bounded/sanitized output, local account and data-root composition. |
| `src/threads_platform/standalone/accounts.py` | MUST_COPY | `threads_local.standalone.accounts` | Local account JSON v1, alias/path validation, data-root resolution, credential-reference binding. |
| `src/threads_platform/standalone/api.py` | MUST_COPY | `threads_local.standalone.api` | Local API runtime, per-call `env://` resolution, bounded API inputs/results and sanitized errors. |
| `src/threads_platform/standalone/app.py` | MUST_COPY | `threads_local.standalone.app` | Local composition root for API, mutation and optional browser runtimes; no distributed composition. |
| `src/threads_platform/standalone/mutations.py` | MUST_COPY | `threads_local.standalone.mutations` | API mutations, strict local operation journal, per-account lock, irreversible-boundary and ambiguity/no-retry behavior. |
| `src/threads_platform/standalone/nurture.py` | MUST_COPY | `threads_local.standalone.nurture` | Strict built-in `NurturePresetV1`, bounded deterministic policy values and decision contracts. |
| `src/threads_platform/standalone/nurture_content.py` | MUST_COPY | `threads_local.standalone.nurture_content` | Operator content packet validation and source/draft/content fingerprints; raw packet text is not Nurture state. |
| `src/threads_platform/standalone/nurture_conversations.py` | MUST_COPY | `threads_local.standalone.nurture_conversations` | Bounded conversation and inbound-reply normalization, ownership evidence and fingerprint dedupe. |
| `src/threads_platform/standalone/nurture_discovery.py` | MUST_COPY | `threads_local.standalone.nurture_discovery` | API-first bounded discovery, target normalization, dedupe and deterministic candidate ranking. |
| `src/threads_platform/standalone/nurture_draft.py` | MUST_COPY | `threads_local.standalone.nurture_draft` | Strict operator-supplied reply-draft input; text is excluded from the local journal and receipts. |
| `src/threads_platform/standalone/nurture_insights.py` | MUST_COPY | `threads_local.standalone.nurture_insights` | Explicit insights refresh and account-relative scoring over immutable snapshots. |
| `src/threads_platform/standalone/nurture_quote_draft.py` | MUST_COPY | `threads_local.standalone.nurture_quote_draft` | Strict operator-supplied quote-draft input with redacted representation. |
| `src/threads_platform/standalone/nurture_runner.py` | MUST_COPY | `threads_local.standalone.nurture_runner` | Bounded sequential Nurture orchestration, receipt transitions, apply boundary and operation linkage. |
| `src/threads_platform/standalone/nurture_store.py` | MUST_COPY | `threads_local.standalone.nurture_store` | Strict filesystem run receipts, per-preset target/content state, insights snapshots, path checks and Nurture ownership lock. |
| `src/threads_platform/standalone/recurrences.py` | MUST_COPY | `threads_local.standalone.recurrences` | Explicit foreground fixed-interval recurrence over validated workflow snapshots; no catch-up or daemon. |
| `src/threads_platform/standalone/runtime.py` | MUST_COPY | `threads_local.standalone.runtime` | Headed persistent local Chromium, direct route, account-specific profile/lock paths, bounded feed/profile/thread reads and fail-closed mapping. |
| `src/threads_platform/standalone/workflows.py` | MUST_COPY | `threads_local.standalone.workflows` | Strict v1 ordered workflow plan, current API/browser/read and one final text-post step; stop at first failure. |

Preserve the existing script name `threads-local` and operator-visible command names. The destination entry point should resolve to its own `__main__:main`; it must not keep an import-time dependency on the public monorepo package.

## Current command surface

| Command family in `__main__.py` | Current subcommands / role | Destination owner |
| --- | --- | --- |
| `account` | `add`, `list`, `login`, `credential set-env` | `standalone.accounts`, `standalone.runtime`, `standalone.api` |
| Browser reads | `feed`, `profile`, `thread` | `standalone.runtime`, browser contracts and adapter |
| `api` reads | `quota`, `media`, `post-insights`, `public-profile`, `profile-posts`, `search`, `mentions`, `replies`, `conversation` | `standalone.api`, `api.client`, `api.contracts` |
| API mutations | `post`, `reply`, `moderate-reply`, `post-image`, `post-video`, `post-carousel` | `standalone.mutations`, `api.client`, local operation journal |
| `operation` | `show` | `standalone.mutations` |
| `workflow` | `run FILE` | `standalone.workflows` and the local API/browser/mutation runtimes |
| `recurrence` | `create`, `list`, `show`, `run`, `disable` | `standalone.recurrences` and workflow snapshot |
| `nurture` | `run`, `insights refresh`; run accepts explicit `--apply`, reply/post/quote input files | `standalone.nurture_*` and existing mutation runtime |

The CLI accepts explicit local operator intent. Do not add commands or imply an automatic scheduler, local service, or distributed control plane during the split.

## Transitive source dependency inventory

The following is the import closure observed from source imports, not a proposed redesign of the public repository.

| Source module(s) | Disposition | Destination owner / exact edge | Findings and boundary |
| --- | --- | --- | --- |
| `src/threads_platform/application/ports/threads.py` | MUST_COPY | `threads_local.api.contracts` | Standalone API/client use the `ThreadsAPI` protocol, credential resolver/error contracts, request/response DTOs, pagination, reply ownership and discovery DTOs. It imports `pydantic.SecretStr` and two discovery enums. Keep API contracts used by all listed CLI capabilities. |
| `src/threads_platform/infrastructure/threads_api/client.py` | MUST_COPY | `threads_local.api.client` | Official API adapter used by local composition. Imports `httpx2`, Pydantic validation, API ports and discovery search enums; no persistence composition. Preserve request bounds, typed sanitized errors, cursor handling and transport behavior. |
| `src/threads_platform/infrastructure/threads_api/environment_credentials.py` | MUST_COPY | `threads_local.credentials.environment` | Environment-only `env://` reference validation, value validation and resolver; direct keyed lookup, no cache, `SecretStr`, sanitized errors. Preserve `THREADS_PLATFORM_THREADS_TOKEN_` namespace and current syntax. |
| `src/threads_platform/application/ports/process_lock.py` | MUST_COPY | `threads_local.ports.process_lock` | Generic `ProcessLock` protocol and `ProcessAlreadyRunning` outcome imported by mutation, recurrence and Nurture stores. |
| `src/threads_platform/infrastructure/local/process_lock.py` | MUST_COPY | `threads_local.local.process_lock` | Generic OS file-lock adapter implementing the process-lock port; imports only standard library and the generic port. Keep non-blocking acquisition and release behavior. |
| `src/threads_platform/application/ports/browser.py` | MUST_COPY, scoped to Standalone-used generic contracts | `threads_local.browser.contracts` | `BrowserEngine`, base session, feed/profile/thread session ports, launch request, network route/proxy value types and generic adapter errors. Do not add Worker identity, account affinity, lease, checkpoint or session lifecycle fields. Do not include media-staging contracts unless separately authorized. |
| `src/threads_platform/infrastructure/browser/playwright_engine.py` | REFACTOR_MINIMAL | `threads_local.browser.playwright_engine` | Retain only Standalone-used browser launch/session, allowlisted navigation, feed/profile/thread recognition and typed failure behavior. The current adapter also contains concrete Worker synthetic inspection and media-composer behavior; those paths and Worker fixtures are not Standalone authorization. |
| `src/threads_platform/application/browser_read_semantics.py` | MUST_COPY | `threads_local.browser.read_semantics` | Topology-neutral feed permalink normalization and bounded read semantics over browser port contracts and feed result DTO. |
| `src/threads_platform/application/browser_capabilities.py` | REFACTOR_MINIMAL | `threads_local.browser.models` | Extract only `BrowserFeedItemResultV1`, `BrowserFeedResultV1` and `BrowserTargetOpenResultV1` with strict Pydantic field constraints. The current module also owns Worker capability policy and media-stage DTOs. It imports `domain.workers.BrowserSessionState`, `domain.capabilities` and `domain.browser_media`; do not copy those broader owners to support the read DTOs. |
| `src/threads_platform/domain/discovery.py` | REFACTOR_MINIMAL | `threads_local.api.contracts` | Rehome only `DiscoverySearchMode` (`KEYWORD`, `TAG`) and `DiscoverySearchType` (`TOP`, `RECENT`) needed by Standalone API search/client. The whole module contains campaign, run, evidence and lead-candidate lifecycle models not consumed by this slice. |
| `src/threads_platform/config/settings.py` | REFACTOR_MINIMAL | `threads_local.config` | Replace monolithic `Settings` import with a small local settings type for `threads_api_base_url`, retaining prefix `THREADS_PLATFORM_` and default `https://graph.threads.net/v1.0/`. Standalone currently pulls this module although it also declares DB, Worker, scheduler and operator settings. |
| `src/threads_platform/domain/time.py` | MUST_COPY only if retained imports require it | `threads_local` value/helper owner | Current `domain.discovery` import reaches this standard-library time helper. Prefer the minimal enum extraction above, which removes the dependency; do not copy speculatively. |
| `src/threads_platform/domain/accounts.py`, `domain/capabilities.py`, `domain/browser_media.py`, `domain/workers.py` | REJECT | Remain in distributed source | The Standalone-used browser result DTO subset can be separated without these broad business/Worker modules. `application.browser_capabilities` currently imports Worker-owned `BrowserSessionState`; this is the concrete Worker-coupled DTO edge to remove. |
| `src/threads_platform/infrastructure/threads_api/credentials.py`, `threads_api/composition.py` | REJECT | Remain in distributed source | Persistent account credential provider and Control Plane composition depend on UnitOfWork/account persistence. Standalone must use only `environment_credentials.py` and local API composition. |
| `src/threads_platform/application/ports/repositories.py`, `src/threads_platform/infrastructure/persistence/**`, `src/threads_platform/domain/worker_jobs.py`, `src/threads_platform/workers/**`, `src/threads_platform/infrastructure/worker_agent/**`, transport/Control Plane modules | REJECT | Remain in distributed source | No Standalone runtime import edge is required. Do not copy PostgreSQL repositories, migrations, Worker jobs/leases, enrollment, network assignment, transport or control clients. |

### Import DAG at the frozen source

```text
threads_platform.standalone.__main__
  -> standalone.accounts / api / app / mutations / runtime / workflows / recurrences
  -> standalone.nurture and nurture_content / conversations / discovery / drafts / insights / runner / store
  -> application.browser_capabilities / application.ports.threads / domain.discovery

standalone.app
  -> config.settings
  -> infrastructure.threads_api.client + environment_credentials
  -> standalone.accounts / api / mutations / runtime

standalone.api
  -> application.ports.threads + domain.discovery + config.settings
  -> infrastructure.threads_api.environment_credentials
  -> standalone.accounts + httpx2 + pydantic

standalone.runtime
  -> application.browser_capabilities + application.browser_read_semantics
  -> application.ports.browser + application.ports.process_lock
  -> infrastructure.browser.playwright_engine + infrastructure.local.process_lock
  -> standalone.accounts

standalone.mutations / recurrences / nurture_store
  -> application.ports.process_lock + infrastructure.local.process_lock
  -> standalone.accounts / workflows / nurture preset/content contracts as applicable

infrastructure.threads_api.client
  -> application.ports.threads + domain.discovery + httpx2 + pydantic
infrastructure.threads_api.environment_credentials
  -> application.ports.threads + pydantic + standard library
infrastructure.browser.playwright_engine
  -> application.ports.browser + Playwright + standard library
infrastructure.local.process_lock
  -> application.ports.process_lock + standard library
application.browser_read_semantics
  -> application.browser_capabilities + application.ports.browser
application.ports.threads
  -> domain.discovery + pydantic.SecretStr
application.browser_capabilities
  -> domain.browser_media + domain.capabilities + domain.workers.BrowserSessionState + pydantic
domain.discovery
  -> domain.time + standard library
```

Static search of this closure at the baseline found no imports of `threads_platform.workers.*`, `infrastructure.worker_agent.*`, `infrastructure.persistence.*`, SQLAlchemy, asyncpg, Alembic, FastAPI or Uvicorn. The indirect `domain.workers` edge in `application.browser_capabilities` is not a `workers.*` package edge, but remains a Worker-domain coupling and is called out for DTO extraction. The closure uses `httpx2`, `pydantic`, `pydantic-settings` via `config.settings`, and Playwright; remaining imports are standard library or first-party pure contracts.

The destination must not install or import the public monorepo package at runtime. It should contain the owned API/browser/process-lock contracts and adapters described above.

## Test and fixture inventory

All test paths below exist at the frozen source baseline. They use temporary directories, test values, fake API/runtime objects or local synthetic transports. Their presence is not a test pass claim for this documentation checkpoint.

| Test source | Disposition / destination owner | Contract to preserve |
| --- | --- | --- |
| `tests/unit/test_standalone_accounts.py` | MUST_COPY to `tests/unit/test_accounts.py` | Account v1 schema, alias, data-root, atomic write, path containment and credential-ref shape. |
| `tests/unit/test_standalone_api.py` | MUST_COPY to `tests/unit/test_api_runtime.py` | API calls, per-call secret resolution, bounds and output/error redaction; fake API and transport only. |
| `tests/unit/test_standalone_app.py` | MUST_COPY to `tests/unit/test_composition.py` | Local composition toggles and dependency ownership without DB setup. |
| `tests/unit/test_standalone_cli.py` | MUST_COPY to `tests/unit/test_cli.py` | All current CLI commands, argument bounds and safe output using local fakes. |
| `tests/unit/test_standalone_mutations.py` | MUST_COPY to `tests/unit/test_mutations.py` | Journal state machine, persistence failure, cancellation, lock release, ambiguous outcomes and no-blind-retry behavior. Keep only synthetic IDs and text. |
| `tests/unit/test_standalone_nurture.py` | MUST_COPY to `tests/unit/test_nurture_policy.py` | Strict preset and deterministic decision rules. |
| `tests/unit/test_standalone_nurture_apply.py` | MUST_COPY to `tests/unit/test_nurture_apply.py` | Explicit apply boundary, draft binding, operation linkage and ambiguity halt/no retry. |
| `tests/unit/test_standalone_nurture_content.py` | MUST_COPY to `tests/unit/test_nurture_content.py` | Content packet validation, account/preset binding, redaction and fingerprint stability. |
| `tests/unit/test_standalone_nurture_conversations.py` | MUST_COPY to `tests/unit/test_nurture_conversations.py` | Reply ownership and deterministic inbound normalization/dedupe. |
| `tests/unit/test_standalone_nurture_discovery.py` | MUST_COPY to `tests/unit/test_nurture_discovery.py` | Bounded API source calls, normalization, candidate dedupe and ranking. |
| `tests/unit/test_standalone_nurture_durability.py` | MUST_COPY to `tests/unit/test_nurture_durability.py` | Stale RUNNING receipt recovery only under the Nurture lock; filesystem failure and same-account ownership. |
| `tests/unit/test_standalone_nurture_insights.py` | MUST_COPY to `tests/unit/test_nurture_insights.py` | Snapshot schema, deterministic ID, null-vs-zero metrics and account-relative scoring. |
| `tests/unit/test_standalone_nurture_own_content.py` | MUST_COPY to `tests/unit/test_nurture_own_content.py` | Content provenance, apply gate, dedupe and ambiguity handling. |
| `tests/unit/test_standalone_nurture_runner.py` | MUST_COPY to `tests/unit/test_nurture_runner.py` | Observe-only run, bounded calls, durable receipts and deterministic outcomes. |
| `tests/unit/test_standalone_nurture_runner_inbound.py` | MUST_COPY to `tests/unit/test_nurture_inbound.py` | Inbound priority, ownership uncertainty and safe no-action outcomes. |
| `tests/unit/test_standalone_recurrences.py` | MUST_COPY to `tests/unit/test_recurrences.py` | Fixed anchored intervals, per-recurrence lock, restart state, no catch-up bursts and READ-only validation. |
| `tests/unit/test_standalone_runtime.py` | MUST_COPY to `tests/unit/test_browser_runtime.py` | Synthetic feed/profile/thread browser sessions, headed/direct launch request, per-account profile/lock and fail-closed results. The fake engine asserts no `worker_id`. |
| `tests/unit/test_standalone_workflows.py` | MUST_COPY to `tests/unit/test_workflows.py` | Strict workflow schema, ordered execution, stop-on-first-failure, bounded output and mutation ambiguity handling. |
| `tests/unit/test_standalone_architecture.py` | MUST_COPY with minimal package-root updates | AST import-boundary, no fake Worker identity, shared browser request shape and no compatibility-owner regressions. Keep destination rules; remove assumptions about unrelated monorepo packages only when the standalone boundary remains enforced. |
| `tests/unit/test_threads_api_contract.py`, `tests/unit/test_threads_discovery_contract.py` | MUST_COPY for consumed API operations | `httpx2.MockTransport` documented-shape fixtures, strict DTO mapping, cursor bounds and sanitized failures. These are deterministic contract fixtures, not live API evidence. |
| `tests/unit/test_threads_credential_secrets.py` | MUST_COPY for env-only resolver | Exact `env://` validation, direct keyed lookup, no caching, sanitized missing/invalid values and no token rendering. |
| `tests/unit/test_browser_capabilities.py` | REFACTOR_MINIMAL | Retain only feed/profile/thread DTO schema tests. The full module also covers Worker advertising/policy and should not be copied as a Standalone fixture. |
| `tests/unit/test_browser_adapter.py` | REFACTOR_MINIMAL | Extract only Playwright engine tests needed for feed/profile/thread, allowlisted navigation, cleanup and typed errors. The current file imports Worker sessions, WorkerJob state, affinity and synthetic Worker UI fixtures; do not copy those fixtures or treat them as Standalone/live evidence. |
| `tests/unit/test_worker_agent_foundation.py` | REJECT as a whole | Tests Worker Agent identity, keys, enrollment/session lifecycle and runtime. It includes a generic lock assertion, but Standalone lock semantics are already exercised by Standalone mutation, recurrence and Nurture durability tests; do not bring the Worker fixture graph. |
| `tests/unit/test_feed_browse.py`, `test_profile_open.py`, `test_thread_open.py`, `test_media_local_upload.py` | REJECT as a whole | WorkerJob, lease/fencing, Worker session and capability-dispatch fixtures are topology-specific. Standalone browser behavior is covered by `test_standalone_runtime.py`; only shared generic port contract tests may be extracted. |
| `tests/integration/test_threads_credentials.py`, persistence and Worker integration suites | REJECT | Validate persistent provider, PostgreSQL repositories, Worker transport or distributed runtime. They are not dependencies of the local environment resolver/client contract. |

No Standalone-specific unit module imports `threads_platform.workers.*`; the browser test support is local fake engine/session code. Shared browser adapter and capability test files are Worker-coupled and require selective fixture extraction. No local tokens, cookies, account/profile data, raw private Thread IDs, usernames, cursors, reply text or operator evidence blobs are part of this inventory.

## Runtime, development and packaging inventory

| Concern | Frozen source finding | Destination requirement / owner |
| --- | --- | --- |
| Python | `pyproject.toml` requires `>=3.14,<3.15`; Pyright targets Python 3.14. | Preserve this range for first parity build unless a separately accepted compatibility decision changes it. |
| Runtime imports | Standalone closure requires `httpx2>=2,<3`, `playwright>=1.50,<2`, `pydantic>=2.7,<3`, `pydantic-settings>=2.7,<3`; standard library supplies remaining imports. | Declare only dependencies present in the destination import/test closure. |
| Monorepo runtime dependencies not in Standalone closure | `alembic`, `asyncpg`, `cryptography`, `fastapi`, OpenTelemetry packages, `prometheus-client`, `sqlalchemy[asyncio]`, `structlog`, and `uvicorn` are declared by the monorepo but are not imported by the audited Standalone closure. | Do not inherit these because the source monorepo declares them. |
| Development tooling | Current `dev` group contains `httpx`, `pyright`, `pytest`, `pytest-asyncio`, `ruff`; separate `packaging` group contains PyInstaller. Standalone unit/API fixtures use `httpx2`, not the unrelated `httpx` package in the repository dev group. | Candidate destination tools are `pytest`, `pytest-asyncio`, `ruff`, `pyright`; confirm any extra dependency from selected test files. |
| Build backend | `uv_build==0.12.7`; uv lock-based workflow; source script `threads-local = threads_platform.standalone.__main__:main`. | Keep uv-based reproducible build, use the destination entrypoint, build and install a wheel in a clean environment. Do not copy Worker or credential-admin scripts. |
| Native packaging | PyInstaller is present only in the monorepo `packaging` group; no Standalone-only bundle contract was found in this source inventory. | Review Windows CLI distribution separately. Do not claim PyInstaller parity until a destination smoke build exists. |
| Copyright / provenance | Public source, but no first-party license file or SPDX field found in the frozen tree. | Obtain explicit provenance/license disposition before copying source or tests into a private destination. Preserve third-party notices for any third-party artifacts actually bundled. |

## PostgreSQL and Worker boundary proof target

The audited import closure can omit PostgreSQL because local account and operational state is filesystem-only. It composes `HttpThreadsAPI`, environment-only credential resolution, generic browser/local-process-lock adapters and Standalone orchestration. It does not create a `Command`, `WorkerJob`, Worker enrollment, heartbeat, lease, checkpoint, Control Plane client or PostgreSQL connection. PostgreSQL remains authoritative only for distributed/Control Plane business state per root architecture rules.

Before destination acceptance, prove this with a clean Python 3.14 environment whose installed runtime set omits SQLAlchemy, asyncpg, Alembic, FastAPI and Uvicorn; import the Standalone package and run CLI help with no database URL or service; run selected deterministic suites with no DB service and no live Threads transport. Add an AST/import boundary check that fails on `workers.*`, `infrastructure.worker_agent.*`, persistence, SQLAlchemy/asyncpg/Alembic, FastAPI or Uvicorn edges. This manifest does not execute that destination proof.

## Open risks for coordinator review

1. `docs/adr/0009-standalone-execution-isolation.md` still says `Status: Proposed`, while `docs/STANDALONE_ARCHITECTURE_ISOLATION.md` labels its baseline accepted and issue #176 records ADR-0009 plus the migration map as merged architecture authority. This checkpoint does not rewrite ADR-0009; coordinator should reconcile the status label before implementation relies on it as an accepted ADR.
2. Browser result DTOs are imported from `application.browser_capabilities`, which imports `domain.workers.BrowserSessionState`. The minimal DTO split is a real extraction edge, not an inferred feature.
3. Monorepo dependencies exceed Standalone runtime imports. A private build must use an independent minimal dependency set and prove no DB startup requirement.
4. Copyright/provenance remains unresolved by source metadata and must be decided before code copy.
5. Current unit fixtures are deterministic code contracts only. Synthetic Worker DOM fixtures and historic browser failures do not establish current Standalone live readiness.
