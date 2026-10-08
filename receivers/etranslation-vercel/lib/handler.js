// ADR 022, Decision 2 — the request handling for the eTranslation receiver,
// kept free of any Vercel import so it can be tested offline with an
// in-memory store. api/etranslation.js wires it to the real Blob store.
//
// Three verbs on one path, all guarded by the same shared secret (?token=):
//   POST   ?kind=delivery|success|failure  — called by eTranslation
//   GET    ?requestId=N                    — called by the pipeline CLI
//   DELETE ?requestId=N                    — called by the pipeline CLI

import { createHash, timingSafeEqual } from 'node:crypto';

export const KINDS = ['delivery', 'success', 'failure'];

// eTranslation request ids are positive integers. Anything else is rejected
// before it can reach a storage pathname.
const REQUEST_ID = /^[0-9]{1,18}$/;

const pathFor = (requestId, kind) => `etranslation/${requestId}/${kind}.json`;

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
  });
}

// Constant-time comparison. Hashing first makes both buffers the same length,
// which timingSafeEqual requires, without leaking the secret's length.
function sameSecret(given, expected) {
  const digest = (s) => createHash('sha256').update(String(s)).digest();
  return timingSafeEqual(digest(given), digest(expected));
}

/**
 * @param {{ put(path: string, body: string): Promise<void>,
 *           get(path: string): Promise<string | null>,
 *           del(paths: string[]): Promise<void> }} store
 * @param {string | undefined} secret  the RECEIVER_SECRET value
 */
export function createHandler(store, secret) {
  return async function handle(request) {
    // Fail closed: a deployment without a secret must not accept anything.
    if (!secret) return json({ error: 'RECEIVER_SECRET is not configured' }, 500);

    const url = new URL(request.url);
    if (!sameSecret(url.searchParams.get('token') ?? '', secret)) {
      return json({ error: 'unauthorized' }, 401);
    }

    if (request.method === 'POST') return receive(request, url, store);

    const requestId = url.searchParams.get('requestId') ?? '';
    if (!REQUEST_ID.test(requestId)) return json({ error: 'bad requestId' }, 400);

    if (request.method === 'GET') return lookup(requestId, store);
    if (request.method === 'DELETE') {
      await store.del(KINDS.map((kind) => pathFor(requestId, kind)));
      return json({ deleted: true });
    }
    return json({ error: 'method not allowed' }, 405);
  };
}

async function receive(request, url, store) {
  const kind = url.searchParams.get('kind') ?? '';
  if (!KINDS.includes(kind)) return json({ error: 'bad kind' }, 400);

  // The User-Agent is logged, not enforced: eTranslation documents a fixed
  // "etranslation/2.0", but rejecting on a header would turn any change on
  // their side into a silent loss of every translation. The shared secret is
  // the actual gate.
  const agent = request.headers.get('user-agent') ?? '';
  if (!agent.startsWith('etranslation/')) console.warn(`unexpected User-Agent: ${agent}`);

  const raw = await request.text();
  let body;
  try {
    body = JSON.parse(raw);
  } catch {
    return json({ error: 'body is not JSON' }, 400);
  }
  const requestId = String(body?.requestId ?? '');
  if (!REQUEST_ID.test(requestId)) return json({ error: 'body has no valid requestId' }, 400);

  // Overwriting makes a duplicate delivery harmless (the documented
  // "rare" case) — the same bytes simply land on the same pathname.
  await store.put(pathFor(requestId, kind), raw);
  // eTranslation retries on 5xx and requires a 200 from us.
  return json({ stored: true });
}

async function lookup(requestId, store) {
  const delivery = await store.get(pathFor(requestId, 'delivery'));
  if (delivery !== null) return json({ status: 'delivered', delivery: JSON.parse(delivery) });

  const failure = await store.get(pathFor(requestId, 'failure'));
  if (failure !== null) return json({ status: 'failed', failure: JSON.parse(failure) });

  return json({ status: 'pending' });
}
