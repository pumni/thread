# Standalone Architecture Isolation — ARCH-01 ownership and migration map

## Accepted baseline

This audit uses the authorized source at:

```text
main@cd2a1d73800cfa41bf8c4c646c49bac4dc2bd018
```

The worktree was created from this exact SHA on
`codex/177-local-arch-01`. The findings below come from source and tests at that
baseline, including `AGENTS.md`, ADR-0008, `docs/ARCHITECTURE.md`, the
browser-capability and Threads API contract Skills, and the named source/test
modules. Planning documents are not used to override current code.

## Current dependency graph

```text
threads_platform.standalone.__main__
  ├─ standalone.accounts / standalone.api / standalone.runtime
  ├─ application.ports.threads
  ├─ config.settings
  ├─ infrastructure.threads_api.client
  └─ infrastructure.threads_api.credentials
       ├─ environment resolver + ref validator (needed locally)
       ├─ application.ports.repositories.UnitOfWork (not needed locally)
       └─ domain.accounts / domain.time lifecycle (not needed locally)

standalone.runtime
  ├─ domain.workers.NetworkProtocol                         [cross-topology]
  ├─ infrastructure.worker_agent.process_lock               [cross-topology]
  ├─ infrastructure.browser.playwright_engine
  │    ├─ domain.workers.NetworkProtocol                     [cross-topology]
  │    └─ workers.browser generic engine contract/errors/DTOs [cross-topology]
  ├─ workers.browser generic engine contract/errors/DTOs     [cross-topology]
  ├─ workers.sessions.NetworkRoute / ProxyCredentials        [cross-topology]
  └─ standalone.accounts

infrastructure.browser.playwright_engine
  ├─ Playwright
  ├─ domain.workers.NetworkProtocol
  └─ workers.browser contracts, errors, observations, and
       Threads browser-origin/bound constants

workers.browser
  ├─ application.ports.worker_agent
  ├─ domain.worker_jobs / domain.workers
  └─ workers.sessions
       ├─ application.ports.worker_agent
       └─ domain.workers.NetworkProfile / NetworkProtocol

workers.feed_browse / profile_open / thread_open / media_local_upload
  ├─ Worker-specific procedures and capability policy
  └─ generic engine Protocols, observations, errors from workers.browser

workers.__main__ / workers.runtime
  ├─ infrastructure.worker_agent.process_lock.WorkerProcessLock
  └─ application.ports.worker_agent.WorkerProcessLock Protocol

infrastructure.threads_api.composition
  ├─ application Control Plane composition / UnitOfWorkFactory
  ├─ infrastructure.threads_api.client
  └─ infrastructure.threads_api.credentials
       ├─ EnvironmentThreadsCredentialSecretResolver
       └─ PersistentThreadsAccessTokenProvider
```

The Standalone path does not currently import PostgreSQL, persistence, transport,
Control Plane runtime, WorkerJob, or Worker protocol modules. The credential
module import is nevertheless coupled to UnitOfWork and persisted account
lifecycle definitions because both credential paths are defined in one Python
module.

## Direct import inventory

“External imports” below means standard-library or third-party runtime imports;
they are generic infrastructure/runtime dependencies. Every first-party module
and imported symbol relevant to this audit is assigned a category in the tables
and ownership matrix that follow.

