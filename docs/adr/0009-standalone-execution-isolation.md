# ADR-0009: Standalone execution isolation

## Status

Proposed

## Context

ADR-0008 establishes Standalone as an execution topology separate from the
Control Plane and distributed Worker. The accepted source at
`main@cd2a1d73800cfa41bf8c4c646c49bac4dc2bd018` already keeps Standalone
accounts, profiles, and CLI authority local, but several implementation
modules still make Standalone borrow Worker-owned symbols.

The current browser path puts engine ports, request DTOs, adapter errors, and
observation DTOs in `workers.browser`; network launch configuration is in
`workers.sessions`; the Playwright adapter imports both that Worker module and
`domain.workers.NetworkProtocol`. Standalone also imports the Worker Agent's
filesystem lock implementation and passes its local account UUID as
`BrowserLaunchRequest.worker_id`. The environment credential resolver shares a
module with a persistent access-token provider, so importing the resolver also
imports UnitOfWork and persisted account lifecycle contracts.

These dependencies do not mean Standalone currently uses WorkerJob, leases,
Control Plane transport, or PostgreSQL. They do mean process/topology isolation
has not yet become module/dependency isolation.

## Decision

Worker and Standalone are peer execution hosts. Both may consume topology-neutral
application ports and generic infrastructure adapters. Shared contracts live
under `application.ports`; Worker orchestration remains under `workers`; OS and
browser implementations remain under their generic infrastructure packages.

```text
                         application ports
             +----------------+------------------+
             |                                   |
      browser / process lock             Threads API
             |                                   |
    +--------+----------+               infrastructure/threads_api
    |                   |
infrastructure/     infrastructure/
browser             local
    |                   |
    +---------+---------+
              |
       +------+------+
       |             |
     Worker       Standalone
```

Worker-specific account/session orchestration may map domain and affinity data
into shared browser launch contracts. The shared browser engine and its
infrastructure adapter never receive or infer Worker identity, WorkerJob state,
or account/profile/network ownership.

### Process lock ownership

- `ProcessLock` and `ProcessAlreadyRunning` live in
  `threads_platform.application.ports.process_lock`.
- `FilesystemProcessLock` lives in
  `threads_platform.infrastructure.local.process_lock`. It owns the
  OS-specific non-blocking file-lock implementation and implements `ProcessLock`.
- Worker Agent composition and Standalone local composition create this same
  generic adapter for their own lock paths. Worker-specific and account-specific
  lock path selection remains with the respective host.
- The old `WorkerProcessLock` Protocol in `application.ports.worker_agent` and
  `WorkerProcessLock` / `WorkerProcessAlreadyRunning` implementation in
  `infrastructure.worker_agent.process_lock` are removed in ARCH-02. There is
  no second Worker-owned lock implementation.

### Browser contract ownership

The topology-neutral browser engine port and its errors/DTOs live in
`threads_platform.application.ports.browser`. It contains:

- `BrowserEngine` and `BrowserEngineSession`, plus the feed, profile, thread,
  and media engine-session Protocols. The shared base `BrowserEngineSession`
  declares only these operations:

  ```python
  async def navigate(self, url: str, *, allowed_origins: frozenset[str]) -> None: ...
  async def close(self) -> None: ...
  ```

  It does not declare `inspect_surface()` or any Worker synthetic/session
  inspection operation;
- `BrowserSurface` and `PreparedMediaComposer`. `BrowserSurface` remains a
  shared raw observation DTO and does not carry the Worker classifier or policy.
  The feed engine Protocol returns bounded permalink strings; normalized feed
  results are built by topology-neutral application semantics;
- `BrowserAdapterError` and engine/contract outcomes used by the adapter:
  `BrowserContractError`, `LocatorNotFound`, `SessionExpired`,
  `ChallengeDetected`, `RemoteSessionStateUncertain`, `NavigationTimeout`,
  `MediaUploadFailed`, `UnsupportedUIState`, `BrowserProcessCrashed`,
  `BrowserRuntimeUnavailable`, and `BrowserNetworkRouteUnsupported`;
- the shared browser origin and bounded feed-observation contract constants.

The following remain in `workers.browser` because they encode WorkerJob or
Worker capability orchestration: `WorkerJobExecution`, reconnect recovery,
`WorkerBrowserSession`, `PlaywrightBrowserAdapter`, managed Worker profile
resolution, `WorkerSurfaceEngineSession`, `BrowserAccountAffinityMismatch`,
`ActionOutcomeAmbiguous`, `WorkerJobLeaseLost`,
`WorkerJobRetrySafetyViolation`, `BrowserSurfaceState`,
`classify_browser_surface`, `SUPPORTED_UI_CONTRACT_ID`,
`SUPPORTED_UI_CONTRACT_VERSION`, and `BrowserNavigationPolicy`.

