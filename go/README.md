# Go toolchain hosted by Wasmtime

This toolchain packages Go 1.27.2's `cmd/compile`, `cmd/asm`, and `cmd/link` as WASIp1 modules, together with the complete exportable standard library for `GOOS=wasip1` and `GOARCH=wasm`. A Python adapter builds ordinary local Go packages without `cmd/go`. The build runner invokes no native Go tool and gives each Wasm tool only declared files, directories, and environment variables.

The package builder produces `dist/go-toolchain-full-1.27.2/`.

`examples/multipkg` has two main files and three local packages. It imports `fmt`, `encoding/json`, `crypto/sha256`, `strings`, and `bytes`, among other standard packages. Its executable prints normalized JSON and a SHA-256 digest.

## Build the package and projects

Run the following commands from the `go/` directory. The repository root also
provides a shared build interface for every toolchain.

The package builder needs an extracted official Go 1.27.2 distribution. Native Go runs only while **making the toolchain package**. User project builds need Python 3.11 or newer, Wasmtime, and the packaged toolchain. They need no host Go installation or network.

```sh
python3 scripts/build-toolchain.py \
  --go-root .bootstrap/go-host/go \
  --output dist/go-toolchain-full-1.27.2
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner \
  -cf - -C dist go-toolchain-full-1.27.2 | \
  gzip -n -6 > dist/go-toolchain-full-1.27.2.tar.gz
```

The package builder pins Go 1.27.2 and checks the release version. It runs `go list -export std` for the WASIp1 target, copies every exported archive, and writes `SHA256SUMS`. Of the 377 paths in `go list std`, 370 have archive exports. The seven without exports are `unsafe` and six source-only internal test packages. The builder includes the Go license.

```sh
python3 scripts/build.py \
  --toolchain dist/go-toolchain-full-1.27.2 \
  --wasmtime .bootstrap/wasmtime/wasmtime-v50.0.0-x86_64-linux/wasmtime \
  --project examples/multipkg \
  --output out/multipkg/main.wasm \
  --cache .bootstrap/wasmtime-cache \
  --build-cache .bootstrap/build-cache
```

Use `--source examples/hello/main.go` for the single-file example. `--project` accepts a root package named `main` and discovers its reachable local imports. A `go.mod` module directive supplies the local import prefix; without one, the prefix is `project`. `--import-prefix` overrides it. All `.go` files in each selected directory compile in one `compile.wasm` call. The adapter handles common `GOOS`/`GOARCH` filename suffixes and `//go:build` expressions for this target. It excludes `_test.go` files. It does not fetch modules, run generators, build cgo code, or invoke `cmd/go`.

The package layout is:

```text
go-toolchain-full-1.27.2/
  bin/{compile,asm,link}.wasm
  goroot/pkg/wasip1_wasm/**/*.a
  goroot/pkg/include/*
  manifest.toml
  SHA256SUMS
  LICENSE
```

`goroot/pkg/wasip1_wasm` supplies compiler and linker imports. `goroot/pkg/include` supplies assembler headers. These are the GOROOT resources needed by the direct tool calls exercised here; no `goroot/src`, host Go binary, or `stdlib.importcfg` is present. The runner reads tool locations and the GOROOT path from `manifest.toml`, verifies the package against `SHA256SUMS`, and scans the archive tree to create importcfg entries. Compile nodes add only their direct local imports. The link node includes all reachable local archives and the standard-library map.

## Graph and cache

`scripts/wasmbuild/graph.py` defines a tool-independent DAG node with a tool name, arguments, explicit environment, staged inputs, dependency outputs, declared outputs, mounts, and capabilities. `scripts/wasmbuild/runtime.py` executes such nodes with Wasmtime. `scripts/wasmbuild/go_adapter.py` is the only layer that knows Go source files, imports, compile arguments, and link arguments. The same graph engine can execute a different Wasm tool when its adapter supplies nodes and a manifest supplies its tool path.

Each node key hashes the tool artifact, verified toolchain digest, runtime ABI and settings, arguments, explicit environment, input names and bytes, dependency output bytes, mounts, capabilities, and output declarations. Host source paths, user names, `HOME`, host `PATH`, and timestamps are absent. Outputs live in a content-addressed cache and are checked before reuse. A changed dependency only propagates when its archived output bytes change.

For each invocation the executor stages declared inputs under stable guest paths: `/src`, `/deps`, `/build`, and `/out`. It mounts the packaged GOROOT at `/goroot`. Wasmtime receives `PWD=/build`, `GOROOT=/goroot`, `GOOS=wasip1`, `GOARCH=wasm`, and `TMPDIR=/build`. It inherits no host environment or network access. Its host process gets an isolated cache location for compiled Wasmtime modules. That native cache is disposable: deleting it changes startup time, not output bytes.

Run the integration cache check and the source-discovery tests with:

```sh
python3 scripts/check-cache.py \
  --toolchain dist/go-toolchain-full-1.27.2 \
  --wasmtime .bootstrap/wasmtime/wasmtime-v50.0.0-x86_64-linux/wasmtime \
  --cache .bootstrap/wasmtime-cache
python3 -m unittest discover -s tests -v
```

The cache check verifies these invalidations among `shared`, `format`, `hash`, `main`, and `link`:

| Change | Nodes that ran |
| --- | --- |
| Identical rebuild | none |
| One main source file | main, link |
| Leaf `format` package | format, main, link |
| Shared dependency | shared, format, hash, link |
| Compiler flag `-N` | all five |
| Toolchain package hash | all five |

In the shared-dependency case, `format` and `hash` recompiled but produced byte-identical archives. The main node reused its cached output, while the linker consumed the changed `shared` archive.

## Compare reproducibility across hosts

`scripts/repro-report.py` records the archive hash, source-tree hash, build
configuration and output hash. Run it with the same package and source tree on
each machine, then compare the reports. Linux x86_64 is the tested host;
macOS and Windows need separate acceptance checks.

```sh
python3 scripts/repro-report.py \
  --archive dist/go-toolchain-full-1.27.2.tar.gz \
  --toolchain dist/go-toolchain-full-1.27.2 \
  --wasmtime /absolute/path/to/wasmtime \
  --project examples/multipkg > repro-linux.json
python3 scripts/repro-report.py --compare repro-linux.json repro-macos.json repro-windows.json
```

The comparison fails if package, source, configuration or output hashes differ.
Windows execution omits RSS because Python lacks `wait4`. Keep generated reports
outside the source tree and use `python3 ../build.py sizes go` to measure an
installed package.
