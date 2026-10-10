# SPLIT-00 state compatibility

## Scope and handling

This contract records the local JSON and path behavior in `pumni/thread@0dfbd36ff3756f8a68edcd6cc0bd083f6f3570a1`. No account data, token, browser profile, cookie, local evidence or real identifier was read or copied for this inventory. No migration is authorized or performed in SPLIT-00.

Standalone state is operational, machine-local state. It is not a PostgreSQL replica and has no distributed business authority. Worker profiles and Standalone profiles are separate; do not share or automatically migrate a profile across execution modes.

## Data root and path contract

`THREADS_LOCAL_DATA_ROOT` is the explicit root override. An empty override fails closed. On Windows, when unset, the current default is `%LOCALAPPDATA%/ThreadsOperations/standalone`; on non-Windows platforms an explicit root is required. `threads-local` account IDs are canonical UUIDv4 values. Preserve account alias spelling in JSON and its case-folded account filename.

```text
<data-root>/
  accounts/<alias.casefold()>.json
  profiles/<account-uuid>/
  locks/<account-uuid>.lock
  operations/<operation-uuid>.json
  recurrences/<recurrence-id>/
    record.json
    workflow.json
    runner.lock
  nurture/
    locks/<account-uuid>.lock
    runs/<account-uuid>/{active,completed}/<run-uuid>.json
    state/<account-uuid>/<preset-id>.json
    content/<account-uuid>/<preset-id>.json
    insights/<account-uuid>/<content-fingerprint>/<snapshot-id>.json
```

The browser/profile lock path and mutation lock path use `locks/<account-uuid>.lock`. Nurture deliberately owns a separate `nurture/locks/<account-uuid>.lock`; it does not alias the capability lock. Each recurrence has its own `runner.lock`. Keep these lock scopes and ownership semantics distinct.

Paths are rooted and resolved before use. Source stores check expected parent relationships, regular-file status and, where implemented, symlink/reparse-point conditions. Preserve containment checks and fail-closed behavior. Do not flatten directories, case-normalize opaque state names, follow links, or deserialize/reserialize state as a migration shortcut.

## Exact v1 document contracts

All persisted schemas below are v1 at the frozen baseline. Readers reject unknown or missing fields where the source uses strict key sets. UUID fields and file names are canonical; UUIDs created for local account, operation and Nurture run identity are UUIDv4. Timestamps use canonical UTC text with `Z` in Nurture state. Preserve existing bytes and field values unless a separately authorized, versioned migration defines otherwise.

| Store / owner | Path | Exact v1 fields and invariants |
| --- | --- | --- |
| Local account, `standalone/accounts.py` | `accounts/<case-folded-alias>.json` | Required keys are `version`, `id`, `alias`; optional `credential_ref` is the only additional key. `version` is integer `1`; `id` is canonical UUID; `alias` is 1-64 characters matching the current ASCII alias rule. A credential reference is a reference, never a token value. |
| Mutation journal, `standalone/mutations.py` | `operations/<operation-uuid>.json` | Required keys: `version`, `id`, `account_id`, `kind`, `phase`. Allowed additional keys: `container_id`, `media_id`, `outcome_code`, `child_container_ids`, `action`. Version is `1`; each journal is bounded to 8192 bytes; at most 20 carousel child IDs. `child_container_ids` is carousel-only; `action` is moderation-only. Preserve current operation kind/phase validation and exact permitted transitions. |
| Workflow input / recurrence snapshot, `standalone/workflows.py` and `recurrences.py` | Operator path; recurrence copy at `recurrences/<id>/workflow.json` | Workflow root keys are exactly `version`, `account`, `steps`; version `1`; 1-32 ordered steps; at most 65536 bytes. Each step has an exact action-specific schema. A text-post workflow step may appear at most once and only last. Recurrence creation accepts only the source-defined READ-only plan; preserve its validated snapshot and do not introduce catch-up or mutation recurrence. |
| Recurrence record, `standalone/recurrences.py` | `recurrences/<id>/record.json` | Exact keys: `version`, `id`, `workflow_file`, `account`, `interval_seconds`, `status`, `created_at`, `updated_at`, `anchor_at`, `next_due_at`, `last_started_at`, `last_finished_at`, `last_outcome`, `last_error_code`, `last_step_index`. Version `1`; intervals are bounded by source constants; due times remain anchored; record transitions, last outcome/error and step index are durable. |
| Nurture run receipt, `standalone/nurture_store.py` | `nurture/runs/<account-uuid>/active|completed/<run-uuid>.json` | Exact keys: `version`, `id`, `account_id`, `preset_id`, `preset_version`, `started_at`, `finished_at`, `outcome`, `discovered_count`, `deduped_count`, `selected_count`, `enriched_count`, `replied_count`, `published_count`, `skipped_count`, `decision_codes`, `operation_ids`, `error_code`, `failed_stage`. Version and initial preset version are `1`; operation IDs link to the mutation journal. A run moves from `active` to `completed` only through the existing store transition. |
| Nurture target state, `standalone/nurture_store.py` | `nurture/state/<account-uuid>/<preset-id>.json` | Root keys: `version`, `account_id`, `preset_id`, `targets`. Each target has exactly `fingerprint`, `first_seen_at`, `last_seen_at`, `last_decision_code`, `last_action_at`, `action_state`, `last_run_id`, `last_operation_id`. Values preserve deterministic dedupe, action reservation/confirmation and run/operation linkage. |
| Nurture content state, `standalone/nurture_store.py` | `nurture/content/<account-uuid>/<preset-id>.json` | Root keys: `version`, `account_id`, `preset_id`, `candidates`. Each candidate has exactly `candidate_id`, `source_fingerprint`, `draft_fingerprint`, `category`, `first_seen_at`, `last_action_at`, `publication_state`, `last_run_id`, `operation_id`. Preserve NEW/RESERVED/PUBLISHED/AMBIGUOUS/REJECTED state and operation linkage. Raw source ID and draft text are not stored in this document. |
| Nurture insights snapshot, `standalone/nurture_store.py` | `nurture/insights/<account-uuid>/<content-fingerprint>/<snapshot-id>.json` | Exact keys: `version`, `snapshot_id`, `account_id`, `preset_id`, `content_fingerprint`, `source_fingerprint`, `draft_fingerprint`, `category`, `observed_at`, `likes`, `replies`, `reposts`, `quotes`, `operation_id`. Version `1`; metrics are nonnegative integers or null. Null means unknown and must not be rewritten as zero. |
| Nurture reply/quote draft input, `nurture_draft.py`, `nurture_quote_draft.py` | Operator-selected file, outside the managed state schema | Exact keys: `version`, `action`, `target_fingerprint`, `text`; version `1`; action is respectively `REPLY` or `QUOTE`; target fingerprint is 64 lowercase hex characters. This file contains operator-authored reply text and must be handled as private input, never copied to public fixtures or normal receipts. |
| Nurture content candidate input, `nurture_content.py` | Operator-selected file, outside the managed state schema | Exact keys: `version`, `action`, `candidate_id`, `account`, `preset`, `source_kind`, `source_id`, `category`, `text`; version `1`, action `POST_TEXT`, source kind `OPERATOR_SOURCE_ID`. The input contains source ID and content text; the persistent content state stores fingerprints instead. |