`workers.browser.WorkerSurfaceEngineSession` is the Worker-owned structural
Protocol for interpreting the shared raw observation:

```python
class WorkerSurfaceEngineSession(Protocol):
    async def inspect_surface(self) -> BrowserSurface: ...
```

`BrowserEngine.open()` continues returning the shared base Protocol. At the
Worker adapter boundary, Worker code casts/uses the returned concrete session
as `WorkerSurfaceEngineSession`; `WorkerBrowserSession` consumes that
Worker-owned Protocol. Standalone uses only `BrowserEngineSession` and never
depends on or calls synthetic surface inspection.

The concrete `_PlaywrightBrowserSession` in
`infrastructure.browser.playwright_engine` may retain `inspect_surface()` as
an extra concrete capability. ARCH-03 preserves its exact existing markers and
locator—`worker-ui-contract`, `worker-ui-version`,
`worker-session-state`, and `[data-worker-ui-root]`—without renaming, widening,
or changing their semantics. This interface extraction does not change browser
behavior.

`BrowserNavigationPolicy` chooses the allowlist for a specific Worker
capability. The shared Playwright adapter enforces the supplied origin
allowlist for initial and direct top-level navigation requests. Chromium
handles redirects normally; the adapter checks the final page origin after
navigation and rechecks it before each semantic operation. A final off-origin
page is rejected with the existing typed outcome. This keeps the allowlist
unchanged while avoiding manual redirect fetching in the adapter.

### Exact browser launch and network contracts

`BrowserLaunchRequest` in `application.ports.browser` has exactly these fields,
in this order:

```python
profile_directory: Path
network_route: BrowserNetworkRoute
proxy_credentials: BrowserProxyCredentials | None = field(default=None, repr=False)
headless: bool = False
```

It has no account ID, profile reference, Worker ID, WorkerJob ID, lease,
checkpoint, control-client state, or Worker session state. The caller resolves
and validates profile ownership before creating the request; the engine only
receives the resulting directory and engine configuration.

The exact topology-neutral network types in `application.ports.browser` are:

```python
class BrowserNetworkProtocol(StrEnum):
    DIRECT = "DIRECT"
    HTTP = "HTTP"
    HTTPS = "HTTPS"
    SOCKS5 = "SOCKS5"


@dataclass(frozen=True, slots=True)
class BrowserNetworkRoute:
    protocol: BrowserNetworkProtocol
    host: str | None
    port: int | None


@dataclass(frozen=True, slots=True)
class BrowserProxyCredentials:
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
```

The generic route contains only engine routing configuration. It contains no
account ID, network-profile ID, or credential reference. Proxy credential
references stay in Worker-owned metadata until the Worker resolves credentials;
credential values are passed to the engine only in memory.

### Worker network/profile affinity

`domain.workers.NetworkProtocol` and `NetworkProfile` remain business/domain
types used by distributed assignment, transport, and persistence. The Worker
mapping lives in `workers.sessions.NetworkProfileApplication.resolve`:

1. Verify `NetworkProfile.account_id` equals the requested account ID.
2. Map `NetworkProtocol.value` one-to-one to `BrowserNetworkProtocol`.
3. Build `BrowserNetworkRoute(protocol, host, port)` and retain account ID and
   `credential_ref` in a Worker-owned `WorkerNetworkRoute` value.
4. For no profile, build the same Worker-owned value around a `DIRECT` generic
   route with no host, port, or credential reference.

`LocalBrowserSessionManager.open` continues checking its Worker ID, refusing to
move an active session to a different profile, resolving the Worker-managed
profile, and invoking the network-profile account check. Before launch,
`workers.browser.PlaywrightBrowserAdapter.open_reserved_session` continues
checking the Worker ID, reserved account/profile, session state, duplicate
session, and `WorkerNetworkRoute.account_id`. It resolves the Worker proxy
credential reference there, then creates an identity-free
`BrowserLaunchRequest`. `infrastructure.browser.playwright_engine` validates
only engine configuration (direct/proxy field consistency and supported proxy
protocols); it does not compare Worker/account/profile identifiers.

### Standalone browser composition

`standalone.runtime.LocalRuntime` continues deriving the local persistent
profile and account lock paths from its local account ID. It creates
`BrowserNetworkRoute(BrowserNetworkProtocol.DIRECT, None, None)` and a
`BrowserLaunchRequest` containing only the profile directory, direct route,
no proxy credentials, and `headless=False`. It imports the shared browser port,
`infrastructure.browser.playwright_engine`, and
`infrastructure.local.process_lock`; it imports no Worker domain or orchestration
type and manufactures no Worker identity.

