# Desktop v1 — Security, Pairing and Account Data Contracts

**Status:** DX-05 operator identity, session, role and lifecycle policy is implemented against the frozen #99 authorization; Controller TLS/pairing and later account flows remain planned.

## 1. Three separate identities

| Identity | Owner and purpose | Storage and boundary |
|---|---|---|
| Windows interactive account | Runs one provisioned Controller/Worker node on a 24/7 PC | Existing Windows logon; never used as Threads RBAC identity |
| Controller trust identity | Root of application-private TLS trust, scoped to one Workspace | Controller private key encrypted with current-user DPAPI; only Rust/native and TLS leaf endpoint must not hold Controller root CA private key |
| Worker device identity | Proves an enrolled Worker is allowed to claim/report jobs | Existing Worker UUID + Ed25519/current-user DPAPI key and challenge/device session; Python Worker owns it |
| OperatorUser identity | Human authorization, audit and role-specific account management | Controller PostgreSQL; independent from Windows and Worker device identity |

One Controller serves exactly one Workspace. Controller replacement/identity rotation is an explicit future recovery/migration event, not a silent first-run path.

## 2. Secrets and allowed transport

| Material | May exist on | May be sent to Controller | Forbidden |
|---|---|---|---|
| Operator password | React input transiently -> Rust -> Controller login endpoint | HTTPS remotely; loopback HTTP only on local Controller | Persisting in UI/localStorage/CLI/log |
| Operator session bearer | Rust process memory | HTTPS Authorization header; loopback HTTP only on local Controller | Returning to React, persisted webview storage/log |
| Controller root private key | Controller current-user DPAPI-protected state | No, except private TLS process provisioning as strictly required | Worker/Console/global CA installation |
| Controller root public cert/fingerprint | Controller, verified Worker and Console | Public trust anchor | Treating unverified certificate as trusted without a comparison |
| DB credential | Controller protected configuration / child env or secure launch channel | Internal loopback only | UI/log/argv/Git |
| Worker private key | Existing Worker DPAPI profile | No | Rust copy, React, Controller DB, cross-Workspace reuse |
| Worker enrollment secret | Controller short-lived invitation and Worker transient input | Only **after** trust confirmation | Saved host-config, process argv, task XML or logs |
| Browser cookie/storage/password/MFA/profile directory | Owning Worker browser profile / human browser only | **Never** in onboarding/session-summary protocol | Controller DB, events/telemetry, screenshots or generic debug dump |
| Threads API/OAuth token | Existing approved Controller secret provider | Only through existing approved provider | Mixing with browser storage/Worker pairing credentials |

Tauri allowed commands are narrowly scoped; webview cannot choose arbitrary binary, filesystem path, URL proxy, TLS validation disablement or HTTP Authorization header. Rust maintains a per-Controller authenticated client with a deliberately small IPC response surface.

## 3. Human login and sessions

- Initial Workspace and first OWNER are provisioned locally in a one-shot bootstrap transaction before the LAN Operator API opens. Windows invokes the local runtime's `bootstrap-owner` mode with the password on stdin. Linux/Docker uses `python -m threads_platform.operator_bootstrap --username <name>` with the password on stdin. No network Owner-bootstrap endpoint exists; repeat bootstrap fails.
- Passwords use salted scrypt (`N=32768`, `r=8`, `p=1`). Passwords are at least 12 characters; login failures use a generic response and lock an account after 5 failures in a 15-minute window for 5 minutes. Audit events contain no credential values.
- `OperatorSession` uses a 256-bit random bearer. PostgreSQL stores only its SHA-256 digest, issue/expiry/revocation and user association. Sessions expire after 8 hours by default. Every request checks current user status and current role; role change/disable takes effect on the next request.
- Rust retains bearer in process memory. React gets safe `/me` details and typed DTOs only. Window hide (including X-to-tray), Windows session lock/unlock, 5 minutes of inactivity and explicit Operator logout are session-lock boundaries; a hidden-window reopen requires sign-in. Ordinary window focus loss and generic document visibility changes do not revoke the session. Locking the human UI does not stop Controller/Worker.
- Remote Operator URLs must use HTTPS with normal certificate validation and redirects disabled. Plain HTTP is accepted only for loopback Controller access; LAN TLS provisioning is DX-06.
- A Worker continues job protocol under **device identity** while no human is logged in. A human Operator session in Worker Desktop is solely an authorization to perform permitted operator actions. Deliberate Worker Quit/Restart requires OWNER/ADMIN/OPERATOR; Controller stop/restart requires OWNER/ADMIN. A Worker device credential can complete but cannot initiate drain. No device self-drain endpoint or hidden admin bearer exists. UI logout does not stop the node.
- Windows always ignores the legacy `worker_admin_token`; Linux/IT migration compatibility is opt-in with `THREADS_PLATFORM_WORKER_ADMIN_AUTH_PROFILE=legacy_linux_it`. Existing CRM ingress remains a separate principal.
- No localhost/same-Windows-user exemption for Owner. Console and Controller use same authorization policy and server.

