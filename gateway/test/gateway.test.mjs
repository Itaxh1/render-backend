import assert from 'node:assert/strict';
import { test, mock } from 'node:test';
import gateway from '../src/index.mjs';

const env = { UPSTREAM_ORIGIN: 'https://upstream.example', WEB_ORIGIN: 'https://rexy.baememory.com' };

test('preserves query, authorization and streaming response without caching', async () => {
  const upstream = mock.method(globalThis, 'fetch', async (url, init) => {
    assert.equal(url.href, 'https://upstream.example/v1/dashboard?year=2026&day=2026-09-04');
    assert.equal(init.headers.get('authorization'), 'Bearer test-only');
    assert.equal(init.headers.get('cookie'), null);
    assert.equal(init.redirect, 'manual');
    return new Response('{"sessions":[]}', { headers: { 'content-type': 'application/json' } });
  });
  try {
    const result = await gateway.fetch(new Request('https://rexy-api.baememory.com/v1/dashboard?year=2026&day=2026-09-04', {
      headers: { authorization: 'Bearer test-only', cookie: 'unrelated=value' },
    }), env);
    assert.equal(result.status, 200);
    assert.equal(result.headers.get('cache-control'), 'no-store');
    assert.deepEqual(await result.json(), { sessions: [] });
  } finally { upstream.mock.restore(); }
});

test('forwards upload bytes, method, and durable receipt unchanged', async () => {
  const payload = JSON.stringify({ protocol_version: 1, records: [] });
  const upstream = mock.method(globalThis, 'fetch', async (_url, init) => {
    assert.equal(init.method, 'POST');
    assert.equal(await new Response(init.body).text(), payload);
    return Response.json({ accepted: 0, duplicate: 0 });
  });
  try {
    const result = await gateway.fetch(new Request('https://rexy-api.baememory.com/v1/ingest/batches', {
      method: 'POST', body: payload, headers: { 'content-type': 'application/json' },
    }), env);
    assert.deepEqual(await result.json(), { accepted: 0, duplicate: 0 });
  } finally { upstream.mock.restore(); }
});

test('redirects are rejected without a second fetch', async () => {
  const upstream = mock.method(globalThis, 'fetch', async () => new Response(null, {
    status: 302, headers: { location: 'https://foreign.example' },
  }));
  try {
    const result = await gateway.fetch(new Request('https://rexy-api.baememory.com/v1/dashboard', {
      headers: { origin: env.WEB_ORIGIN, authorization: 'Bearer test-only' },
    }), env);
    assert.equal(result.status, 502);
    assert.equal(upstream.mock.callCount(), 1);
    assert.equal(result.headers.get('access-control-allow-origin'), env.WEB_ORIGIN);
  } finally { upstream.mock.restore(); }
});

test('upstream authentication failure is preserved', async () => {
  const upstream = mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'invalid access token' }, { status: 401 }));
  try {
    const result = await gateway.fetch(new Request('https://rexy-api.baememory.com/v1/dashboard'), env);
    assert.equal(result.status, 401);
  } finally { upstream.mock.restore(); }
});

test('invalid paths never reach the origin', async () => {
  const upstream = mock.method(globalThis, 'fetch', async () => { throw new Error('unexpected call'); });
  try {
    const result = await gateway.fetch(new Request('https://rexy-api.baememory.com/secrets'), env);
    assert.equal(result.status, 404);
    assert.equal(upstream.mock.callCount(), 0);
  } finally { upstream.mock.restore(); }
});

test('unavailable origin returns a bounded error and does not allow foreign origins', async () => {
  const upstream = mock.method(globalThis, 'fetch', async () => { throw new Error('private diagnostics'); });
  try {
    const result = await gateway.fetch(new Request('https://rexy-api.baememory.com/readyz', {
      headers: { origin: 'https://foreign.example' },
    }), env);
    assert.equal(result.status, 503);
    assert.equal(result.headers.get('access-control-allow-origin'), null);
    assert.equal((await result.text()).includes('private diagnostics'), false);
  } finally { upstream.mock.restore(); }
});
