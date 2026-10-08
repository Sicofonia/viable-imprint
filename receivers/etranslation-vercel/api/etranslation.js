// ADR 022, Decision 2 — Vercel entry point. All behavior lives in
// lib/handler.js; this file only binds it to the private Blob store.
//
// Credentials: the store must be connected to this project, which gives the
// SDK OIDC access automatically (no token to copy). `access: 'private'` is
// required on every call and must match the store's mode.

import { del, get, put } from '@vercel/blob';
import { createHandler } from '../lib/handler.js';
import { streamToText } from '../lib/stream.js';

const store = {
  async put(path, body) {
    await put(path, body, {
      access: 'private',
      allowOverwrite: true,
      contentType: 'application/json',
    });
  },
  async get(path) {
    // useCache: false — we overwrite and then read back within seconds, and
    // the default CDN cache can serve the previous version for up to a minute.
    const result = await get(path, { access: 'private', useCache: false });
    if (result?.statusCode !== 200) return null;
    return await streamToText(result.stream);
  },
  async del(paths) {
    await del(paths);
  },
};

// Named method exports, not `export default`: a default export is invoked
// the Node way, (req, res), where req.url is only a path — the first deploy
// crashed on exactly that. Named GET/POST/DELETE exports receive a standard
// Web Request, which is what lib/handler.js expects.
const handle = createHandler(store, process.env.RECEIVER_SECRET);
export const GET = handle;
export const POST = handle;
export const DELETE = handle;