## 4. Controller TLS identity and first-contact protocol

**No credentials, enrollment code or operator password may be transmitted before first-contact trust is established.** LAN address is a locator, not an identity.

1. Controller provisions a long-lived private trust root and current SAN-correct TLS server certificate for the configured stable LAN endpoint and loopback access. It stores private trust material DPAPI-encrypted and ACL-protected; protect issuance of subordinate certificates.
2. Controller local Desktop reads the canonical **root** fingerprint directly from its local protected identity (not an untrusted incoming website). Console/Worker fetch an untrusted peer TLS certificate chain from the entered address in a special **bootstrap-only** client. This client cannot invoke authenticated APIs and never disables verification for normal Operator/Worker traffic.
3. User compares a sufficiently long, collision-resistant rendered verification fingerprint from both independent views via a trusted channel. A cosmetic matching code only in the untrusted remote UI is not sufficient. If a legitimate Controller display is unavailable, the operator must use an equivalent authenticated offline/out-of-band transfer; do not provide a `Trust anyway` fallback.
4. Upon exact root identity match, persist that root as **application-private** trust and establish an ordinary verified HTTPS client. Verify certificate chain, SAN (IP or hostname), validity, expected Controller root and TLS constraints on *every* connection.
5. Console then shows login. Worker then submits a short-lived code and device enrollment. Do not install the Controller CA into the OS global trust store.
6. Endpoint or certificate rotation is explicit, authorized and auditable; silent new root means **trust mismatch** requiring re-verification. IP-only DHCP churn is not silently repaired by changing endpoint.

**Threat cases to test:** attacker swaps LAN endpoint during first contact; self-signed fake peer returns a visually plausible fingerprint; stale/expired cert; wrong SAN after IP change; chain substitution; proxy redirects; HTTP downgrade; trust file tampering; stolen/incorrect pairing code; rate-limit boundary. Fingerprint comparison must be against actual TLS trust key material, not display labels or an unbound invite code.

## 5. Worker one-time pairing and decommission

- OWNER/ADMIN requests a server-attributed audited enrollment session. The **current accepted Worker enrollment code is a 256-bit `secrets.token_urlsafe(32)` value**, stored as a SHA-256 digest, single-use with a 10-minute expiry. Preserve that exact high-entropy credential and wire contract. First Desktop version may securely convey the full token after TLS trust via copy/paste. Any optional six-digit UX is a **separate, security-reviewed short-code redemption** endpoint *after* verified TLS, yielding the unchanged high-entropy enrollment credential; short code requires keyed hash/HMAC, strict global/per-invitation rate limiting and distributed guessing tests. Do not silently replace the Worker enrollment token with six digits.
- The UI hides pairing code in logs, crash diagnostics, analytics and persistent browser storage. Its lifetime is shown. The Controller proves authorization to enroll, not first-contact identity.
- Worker completes independent Controller fingerprint verification **first**. Rust supplies code transiently to existing Python Worker enrollment via the permitted process environment; never saved in local host config, command line or Windows task definition.
- Controller associates Worker public key/UUID with the single Workspace and enforces existing challenge, device sessions, Worker protocol version, presence/capability and WorkerJob claim authorization.
- Revoke denies future Worker claims/enrollment even if Worker has a cached session. Offline force-revoke means the old machine cannot be remotely wiped or stopped while offline; its next authenticated reconnect fails. No promises of remote browser-data deletion.
- Change Controller: request drain; reach quiescence and OFFLINE; revoke at old Controller when reachable; locally quarantine all previous-workspace profile refs/bytes and local session journals per tested retention policy; clear old trust/enrollment; pair with new Controller. If old Controller is permanently unreachable, warn that only local decommission is possible and prior authority must revoke that device independently if recovered.
- Old browser profiles are never automatically imported/attached on new Workspace, and local cleanup is separately confirmed. No auto-migrate via copy, symlink, network share or profile re-key.