| Source file | Direct first-party imports and symbols | External imports |
|---|---|---|
| `standalone/__main__.py` | `application.ports.threads`: `ThreadsAPIError`, `ThreadsCredentialError`; `config.settings.Settings`; `infrastructure.threads_api.client.HttpThreadsAPI`; `infrastructure.threads_api.credentials.EnvironmentThreadsCredentialSecretResolver`; `standalone.accounts`: `LocalAccountStore`, `StandaloneAccountError`, `resolve_standalone_data_root`; `standalone.api`: `LocalThreadsApiRuntime`, `StandaloneApiError`, `bind_env_credential`, `build_threads_http_client`; `standalone.runtime`: `LocalRuntime`, `StandaloneRuntimeError` | `argparse`, `asyncio`, `re`, `sys`, `collections.abc.Sequence` |
| `standalone/accounts.py` | No first-party imports; owns `LocalAccount`, `StandaloneAccountError`, local JSON account/profile metadata, root/path and credential-ref-shape checks | `json`, `os`, `re`, `tempfile`, `collections.abc.Mapping`, `dataclasses`, `pathlib.Path`, `typing`, `uuid` |
| `standalone/runtime.py` | `domain.workers.NetworkProtocol`; `infrastructure.browser.playwright_engine.PlaywrightBrowserEngine`; `infrastructure.worker_agent.process_lock.WorkerProcessAlreadyRunning`, `WorkerProcessLock`; `standalone.accounts.LocalAccount`, `LocalAccountStore`; `workers.browser.BrowserAdapterError`, `BrowserEngine`, `BrowserEngineSession`, `BrowserLaunchRequest`; `workers.sessions.NetworkRoute` | `re`, `collections.abc.Callable`, `pathlib.Path`, `typing.cast` |
| `standalone/api.py` | `application.ports.threads`: API DTOs, `ThreadsAPI`, credential error/secret-resolver contracts; `config.settings.Settings`; `infrastructure.threads_api.credentials.normalize_threads_credential_ref`; `standalone.accounts.LocalAccount`, `LocalAccountStore` | `re`, `httpx2`, `pydantic.SecretStr` |
| `infrastructure/browser/playwright_engine.py` | `domain.workers.NetworkProtocol`; `workers.browser`: `BROWSER_FEED_CANDIDATE_BOUND`, `BROWSER_FEED_ORIGIN`, generic browser errors/Protocols, `BrowserLaunchRequest`, `BrowserSurface`, feed observation DTOs, `PreparedMediaComposer` | `asyncio`, `re`, `dataclasses`, `pathlib.Path`, `typing.cast`, `urllib.parse`, `uuid`, `playwright.async_api` |
| `workers/browser.py` | `application.ports.worker_agent`: recovery/session/WorkerJob control types; `domain.worker_jobs`: retry-safety/status; `domain.workers.BrowserSessionState`; `workers.sessions`: `BrowserSessionManager`, `BrowserSessionOpenResult`, `NetworkRoute`, proxy credential Protocol/value | `ipaddress`, `json`, `re`, `collections.abc`, `dataclasses`, `datetime`, `enum`, `pathlib.Path`, `typing`, `urllib.parse`, `uuid` |
| `workers/sessions.py` | `application.ports.worker_agent`: `LocalSessionState`, `ManagedProfileDirectory`, `WorkerAccountContext`, `WorkerLocalState`; `domain.workers`: `BrowserSessionState`, `NetworkProfile`, `NetworkProtocol` | `dataclasses`, `datetime`, `typing.Protocol`, `uuid` |
| `infrastructure/worker_agent/process_lock.py` | No first-party imports; owns `WorkerProcessLock` and `WorkerProcessAlreadyRunning` | `os`, `pathlib.Path`, `typing.BinaryIO`, runtime `msvcrt` on Windows or `fcntl` on POSIX |
| `application/ports/worker_agent.py` | `domain.worker_jobs`: retry-safety/status; `domain.workers`: session/network/Worker status; `workers.key_store.WorkerDeviceIdentity`; defines a Worker-owned `WorkerProcessLock` Protocol | `collections.abc`, `dataclasses`, `datetime`, `typing`, `uuid` |
| `application/ports/threads.py` | `domain.discovery.DiscoverySearchMode`, `DiscoverySearchType`; defines shared Threads API DTOs/errors/Protocols including `ThreadsCredentialSecretResolver` | `dataclasses`, `datetime`, `enum`, `typing`, `uuid`, `pydantic.SecretStr` |
| `infrastructure/threads_api/credentials.py` | `application.clock.Clock`; `application.ports.repositories.UnitOfWork`, `UnitOfWorkFactory`; `application.ports.threads` credential errors/resolver; `domain.accounts` account and OAuth metadata/status; `domain.time.normalize_utc` | `os`, `re`, `collections.abc.Mapping`, `datetime`, `uuid`, `pydantic.SecretStr` |
| `infrastructure/threads_api/client.py` | `application.ports.threads` API DTOs/errors; `domain.discovery.DiscoverySearchMode`, `DiscoverySearchType` | `datetime`, `typing.cast`, `urllib.parse`, `httpx2`, `pydantic` |
| `infrastructure/threads_api/composition.py` | application command/runtime composition, `Clock`, `UnitOfWorkFactory`, Threads ports, `WorkerJobService`, settings, Threads HTTP client, both credential classes | `collections.abc.Mapping`, `dataclasses`, `httpx2` |
| `workers/runtime.py`, `workers/__main__.py` | `application.ports.worker_agent.WorkerProcessLock`; `infrastructure.worker_agent.process_lock.WorkerProcessLock` | Worker host/runtime composition; standard library and platform-local data-root adapter |
| `workers/feed_browse.py`, `workers/profile_open.py`, `workers/thread_open.py`, `workers/media_local_upload.py` | Capability procedures import `BrowserNavigationPolicy`, generic engine session Protocols, observations, and capability errors from `workers.browser`; they remain WorkerJob orchestrators | Standard library and application/domain Worker contracts |

### Relevant transitive import inventory

The following are the first-party import branches reached from those direct
imports and material to this ownership decision:

