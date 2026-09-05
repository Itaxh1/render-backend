# Rexy backend

FastAPI ingestion service for normalized Linus events. Raw transcript objects
and unrestricted tool output are not accepted by this API.

## Local setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/pytest
```

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

## Production wiring

The production web origin is `https://rexy.baememory.com`; Rexy calls
`https://rexy-api.baememory.com`. Keep those stable public names when the
underlying Cloudflare deployment changes so issued Linus install commands do
not need to change.

The dashboard is deployed as a Cloudflare static-assets Worker. The API domain
currently runs the small Worker in `gateway/`, which forwards to the existing
FastAPI origin through the `rexy-api.tryoz.dev` tunnel. FastAPI and the Grok
worker still run on the origin machine; this is a domain cutover, not a complete
migration of backend execution to Workers. The API requires that origin and its
tunnel to remain running. No Render service has been deployed.

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
