// Read a web ReadableStream to a string. @vercel/blob's get() returns a plain
// ReadableStream (its docs show stream.text(), which does not exist on one —
// the first real read-back crashed on exactly that). Wrapping it in a
// Response is the standard way to consume it.
export async function streamToText(stream) {
  return await new Response(stream).text();
}