## 6. Worker-centric browser onboarding transaction

The user chooses **Add Threads account on Worker**, but the Worker never creates authoritative business Accounts.

```text
OWNER/ADMIN logs into Worker Desktop via Operator API
        |
Rust requests Controller onboarding intent (operator bearer)
        |  Controller binds: intent_id, worker_id, actor, label, expiry
Worker creates a locally isolated pending profile, opens headed Threads login
        |  human enters password/MFA directly in Chromium
        |  bounded approved session validation -> state evidence
Python Worker sends device-authenticated completion with intent_id,
        |  worker_id inferred from device auth, opaque profile_ref,
        |  event/version, idempotency key, bounded session status
Controller transaction validates unconsumed intent + worker binding + state
        |  creates Account + BrowserProfile + AccountWorkerAssignment
        |  consumes intent + appends actor-linked audit
        v
Controller returns canonical account_id; Worker binds local profile
        v
Existing account-affine WorkerSession/WorkerJob protocol resumes
```

This is an **onboarding intent**, not a separate persistent candidate Account. It exists so the Controller can bind human permission to the subsequently authenticated device completion without handing the human session bearer to the Python Worker.

### Minimal payload

```json
{
  "intent_id": "opaque-id",
  "profile_ref": "opaque-logical-ref",
  "idempotency_key": "unique-per-attempt",
  "validation": {
    "session_state": "AUTHENTICATED",
    "adapter_version": "reviewed-version",
    "observed_at": "UTC-timestamp"
  }
}
```

Device identity is derived from TLS-authenticated Worker session, not from user-supplied `worker_id`. Avoid raw URL/DOM/screenshot in evidence. Only a narrow reviewed adapter may state AUTHENTICATED; the UI's “I've finished” button triggers re-validation rather than asserting trust.

### Server invariants

- Validate intent issuer role against **current** actor role/status, target Worker enrollment/affinity, expiry, consumed state and per-actor/Worker limits. An intent is revoked if creator loses permissions.
- Controller persists Account, BrowserProfile reference, one active Assignment, intent consumption and audit **in one DB transaction**; unique constraints enforce registration idempotency. Same idempotency key returns same result, changed payload rejects. **Existing Worker `LocalBrowserSessionManager.open()` and Controller `WorkerSessionService.account_context()` require a preexisting Account and Assignment**. DX-09 must provide a separate bounded provisioning-only pending-profile launch **before** registration; it never creates WorkerJobs or bypasses established account affinity.
- **Network preflight:** Because existing `NetworkProfile` is account-scoped and pending onboarding has no Account yet, explicit DIRECT-only onboarding is permissible only when no configured proxy route is required. If one is required, stop before browser login until an approved pre-account route and proxy-credential provider exist; current packaged Worker cannot silently resolve production proxy secrets. Route changes after credential-bearing login require a separately reviewed re-login path.
- A local pending profile never receives business Commands and can be safely retried after temporary Controller disconnect until intent expiry. On expiry request a fresh authenticated human intent, preserving local profile only if policy allows; no unbounded unauthorized pending workspace.
- If registration response is lost after commit, idempotent retry must retrieve the original account_id. No duplicate accounts/assignments.
- Only owning Worker may report that profile's subsequent session state; revisions fence stale state updates and assignment changes. The approved browser read/upload capabilities do **not** automatically prove fresh-login recognition. Implement a separately reviewed login-state adapter with negative unknown-UI tests; never equate the user confirmation button with AUTHENTICATED. If Controller commits but Worker crashes before local binding, reconcile by idempotent intent to the **original** account_id/profile_ref. New onboarding endpoints require explicit additive Worker protocol/capability version negotiation; old v2 Workers must fail closed on unsupported calls.
- Browser profile is a **local credential container**, not a migratable blob. Never transmit cookie, localStorage, sessionStorage, password, OTP, raw DOM, profile path, request headers, unredacted page URLs or screenshot as a registration field.

## 7. Identity, execution modes and account consistency

