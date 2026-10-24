// Bootstrap only. Preserve upstream's embedded Wasm and its minified C-ABI names.
import fs from 'node:fs';

const originalInstantiate = WebAssembly.instantiate;
let exports;
WebAssembly.instantiate = async function (bytes, imports) {
  if (bytes instanceof Uint8Array || bytes instanceof ArrayBuffer) {
    fs.writeFileSync('dist/binaryen.wasm', Buffer.from(bytes));
  }
  const result = await originalInstantiate.call(this, bytes, imports);
  exports = result.instance?.exports || result.exports;
  return result;
};
const binaryen = (await import('../node_modules/binaryen/index.js')).default;
const aliases = {};
for (const [name, value] of Object.entries(binaryen)) {
  for (const [key, exported] of Object.entries(exports)) {
    if (value === exported) aliases[name] = key;
  }
}
// Upstream replaces _malloc with a JS unsigned-result wrapper after initialization.
const glue = fs.readFileSync('node_modules/binaryen/index.js', 'utf8');
const malloc = glue.match(/A\._malloc=j\.([A-Za-z_$][A-Za-z0-9_$]*)/);
if (!malloc) throw new Error('The pinned Binaryen malloc export was not found');
aliases._malloc = malloc[1];
fs.writeFileSync('dist/binaryen-aliases.json', JSON.stringify(aliases, null, 2) + '\n');
console.log(`Extracted upstream Binaryen Wasm with ${Object.keys(aliases).length} C-ABI names`);