Mutation journal phases encode irreversible progress. Publish flows include `RECEIVED`, carousel child/container creation, `PUBLISH_REQUESTED`, then `PUBLISHED`, `AMBIGUOUS` or `FAILED_FINAL`. Reply-moderation flows include `RECEIVED`, `MUTATION_REQUESTED`, then `CONFIRMED` or `AMBIGUOUS`. The journal stores provider container/media identifiers when required for reconciliation, but not credential values or mutation text. Treat journal files as private operational data. An exception or timeout after the irreversible boundary is not proof that the remote operation failed; preserve the journal and never blindly retry.

## Fingerprint and receipt compatibility

Fingerprints are persistent identity contracts, not replaceable implementation details. Keep normalization, namespaces, JSON encoding, field names, version prefixes, separators, encoding and digest algorithm byte-for-byte compatible:

- Remote Thread target: trim then NFC-normalize the remote ID; canonical compact, key-sorted UTF-8 JSON containing platform `threads` and the normalized ID; SHA-256 hex digest.
- Remote reply: trim then NFC-normalize the reply ID; SHA-256 of UTF-8 `reply:` plus the normalized ID.
- Content source: validate the `OPERATOR_SOURCE_ID`; canonical compact, key-sorted UTF-8 JSON over `source_kind` and NFC source ID; SHA-256 hex digest.
- Draft: NFC-normalize text, convert CRLF and CR to LF, then SHA-256 over `threads-native-post:v1` plus a NUL separator and UTF-8 text.
- Content identity: canonical compact key-sorted ASCII JSON over version `1`, source fingerprint and draft fingerprint; SHA-256 over `threads-nurture-content:v1` plus a NUL separator and that JSON.
- Insights snapshot identity: SHA-256 over `threads-nurture-insights-snapshot:v1` plus a NUL separator and the content fingerprint, newline, and canonical UTC timestamp with microseconds and `Z`.
- All persisted fingerprints are 64 lowercase hexadecimal characters. Do not add sample IDs, sample fingerprints derived from real identifiers, usernames, cursor values or text to this document.

Nurture receipts contain bounded decision/error codes and aggregate counts, not raw discovery/reply text, usernames, pagination cursors or provider Thread IDs. Run, target, content and operation UUIDs are linked across these schemas; preserve them exactly to retain dedupe and recovery behavior.

## Credential and profile invariants

