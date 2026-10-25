# Python toolchain for Wisp

The Python package embeds CPython 3.14 in a WebAssembly Component. Application
parsing, bytecode compilation, initialization and execution run inside Wasm.
Native Python orchestrates file I/O, hashes and guest invocations. It never
imports or parses application source.

## Inferred contracts

With the current package installed, `wisp build` needs only a Python source
entrypoint and its project root. Public top-level functions, including reexports from bundled modules, are
inferred using guest CPython's `ast`. A literal `__all__` can select the exports.
Select another API module by making it the entrypoint. Imported dataclasses,
enums, type aliases and module-qualified annotations are resolved statically
across the bundle. Imported types use module-qualified WIT names to avoid collisions.
Conditional definitions and dynamically computed export lists are unsupported.

```python
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from componentize_py_types import Result, Err

@dataclass
class Person:
    name: str
    age: int

class Color(Enum):
    RED = 'red'
    BLUE = 'blue'

def birthday(value: Person) -> Person:
    return Person(value.name, value.age + 1)

def checked(value: int) -> Result[int, str]:
    if value < 0:
        raise Err('value must be nonnegative')
    return value * 2
```

`int`, `float`, `bool`, `str`, `bytes`, `list[T]`, `Optional[T]`, `T | None`,
bundled-module dataclasses and enums map to WIT. Integers use `s64`; floats use
`f64`. `None` is a void return. `Result[T, E]` declares a normal success return
or an `Err(E)` exception, including `None` for either payload. Ordinary exceptions
trap. Guest wrappers convert dataclasses and enums to generated binding values.
Private functions beginning with `_` are ignored. Unsupported or missing
annotations fail with the function and type in the diagnostic. Nested options that collapse in Python, recursive types,
async/decorated/generic functions and variadic/keyword-only parameters fail.

`infer.py ENTRY.py` prints a complete inferred WIT document. It packages the
source bytes into `dist/inference.component.wasm` using the WASI packager and
invokes guest `infer()`. It does not execute application code during inference.
Wisp uses this tool before normal source packaging. Outside Wisp, pass the
resulting WIT to `compile.py --wit ... --world library --inferred` to select
top-level functions. Authored contracts keep interface-class precedence over
same-named helpers.

## Automatic contract templates

When an application contract has no installed template, `compile.py --wit ...
--world ... --cache-dir ...` obtains one automatically. Wisp supplies its normal
cache. Templates are keyed by provisioner input hashes and canonical public WIT
hash, with a file lock around creation. Application-body changes reuse the
snapshot; API changes get a new cache entry. No network access is needed.

Provisioning is a compiler-package bootstrap operation. Pinned native
componentize-py, Binaryen, WAC and wasm-tools read only WIT and generated
bindings, never application source. They create a reusable CPython runtime
snapshot. The native componentize-py wheel and bootstrap binaries are included
in `dist/provision` by the package bootstrap and verified through the package
manifest and `toolchain.lock.json`. The WASI export-projection helper is built
once at package bootstrap, so template creation needs no Cargo installation.
Provisioning scratch is disk-backed under the cache and removed on completion.
The first API build needs substantially more memory and disk than reuse.
Automatic creation currently requires Linux x86_64; other compatible hosts can
reuse matching preprovisioned templates.

Application builds then use the WASI source packager, guest CPython `compile`
and `exec`, WASI export projection and WASI wasm-tools validation. The receipt's
`fullyWasmHostedBuild` describes these application stages. It does not claim
that the template bootstrap runs in Wasm.

Authored WIT remains supported. Select it with `build.wit`/`build.world` in Wisp,
or `--wit`/`--world` here. WIT names map to Python snake_case functions or generated
interface implementation classes. The contract adapter accepts import-free
synchronous worlds with scalar values, strings, lists, records, options, tuples,
enums, flags, variants and results. Resources, handles, futures, streams, inline
interfaces and world-level exported types are unsupported.

Explicit provisioning remains available for custom stdlib snapshots:

```sh
python3 python/bootstrap_contract.py --wit library.wit --world library \
  --preload-module html.parser --preload-module logging
```

Guest AST analysis follows reachable local imports and discovers stdlib imports,
including function-local imports and literal `importlib.import_module("name")`
calls. Those imports are preinitialized automatically in the matching runtime.
Changing the discovered set changes the template cache key. Unavailable stdlib
modules can still fail when application initialization or execution imports them;
WASI does not provide every CPython OS facility. Computed imports and lazy stdlib
helpers may need an explicit `preload-modules = ["html.parser"]` setting under
`[tool.wisp.python]` in `pyproject.toml`. Explicit imports must succeed during
provisioning. Resource readers and distribution metadata helpers are included
in every contract runtime.
Installed `dist/contracts/<hash>` templates are reused when their loader and
preload set match. Older loader snapshots are upgraded automatically.

## Pure Python dependencies