### Worker-owned browser modules after extraction

`workers.browser` retains WorkerJob fencing/checkpoint/recovery, Worker session
orchestration, per-capability navigation allowlists, Worker account/profile
affinity checks, Worker-specific errors, and the synthetic Worker UI contract.
It imports the shared browser port for engine Protocols, request/observation
DTOs, and generic adapter outcomes. It does not own generic engine contracts.

`workers.sessions` retains Worker session state transitions, session capacity and
reservation lifecycle, `BrowserSessionManager`, `LocalBrowserSessionManager`,
`NetworkProfileApplication`, `ProxyCredentialProvider`, and
`BrowserSessionOpenResult`. It owns `WorkerNetworkRoute`, which carries the
Worker-side account association and credential reference alongside a
`BrowserNetworkRoute`. It no longer owns the generic browser route or proxy
credential value type.

### Threads credential ownership

- `application.ports.threads.ThreadsCredentialSecretResolver` remains the shared
  resolver Protocol, with its existing sanitized credential errors.
- `infrastructure.threads_api.environment_credentials` owns
  `THREADS_TOKEN_ENV_PREFIX`, `validate_threads_credential_ref`,
  `normalize_threads_credential_ref`,
  `infrastructure.threads_api.environment_credentials._valid_secret_value`,
  and `EnvironmentThreadsCredentialSecretResolver`. Importing this module does
  not import UnitOfWork/repository contracts, persisted account lifecycle, or
  persistence adapters.
- `infrastructure.threads_api.credentials` owns
  `PersistentThreadsAccessTokenProvider` and its UnitOfWork/account lifecycle
  behavior. It may import `validate_threads_credential_ref` and
  `_valid_secret_value` from `environment_credentials`; the provider uses
  `_valid_secret_value` after resolver return for the existing defensive token
  validation. This dependency is one-way. The environment module never imports
  UnitOfWork, repositories, persistent provider, persistence adapters, or domain
  account lifecycle types.
- `infrastructure.threads_api.composition` composes the environment resolver
  and persistent provider for Control Plane processes. Standalone imports only
  the environment credential module and existing Threads API adapter/port.

This split preserves current reference validation, secret lookup, rotation,
expiry, retryability, sanitization, and persistence semantics. It does not add
credential capabilities or alter token lifecycle behavior.

### Compatibility and re-exports

No compatibility aliases or re-exports are retained for moved internal
symbols. Each implementation checkpoint updates all in-repository imports and
tests with the move. In particular, no Worker-owned module re-exports a generic
lock or browser contract. This avoids treating a temporary import path as the
accepted architecture.

### Allowed and forbidden dependency edges

Allowed:

- `application.ports` may refer to domain values where an application contract
  requires them; the browser and process-lock ports remain independent of
  domain/Worker modules.
- `infrastructure.browser` and `infrastructure.local` implement shared
  application ports.
- Worker orchestration may import Worker/domain ports and the shared browser and
  process-lock ports; Worker composition may create generic infrastructure
  adapters.
- Standalone may import its own modules, relevant application ports, and generic
  browser/local/Threads API infrastructure adapters.
- Control Plane/transport/persistence composition may import its application
  contracts and persistent Threads API adapter.

Forbidden:

- `standalone/**` importing `threads_platform.workers.*` or
  `threads_platform.infrastructure.worker_agent.*`;
- `infrastructure/browser/**` importing `threads_platform.workers.*` or
  `threads_platform.domain.*`;
- `application/ports/browser.py` or `application/ports/process_lock.py`
  importing Worker, Standalone, infrastructure, transport, persistence, or
  domain modules;
- `infrastructure/threads_api/environment_credentials.py` importing UnitOfWork,
  repository/persistence, or persisted account lifecycle modules;
- generic browser launch/network contracts carrying Worker, WorkerJob, account,
  profile, lease, checkpoint, control-client, or Worker-session identity/state;
- any shared primitive depending on `workers.*` as its implementation owner.

## Architecture fitness rules for ARCH-05

ARCH-05 will add deterministic AST/import checks, without importing application
modules or requiring credentials, Playwright, a database, or live Threads access.
The checks will:

1. Parse every Python file under `src/threads_platform/standalone/` and reject
   absolute or relative imports resolving to `threads_platform.workers` or
   `threads_platform.infrastructure.worker_agent`.
2. Parse every Python file under
   `src/threads_platform/infrastructure/browser/` and reject imports resolving
   to `threads_platform.workers` or `threads_platform.domain`.
