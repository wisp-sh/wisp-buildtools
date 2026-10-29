// Cold CLI-runtime probe. This does not test a WIT-exported async function.
export async function add(a, b) {
  return await Promise.resolve(a + b);
}

function assert(value, label) {
  if (!value) throw new Error(label);
}

assert(await add(20, 22) === 42, 'ES module async export');
assert(new URL('x', 'https://example.com/').href === 'https://example.com/x', 'URL');
assert(new URLSearchParams('a=42').get('a') === '42', 'URLSearchParams');
assert(new TextDecoder().decode(new TextEncoder().encode('世界')) === '世界', 'text encoding');
assert(atob(btoa('abc')) === 'abc', 'base64');
assert(typeof fetch === 'function', 'fetch global');
const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode('abc'));
const hex = Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join('');
assert(hex === 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad', 'crypto.subtle');
const stream = new ReadableStream({ start(controller) { controller.enqueue('ok'); controller.close(); } });
assert((await stream.getReader().read()).value === 'ok', 'ReadableStream');
assert(typeof WritableStream === 'function' && typeof TransformStream === 'function', 'stream globals');
assert(typeof setTimeout === 'function' && typeof setInterval === 'function', 'timer globals');
assert(typeof Buffer === 'undefined' && typeof process === 'undefined', 'Node globals absent');
console.log(JSON.stringify({ passed: true, asyncModuleExport: 42, url: true, encoding: true,
  base64: true, cryptoSubtle: true, streams: true, fetchGlobal: true, timerGlobals: true,
  nodeBuffer: false, nodeProcess: false, witAsyncExport: 'not tested' }));
