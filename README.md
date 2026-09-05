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

Render runs `rexy-api` and `rexy-worker` from `render.yaml`. Configure the web
origin as `https://rexy.tryoz.dev`; Rexy is built against
`https://rexy-api.tryoz.dev`. The latter is currently a Cloudflare Tunnel
hostname and can later become the stable custom domain for the Render web
service without changing Linus commands already issued by the dashboard.

Google sign-in is configured in Supabase Auth, not in FastAPI. The Google Web
OAuth client must use this authorized redirect URI:

```text
https://ysnncnissneicxbecbex.supabase.co/auth/v1/callback
```

Supabase must have Google enabled with that client ID/secret, `Site URL` set to
`https://rexy.tryoz.dev`, and the same Rexy origin in the redirect allow list.
These are one-time control-plane settings; Google credentials do not belong in
the Render runtime environment.
