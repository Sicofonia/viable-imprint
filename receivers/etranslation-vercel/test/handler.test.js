// Offline tests for lib/handler.js against an in-memory store.
// Run: npm test   (from receivers/etranslation-vercel/)

import assert from 'node:assert/strict';
import { beforeEach, describe, it } from 'node:test';
import { createHandler } from '../lib/handler.js';

const SECRET = 'test-secret';
const BASE = 'https://receiver.invalid/api/etranslation';

function memoryStore() {
  const files = new Map();
  return {
    files,
    async put(path, body) { files.set(path, body); },
    async get(path) { return files.has(path) ? files.get(path) : null; },
    async del(paths) { for (const p of paths) files.delete(p); },
  };
}

const call = (handle, method, query, body, headers = {}) =>
  handle(new Request(`${BASE}?${new URLSearchParams(query)}`, {
    method,
    headers: { 'user-agent': 'etranslation/2.0', ...headers },
    body,
  }));

describe('receiver', () => {
  let store;
  let handle;
  beforeEach(() => {
    store = memoryStore();
    handle = createHandler(store, SECRET);
  });

  it('fails closed when no secret is configured', async () => {
    const res = await call(createHandler(store, undefined), 'GET', { token: 'x', requestId: '1' });
    assert.equal(res.status, 500);
  });

  it('rejects a wrong or missing token on every verb', async () => {
    for (const method of ['POST', 'GET', 'DELETE']) {
      const query = { requestId: '1', kind: 'delivery' };
      assert.equal((await call(handle, method, { ...query, token: 'nope' }, method === 'POST' ? '{}' : undefined)).status, 401);
      assert.equal((await call(handle, method, query, method === 'POST' ? '{}' : undefined)).status, 401);
    }
    assert.equal(store.files.size, 0);
  });

  it('is pending until a delivery arrives, then returns it', async () => {
    let res = await call(handle, 'GET', { token: SECRET, requestId: '42' });
    assert.deepEqual(await res.json(), { status: 'pending' });

    const delivery = { requestId: 42, targetLanguage: 'ES', outputFormat: 'html', result: 'PGI+aG9sYTwvYj4=' };
    res = await call(handle, 'POST', { token: SECRET, kind: 'delivery' }, JSON.stringify(delivery));
    assert.equal(res.status, 200);

    res = await call(handle, 'GET', { token: SECRET, requestId: '42' });
    assert.deepEqual(await res.json(), { status: 'delivered', delivery });
  });

  it('reports a failure notification', async () => {
    const failure = { requestId: 7, errorCode: -30000, errorMessage: 'Cannot convert input file' };
    await call(handle, 'POST', { token: SECRET, kind: 'failure' }, JSON.stringify(failure));
    const res = await call(handle, 'GET', { token: SECRET, requestId: '7' });
    assert.deepEqual(await res.json(), { status: 'failed', failure });
  });

  it('a success notification alone does not make a request look delivered', async () => {
    await call(handle, 'POST', { token: SECRET, kind: 'success' }, JSON.stringify({ requestId: 9 }));
    const res = await call(handle, 'GET', { token: SECRET, requestId: '9' });
    assert.deepEqual(await res.json(), { status: 'pending' });
  });

  it('a duplicate delivery overwrites instead of failing', async () => {
    const body = JSON.stringify({ requestId: 5, result: 'AAAA' });
    assert.equal((await call(handle, 'POST', { token: SECRET, kind: 'delivery' }, body)).status, 200);
    assert.equal((await call(handle, 'POST', { token: SECRET, kind: 'delivery' }, body)).status, 200);
    assert.equal(store.files.size, 1);
  });

  it('DELETE removes everything stored for that request, and only that request', async () => {
    for (const [id, kind] of [[1, 'delivery'], [1, 'success'], [1, 'failure'], [2, 'delivery']]) {
      await call(handle, 'POST', { token: SECRET, kind }, JSON.stringify({ requestId: id }));
    }
    const res = await call(handle, 'DELETE', { token: SECRET, requestId: '1' });
    assert.equal(res.status, 200);
    assert.deepEqual([...store.files.keys()], ['etranslation/2/delivery.json']);
  });

  it('rejects malformed input before touching storage', async () => {
    const t = { token: SECRET };
    assert.equal((await call(handle, 'POST', { ...t, kind: 'bogus' }, '{"requestId":1}')).status, 400);
    assert.equal((await call(handle, 'POST', { ...t, kind: 'delivery' }, 'not json')).status, 400);
    assert.equal((await call(handle, 'POST', { ...t, kind: 'delivery' }, '{"requestId":"../x"}')).status, 400);
    assert.equal((await call(handle, 'POST', { ...t, kind: 'delivery' }, '{}')).status, 400);
    assert.equal((await call(handle, 'GET', { ...t, requestId: '../../etc' })).status, 400);
    assert.equal((await call(handle, 'GET', t)).status, 400);
    assert.equal(store.files.size, 0);
  });

  it('does not reject an unexpected User-Agent (logged only)', async () => {
    const res = await call(handle, 'POST', { token: SECRET, kind: 'delivery' }, '{"requestId":3}', { 'user-agent': 'curl/8' });
    assert.equal(res.status, 200);
  });
});