| Import branch | Relevant transitive modules/symbols | Classification |
|---|---|---|
| Standalone API port | `application.ports.threads` → `domain.discovery` for API query enums/DTO contracts | Shared execution/API port → business/domain concepts |
| Standalone API adapter | `infrastructure.threads_api.client` → `application.ports.threads`, `domain.discovery`; `config.settings` → Pydantic Settings | Generic infrastructure adapter; domain enum dependency is API contract data |
| Standalone credential resolver | `infrastructure.threads_api.credentials` → `application.ports.repositories` → domain repository DTOs; `application.clock`; `application.ports.threads`; `domain.accounts`; `domain.time` | Accidental mixed-module dependency for the environment resolver; persistent provider dependencies remain intentional |
| Browser launch request | `workers.browser` → `application.ports.worker_agent` → `domain.worker_jobs`, `domain.workers`, `workers.key_store`; and `workers.sessions` → `application.ports.worker_agent`, `domain.workers` | Worker/distributed orchestration; generic symbols imported from that module are accidental cross-topology dependencies |
| Worker session management | `workers.sessions` → `domain.workers.NetworkProfile/NetworkProtocol/BrowserSessionState` and Worker-local state/profile ports | Worker/domain concepts; its current generic route and credential values are misplaced |
| Playwright adapter | `workers.browser` → Worker port/domain/session tree above; separate `domain.workers.NetworkProtocol` import | Generic infrastructure adapter with accidental reverse dependency on Worker/domain modules |
| Process lock | `infrastructure.worker_agent.process_lock` → OS runtime locking APIs; its Protocol counterpart is in `application.ports.worker_agent` | Generic local OS primitive with accidental Worker package ownership |
| Persistent credential composition | `infrastructure.threads_api.composition` → `application.ports.repositories`, command/runtime composition, WorkerJob service, API client, resolver, persistent provider | Control Plane/infrastructure composition; not imported by Standalone |
| Admin credential consumers | `tools.credential_admin` → environment resolver, credential ref normalizer, persistent metadata service | Shared environment resolver consumer; changes in ARCH-04 must update its import path |
| Worker capability consumers | `workers.feed_browse`, `workers.profile_open`, `workers.thread_open`, `workers.media_local_upload` → `workers.browser` contracts and orchestration; session implementations → `workers.sessions` | Worker/distributed orchestration; only topology-neutral engine contract symbols move to the shared port |
| Worker lock composition | `workers.__main__` creates the concrete lock; `workers.runtime` accepts the `application.ports.worker_agent.WorkerProcessLock` Protocol | Worker host composition consuming a misplaced shared OS primitive/Protocol |

The inventory intentionally stops at the public imports of third-party
libraries and at the broader WorkerJob/domain contract boundary. It does not
expand Playwright, Pydantic, `httpx2`, or the full Control Plane persistence
graph into implementation internals.

## Symbol ownership matrix

The categories are exclusive: standalone-owned; genuinely shared execution
primitive/port; Worker/distributed orchestration; generic infrastructure
adapter; business/domain concept; accidental cross-topology dependency.

