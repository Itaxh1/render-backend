# Canonical conversations and stable titles

Sessions are account-scoped conversations, not device installations. Private
identity keys map device/source IDs, native transcript UUIDs, and legacy Codex
metadata-line hashes to one session. A deleted session leaves null-target keys;
replaying another installation cannot revive it. Identical payloads at different
record positions remain separate events. Native identity is optional for legacy
clients, and adding it does not change hashes of their persisted batch receipts.

`display_title` is a short task name derived from the first meaningful request,
excluding injected instructions/environment context. Grok can improve it in the
existing recent-session summary call; its outcome TLDR is not a title. Existing
model titles stay stable. No extra model call or old-history summary backfill is
required. The UI treats the API title as authoritative and does not use TLDR as
a fallback title.

## Maintenance repair

1. Run all tests against a disposable Postgres database.
2. Deploy the tested commit with `uvicorn backend.maintenance:app --host 0.0.0.0 --port 10000`.
   Wait for the previous writer/worker deployment to stop.
3. Back up `public`, `private`, `auth`, and migration history, with owner-only
   permissions. `scripts/backup-session-repair.mjs` reads the archive afterward;
   that verifies archive readability, not a complete restore drill.
4. Apply `20260917054052_canonical_session_identity.sql` with the Supabase CLI.
5. Run `python -m backend.repair_sessions` for the group count, then add `--apply`.
   The operator supplies DATABASE_URL securely. The repair commits each group
   atomically and refuses any conflicting bytes at the same transcript position.
   It preserves unique events, completed tool results, and every saved TLDR;
   summaries are marked stale after unioning input rather than claiming fresh coverage.
6. Recompute dirty rollups. Verify counts, duplicate identities, ownership, and
   receipt replay. Deploy the normal Docker entry point; verify authenticated
   ribbon/extras reads and a two-installation synthetic upload.
7. Publish the tested CLI only after the compatible backend is live.

Never match sessions by title, project, timestamp alone, or across accounts.
Do not reset local CLI queues. Old device aliases remain valid after repair.
Restore from the protected backup if repair validation fails; retain the backup
through the usual operator retention window.
