/**
 * Domain cutover gateway. FastAPI still owns authentication and persistence.
 * UPSTREAM_ORIGIN is deployment configuration, never a client-supplied URL.
 */
export default /** @satisfies {ExportedHandler<Env>} */ ({
  async fetch(request, env) {
    const incoming = new URL(request.url);
    const allowedPath = incoming.pathname === '/healthz'
      || incoming.pathname === '/readyz'
      || incoming.pathname.startsWith('/v1/');
    if (!allowedPath) return reply(404, 'not found', request, env);

    const target = new URL(env.UPSTREAM_ORIGIN);
    if (target.protocol !== 'https:' || target.origin === incoming.origin) {
      return reply(503, 'API origin is not configured', request, env);
    }
    target.pathname = incoming.pathname;
    target.search = incoming.search;

    const headers = new Headers();
    for (const name of ['authorization', 'content-type', 'accept', 'origin',
      'access-control-request-method', 'access-control-request-headers',
      'content-length', 'content-encoding']) {
      const value = request.headers.get(name);
      if (value !== null) headers.set(name, value);
    }

    try {
      const upstream = await fetch(target, {
        method: request.method,
        headers,
        body: ['GET', 'HEAD'].includes(request.method) ? undefined : request.body,
        redirect: 'manual',
        signal: AbortSignal.timeout(30_000),
      });
      // Never forward bearer credentials to a redirect destination.
      if (upstream.status >= 300 && upstream.status < 400) {
        await upstream.body?.cancel();
        return reply(502, 'unexpected API redirect', request, env);
      }
      const responseHeaders = new Headers(upstream.headers);
      responseHeaders.set('cache-control', 'no-store');
      responseHeaders.set('x-content-type-options', 'nosniff');
      return new Response(upstream.body, {
        status: upstream.status,
        statusText: upstream.statusText,
        headers: responseHeaders,
      });
    } catch {
      // Do not log bodies, query parameters, tokens, or upstream error strings.
      console.error(JSON.stringify({ event: 'upstream_unavailable' }));
      return reply(503, 'API temporarily unavailable', request, env);
    }
  },
});

/** @param {number} status @param {string} detail @param {Request} request @param {Env} env */
function reply(status, detail, request, env) {
  const headers = new Headers({ 'cache-control': 'no-store', 'vary': 'Origin' });
  if (request.headers.get('origin') === env.WEB_ORIGIN) {
    headers.set('access-control-allow-origin', env.WEB_ORIGIN);
    headers.set('access-control-allow-credentials', 'true');
  }
  return Response.json({ detail }, { status, headers });
}