| Symbol/module at baseline | Current owner and classification | Why current ownership/use is unsuitable | Accepted target owner/module | Checkpoint |
|---|---|---|---|---|
| `LocalAccount`, `LocalAccountStore`, local account paths and CLI | `standalone.accounts` — standalone-owned | Correct local authority boundary | Remain in `standalone.accounts` | None |
| `WorkerProcessLock` Protocol | `application.ports.worker_agent` — accidental cross-topology dependency on a Worker-owned port | A filesystem lock is a shared local execution primitive, not Worker Agent contract state | `ProcessLock` in `application.ports.process_lock` | #178 |
| `WorkerProcessLock` implementation | `infrastructure.worker_agent.process_lock` — accidental cross-topology dependency | Generic OS/file lock implementation is owned by Worker Agent package, then Standalone imports it | `FilesystemProcessLock` in `infrastructure.local.process_lock` | #178 |
| `WorkerProcessAlreadyRunning` | `infrastructure.worker_agent.process_lock` — accidental cross-topology dependency | Shared consumer outcome has Worker-specific ownership/name | `ProcessAlreadyRunning` in `application.ports.process_lock`; concrete adapter raises it | #178 |
| `BrowserEngine`, `BrowserEngineSession`, feed/profile/thread/media session Protocols | `workers.browser` — accidental cross-topology dependency when imported by Standalone/Playwright adapter | Adapter base/session contracts are not Worker orchestration | `application.ports.browser` — shared engine/session Protocols; base `BrowserEngineSession` contains only `navigate(...)` and `close()` | #179 |
| `BrowserEngineSession.inspect_surface()` | Current shared base Protocol in `workers.browser` | It exposes Worker synthetic-session inspection to every engine consumer | Remove from shared base; define `workers.browser.WorkerSurfaceEngineSession` with `inspect_surface() -> BrowserSurface` | #179 |
| Generic browser errors (`BrowserAdapterError`, contract/locator/session/challenge/navigation/runtime/network errors) | `workers.browser` — accidental cross-topology dependency for standalone and generic adapter | Adapter failures should not be defined by Worker orchestration | `application.ports.browser` — shared browser-port outcomes | #179 |
| `BrowserAccountAffinityMismatch`, `ActionOutcomeAmbiguous`, `WorkerJobLeaseLost`, `WorkerJobRetrySafetyViolation` | `workers.browser` — Worker/distributed orchestration | They carry Worker account/session/job/intervention semantics | Remain in `workers.browser` | None |
| `BrowserSurface`, `FeedAncestorObservation`, `FeedCandidateObservation`, `PreparedMediaComposer` | `workers.browser` — accidental cross-topology dependency for the adapter | Playwright produces these port observations/handles; their types carry no Worker identity | `application.ports.browser` | #179 |
| `BrowserSurfaceState`, `classify_browser_surface`, `worker.synthetic` identifiers | `workers.browser` — Worker/distributed orchestration | The accepted synthetic contract is Worker fixture evidence, not Standalone live-session evidence | Remain in `workers.browser` | None |
| `BrowserLaunchRequest` | `workers.browser` — accidental cross-topology dependency; currently requires fake `worker_id` | Adapter request carries host identity and mixes affinity validation into engine config | `application.ports.browser` with exactly four fields: `profile_directory`, `network_route`, `proxy_credentials`, `headless` | #179 |
| `NetworkProtocol` | `domain.workers` — business/domain concept; its current import into Standalone and Playwright is accidental cross-topology dependency | Domain assignment protocol should not be the low-level browser route type | Remains in `domain.workers`; Worker maps its values to `BrowserNetworkProtocol` | #179 |
| `NetworkProfile` | `domain.workers` — business/domain concept | It describes account-assigned distributed routing metadata | Remains domain-owned and consumed only by Worker assignment/session paths | None |
| `NetworkRoute` (account ID, domain protocol, host/port, credential reference) | `workers.sessions` — accidental cross-topology dependency for generic engine/Standalone | It mixes Worker affinity/credential lookup with engine routing options | `BrowserNetworkRoute` in `application.ports.browser`, plus Worker-only `WorkerNetworkRoute` in `workers.sessions` | #179 |
| `ProxyCredentials` | `workers.sessions` — accidental cross-topology dependency for the engine contract | Secret values are generic engine configuration, while resolver policy is Worker-specific | `BrowserProxyCredentials` in `application.ports.browser` | #179 |
| `ProxyCredentialProvider` | `workers.sessions` — Worker/distributed orchestration | It resolves Worker-managed credential references before launch | Remain in `workers.sessions` | None |
| `BrowserNavigationPolicy` | `workers.browser` — Worker/distributed orchestration | It is created by reviewed Worker capabilities to select their allowed origin; it is not an engine or host identity contract | Remain in `workers.browser`; adapter keeps enforcing the supplied `allowed_origins` | None |
| Browser navigation guard, timeout/redirect handling, Playwright proxy conversion | `infrastructure.browser.playwright_engine` — generic infrastructure adapter | Adapter currently imports Worker errors/DTOs, the domain enum, and Worker-owned origin/bound constants | Remain in `infrastructure.browser.playwright_engine`, importing only shared browser contracts and Playwright; origin/bound constants move to `application.ports.browser` | #179 |
| Worker/account/profile/network affinity checks | Split between `workers.sessions`, `workers.browser`, and `playwright_proxy_settings` | The adapter compares `NetworkRoute.account_id` to launch request account ID, pulling affinity into the generic engine boundary | Remain in Worker session/orchestration layers; generic adapter validates route shape only | #179 |
| `validate_threads_credential_ref`, `normalize_threads_credential_ref`, secret-value validator | `infrastructure.threads_api.credentials` — accidental mixed-module dependency for local use | Importing environment-only primitives imports UoW and persisted account lifecycle definitions | `infrastructure.threads_api.environment_credentials` — generic infrastructure adapter | #180 |
| `EnvironmentThreadsCredentialSecretResolver` | `infrastructure.threads_api.credentials` — generic infrastructure adapter in a module with unrelated persistence dependencies | Standalone requires only environment lookup and the resolver port | `infrastructure.threads_api.environment_credentials` | #180 |
| `PersistentThreadsAccessTokenProvider` | `infrastructure.threads_api.credentials` — generic infrastructure adapter for persistent account credential lifecycle | It intentionally needs UoW, account/OAuth domain metadata, and clock; it must not be needed to import the environment resolver | Remains in `infrastructure.threads_api.credentials` | None |
| `ThreadsCredentialSecretResolver`, sanitized Threads credential errors | `application.ports.threads` — genuinely shared execution/API port | Correct boundary; Standalone and persistent provider can use the same resolver contract | Remain in `application.ports.threads` | None |
| `UnitOfWork`, `UnitOfWorkFactory` | `application.ports.repositories` — genuinely shared application persistence port | Required by the persistent provider; not required by environment-only resolution | Remain in `application.ports.repositories`; imported only by persistence-backed consumers | None |
| `Clock` | `application.clock` — genuinely shared application port | Required by persistent expiry evaluation, not by environment-only resolution | Remain in `application.clock` | None |
| `domain.accounts` credential/status types and `domain.time.normalize_utc` | Domain account lifecycle/time — business/domain concepts | Required by persistent metadata lifecycle only; their current presence in the mixed module burdens environment resolver imports | Remain domain-owned and imported only by the persistent provider path | None |
| `domain.discovery` search enums | `domain.discovery` — business/domain concepts used by the Threads API contract/client | These are API request/response semantics, not browser or topology identity | Remain domain-owned; no change in this isolation track | None |
| `domain.worker_jobs`, `domain.workers` session/status/network assignment values | Domain Worker concepts — business/domain concepts | Correct for distributed execution; `NetworkProtocol` is wrongly imported by Standalone and the generic Playwright adapter | Remain domain-owned; only Worker mapping may use `NetworkProtocol` for browser routing | #179 for mapping |
| `application.ports.worker_agent` remaining contracts and `workers.key_store.WorkerDeviceIdentity` | Worker-specific ports — Worker/distributed orchestration | They are not shared browser/process-lock ports; the current WorkerDeviceIdentity import is unrelated to this checkpoint | Remain Worker-owned; ARCH-01 moves only the process-lock Protocol out | None |
| `application.ports.threads` API DTOs, errors, and `ThreadsAPI` | Shared API port — genuinely shared execution primitive/port | Correct shared boundary for standalone API reads and control-plane API adapter | Remain in `application.ports.threads` | None |
| `config.settings` | `config` — generic infrastructure/configuration adapter | Standalone reads only the API base URL/runtime settings; this module is not a Worker dependency | Remain in `config.settings` | None |
| `infrastructure.threads_api.client` | Generic infrastructure adapter | Correct HTTP adapter; standalone and Control Plane composition may use it | Remain in place | None |
| `tools.credential_admin` | Generic infrastructure/admin adapter | Shares environment credential primitives and persistent metadata service | Update environment imports in ARCH-04; metadata lifecycle remains unchanged | #180 |

