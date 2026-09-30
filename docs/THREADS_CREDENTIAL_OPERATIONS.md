# Threads credential operations

## Checkpoint and release status

The provider work in #70 is based on the accepted, scrubbed Phase A evidence at
[`threads-live-phase-a-2026-09-30.json`](evidence/threads-live-phase-a-2026-09-30.json).
It covers OAuth exchange and refresh observations, effective scopes, token
validity/expiry, and own-profile behavior. Phase B is `NOT_RUN`. Issue #3 stays
OPEN as a production/release gate; enabling the provider does not claim
production release readiness. Parent #55 remains open pending coordinator
acceptance of the provider implementation. The separate CRM result transport
dependency #62 also remains open.

## Enable the provider

FastAPI and the standalone scheduler use the same provider and handler
composition. Set:

```text
THREADS_PLATFORM_THREADS_TOKEN_PROVIDER_MODE=environment
```

The default is `disabled`. Environment mode requires
`THREADS_PLATFORM_DATABASE_URL`. Both processes need the same PostgreSQL
metadata and their deployment secret injection must provide the environment
names referenced by that metadata.

PostgreSQL stores only `credential_ref`, `token_type`, `granted_scopes`,
`expires_at`, and status in `oauth_credentials`. It never stores access or
refresh token values. The only supported reference form is:

```text
env://THREADS_PLATFORM_THREADS_TOKEN_<VERSIONED_NAME>
```

The variable name must match
`THREADS_PLATFORM_THREADS_TOKEN_[A-Z0-9_]+` and is bounded in length. The
resolver looks up only that exact name; it does not enumerate the environment.
File, URL, shell, interpolation, query, and other URI forms are rejected.
Tokens are not cached. Each process reads its own immutable startup
environment, so an environment change requires restart or redeployment.

The provider accepts only an existing non-disabled account with ACTIVE
credential metadata, Bearer token type, future expiry, `threads_basic`, a valid
reference, and a valid resolved secret. Missing or malformed environment
secret values produce a retryable `THREADS_CREDENTIAL_SECRET_UNAVAILABLE`
without changing credential or account state. Expiry atomically marks the
credential `EXPIRED` and a
non-disabled account `REAUTHORIZATION_REQUIRED`. Missing metadata and invalid
metadata fail permanently and require reauthorization. Runtime token refresh
is not implemented.

## Bind, rotate and revoke metadata

The metadata-only CLI never accepts a token value. Bind and rotate take only an
account UUID, allowed environment reference, UTC expiry, token type, and scope
names. For example, using placeholders:

```powershell
uv run threads-credential-admin bind `
  --account-id <account-uuid> `
  --credential-ref THREADS_PLATFORM_THREADS_TOKEN_ACCOUNT_A_V2 `
  --expires-at 2026-11-29T08:50:00Z `
  --token-type Bearer `
  --scope threads_basic `
  --scope threads_content_publish `
  --scope threads_manage_insights `
  --scope threads_manage_replies `
  --scope threads_read_replies
```

`rotate` is an alias for the metadata upsert performed by `bind`. Binding
requires an existing account, sets credential metadata ACTIVE, and restores a
`REAUTHORIZATION_REQUIRED` account to ACTIVE. It never reactivates a DISABLED
account. The CLI does not print the credential reference or environment value.

Check that a deployment process can resolve a reference without printing or
returning the secret:

```powershell
uv run threads-credential-admin verify `
  --credential-ref THREADS_PLATFORM_THREADS_TOKEN_ACCOUNT_A_V2
```

Revoke metadata with:

```powershell
uv run threads-credential-admin revoke --account-id <account-uuid>
```

Revoke sets the credential to REVOKED and a non-disabled account to
`REAUTHORIZATION_REQUIRED`. It does not delete or display the deployment
secret. Unknown CLI arguments are reported without echoing their values.

## Refresh and versioned rotation

Refresh the long-lived token through the approved Meta flow outside this
application and provision its value under a **new versioned** environment name.
Do not pass token values through the CLI, application logs, PostgreSQL, issues,
or pull requests.

Use this order:

1. Complete refresh outside the application.
2. Provision the refreshed token as a new
   `THREADS_PLATFORM_THREADS_TOKEN_<VERSIONED_NAME>` deployment secret.
3. Deploy/restart FastAPI and scheduler processes with both old and new names
   available.
4. Run the metadata-only `rotate`/`bind` command with the new reference, expiry,
   token type, and scopes.
5. Run `verify` from the new deployment context; it reports availability only.
6. Drain old processes, then remove the old environment secret.

Never update expiry metadata to describe a new token while any process can see
only the old token. Do not overwrite a versioned environment value in place.
There is no automatic refresh or remote-401 revocation detection in this
checkpoint; operator bind/rotate and revoke are the credential-state authority
for v1.
