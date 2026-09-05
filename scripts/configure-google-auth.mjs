// Apply only Google provider and frontend redirect settings. Never print secrets.
import { readFile } from 'node:fs/promises';
import { execFile } from 'node:child_process';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { parseEnv, promisify } from 'node:util';

const configuration = parseEnv(await readFile(new URL('../.env', import.meta.url), 'utf8'));
const project = new URL(configuration.SUPABASE_URL).hostname.split('.')[0];
const origin = configuration.REXY_WEB_ORIGIN;
if (!project || !origin?.startsWith('https://') || !configuration.GoogleClient || !configuration.GoogleClientSecret) {
  throw new Error('SUPABASE_URL, HTTPS REXY_WEB_ORIGIN, GoogleClient, and GoogleClientSecret are required');
}

let token = process.env.SUPABASE_ACCESS_TOKEN || configuration.SUPABASE_ACCESS_TOKEN;
if (!token && process.platform === 'darwin') {
  const execute = promisify(execFile);
  for (const account of ['supabase', 'access-token']) {
    try {
      const result = await execute('security', ['find-generic-password', '-s', 'Supabase CLI', '-a', account, '-w']);
      token = result.stdout.trim();
      if (token) break;
    } catch { /* Try the documented legacy key or protected file fallback. */ }
  }
}
if (!token) {
  try { token = (await readFile(join(homedir(), '.supabase', 'access-token'), 'utf8')).trim(); }
  catch { /* Report one actionable error below. */ }
}
if (!token) throw new Error('Run npx supabase login before configuring hosted authentication');

const endpoint = `https://api.supabase.com/v1/projects/${project}/config/auth`;
const headers = { authorization: `Bearer ${token}`, 'content-type': 'application/json' };
const currentResponse = await fetch(endpoint, { headers, signal: AbortSignal.timeout(15_000) });
if (!currentResponse.ok) throw new Error(`Cannot read hosted auth settings (${currentResponse.status})`);
const current = await currentResponse.json();
const redirects = new Set((current.uri_allow_list || '').split(',').map(value => value.trim()).filter(Boolean));
for (const destination of [origin, `${origin}/`, `${origin}/auth/callback`]) redirects.add(destination);
const changes = {
  external_google_enabled: true,
  external_google_client_id: configuration.GoogleClient,
  external_google_secret: configuration.GoogleClientSecret,
  site_url: origin,
  uri_allow_list: [...redirects].join(','),
};
console.log(JSON.stringify({ project, google_was_enabled: current.external_google_enabled, site_url: origin, redirects: [...redirects], apply: process.argv.includes('--apply') }));
if (process.argv.includes('--apply')) {
  const updated = await fetch(endpoint, { method: 'PATCH', headers, body: JSON.stringify(changes), signal: AbortSignal.timeout(20_000) });
  if (!updated.ok) throw new Error(`Auth configuration update failed (${updated.status})`);
  const verified = await fetch(endpoint, { headers, signal: AbortSignal.timeout(15_000) });
  if (!verified.ok) throw new Error(`Cannot verify auth settings (${verified.status})`);
  const result = await verified.json();
  if (result.external_google_enabled !== true || result.site_url !== origin || !result.uri_allow_list?.split(',').includes(origin)) {
    throw new Error('Auth configuration did not match requested settings');
  }
  console.log(JSON.stringify({ verified: true, google_enabled: true, site_url: result.site_url, callback: `${configuration.SUPABASE_URL}/auth/v1/callback` }));
}
