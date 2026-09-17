# Session identity and task-title repair

Executed against production on 2026-09-17 UTC. No cross-account merges.

## Verified results

| Record | Before | After |
| --- | ---: | ---: |
| Sessions | 433 | 348 |
| Events | 660,580 | 500,086 |
| Saved summaries | 131 | 131 |

- Merged 74 verified identity groups, removing 85 redundant sessions and
  160,494 duplicate event rows. Different raw records were not treated as
  duplicates merely because their titles or content resembled each other.
- Removed another 161 redundant tool rows after checking their canonical event
  identity. No duplicate `(user_id, event_id)` tool groups remain.
- All 348 remaining sessions have persisted short task titles. Titles are
  separate from TLDRs and no longer use injected setup instructions.
- Existing summaries were preserved. Older summaries may show stale coverage;
  automatic summary jobs remain restricted to the latest seven UTC dates.
- Calendar rollups were recomputed from repaired events; no dirty days remained
  at verification. Day revision/purge barriers invalidate cached pre-repair data.
- Read verification for September 4 returned four sessions and 1,857 unique
  ribbon events, with summaries and token sections available.

## Safety and recovery

A protected pre-repair archive is stored on the operator's machine:
`/Users/itaxhi/.rexy-backups/session-repair-3hmEmc/pre-repair.dump`.
It includes public/private tables, Auth, and migration history. The archive was
read-verified, not restored in a full recovery drill. Duplicate removal is
recoverable from that backup. Do not restore it over the live database without
a separately reviewed restore procedure.

Ingestion was paused during repair. Each identity group was repaired atomically.
Interrupted large repairs rolled back; the bounded repair subsequently completed.
Maintenance mode has been removed.

## Release verification

- Backend: `55a3b05`, deployed on Render; health and readiness return 200.
- UI: `b0a686a`, deployed on Cloudflare; served asset matches the built release.
- CLI: `75d93ea`, version 0.1.3 prepared and pushed, **not published**.
- Tests: 66 backend (including real isolated PostgreSQL), 28 CLI, 58 UI: 152 pass.
- Production database read paths were exercised. A fresh live ingest smoke test
  remains blocked by the storage restriction below; no claim of full live-write
  verification is made.

## Outstanding external blockers

Supabase reports `default_transaction_read_only=on` and about 1.2 GB allocated
database size. Ordinary vacuum completed but did not shrink allocated files.
Maintenance cleanup used explicitly scoped read-write transactions; application
code does not bypass the provider's read-only restriction. New uploads and
summary writes require storage capacity to be resolved. No paid plan change was
made and no unrelated user history was removed.

The configured npm publishing token returns E401. Publishing `rexy-linus@0.1.3`
requires a valid token; the currently published CLI was not falsely marked updated.