- Account `credential_ref` keeps its exact string value. The source accepts a valid full `env://` reference or the dedicated environment variable name and normalizes to `env://...`. Preserve the `THREADS_PLATFORM_THREADS_TOKEN_` prefix and current suffix/length validation.
- Keep only the credential reference in account state. Never read, export, copy, print or commit the environment token value. Rebind secrets in the destination environment as a separate operator action; resolution remains direct keyed lookup, on every call, with no cache.
- The browser profile directory is `profiles/<account-uuid>` and contains persistent Chromium state, including cookies/session material. Preserve it only through an explicitly authorized operator-controlled procedure. This checkpoint did not inspect, read, hash, copy or archive any profile bytes.
- Do not move a Standalone profile into Worker storage, reuse it concurrently, or claim cross-machine portability. Browser profile encryption and OS user context make a profile transfer a separate security and compatibility decision.
- Workflow snapshots and operator input files can contain queries, target references, cursors or authored text. Keep them private and require explicit review before use after a cutover.

## Atomicity, locking and path safety

- Account, operation, recurrence and Nurture stores use bounded JSON parsing and atomic temporary-file replacement in their current owners. Preserve each writer’s exact write/update and validation contract.
- `locks/<account-uuid>.lock` serializes browser and mutation use for that account. `nurture/locks/<account-uuid>.lock` serializes Nurture ownership independently. `recurrences/<id>/runner.lock` serializes one recurrence runner.
- A lock path is not a durable lock owner record. Stop every old CLI/recurrence process before a cutover. Do not infer ownership from a leftover file, manually remove an apparently stale lock, or allow old and new runtimes to write one data root concurrently.
- Keep `active` Nurture receipts distinct from `completed`. Existing stale RUNNING recovery may update state only under the Nurture account lock; do not invoke recovery during a copy or validate-and-rewrite pass.
- Preserve path containment, regular-file validation, symlink/reparse-point rejection and per-account path naming. Fail closed on unexpected files or paths; do not broaden paths to make a copied tree load.

## Exact data to preserve if later authorized

For a same-machine, cold cutover with compatible v1 readers, preserve the existing data root in place where possible. If a separately authorized procedure uses a new root, preserve these items as a cold, operator-controlled copy without changing bytes:

1. All account JSON, including UUID, alias and credential reference.
2. Every mutation journal, including terminal and nonterminal records, so operation IDs, provider container/media IDs, phase and outcome remain reconcilable.
3. All recurrence records and their exact validated READ-only `workflow.json` snapshots, including disabled records and due anchors.
4. All Nurture `active` and `completed` run receipts, target state, content state and insights snapshots, retaining IDs/fingerprints and links.
5. The existing per-account persistent browser profile directories, only via a separately approved secure operator action. They are opaque private state, not Git artifacts or test fixtures.
6. Operator-owned workflow, reply/quote draft and content-candidate input files only when the operator explicitly selects them for preservation; never auto-apply a copied draft.

Do not copy environment token values, live account data into this public repository, private browser/evidence blobs into a PR, database files, Worker state, or transient open lock ownership. A migration inventory must not log filenames that disclose account aliases or values.

## Migration hazards and rollback

- **Schema drift:** strict v1 key sets mean an added, missing or retyped field can reject a state tree. Do not silently rewrite v1 documents or bump schemas during extraction.
- **Split identity:** changing account UUID, alias filename case-folding, recurrence ID, operation ID, run ID, preset ID or fingerprint algorithm breaks profile paths, operation lookup, dedupe and receipt linkage.
- **Ambiguous side effect:** a pending `PUBLISH_REQUESTED`, `MUTATION_REQUESTED`, carousel creation or `AMBIGUOUS` journal can correspond to a completed remote side effect. Preserve the journal and reconcile using the existing operator process. Never replay because a copied snapshot appears incomplete.
- **Active work:** a RUNNING Nurture record, held account lock or active recurrence can race with copying. Quiesce all source processes first; no live copy or dual writer.
- **Profile portability:** browser profiles may be bound to the same Windows user/machine. Do not assume copying to another host preserves login or cookie usability.
- **Credential availability:** preserving `env://` references does not preserve the external environment secret. Missing secrets must remain sanitized and fail closed.
- **Path aliasing:** case-insensitive Windows paths, alias case-folding, symlinks/reparse points and resolved-root checks can make a nominally equal path behave differently. Validate using the same OS/path rules.
- **Rollback:** retain the untouched source executable and source data root until destination read-only validation succeeds. Prefer switching code while retaining the same v1 data root. If a distinct root is used, retain both copies and never merge by blind overwrite.
- **No rollback over remote effects:** after the destination performs any external mutation, do not restore an older snapshot and rerun from it. Preserve the latest journal/receipt, stop, and reconcile the outcome first. An ambiguous or confirmed operation is never erased as part of rollback.

Before a later cutover, define a read-only schema/link validator, exact copy inventory, operator backup/restore steps, no-concurrent-writer gate and failure procedure. If any schema is incompatible, stop before writing and require a separately authorized migration contract. SPLIT-00 performs no credential, profile or state migration.
