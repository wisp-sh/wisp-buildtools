# WASI component tooling

This package pins the upstream wasm-tools 1.262.0 WASIp1 release. WIT parsing,
canonical ABI generation, Component assembly and validation run inside this
Wasm module. The host Python helper only selects files, grants filesystem
preopens and launches Wasmtime. It rejects native wasm-tools binaries.

```sh
python3 wasm-tools/bootstrap.py
```

Workspace adapters use `wasm_tools.command(*arguments)` to obtain the Wasmtime
command. Absolute input and output paths receive matching directory preopens.
`WISP_WASMTIME` chooses the host runtime. `WISP_WASM_TOOLS` or the workspace
`--wasm-tools` option can select another WASI Core Wasm module explicitly.

The release archive SHA-256 is pinned in `toolchain.lock.json`; bootstrap also
records and verifies the extracted module SHA-256. Downloads happen during
bootstrap. Source builds use the packaged module without a native fallback.

Upstream source and license: <https://github.com/bytecodealliance/wasm-tools>.

The package also supplies the checksum-pinned Wasmtime 50.0.4 Preview 1 reactor
adapter for Component assembly. `adapter.lock.json` records its source, version
and license. Adapters resolve it through `wasm_tools.preview1_adapter()` or an
explicit `WISP_PREVIEW1_ADAPTER` override. The module is Wasm, not a native engine.
