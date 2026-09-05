// Explicit opt-in integration test against the deployed API and real Supabase.
// Creates one confirmed synthetic test account, then deletes it and its rows.
import assert from 'node:assert/strict';
import { readFile, mkdtemp, mkdir, copyFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseEnv, promisify } from 'node:util';
import { randomUUID } from 'node:crypto';
import { execFile } from 'node:child_process';

if (process.env.REXY_LIVE_SMOKE !== '1') throw new Error('Set REXY_LIVE_SMOKE=1 to run the live test');
const here = fileURLToPath(new URL('.', import.meta.url));
const backend = resolve(here, '../..');
const local = parseEnv(await readFile(join(backend, '.env'), 'utf8'));
const api = process.env.REXY_API_BASE || 'https://rexy-api.baememory.com';
const run = randomUUID();
const email = `rexy-smoke-${run}@example.com`;
const password = `Smoke-${randomUUID()}-a9!`;
const auth = local.SUPABASE_URL;
const admin = { apikey: local.SUPABASE_SECRET_KEY, authorization: `Bearer ${local.SUPABASE_SECRET_KEY}`, 'content-type': 'application/json' };
const temporary = await mkdtemp(join(tmpdir(), 'rexy-domain-smoke-'));
let createdUser;

async function json(url, init = {}) {
  const response = await fetch(url, { ...init, signal: AbortSignal.timeout(30_000) });
  assert.ok(response.ok, `${new URL(url).pathname} returned ${response.status}`);
  return response.json();
}

try {
  const user = await json(`${auth}/auth/v1/admin/users`, {
    method: 'POST', headers: admin,
    body: JSON.stringify({ email, password, email_confirm: true, app_metadata: { rexy_smoke_run: run } }),
  });
  createdUser = user.id;
  assert.ok(createdUser);
  const login = await json(`${auth}/auth/v1/token?grant_type=password`, {
    method: 'POST', headers: { apikey: local.SUPABASE_PUBLISHABLE_KEY, 'content-type': 'application/json' },
    body: JSON.stringify({ email, password }),
  });
  const headers = { authorization: `Bearer ${login.access_token}`, origin: 'https://rexy.baememory.com' };
  const before = await json(`${api}/v1/dashboard?year=2026&day=2026-09-04`, { headers });
  assert.equal(before.stats.strokes, 0);
  const claim = await json(`${api}/v1/install/claims`, { method: 'POST', headers });

  const fixtureRoot = join(temporary, 'transcripts');
  await mkdir(join(fixtureRoot, '.claude/projects/smoke'), { recursive: true });
  await copyFile(join(here, 'fixtures/claude.jsonl'), join(fixtureRoot, '.claude/projects/smoke/session.jsonl'));
  const cli = resolve(backend, '../linus/dist/cli.js');
  const runCli = promisify(execFile);
  const environment = { ...process.env, LINUS_DATA_DIR: join(temporary, 'state'), REXY_SMOKE_TRANSCRIPTS: fixtureRoot };
  const first = await runCli(process.execPath, [
    '--import', join(here, 'fixtures/isolated-home.mjs'), cli,
    '--claim', claim.claim_token, '--api', api, '--once',
  ], { env: environment, timeout: 60_000 });
  assert.match(first.stdout, /0 pending uploads/);

  const after = await json(`${api}/v1/dashboard?year=2026&day=2026-09-04`, { headers });
  assert.equal(after.stats.strokes, 4);
  assert.equal(after.sessions.length, 1);
  assert.equal(after.tools.length, 1);
  assert.equal(after.tools[0].name, 'Bash');
  assert.equal(after.tools[0].ok, 1);
  assert.equal(after.events.filter(e => e.k === 'tool').length, 1);
  assert.equal(after.tokens['2026-09-04'].in, 12);
  assert.equal(after.tokens['2026-09-04'].out, 20);

  const again = await runCli(process.execPath, ['--import', join(here, 'fixtures/isolated-home.mjs'), cli, '--once'], {
    env: environment, timeout: 60_000,
  });
  assert.match(again.stdout, /0 new records/);
  const repeated = await json(`${api}/v1/dashboard?year=2026&day=2026-09-04`, { headers });
  assert.equal(repeated.stats.strokes, after.stats.strokes);
  console.log(JSON.stringify({ passed: true, api, strokes: after.stats.strokes, sessions: after.sessions.length, successful_tools: 1, input_tokens: 12, output_tokens: 20, retry_without_duplicates: true }));
} finally {
  if (createdUser) {
    const response = await fetch(`${auth}/auth/v1/admin/users/${createdUser}`, { method: 'DELETE', headers: admin });
    assert.ok(response.ok, `Test user cleanup failed with ${response.status}`);
    console.log('Temporary test account and owned cloud rows deleted.');
  }
  await rm(temporary, { recursive: true, force: true });
}