### Exact target module map

| Target module | Exact symbols/ownership |
|---|---|
| `application/ports/process_lock.py` | `ProcessLock` Protocol (`held`, `acquire()`, `release()`); `ProcessAlreadyRunning` shared outcome exception |
| `infrastructure/local/process_lock.py` | `FilesystemProcessLock`, the OS-specific file-lock implementation of `ProcessLock`; no Worker-specific imports or names |
| `application/ports/browser.py` | `BrowserEngine`; base `BrowserEngineSession` Protocol with only `navigate(...)` and `close()`; feed/profile/thread/media engine-session Protocols; `BrowserLaunchRequest`; raw `BrowserSurface`, `FeedAncestorObservation`, `FeedCandidateObservation`, `PreparedMediaComposer`; generic browser adapter errors listed in ADR-0009; `BrowserNetworkProtocol`, `BrowserNetworkRoute`, `BrowserProxyCredentials`; shared `BROWSER_FEED_ORIGIN` and `BROWSER_FEED_CANDIDATE_BOUND` contract constants |
| `workers/browser.py` | `WorkerSurfaceEngineSession` Protocol with `inspect_surface() -> BrowserSurface`; `WorkerJobExecution`, `WorkerJobReconnectRecovery`, `WorkerBrowserSession`, `PlaywrightBrowserAdapter`, managed Worker profile resolver; `BrowserNavigationPolicy`, `BrowserSurfaceState`, `classify_browser_surface`, `SUPPORTED_UI_CONTRACT_ID`, `SUPPORTED_UI_CONTRACT_VERSION`; `BrowserAccountAffinityMismatch`, `ActionOutcomeAmbiguous`, `WorkerJobLeaseLost`, `WorkerJobRetrySafetyViolation`; imports generic contracts from `application.ports.browser` |
| `workers/sessions.py` | `InvalidSessionTransition`, Worker session state/manager Protocols and implementation, `NetworkProfileApplication`, `ProxyCredentialProvider`, `BrowserSessionOpenResult`; `WorkerNetworkRoute(account_id, browser_route, credential_ref)` holds Worker-only network affinity metadata and a shared `BrowserNetworkRoute` |
| `infrastructure/browser/playwright_engine.py` | `PlaywrightBrowserEngine`, Playwright session, selectors/recognition, existing guard/error mapping, and proxy-settings conversion; imports only `application.ports.browser`, stdlib, and Playwright |
| `infrastructure/threads_api/environment_credentials.py` | `THREADS_TOKEN_ENV_PREFIX`, ref validator/normalizer, exact helper `infrastructure.threads_api.environment_credentials._valid_secret_value`, `EnvironmentThreadsCredentialSecretResolver`; imports the resolver/error port but no persistence/UoW/domain account lifecycle |
| `infrastructure/threads_api/credentials.py` | `PersistentThreadsAccessTokenProvider` and private persistence lifecycle helpers; imports `UnitOfWork`, domain account metadata, `Clock`, Threads resolver port, `validate_threads_credential_ref`, and `_valid_secret_value` from `environment_credentials` |
| `infrastructure/threads_api/composition.py` | Control Plane composition imports environment resolver and persistent provider from their separate modules |

`BrowserNetworkRoute` fields are exactly `protocol: BrowserNetworkProtocol`,
`host: str | None`, and `port: int | None`. `BrowserProxyCredentials` fields are
exactly `username: str | None` and `password: str | None`, both `repr=False`.
`BrowserLaunchRequest` exact ordered fields are `profile_directory: Path`,
`network_route: BrowserNetworkRoute`,
`proxy_credentials: BrowserProxyCredentials | None = field(default=None,
repr=False)`, and `headless: bool = False`.

### Affinity validation and network mapping

The Worker retains all current account/profile/network checks above the engine:

