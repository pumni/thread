# ADR-0006: Playwright Browser Adapter Foundation

- Status: Accepted
- Date: 2026-09-26

## Context

C3-02 needs a Windows-first, headful-capable browser adapter that reuses C3-01 managed profiles and local session capacity, has an async lifecycle compatible with the Worker Agent, supports local synthetic UI contracts, and keeps browser and DOM types out of domain/application code. It must stop before production Threads selectors or business actions.

The current project requires Python 3.14. The engine and browser binary must be installed reproducibly, and ordinary worker startup must not download binaries.

## Candidates

### Playwright for Python

Upstream documents Python 3.8+, Windows 11/Windows Server 2019 support, and synchronous and asynchronous Python APIs. Its persistent-context launch accepts a user-data directory, proxy configuration, and lifecycle options. The APIs include file upload and download support and browser/page crash or disconnect events. Browser binaries are installed separately and are tied to the Playwright package version; the upstream Windows cache is under `%USERPROFILE%\AppData\Local\ms-playwright`, and the installed browsers consume hundreds of megabytes.

Tradeoffs: the Windows Python wheel includes the Playwright driver runtime, and Chromium adds a substantial separately installed binary. Package/browser versions must move together. This project will pin the Python package in `uv.lock`, use the matching Chromium binary, and install it explicitly during worker provisioning and CI. Worker startup will fail with a bounded error if Chromium is absent or incompatible; it will not download a binary.

### Selenium WebDriver for Python

Upstream documents Python 3.10+ and supports Windows browsers, proxy capabilities, file upload and browser downloads. Browser-specific options can point at a persistent user-data directory. Selenium's Python examples use synchronous WebDriver calls; the official Python binding does not document an async lifecycle API comparable to Playwright's async API. Selenium Manager can resolve browser drivers, but relying on startup-time resolution would make the worker's binary state less deterministic. A separately provisioned browser and matching driver can avoid that behavior, at the cost of another versioned deployment component.

Tradeoffs: the Python client package is comparatively small and WebDriver is a vendor-backed standard, but persistent profiles and downloads depend more on browser/driver-specific behavior. The current worker is async and the engine would need thread offloading or a separately managed command execution layer.

## Decision

Use the official Playwright Python package with Chromium for the C3-02 adapter.

The locked implementation dependency is `playwright==1.63.0`, imported only in `threads_platform.infrastructure.browser`. Engine-neutral contracts and typed project errors live in the worker layer. The adapter launches a persistent context using the profile directory resolved by C3-01, and it consumes only the account-specific `NetworkRoute` and credentials returned by the existing `ProxyCredentialProvider`. Credentials remain in memory for launch and are excluded from exception text, checkpoints, logs, and diagnostics.

Install the package with the locked project dependencies, then provision Chromium explicitly:

```powershell
uv run playwright install chromium
```

CI installs the same locked Chromium before adapter tests. The application does not invoke browser installation or an updater at startup. Playwright upgrades and its matching Chromium revision are deliberate dependency updates.

The adapter supports authenticated HTTP/HTTPS proxies through Playwright's in-memory proxy options. It supports SOCKS5 without credentials. It fails closed with a safe typed configuration error for credentialed SOCKS5 because the current Playwright Python proxy contract documents username/password for HTTP(S) proxy authentication, not SOCKS5 authentication. No route is silently changed to direct traffic.

The C3-02 contract is limited to managed lifecycle, navigation, exact versioned synthetic contract inspection, and safe error mapping. It exposes no general script, selector, or click API. Synthetic mutation-boundary tests use a local test operation guarded by a current WorkerJob lease; they do not add a production browser action.

## Consequences

- Playwright browser types and imports stay inside infrastructure; the Control Plane and domain/application layers do not depend on the engine.
- Persistent profile ownership, local session reservations, session states, and NetworkProfile ownership continue to come from C3-01.
- Chromium requires a separate, explicit install step and several hundred megabytes of worker/CI storage. Its version is coupled to the locked Playwright package.
- The adapter can use an operator-visible headed session on Windows and headless Chromium for deterministic synthetic fixtures.
- Authenticated SOCKS5 routes are rejected until an approved engine capability or credential transport is documented and tested.
- Browser crashes never prove that a remote side effect failed. A crash after a recorded mutation boundary requires reconciliation or intervention.

## Upstream references

- [Playwright Python installation and supported systems](https://playwright.dev/python/docs/intro)
- [Playwright Python browser launch and persistent context](https://playwright.dev/python/docs/api/class-browsertype)
- [Playwright Python proxy configuration](https://playwright.dev/python/docs/network)
- [Playwright Python file uploads](https://playwright.dev/python/docs/input)
- [Playwright Python downloads](https://playwright.dev/python/docs/downloads)
- [Playwright Python browser binary installation and cache](https://playwright.dev/python/docs/browsers)
- [Selenium Python bindings and supported Python versions](https://www.selenium.dev/selenium/docs/api/py/)
- [Selenium WebDriver options and proxy](https://www.selenium.dev/documentation/webdriver/drivers/options/)
- [Selenium WebDriver remote downloads](https://www.selenium.dev/documentation/webdriver/drivers/remote_webdriver/)