Set `--package-root` to the directory containing local top-level packages.
For a `src/` layout, pass `--package-root PROJECT/src --dependency-root PROJECT`;
Wisp uses `build.project` as the package root and the configuration directory as
the dependency root. Extra vendored roots can use repeatable `--python-path`.
Package initialization,
relative and namespace imports, circular imports and lazy imports run in guest
CPython through the source loader.

Dependencies in `[project].dependencies` in `pyproject.toml` or `requirements.txt`
accept PEP 440 versions and PEP 508 requirements. Place
`python-deps.lock.json` beside the dependency declarations. It selects local,
SHA-256-checked wheels or vendor directories:

```json
{
  "schema": 1,
  "packages": [
    {
      "name": "example-package",
      "version": "1.2.3",
      "path": "wheels/example_package-1.2.3-py3-none-any.whl",
      "sha256": "<64 lowercase hex digits>"
    }
  ]
}
```

The requirement can use a range such as `example-package>=1,<2`; the lock still
selects exactly one version and artifact per distribution. Every active
transitive package must have a lock entry. Names, versions, `Requires-Python`,
wheel tags and dependency constraints are checked. Only purelib `*-none-any`
wheels compatible with guest CPython 3.14 are accepted.

The pinned, vendored `packaging` 26.2 parser supports markers, extras, prereleases,
epochs and local versions. Markers are evaluated for CPython 3.14.0 on WASI
(`sys_platform="wasi"`, `platform_system="WASI"`, `platform_machine="wasm32"`),
independently of the host. Project optional dependencies are selected with
`[tool.wisp.python] extras = ["feature"]`. Requirements files accept `-r` includes,
`-c` constraints, continuations and `--hash=sha256:...`; hashes must agree with
the lock. A direct URL is an identity only: it must match the lock's `url`, its
optional SHA-256 fragment must agree, and bytes always come from the lock's local
`path`. No URL is fetched. Editable installs, source distributions, installer
options and dynamic project dependencies are unsupported. No pip, setup code
or build backend runs. See the [upstream requirement syntax](https://packaging.pypa.io/en/stable/requirements.html).

A vendor entry uses a directory `path`, a `files` map of relative source and data filenames
to SHA-256 values and a `sha256` of that map's UTF-8 JSON encoding with sorted
keys and separators `,` and `:` without whitespace. Its optional `requires` list
declares transitive constraints. All files must match the inventory. Original
vendor roots are excluded from the project scan and bundled once from verified
scratch copies. Dependency pins and original input hashes appear in the receipt.

Package data and wheel `.dist-info` metadata are bundled with source. Guest code
can read them through `importlib.resources`, `pkgutil.get_data`,
`importlib.metadata`, and Python `open`/`pathlib` calls using `__file__` paths.
They live in a read-only virtual `/__wisp__/` tree, without host filesystem
preopens. Data is not exposed as an OS file descriptor, and writes, mmap and
native filesystem consumers are unsupported. Wheel `.data/purelib` relocation
is supported; other installation schemes are rejected. Vendor locks can declare
`extras` and `requires`; missing distribution metadata is synthesized from that
lock. Native extensions, bytecode-only packages, symlinks, overlapping roots and
ambiguous module names fail. A pure wheel can still depend on unavailable WASI
facilities.

Hidden files and common build directories are excluded. The dependency root's
top-level `out/` is also excluded so previous Wisp outputs are not bundled as
package data. Nested package directories named `out` remain available.

## Bootstrap and validation

The base package bootstrap targets Linux x86_64 and needs native Python, Cargo
with `wasm32-wasip1`, and the native binaries pinned in `toolchain.lock.json`.
It downloads only pinned bootstrap artifacts and prepares the shared WASI
wasm-tools package. Check RAM, disk and scratch filesystems before starting.
Use disk-backed scratch and one compiler worker.

```sh
python3 python/bootstrap.py
python3 -m unittest discover -s python -p 'test_*.py'
python3 python/verify.py
python3 python/verify_inferred.py
```

Unit tests use host doubles for guest behavior. `verify_inferred.py` exercises
guest inference, automatic provisioning, cache reuse, rich calls and failure
atomicity. The guest path is checked on Linux x86_64, including a build with no external
compiler tools on `PATH`. Runtime Components retain their WASI host requirements.

The legacy scalar path remains `compile.py source.py -o output.wasm` without
`--wit`. It exports `add(s32, s32) -> s32`; `--entry` chooses the two-argument
annotated Python callable. Wisp retains this profile when `entry` is explicit.
The default Wisp path instead infers the source API.

Aggregate source plus package data is limited to 16 MiB; the encoded guest
program plus loader is limited to 32 MiB, and finished Components to 64 MiB.
The bundle allows at most 4096 modules and 16384 source/data files. There are at most 64 public functions, 16 parameters per
function and 32 levels of inferred type expansion. Builds deny guest networking,
inherited environment and I/O, and filesystem preopens. A consuming host chooses
its own capabilities. Runtime initialization is time-limited and failed builds
preserve existing outputs. Imports and initialization are retried on each fresh
Component instance. Source and template hashes are recorded in build receipts.