1. `LocalBrowserSessionManager.open` checks `context.worker_id`, active-session
   profile stability, and managed-profile ownership.
2. `NetworkProfileApplication.resolve` checks
   `NetworkProfile.account_id == context.account_id`, maps the domain protocol
   one-to-one to `BrowserNetworkProtocol`, and returns a `WorkerNetworkRoute`
   with the account ID, `BrowserNetworkRoute`, and credential reference.
3. `PlaywrightBrowserAdapter.open_reserved_session` checks Worker identity,
   session reservation account/profile/state/uniqueness, and
   `WorkerNetworkRoute.account_id`; it resolves proxy credentials and constructs
   the identity-free launch request.
4. `PlaywrightBrowserEngine` validates only the generic route shape and current
   adapter capability. It does not validate account, Worker, or profile
   affinity.

Standalone derives the local profile directory and per-account lock path from
`LocalAccount.id`; it builds a direct route as
`BrowserNetworkRoute(BrowserNetworkProtocol.DIRECT, None, None)`, with no proxy
credentials. Neither account UUID nor a substituted Worker UUID enters the
generic browser request.

### Synthetic surface inspection ownership and marker freeze

The shared `application.ports.browser.BrowserEngineSession` is a topology-neutral
base Protocol containing exactly `navigate(url, *, allowed_origins)` and
`close()`. It does not declare `inspect_surface()`.
`BrowserSurface` remains the shared raw observation DTO. The Worker-owned
structural Protocol is exact:

```python
class WorkerSurfaceEngineSession(Protocol):
    async def inspect_surface(self) -> BrowserSurface: ...
```

`BrowserSurfaceState`, `classify_browser_surface`,
`SUPPORTED_UI_CONTRACT_ID`, and `SUPPORTED_UI_CONTRACT_VERSION` remain in
`workers.browser`. `BrowserEngine.open()` returns the shared base Protocol;
Worker code casts/uses the returned concrete engine session as
`WorkerSurfaceEngineSession`, and `WorkerBrowserSession` consumes that Worker
Protocol. Standalone uses only `BrowserEngineSession` and never depends on
synthetic surface inspection.

The concrete `_PlaywrightBrowserSession` may retain `inspect_surface()` as an
extra concrete capability. ARCH-03 keeps these existing marker names and root
locator unchanged: `worker-ui-contract`, `worker-ui-version`,
`worker-session-state`, `[data-worker-ui-root]`. Do not rename, widen, or change
their semantics; this extraction changes interface ownership only.

### Before/after import examples

Standalone currently reaches across topology boundaries:

```python
from threads_platform.domain.workers import NetworkProtocol
from threads_platform.infrastructure.worker_agent.process_lock import (
    WorkerProcessAlreadyRunning,
    WorkerProcessLock,
)
from threads_platform.workers.browser import (
    BrowserAdapterError,
    BrowserEngine,
    BrowserEngineSession,
    BrowserLaunchRequest,
)
from threads_platform.workers.sessions import NetworkRoute
```

The target Standalone imports shared contracts and generic adapters directly:

```python
from threads_platform.application.ports.browser import (
    BrowserAdapterError,
    BrowserEngine,
    BrowserEngineSession,
    BrowserLaunchRequest,
    BrowserNetworkProtocol,
    BrowserNetworkRoute,
)
from threads_platform.application.ports.process_lock import (
    ProcessAlreadyRunning,
    ProcessLock,
)
from threads_platform.infrastructure.browser.playwright_engine import PlaywrightBrowserEngine
from threads_platform.infrastructure.local.process_lock import FilesystemProcessLock
```

The Playwright adapter currently imports Worker/domain-owned browser contracts:

```python
from threads_platform.domain.workers import NetworkProtocol
from threads_platform.workers.browser import BrowserLaunchRequest, BrowserRuntimeUnavailable
```

The target adapter imports the topology-neutral contract:

```python
from threads_platform.application.ports.browser import (
    BrowserLaunchRequest,
    BrowserNetworkProtocol,
    BrowserNetworkRoute,
    BrowserRuntimeUnavailable,
)
```

The Worker performs the domain-to-engine route mapping before launch:

```python
browser_route = BrowserNetworkRoute(
    protocol=BrowserNetworkProtocol(network_profile.protocol.value),
    host=network_profile.host,
    port=network_profile.port,
)
request = BrowserLaunchRequest(
    profile_directory=profile_directory,
    network_route=browser_route,
    proxy_credentials=resolved_proxy_credentials,
    headless=headless,
)
```

The Standalone direct route is:

```python
request = BrowserLaunchRequest(
    profile_directory=profile_directory,
    network_route=BrowserNetworkRoute(BrowserNetworkProtocol.DIRECT, None, None),
    proxy_credentials=None,
    headless=False,
)
```

Worker capability modules also change imports in ARCH-03 because they currently
import generic browser symbols from `workers.browser`. The exact source family
is:

```text
src/threads_platform/workers/feed_browse.py
src/threads_platform/workers/profile_open.py
src/threads_platform/workers/thread_open.py
src/threads_platform/workers/media_local_upload.py
```

