# Day API v1: backend handoff

Canonical design: `V2/plan-day-loading.md`, §7 (plus the rollout gates in §9).
This document records implementation status, not a second response specification.

## Implemented locally

- `GET /v1/day/ribbon?date=YYYY-MM-DD&tz=America/Phoenix`: session headers,
  complete visible event skeleton, real local-midnight axis, and off-axis IDs.
  No token aggregation, summary query, tool output, or exchange text is required
  for this response. Titles may use a short first-prompt fallback.
- `GET /v1/day/extras?date=YYYY-MM-DD`: saved summaries and per-agent usage.
  Separate section status preserves successful tokens when the summary read
  times out (and vice versa). A ribbon-version mismatch does not forbid showing
  extras as-of their `generated` timestamp.
- `GET /v1/day/story?date=YYYY-MM-DD&session_id=...&limit=200&cursor=...`:
  signed, account/date/session-scoped pagination. Optional session filter; maximum
  page size 500. Appends do not invalidate traversal; source edits/deletion return
  409, expiry returns 410, invalid/scoped-to-another-user cursors return 400.
  Check `has_newer_data` and the calendar story version even after the final page.
- Calendar adds every requested year's date to `day_revisions` (365/366 entries).
  Existing fields and `/v1/day`, `/v1/dashboard`, `/v1/events/{id}` remain.
- Revisions include a restore-generation salt, user, day, tier and durable
  counter. Treat them as opaque equality tokens, not ordered strings.
- Deletion versions survive deleted sessions; event deletion removes derived
  summaries; session tombstones suppress replay from that same source/device.
  Old receipts classify tombstoned replays as already-handled duplicates.
- Ingestion invalidates all relevant dates of changed sessions, including
  previous usage days and the invocation date of a late tool result.
- Summary jobs use dedicated input versions and attempt leases, not max event
  ID. Old saved TLDRs remain visible during refresh; stale attempts cannot
  complete a newer job or restore deleted text. Automatic scheduling and worker
  eligibility use exactly today's UTC date and the previous six dates. Older
  work requires the existing explicit summary-request endpoint. Debounce now
  starts no earlier than receipt time, including during historical imports.
- Rollups claim one dirty day briefly, compute without holding its dirty-row
  lock, then publish only for the matching generation and lease. Re-dirtied work
  stays queued. Expired workers cannot replace newer published results.
- Database pool budget per deployed API process: 6 reads, 2 ingestion, 1 rollup,
  plus 1 embedded summary connection. Bounded waits and database timeouts return
  retryable 503s rather than unlimited request queues. Multiple API processes or
  a separate worker multiply this budget; the current container uses one process.

All private responses remain `Cache-Control: no-store`; persistent caching is
Claude's application-managed IndexedDB work, not CDN caching.

## Claude: use this fixture and preserve these rules

`day-api-v1.fixture.json` is synthetic and validated against the actual Python
response models in tests. Its IDs deliberately exceed JavaScript's safe-integer
range. Keep IDs as strings. The running development backend's `/openapi.json`
contains the generated response schemas.

- Group ribbon rows by session ID; do not group every session into agent lanes.
- Keep `ms: null` unknown. Codex does not currently provide measured durations.
- Non-tool event status is `unknown`, not tool success. Type determines color.
- `snapshot_complete` is about the returned database snapshot, not import
  completion. Empty successful data replaces cached data; errors do not.
- `markers_state` and finding/analysis states currently say `unsupported`.
  Do not turn that into “no friction found.” Neutral markers/analysis are a later
  phase; the backend does not manufacture accusations from absent evidence.
- The fixture intentionally includes a saved, stale TLDR with a pending refresh.
  Show the saved sentence while updating, not an empty “summarizing” replacement.
- Enforce §7's purge/account/request-generation barriers across tabs before
  persistent cache writes. The backend does not implement browser cache eviction.

## Verification performed

54 tests passed with a disposable PostgreSQL 15 database, including all existing
tests. No PostgreSQL cases skipped. One pre-existing Starlette/AnyIO deprecation
warning remains. Covered migrations, owner-only reads, FK cascades, rollback,
deleted replay, seven-day bounds, signed pagination, DST, multi-day invalidation,
read-snapshot consistency during another connection's write, stale workers,
saved-summary persistence and rollup claim/publication races.

Read-only production `EXPLAIN ANALYZE` and bidirectional `EXCEPT ALL`:

| Captured day | Visible events | Original join | Bounded join | Row differences |
| --- | ---: | ---: | ---: | ---: |
| 2026-09-16 | 82 | 7,697.952 ms | 39.210 ms | 0 |
| 2026-06-14 | 39,944 | 4,946.508 ms | 1,135.681 ms | 0 |

These are individual database execution samples, **not page-load measurements**
or p95s. The busiest-day sample does not meet the sub-second goal. Production
schema/data were not changed. No paid Grok requests were made by the tests.

Reproduce the read-only production comparison with `scripts/benchmark-ribbon.py`;
it accepts the account UUID and date explicitly, applies a read-only transaction
and statement timeout, and prints only timings/counts, not transcript content.

## Remaining gates before a full-plan release

- Apply the migration and deploy in a coordinated writer cutover; do not expose
  version-based caching while old ingestion/summary processes can bypass bumps.
- Qualify the new `(user_id,event_id)` index and its write cost at production
  scale. It is in the migration but has not been built in production.
- Benchmark full response/auth/pool/serialization/transfer costs, 39,944-event
  browser rendering, 100 warm date switches, mobile and concurrent import load.
  No unconditional 100 ms promise is possible for uncached/network/cold starts.
- Claude still owns non-blocking startup, per-session UI, IndexedDB cross-tab
  purge safety, bounded prepared caches, prefetch and layout stability.
- The planned atomic platform/user LLM budget ledger, model/prompt cache identity
  upgrade, five-minute active-session checkpoints, and diagnostic-log retention
  are not implemented in this change. Do not describe those cost/maintenance
  gates as complete. Existing per-call caps and sequential worker remain.
- Linus neutral rollback/compaction markers and optional finding analysis,
  dismissal/resolution routes, and labelled accuracy evaluation remain later.
- `MemoryStore` is the legacy unit-test fake; split-endpoint integration tests use
  real disposable PostgreSQL, not a second in-memory implementation of SQL.

## Controlled deployment checklist (not executed)

1. Back up production and record current deployed revision. Drain/stop all old
   writers and workers during an explicitly scheduled migration window. Linus
   retains its durable upload queue; do not clear clients' state.
2. Apply `20260917032555_day_read_contract.sql`. Its regular index creation needs
   a controlled window; test runtime/lock impact first. Legacy job revisions are
   rebound to dedicated session versions; existing successful summaries remain.
3. Deploy the matching API/worker revision, then smoke-test own-account reads,
   ingestion retries, versions, summary transitions and rollup backlog draining.
4. Enable Claude's split-cache client only after every active writer is updated
   and the shared cache/snapshot/deletion tests pass. Keep legacy routes available.
5. After a database restore, rotate `private.day_api_state.generation` before
   reopening reads; rotate `cursor_key` too if existing cursors must be invalidated.
   Do not reset day counters while old caches are in use.

No npm release or CLI upgrade is needed for the day-read split itself. No new
secret or environment variable is required for these backend changes.