- New browser-only Account has an internal UUID, operator label, `identity_state=LOCAL_ONLY`, canonical remote `threads_user_id=NULL`, `username=NULL`, mode `BROWSER_ONLY` and Worker/profile assignment. Nullable remote fields require explicit migration and partial unique constraints; never synthesize `browser:` IDs or `unknown` usernames.
- A verified remote ID must come from approved evidence. A user label or claimed username cannot prove browser profile ownership. OAuth verifies the **API principal**; it does **not**, by itself, prove that an already-logged-in browser profile is the same principal.
- Before marking cross-executor identity binding verified or enabling unsafe cross-executor operations, require reviewed reliable browser-to-remote evidence; if unavailable keep the browser binding `UNVERIFIED`, require deliberate operator confirmation for presentation only, and do not silently merge two Accounts. `HYBRID` provisioning/dispatch must honor capability evidence/authorization rather than relying on display name.
- Four accepted modes remain `API_ONLY`, `BROWSER_ONLY`, `HYBRID`, `MANUAL`. AccountStatus, credential health, worker presence and browser session state remain independent. `Worker OFFLINE` does not set AccountStatus ERROR. Capability Router remains authority over approved API fallback and side-effect recovery.
- Re-login occurs on the same owning Worker and profile, never by passing credentials over Operator API. MFA/challenges are human-assisted; browser adapter must fail closed on unknown Threads UI.
- Reassigning Account is an authenticated Controller mutation coordinating old lease/drain/quiescence with the existing account-execution lease. New Worker must create a new profile and ask the human to login. Do not imply automatic profile migration or guarantee old Worker cleanup while disconnected.

## 8. Legacy admin paths, TLS termination and migration profiles

Existing `/v1/workers/enrollments`, admin drain/abort and intervention resolution are protected by a distinct `worker_admin_token` static bearer; request fields `created_by` and `resolved_by` currently accept untrusted caller-provided strings. DX-05 must inventory all privileged routes and migrate new Windows Desktop mutations to canonical Operator authentication/RBAC, with **server-derived human actor**, or **explicitly isolate and disable** the old token path on the Windows customer deployment profile. Linux/IT compatibility requires a restricted opt-in plan and regression tests; no accidental token interchange with CRM ingress, Worker device auth or Operator session. A new Operator API alone does not close the old bypass.

Existing `WorkerTransportTLSMiddleware` validates Worker request ASGI `scope.scheme` only on `/v1/workers` paths and relies on an externally trusted TLS ingress. Linux/Docker Controller remains an official IT/developer profile: DX-06 must specify a **non-DPAPI** secret/trust-identity store, HTTPS/WSS ingress and locally authenticated CLI fingerprint for independent verification; neither Python business/domain nor Linux startup may require Tauri or Windows DPAPI. DX-06 must choose and test the real TLS terminator, leaf vs root private key custody, WSS upgrade compatibility, narrowly trusted proxy headers if any, and enforce HTTPS for all **public Operator and Worker** routes. First-contact untrusted certificate fetch is isolated, read-only and **never** carries an Operator password or Worker enrollment code. M1 only binds to loopback with disposable state; public LAN starts after DX-05/06 signoff.

## 9. Threat-model checklist before acceptance

- [ ] Operator session stolen through React/XSS/developer tools (token never exposed to JS; minimize IPC response data).
- [ ] Untrusted first-connection TLS / intercepted pairing code / fingerprint spoofing.
- [ ] Worker device authorized without human add-account permission or stale intent after role revocation.
- [ ] Deliberate Worker Quit without a human Operator session after legacy admin token isolation: must obtain approved Operator authorization or pass a security-reviewed self-only device drain contract; never silently embed a global administrator key.
- [ ] Windows Worker/Console securely connect to a Linux/Docker Controller over verified HTTPS/WSS with independent trust verification and no Windows DPAPI assumptions in Python domain.
- [ ] Cross-Workspace reuse of browser profile/private key after re-pair.
- [ ] Windows privilege escalation through Tauri capabilities/process argv/path traversal/symlink ACL.
- [ ] Two Worker processes or two Controller supervisors sharing one live data root.
- [ ] Login/Owner lockout, last-owner disable race, credential/session logs or backups leaking secrets.
- [ ] Worker offline/revoked session state wrongly shown current; stale WorkerJob lease finalizes after reassignment.
- [ ] API OAuth identity wrongly merged with unverified browser identity.
- [ ] Remote features appear as released while #3/#80/#62/#11 gate remains open.