Split their imports by ownership: generic/shared symbols such as
`BROWSER_FEED_ORIGIN`, `BROWSER_FEED_CANDIDATE_BOUND`, `BrowserAdapterError`,
`BrowserContractError`, `FeedCandidateObservation`, `PreparedMediaComposer`,
`SessionExpired`, `ChallengeDetected`, and `RemoteSessionStateUncertain` come
from `application.ports.browser`; Worker policy/orchestration symbols such as
`BrowserNavigationPolicy`, `WorkerBrowserSession`, `WorkerJobExecution`,
`WorkerJobLeaseLost`, and `ActionOutcomeAmbiguous` remain imported from
`workers.browser`. Do not retain or add an implicit or explicit compatibility
re-export from `workers.browser` to avoid these four import updates.

## Expected changed file families by checkpoint

These are planned ownership surfaces, not additional authorization for ARCH-01.

| Checkpoint | Expected source/test families |
|---|---|
| #178 ARCH-02 | Add `application/ports/process_lock.py` and `infrastructure/local/process_lock.py`; remove `WorkerProcessLock` Protocol from `application/ports/worker_agent.py` and Worker-owned implementation module; rewire `workers/runtime.py`, `workers/__main__.py`, `standalone/runtime.py`; update `tests/unit/test_worker_agent_foundation.py` and `tests/unit/test_standalone_runtime.py` lock imports/assertions. |
| #179 ARCH-03 | Add `application/ports/browser.py`; update `workers/browser.py`, `workers/sessions.py`, `workers/feed_browse.py`, `workers/profile_open.py`, `workers/thread_open.py`, `workers/media_local_upload.py`, `infrastructure/browser/playwright_engine.py`, and `standalone/runtime.py`; update browser adapter, standalone runtime, Worker feed/profile/thread/media unit tests and test doubles. Split generic imports to the shared port and Worker policy/orchestration imports to `workers.browser`; do not use compatibility re-exports. Preserve `worker-ui-contract`, `worker-ui-version`, `worker-session-state`, and `[data-worker-ui-root]` exactly, with no selector or semantics change. No redirect rules, timeouts, feature allowlists, proxy capabilities, or WorkerJob behavior change. |
| #180 ARCH-04 | Add `infrastructure/threads_api/environment_credentials.py`; split `infrastructure/threads_api/credentials.py`; update `standalone/__main__.py`, `standalone/api.py`, `tools/credential_admin.py`, `infrastructure/threads_api/composition.py`; update unit and PostgreSQL credential/composition tests. |
| #181 ARCH-05 | Add focused AST/import architecture fitness tests under `tests/unit/` (one test module is sufficient); no runtime implementation or dependency change. Enforce the exact rules below. |

## Dependency matrix

“Allowed dependencies” describes the target after ARCH-05. Relative terms such
as “ports” refer to `threads_platform.application.ports`.

| Layer | May depend on | Must not depend on |
|---|---|---|
| `domain` | Standard library; other domain concepts | FastAPI/httpx/SQLAlchemy/browser/Windows APIs; application ports; infrastructure; workers; standalone; transport/persistence adapters |
| `application/ports` | Domain values where contracts require them; standard library and contract-safe value types | Infrastructure; Worker/Standalone packages; transport or persistence implementations; browser port imports from domain/Worker modules |
| `infrastructure/browser` | `application.ports.browser`; Playwright; standard library | Any `workers.*`; any `domain.*`; Standalone; Worker Agent infrastructure |
| `infrastructure/threads_api` | Threads application ports; domain/API DTOs where needed; configuration; persistence ports for the persistent provider; HTTP library | Worker browser/orchestration; Standalone runtime; environment credential module importing persistence/UoW lifecycle |
| `infrastructure/local` | `application.ports.process_lock`; standard library/OS APIs | Worker Agent package; domain; browser; persistence; transport |
| `infrastructure/worker_agent` | Worker-specific application ports, domain worker concepts, local generic adapters where composed | Ownership or re-export of the generic process lock; Standalone imports |
| `workers` | Worker/domain/application Worker ports; shared browser/process-lock ports; generic adapters at host composition | Standalone; shared browser/process-lock contracts as dependencies |
| `standalone` | Own modules; `application.ports.browser`, `process_lock`, `threads`; `infrastructure.browser`, `infrastructure.local`, `infrastructure.threads_api.client`, `environment_credentials`, `config.settings` | `workers.*`; `infrastructure.worker_agent.*`; domain Worker/network types; persistence, transport, Control Plane composition |
| transport / persistence / Control Plane composition | Application/domain contracts; infrastructure repositories and persistent API composition | Standalone runtime; browser UI/Playwright contracts; Worker implementation internals except through approved ports |

`application.ports.worker_agent` remains a Worker-specific contract module. This
checkpoint moves only its incorrectly housed process-lock Protocol; it does not
authorize a broad cleanup of unrelated port imports such as its current device
identity reference.

## Regression obligations

### Worker obligations

- Process lock still prevents a second local Worker Agent process, records held
  state consistently, and releases on all current shutdown/error paths.
