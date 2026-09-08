# Rexy backend

FastAPI ingestion service for normalized Linus events. Raw transcript objects
and unrestricted tool output are not accepted by this API.

## Local setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/pytest
```

For the SQL regression tests, set `REXY_TEST_DATABASE_URL` to a disposable
PostgreSQL database and run the same test command. Those tests execute the
application's queries against connection-local temporary tables, covering
account isolation, saved TLDR selection, duplicate/reset token counters,
midnight/year predecessors, and legacy yearly totals. Without that test-only
variable, the SQL cases are skipped; parameter-contract tests still run.

Copy `.env.example` to `.env` or configure the listed values in your process
manager. The web service needs `DATABASE_URL`, `SUPABASE_URL`,
`SUPABASE_PUBLISHABLE_KEY`, and `REXY_WEB_ORIGIN`. The worker needs only
`DATABASE_URL`, `XAI_API_KEY`, and the optional `XAI_MODEL`. Never commit real
credentials; `NPM_TOKEN` belongs in a release environment, not this backend.

The Supabase project lives in `supabase/`. With Docker running:

```bash
npx supabase start
npx supabase db reset
npx supabase db lint --local --level warning --fail-on error
```

Run the API after the migration is available in the configured database:

```bash
uvicorn backend.main:app --reload --port 8000
```

Run the Grok summary worker separately:

```bash
python -m backend.worker
```

Use `python -m backend.worker --once` to process at most one job in an
integration test. New sessions from the latest seven days are queued
automatically; older sessions are queued only through the dashboard button.
Eligible jobs run newest-activity-first, so current sessions are prioritized
before older backfill. The 60-second inactivity debounce still applies. One
worker makes one Grok request at a time; a backlog is not a database deletion.
Saved TLDR revisions are immutable. Refresh reads the latest stored revision,
and regeneration failures leave it intact. Requesting an already-summarized
revision does not enqueue another paid call.

## Production wiring

The production web origin is `https://rexy.baememory.com`; Rexy calls
`https://rexy-api.baememory.com`. Keep those stable public names when the
underlying Cloudflare deployment changes so issued Linus install commands do
not need to change.

The dashboard is deployed as a Cloudflare static-assets Worker. The API domain
runs the small Worker in `gateway/`; its `UPSTREAM_ORIGIN` selects the FastAPI
deployment. Change that origin only after the new deployment passes readiness
and authenticated ingestion tests. Issued Linus commands keep the same domain.

Production backend: `https://rexy-api-stas.onrender.com`, Render Free in
Virginia, service `srv-dafvcg9t0dsc73fujsgg`. The public API remains
`https://rexy-api.baememory.com`; Cloudflare proxies it to Render, not the Mac.
The frontend remains `https://rexy.baememory.com` on Cloudflare. Releases are
deployed explicitly after tests:

```sh
render deploys create srv-dafvcg9t0dsc73fujsgg --commit <tested-commit> --output json --confirm
```

### Render free deployment

`render.yaml` defines **one free web service**, not a paid background worker or
a second database. Use the existing Supabase database's **session pooler** URL
(port 5432) for IPv4 connectivity; do not use the transaction pooler with the
current prepared-statement/pipeline implementation. Keep secrets in Render's
environment settings, never in the image, repository, or frontend.

Select the `Itaxh1/render-backend` repository with the **Docker** runtime, not
the React `Itaxh1/rexy` repository. Leave Docker Command blank to use the
Dockerfile's production start command:

```sh
exec uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1
```

Render supplies `PORT`. The shell uses `exec` so the API receives shutdown
signals directly and can cancel its summary task and close database pools.
Keep the frontend on Cloudflare; no `npm run dev` process belongs on this
backend service.

`REXY_EMBED_SUMMARIES=1` runs one Grok summary loop alongside the API in the same
process. `XAI_API_KEY` is required in this mode. Run a single Uvicorn process;
don't also start the standalone worker. Summary startup/errors do not block
API readiness. Pending jobs and completed summaries stay in Supabase across
restarts; an interrupted processing job is reclaimable after its existing
10-minute lease expires. Seven-day automatic eligibility and newest-first
ordering remain unchanged. Grok API usage is still billed separately.