3. Reject those same outward imports from
   `application.ports.browser` and `application.ports.process_lock`, and reject
   imports from `infrastructure`, `standalone`, `transport`, or `persistence`.
4. Inspect `BrowserLaunchRequest` dataclass annotations and assert its ordered
   fields are exactly `profile_directory`, `network_route`,
   `proxy_credentials`, and `headless`; reject Worker/account/profile identity,
   WorkerJob/lease/checkpoint, control-client, or Worker-session fields.
5. Inspect the shared `BrowserEngineSession` Protocol and assert its ordered
   operation set is exactly `navigate`, `close`; reject `inspect_surface()` and
   Worker synthetic/session-surface inspection from the shared base Protocol.
6. Inspect `BrowserNetworkRoute` and `BrowserProxyCredentials` dataclass
   annotations and assert their ordered fields are exactly `protocol`, `host`,
   `port`, and `username`, `password`, respectively.
7. Reject imports of `domain.workers.NetworkProtocol` from Standalone and
   `infrastructure.browser`; permit the explicit Worker mapping in
   `workers.sessions` only.
8. Parse `infrastructure.threads_api.environment_credentials` and reject
   imports from `application.ports.repositories`, `domain.accounts`,
   `infrastructure.persistence`, or symbols named `UnitOfWork`,
   `UnitOfWorkFactory`, or `PersistentThreadsAccessTokenProvider`.
9. Reject duplicate definitions, compatibility aliases, wildcard imports, and
   explicit `__all__` re-exports of moved symbols from
   `infrastructure.worker_agent`, `workers.browser`, and `workers.sessions`.
   Permit normal imports from the new shared port when Worker implementation
   needs those types internally.

10. The AST walker will resolve `Import`, `ImportFrom`, package-relative
    imports, and literal `__import__` / `importlib.import_module` calls. It will
    report the offending source path and import target. It will not blanket-ban
    the existing Worker Agent ports or redesign unrelated `application.ports`
    dependencies.

## Migration sequence

1. **ARCH-02 / #178:** move the process-lock Protocol/error to
   `application.ports.process_lock`, move the filesystem implementation to
   `infrastructure.local.process_lock`, update Worker and Standalone composition
   and focused lock tests, and remove Worker-owned lock definitions.
2. **ARCH-03 / #179:** add `application.ports.browser`; move generic browser
   contracts, errors, DTOs, network configuration, and shared origin/bound
   constants; split Worker-only affinity metadata into `WorkerNetworkRoute`;
   rewire the Playwright adapter, Worker orchestration, Standalone, and the
   `workers.feed_browse`, `workers.profile_open`, `workers.thread_open`, and
   `workers.media_local_upload` capability modules. Those four modules split
   imports so generic/shared browser symbols come from `application.ports.browser`
   while Worker orchestration/policy symbols remain from `workers.browser`;
   no compatibility re-export is used. Keep the existing synthetic inspection
   markers/selectors exactly unchanged. Remove all Worker/domain imports from
   `infrastructure.browser` and all Worker imports from Standalone.
3. **ARCH-04 / #180:** move environment credential primitives/resolution to
   `infrastructure.threads_api.environment_credentials`; leave the persistent
   provider in `credentials`; update Standalone, admin tooling, composition,
   and credential tests.
4. **ARCH-05 / #181:** add the AST/import fitness checks above and verify the
   exact module and dataclass contracts.

Each checkpoint preserves its existing behavior and is reviewed before the
next starts. ARCH-06 remains the later standalone substrate re-acceptance gate
defined by epic #176.

## Consequences

- Standalone and Worker become peer consumers of shared browser and local OS
  primitives.
- Browser engines receive only filesystem and engine configuration; Worker
  affinity checks remain above the adapter.
- Standalone no longer needs to construct or carry a fake Worker identity.
- Environment credential use no longer loads persistent UnitOfWork/account
  lifecycle contracts at import time.
- The shared browser port includes engine-shaped read Protocols and the Threads
  Web origin/permalink bound because both hosts use the same bounded primitives.
  This is a topology-neutral adapter contract, not authorization for Standalone
  to use Worker capabilities.
- A future topology needing new browser identity, routing, credential, or
  session fields must update this ADR rather than extending the engine request
  with host-specific state.

## Non-goals

This ADR does not authorize runtime implementation in ARCH-01, browser selector
or navigation changes, redirect/timeout/fail-closed changes, WorkerJob
lease/fencing/recovery changes, profile migration, proxy behavior changes,
Threads API behavior or credential semantic changes, new Standalone
capabilities, live Threads/Meta calls, database/schema changes, dependencies,
generic plugin/DI frameworks, or broad Worker/Controller redesign.
