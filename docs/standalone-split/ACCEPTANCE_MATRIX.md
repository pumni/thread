# SPLIT-00 acceptance matrix

## Baseline and status meanings

Assessment is against the frozen source commit `0dfbd36ff3756f8a68edcd6cc0bd083f6f3570a`. This matrix records what exists in that checkout and what a future standalone extraction must prove. SPLIT-00 is documentation only; no tests, build, browser session, or live Threads operation was run for this checkpoint.

- **Static-reviewed** means the source, test, issue, or coordinator record was inspected; it is not runtime evidence.
- **Automated coverage exists** means a deterministic test module is present at the baseline; the test was not run for this documentation change.
- **Coordinator-reported live** means a bounded result was reported publicly by the coordinator; the private evidence was not fetched or copied, and this checkpoint did not independently repeat it.
- **Pending** means a clean extraction must produce the evidence listed before claiming acceptance.

## Deterministic and build acceptance

| Area | Existing evidence at baseline | Assessment | Required extraction evidence |
| --- | --- | --- | --- |
| Frozen scope and file inventory | `src/threads_platform/standalone/` inventory and source at the pinned commit; issue #243 limits this checkpoint to three docs | Static-reviewed | Compare the extracted file list to `EXTRACTION_MANIFEST.md`; reject any unreviewed path or source drift |
| Import isolation | `tests/unit/test_standalone_architecture.py` checks AST imports and identity-free contracts; static dependency search finds no `threads_platform.workers.*`, `infrastructure.worker_agent.*`, `infrastructure.persistence.*`, SQLAlchemy, asyncpg, Alembic, FastAPI, or Uvicorn in the Standalone import closure | Automated coverage exists; static-reviewed; not run here | Run the architecture test and a clean-package import check; prove imports succeed with PostgreSQL and Worker packages absent |
| Extracted imports and runtime dependencies | Current Standalone closure uses standard library, `httpx2`, `playwright`, `pydantic`, and `pydantic-settings`; current monorepo dependency set also includes server and persistence dependencies | Static-reviewed | Build/install in a clean Python 3.14 environment containing only the reviewed Standalone runtime dependencies; inspect installed dependency tree and import graph |
| CLI startup and command surface | `tests/unit/test_standalone_cli.py`, `test_standalone_app.py`, `test_standalone_runtime.py`; `threads-local` entry point in `pyproject.toml` | Automated coverage exists; not run here | Run `threads-local --help` and representative read-only commands with no database URL, PostgreSQL process, Worker service, token, or account data |
| Wheel and Windows packaging | PyInstaller packaging exists for the current desktop product; no standalone-only package/bundle contract is established by the baseline | Pending | Build and install the isolated wheel; separately review Windows executable packaging, included assets, writable paths, and first-run behavior |
| Official Threads API response contracts | `tests/unit/test_threads_api_contract.py` and `test_threads_discovery_contract.py` use `httpx2.MockTransport` and deterministic responses | Automated coverage exists; not run here; no live claim | Run these tests against the extracted client; retain fixtures with synthetic IDs and verify status, pagination, and response-shape failures |
| Credential secrecy and environment lookup | `tests/unit/test_threads_credential_secrets.py`; environment credentials use the `THREADS_PLATFORM_THREADS_TOKEN_` prefix | Automated coverage exists; not run here | Run secret-redaction tests and verify logs, errors, fixtures, and package metadata never contain token values |
| API mutation uncertainty and retries | Standalone mutation and nurture durability tests cover operation states and recovery contracts | Automated coverage exists; not run here | Run targeted deterministic failure tests; prove timeout/unknown outcome is not treated as definite failure and no blind publish retry occurs |
| Browser capability recognition | `tests/unit/test_standalone_runtime.py` uses fake engine/session; selected generic Playwright adapter tests cover current adapter behavior | Automated coverage exists; not run here | Run only the extracted feed/profile/thread recognition tests with deterministic DOM fixtures; unknown or ambiguous UI must fail closed |
| Worker UI separation | Generic adapter tests and Worker job fixtures include synthetic Worker UI and lease/job state; these fixtures are not Standalone live evidence | Static-reviewed | Keep Worker UI, WorkerJob, lease, enrollment, and synthetic inspect-surface fixtures out of the Standalone test/package closure unless separately authorized |
| Local state schemas and atomic writes | Standalone account, mutation, workflow, recurrence, and nurture tests exercise JSON state and storage behavior | Automated coverage exists; not run here | Run schema, exact-key, size-limit, path-containment, atomic-replacement, and compatibility tests using temporary synthetic roots |
| Locks and restart recovery | Standalone recurrence/nurture durability tests cover local locking and recovery states; lock files are operational coordination, not durable ownership | Automated coverage exists; not run here | Exercise concurrent acquisition and restart/recovery cases; prove only lock-held stale RUNNING recovery can occur and never infer remote success from a local lock |
| PostgreSQL boundary | Monorepo persistence tests require PostgreSQL and are outside the Standalone test slice; architecture checks forbid persistence imports | Static-reviewed; no isolated build proof yet | Run the extracted package and selected tests with no PostgreSQL service or URL and with SQLAlchemy, asyncpg, Alembic, and FastAPI unavailable |
| Worker boundary | Current Standalone runtime does not import Worker packages; one application DTO module still imports broad `domain.workers` types | Static-reviewed; DTO extraction pending | Extract only the required browser result DTOs, then prove the package and tests import without Worker modules or worker protocol fixtures |
| Data-root compatibility and migration | State contracts and root/path behavior are documented in `STATE_COMPATIBILITY.md`; no migration of real user data was performed | Static-reviewed; migration dry-run pending | Use generated synthetic v1 files to test same-root reuse and any approved cold-copy migration; verify exact bytes and destination reads without live profile/account material |
| Copyright and source provenance | Baseline public repository has no discovered LICENSE/COPYING/NOTICE file or project SPDX declaration; desktop third-party notices cover bundled third-party components only | Pending coordinator/legal decision | Obtain explicit source-reuse/provenance disposition before copying code into a new repository; do not infer a reuse license from public visibility |

