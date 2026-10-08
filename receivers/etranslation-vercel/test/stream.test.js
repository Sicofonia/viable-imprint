import assert from 'node:assert/strict';
import { it } from 'node:test';
import { streamToText } from '../lib/stream.js';

it('reads a plain web ReadableStream (which has no .text() method) to a string', async () => {
  const bytes = new TextEncoder().encode('{"requestId":1,"result":"aG9sYQ=="} — ñ');
  // Two chunks, to prove the stream is consumed to the end, not just first read.
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(bytes.slice(0, 10));
      controller.enqueue(bytes.slice(10));
      controller.close();
    },
  });
  assert.equal(typeof stream.text, 'undefined');
  assert.equal(await streamToText(stream), '{"requestId":1,"result":"aG9sYQ=="} — ñ');
});
