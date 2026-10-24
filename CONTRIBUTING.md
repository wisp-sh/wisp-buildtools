# Contributing

Use Linux x86_64 and Python 3.12 or newer. Each language's README lists its
bootstrap tools, supported APIs and verification commands. Start by inspecting
`python3 build.py plan TOOL --json`; use `--jobs 2` for large native bootstraps
unless your machine has enough memory for more workers.

## Check a change

Run the shared checks from the repository root:

```sh
python3 test-build.py
python3 test-packages.py
python3 test-ci.py
python3 test-ci-package.py
python3 test-prerequisites.py
python3 test-llvm-ci.py
```

`python3 test-workspace.py` checks Component helpers and needs the pinned
`wasm-tools` guest and Wasmtime. Prepare those with
`python3 tools/ci-prerequisites.py install wasm-tools` and
`python3 build.py build wasm-tools`. The prerequisite installer downloads tools and prints their paths and environment
settings as JSON. It also writes GitHub environment files when those are configured.
Set the reported variables in your shell before building. See [CI builds](docs/ci.md).

For compiler changes, build and test the affected toolchain:

```sh
python3 build.py build TOOL --jobs 2
python3 build.py test TOOL
python3 build.py package TOOL
python3 build.py validate dist/TOOL.wisp.zip
```

Language directories may have additional unit tests. Rust has
`python3 -m unittest discover -s rust/tests -p 'test_*.py'`; LLVM has the same
command with `-s llvm/tests`. Run the affected verifiers after changing compiler
sources, adapters or patches. State which checks ran and which require a full
bootstrap in your pull request.

## Source and package changes

Keep source archives, generated compilers, caches and verification output out
of Git. Include small regression inputs and executable checks with bug fixes.
Keep internal plans, development journals, local benchmark results and debug
dumps in the shared private evidence repository. Public documentation should
explain how to reproduce a check and what its result means.

Preserve compiler provenance and integrity checks. Update the matching source
lock, patch hash, license notices and verifier when changing an upstream pin.
Never replace a failed hash check with an unchecked download.

`buildtools.json` describes builds and dependencies. `package-profiles.json`
defines runtime bundle contents. Check both when adding files or dependencies.
Packages need compiler guests, libraries, adapters, notices and integrity
metadata. Build logs, traces, local paths and internal reports do not belong in
release bundles. See [package boundaries](docs/packages.md).

Bootstrap may use native compilers and package managers. Application source
compilation must keep the documented Wasm execution boundary. Add or update
the language verifier when changing that boundary or supported behavior.
