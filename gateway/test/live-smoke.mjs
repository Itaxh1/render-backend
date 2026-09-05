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
  assert.deepEqual(await json(`${api}/v1/devices`, { headers }), []);
  const claim = await json(`${api}/v1/install/claims`, { method: 'POST', headers });

  const fixtureRoot = join(temporary, 'transcripts');
  await mkdir(join(fixtureRoot, '.claude/projects/smoke'), { recursive: true });
  await copyFile(join(here, 'fixtures/claude.jsonl'), join(fixtureRoot, '.claude/projects/smoke/session.jsonl'));
  const cli = process.env.REXY_SMOKE_CLI_PATH
    ? resolve(process.env.REXY_SMOKE_CLI_PATH)
    : resolve(backend, '../linus/dist/cli.js');
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
  const device = (await json(`${api}/v1/devices`, { headers }))[0];
  assert.equal(device.status, 'connected');
  assert.equal(device.sessions, 1);
  assert.ok(device.last_upload_at);
  assert.ok(!('token_hash' in device));

  // Exercise the maximum batch size, not just the four-event happy path.
  const credential = JSON.parse(await readFile(join(temporary, 'state/credentials.json'), 'utf8'));
  const deviceHeaders = { authorization: `Bearer ${credential.deviceToken}`, 'content-type': 'application/json' };
  const records = Array.from({ length: 500 }, (_, i) => ({
    source: 'codex', source_file_id: 'c'.repeat(64), sequence: i, item_index: 0,
    revision: 1, stage: 'enriched', payload_hash: 'd'.repeat(64),
    event: {
      session_id: `bulk-${Math.floor(i / 100)}`, type: ['user', 'agent', 'tool', 'tool_result'][i % 4],
      created_at: '2026-09-04T12:00:00Z', local_day: '2026-09-04',
      tool_name: i % 4 === 2 ? 'Bash' : null,
      tool_status: i % 4 === 3 ? 'succeeded' : 'unknown',
      source_call_id: i % 4 >= 2 ? `call-${Math.floor(i / 4)}` : null,
      content_preview: 'Synthetic batch performance test',
    },
  }));
  const bulk = { protocol_version: 1, batch_id: randomUUID(), device_sequence: 100_000, extractor_version: 1, records };
  const started = performance.now();
  const receipt = await json(`${api}/v1/ingest/batches`, { method: 'POST', headers: deviceHeaders, body: JSON.stringify(bulk) });
  const bulkMs = Math.round(performance.now() - started);
  assert.equal(receipt.accepted, 500);
  assert.ok(bulkMs < 20_000, `500-record ingest took ${bulkMs}ms (gateway budget: 30s)`);
  assert.deepEqual(await json(`${api}/v1/ingest/batches`, { method: 'POST', headers: deviceHeaders, body: JSON.stringify(bulk) }), receipt);
  const bulkDashboard = await json(`${api}/v1/dashboard?year=2026&day=2026-09-04`, { headers });
  assert.equal(bulkDashboard.stats.strokes, 379);
  assert.equal(bulkDashboard.sessions.length, 6);
  assert.equal(bulkDashboard.tools.find(tool => tool.name === 'Bash').ok, 126);
  assert.ok(bulkDashboard.events.filter(e => e.src === 'codex' && e.k === 'tool').every(e => e.ms === null));
  const newBatch = { ...bulk, batch_id: randomUUID(), device_sequence: 100_001 };
  const duplicates = await json(`${api}/v1/ingest/batches`, { method: 'POST', headers: deviceHeaders, body: JSON.stringify(newBatch) });
  assert.equal(duplicates.accepted, 0); assert.equal(duplicates.duplicate, 500);
  const late = { ...records[3], revision: 2, event: { ...records[3].event, tool_status: 'failed', local_day: '2026-09-05', created_at: '2026-09-05T00:01:00Z' } };
  await json(`${api}/v1/ingest/batches`, { method: 'POST', headers: deviceHeaders,
    body: JSON.stringify({ ...bulk, batch_id: randomUUID(), device_sequence: 100_002, records: [late] }) });
  const detail = await json(`${api}/v1/day?date=2026-09-04`, { headers });
  assert.equal(detail.events.length, 379);
  assert.equal(detail.tools.find(tool => tool.name === 'Bash').fail, 1);
  assert.ok(detail.events.filter(e => e.src === 'codex' && e.k === 'tool').every(e => e.ms === null),
    'late Codex results must not fabricate durations from timestamp differences');
  const calendarStarted = performance.now();
  let calendar;
  do {
    calendar = await json(`${api}/v1/calendar?year=2026`, { headers });
    if (!calendar.rollups_pending && calendar.rollups['2026-09-04']?.codex?.fail === 1) break;
    await new Promise(resolve => setTimeout(resolve, 300));
  } while (performance.now() - calendarStarted < 15_000);
  assert.equal(calendar.rollups['2026-09-04'].codex.events, 375);
  assert.equal(calendar.rollups['2026-09-04'].codex.fail, 1);
  assert.ok(!('events' in calendar));
  const readStarted = performance.now();
  await json(`${api}/v1/calendar?year=2026`, { headers });
  console.log(JSON.stringify({ calendar_ms: Math.round(performance.now() - readStarted), separate_day_detail: true, late_result_rollup_recomputed: true }));
  const revoked = await fetch(`${api}/v1/devices/${device.id}/revoke`, { method: 'POST', headers });
  assert.equal(revoked.status, 204);
  assert.equal((await json(`${api}/v1/devices`, { headers }))[0].status, 'revoked');
  const blocked = await fetch(`${api}/v1/ingest/batches`, { method: 'POST', headers: deviceHeaders, body: JSON.stringify(bulk) });
  assert.equal(blocked.status, 401);
  console.log(JSON.stringify({ bulk_500_ms: bulkMs, device_list_and_revocation: true }));
  console.log(JSON.stringify({ passed: true, api, strokes: after.stats.strokes, sessions: after.sessions.length, successful_tools: 1, input_tokens: 12, output_tokens: 20, retry_without_duplicates: true }));
} finally {
  if (createdUser) {
    const response = await fetch(`${auth}/auth/v1/admin/users/${createdUser}`, { method: 'DELETE', headers: admin });
    assert.ok(response.ok, `Test user cleanup failed with ${response.status}`);
    console.log('Temporary test account and owned cloud rows deleted.');
  }
  await rm(temporary, { recursive: true, force: true });
}
