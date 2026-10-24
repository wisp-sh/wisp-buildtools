// Node-compatible filesystem names implemented by the QuickJS WASI guest.
// This file runs inside Wasm. It does not load Node or spawn processes.
const filesystem = {
  openSync: (path, flags) => __host.open(path, typeof flags === "number" ? flags : 0),
  closeSync: __host.close,
  readSync: __host.read,
  writeSync: __host.write,
  ftruncateSync: __host.truncate,
  mkdirSync: __host.mkdir,
  unlinkSync: __host.unlink,
  rmdirSync: __host.rmdir,
  renameSync: __host.rename,
  readdirSync: __host.readdir,
  readlinkSync: __host.readlink,
  existsSync(path) { try { __host.stat(path); return true; } catch { return false; } },
};
function stat(info) {
  for (const [name, mode] of [["File", 0x8000], ["Directory", 0x4000], ["CharacterDevice", 0x2000], ["BlockDevice", 0x6000], ["SymbolicLink", 0xa000], ["FIFO", 0x1000], ["Socket", 0xc000]]) {
    info["is" + name] = () => (info.mode & 0xf000) === mode;
  }
  return info;
}
filesystem.statSync = filesystem.lstatSync = path => stat(__host.stat(path));
filesystem.fstatSync = fd => stat(__host.fstat(fd));
filesystem.opendirSync = path => {
  const entries = __host.readdir(path);
  let cursor = 0;
  return { readSync: () => cursor < entries.length ? {name: entries[cursor++]} : null, closeSync() {} };
};
globalThis.require = name => {
  name = name.replace(/^node:/, "");
  if (name === "fs") return filesystem;
  if (name === "constants") return __host;
  if (name === "child_process") return {execSync() { throw new Error("Grain compiler subprocesses are disabled"); }};
  throw new Error("Unsupported compiler import " + name);
};
globalThis.process = {
  versions: {node: "wasi-shim"}, platform: "posix", env: {}, argv: __args,
  cwd: () => "/", exit: __host.exit, on() {},
};
globalThis.console = {log: __host.print, info: __host.print, warn: __host.error, error: __host.error};
globalThis.global = globalThis;