- Worker bootstrap still supplies the same lock path; Worker presence,
  authentication, drain, capacity, and shutdown behavior remain unchanged.
- Account-to-Worker/profile affinity checks remain before engine launch; network
  profile account association remains checked before its route reaches the
  adapter; credentials remain resolved in memory and stay out of repr/log/error.
- DIRECT, HTTP, HTTPS, and SOCKS5 behavior is unchanged, including rejection of
  credentialed SOCKS5. No route falls back silently to DIRECT.
- Feed browse, profile open, and thread open preserve bounded exact target
  recognition, allowlisted navigation, redirects/off-origin blocking, timeout
  outcomes, login/session/challenge interventions, lease renewal/fencing, and
  safe checkpoints.
- Media local upload retains opt-in, reviewed composer association, media type
  and size checks, irreversible boundary, correlation proof, no blind retry,
  and reconciliation behavior.
- Existing evidence includes `test_worker_agent_foundation.py`,
  `test_browser_adapter.py`, `test_feed_browse.py`, `test_profile_open.py`,
  `test_thread_open.py`, and `test_media_local_upload.py`.

### Standalone obligations

- CLI account state remains local JSON metadata; profile directories and locks
  remain account-specific and path-contained.
- Login remains headed, persistent-profile, DIRECT network, manually operated,
  and closed/released under success, browser error, and operator-waiter error.
- A held same-account lock still maps to `ACCOUNT_BUSY`; another account's lock
  does not block login; no Worker ID is generated or passed to the engine.
- Browser adapter errors keep the same sanitized Standalone error mapping.
- API credential binding continues accepting only the dedicated environment
  variable or exact `env://` reference; each API invocation resolves the secret
  without caching; missing/invalid credentials remain sanitized and secret
  values remain absent from output.
- HTTP client posture, supported read commands, API results, and Threads API
  behavior remain unchanged.
- Existing evidence includes `test_standalone_runtime.py`,
  `test_standalone_api.py`, `test_standalone_cli.py`, and
  `test_threads_credential_secrets.py`.

## ARCH-05 exact AST/import fitness rules

The ARCH-05 test must parse source with `ast` and inspect imports without
importing runtime modules. It must implement these exact rules:

1. For every `.py` under `src/threads_platform/standalone/**`, resolve absolute
   and package-relative imports and reject a target equal to or below
   `threads_platform.workers` or `threads_platform.infrastructure.worker_agent`.
2. For every `.py` under `src/threads_platform/infrastructure/browser/**`,
   reject any target equal to or below `threads_platform.workers`,
   `threads_platform.domain`, or `threads_platform.infrastructure.worker_agent`.
3. For `application/ports/browser.py` and `application/ports/process_lock.py`,
   reject targets under `domain`, `workers`, `standalone`, `infrastructure`,
   `transport`, or persistence implementation modules.
4. Inspect the shared `BrowserEngineSession` Protocol and assert its ordered
   operation set is exactly `navigate`, `close`; it must not expose
   `inspect_surface()` or Worker synthetic/session-surface inspection.
5. Inspect the `BrowserLaunchRequest` dataclass. Its ordered annotated field
   names must equal exactly `("profile_directory", "network_route",
   "proxy_credentials", "headless")`; its annotations must resolve to `Path`,
   `BrowserNetworkRoute`, `BrowserProxyCredentials | None`, and `bool` with the
   stated defaults. Reject `worker_id`, `worker_job_id`, account/profile IDs,
   lease/checkpoint/control-client/session-state fields, or extra fields.
6. Inspect `BrowserNetworkRoute` and `BrowserProxyCredentials` dataclasses.
   Their ordered fields must equal exactly `("protocol", "host", "port")` and
   `("username", "password")`; no ownership IDs or credential references may
   be added.
7. Reject `domain.workers.NetworkProtocol` imports from Standalone and
   `infrastructure.browser`. Permit its use only in Worker domain/assignment
   code; `workers.sessions` is the explicit mapping owner to
   `BrowserNetworkProtocol`.
8. In `infrastructure/threads_api/environment_credentials.py`, reject imports
   from `application.ports.repositories`, `domain.accounts`,
   `infrastructure.persistence`, and names `UnitOfWork`, `UnitOfWorkFactory`, or
   `PersistentThreadsAccessTokenProvider`.
9. Reject duplicate definitions, compatibility aliases, wildcard imports, and
   explicit `__all__` re-exports of moved symbols from
   `infrastructure.worker_agent.process_lock`, `workers.browser`, and
   `workers.sessions`. Permit ordinary imports from the new port used internally
   by Worker code. Moved symbols must be defined only at their target modules.
10. Inspect `Import`, `ImportFrom`, package-relative imports, and literal calls
   to `__import__` or `importlib.import_module`; report the source file and
   resolved offending module.

Do not add broad rules that fail on unrelated existing Worker Agent port
dependencies or require importing Playwright, loading credentials, connecting
to PostgreSQL, running live Threads tests, or scanning generated output.

## Unresolved ownership decisions

NONE