## Live evidence status

| Capability | Status recorded at this checkpoint | Scope and limitation |
| --- | --- | --- |
| Mention cursor continuation | Coordinator-reported live in the public #229 status record | One-step continuation only; not evidence for terminal/invalid cursor behavior or broader pagination |
| Recruitment observation | Coordinator-reported live in the public #229 status record | One observation only; does not establish complete discovery coverage or repeat behavior |
| Reply-to-Mention publication | Coordinator-reported live in the public #229 status record | One explicitly authorized reply, one publish attempt, and UI confirmation; not evidence for retry, rate-limit, or general mutation recovery |
| Other discovery, apply, failure, and restart paths | Pending / not established by the bounded live record | Includes invalid/terminal cursors, broader discovery matrix, rate/failure paths, repeat suppression, restart/recovery, own-content apply/dedupe, and metrics/static security acceptance |
| Historical feed/profile failures | Historical records are not current acceptance evidence | #193 reports pre-isolation `REMOTE_STATE_UNCERTAIN` and `BROWSER_CONTRACT_MISMATCH`; do not present these as a current pass or current regression |

Public context: [coordinator status #229](https://github.com/pumni/thread/issues/229), [historical browser report #193](https://github.com/pumni/thread/issues/193), and [Standalone boundary issue #215](https://github.com/pumni/thread/issues/215). This checkpoint did not open private evidence, account records, browser profiles, or live tokens, and did not perform live API or browser actions.

## SPLIT-00 review result

- Documentation inventory and state contracts are ready for review against the frozen baseline.
- Deterministic test modules are identified but were not run for this docs-only change.
- No isolated wheel, dependency-minimal environment, or no-PostgreSQL execution has been produced yet.
- No new standalone repository or extracted source package is part of this checkpoint.
- Acceptance remains pending for extraction, packaging, migration dry-run, and the coordinator/legal provenance decision.