Render free web services sleep after 15 minutes without inbound activity and
can take about a minute to wake. The UI checks `/readyz` before loading auth or
dashboard data, retries transient failures, and offers manual retry after two
minutes. Summaries and rollup processing pause while asleep and resume on wake.
There is no keep-alive cron. Render's 750 free hours are shared across the
workspace's free services; exhaustion or suspension is not fixed by retrying.
See [Render's free-tier limits](https://render.com/docs/free).

After a verified Render cutover, the API no longer needs the local Mac or
Cloudflare Tunnel. Keep the previous origin available until smoke tests pass.

Deploy and validate the gateway:

```sh
cd gateway
npm ci
npm run typecheck
npm test
npm run deploy
```

After building Linus, `REXY_LIVE_SMOKE=1 node test/live-smoke.mjs` runs the CLI
against a synthetic transcript and temporary account through the deployed API.
It verifies tool-result merging, token totals, and retry behavior, then deletes
the test account and its data. It reads backend `.env` credentials without
printing them and never scans the operator's transcripts.

## Ingest and dashboard performance

`POST /v1/ingest/batches` groups session updates and pipelines event/tool writes
inside one transaction. It commits the receipt with the data; retrying the
same batch returns that receipt. Device revocation is rechecked under the
transaction lock. Lower revisions are duplicates, not replacements.

The dashboard has independent authenticated reads:

- `GET /v1/calendar?year=2026`: cached daily counts, a change revision, pending
  rollup state, and a suggested active/idle refresh interval.
- `GET /v1/day?date=2026-09-04`: selected-day sessions, event strokes, tools,
  story, and backend-computed token totals. `tokens_by_source[day][source]`
  includes each agent's `total`, fresh `in`, `out`, cache reads `cr`, cache
  writes `cw`, and thinking `th`. Claude adds separate cache usage; Codex
  already includes cached input. Thinking is part of output, not an extra fee
  or extra contribution to the total. Missing usage is omitted, not invented.
- `GET /v1/events/{id}`: owner-only bounded prompt/response and tool input/output
  previews, fetched only when inspected. Device credentials cannot read them.
- `GET /v1/devices` and `POST /v1/devices/{id}/revoke`: owner-scoped connection
  status and revocation. Revocation retains collected history.

The legacy `/v1/dashboard` remains compatible. New clients should load calendar
and day independently, allowing the calendar to paint first. Counts are
eventually consistent: ingestion marks affected days dirty, and a background
task in the API recomputes those days from events. Dirty rows stay locked until
recompute commits, so concurrent uploads re-dirty them without lost updates.
This task runs without Grok or a summary worker. Missing Codex durations remain
unknown even when a result arrives much later than the invocation.

Day sessions are selected with one indexed day scan before joining titles and
saved summaries. Token calculation bounds the session set to the requested
range, then retains each selected session's earlier usage for accurate deltas.
The legacy endpoint uses its whole year for that range. The bounded array
lookup uses the existing usage index; no additional index or migration is
required. Refresh still reads from the database, without waiting for Grok.

The live smoke test also sends a maximum-size 500-record batch, retries it,
checks revision updates and cross-midnight rollups, and revokes its temporary
device. Set `REXY_SMOKE_CLI_PATH` to an installed package's `dist/cli.js` to test
the release artifact; set `REXY_API_BASE` to target a test origin instead.
It also verifies lazy previews and per-agent token deltas across midnight,
including repeated cumulative Codex samples that must contribute zero twice.

Install claims are single-use and expire after ten minutes. An already paired
Linus installation resumes with `npx --yes rexy-linus@latest`, without claim
arguments. A rejected claim does not erase previously saved credentials.

Google sign-in is configured in Supabase Auth, not in FastAPI. The Google Web
OAuth client must use this authorized redirect URI:

```text
https://ysnncnissneicxbecbex.supabase.co/auth/v1/callback
```

Supabase must have Google enabled with that client ID/secret, `Site URL` set to
`https://rexy.baememory.com`, and the same Rexy origin in the redirect allow
list.
These are one-time control-plane settings; Google credentials do not belong in
the Render runtime environment.

Google is enabled for the configured project, with Rexy as the Site URL and
allowed return destination. To apply these specific settings again after an
admin CLI login, run `node scripts/configure-google-auth.mjs --apply`. Omit
`--apply` to preview the target origin and redirect list. The script uses the
CLI credential store or `SUPABASE_ACCESS_TOKEN`, preserves existing redirects,
and never prints the provider secret or management token.
