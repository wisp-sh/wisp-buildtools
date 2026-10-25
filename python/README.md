# Python toolchain for Wisp

The Python package embeds CPython 3.14 in a WebAssembly Component. Application
parsing, bytecode compilation, initialization and execution run inside Wasm.
Native Python orchestrates file I/O, hashes and guest invocations. It never
imports or parses application source.

## Inferred contracts

With the current package installed, `wisp build` needs only a Python source
entrypoint and its project root. Public top-level functions defined in that
module are inferred using guest CPython's `ast`. Imported functions are not
exports. Select another API module by making it the entrypoint.

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
entry-module dataclasses and enums map to WIT. Integers use `s64`; floats use
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

Explicit provisioning is optional and still useful for custom stdlib imports:

```sh
python3 python/bootstrap_contract.py --wit library.wit --world library \
  --preload-module html.parser --preload-module logging
```

Templates snapshot imports. Add any needed stdlib modules, including dynamic
imports, explicitly. Changing a preload list requires `--rebuild`. Installed
`dist/contracts/<hash>` templates are reused when present. Older packages need
rebootstrapping to supply inference, runtime conversions and provisioning assets.

## Pure Python dependencies

Set `--package-root` to the directory containing local top-level packages. Extra
vendored roots can still use repeatable `--python-path`. Package initialization,
relative and namespace imports, circular imports and lazy imports run in guest
CPython through the source loader.

Dependencies in `[project].dependencies` in `pyproject.toml` or `requirements.txt`
must pin exact stable numeric versions. Place `python-deps.lock.json` at the
package root. It selects local, SHA-256-checked wheels or vendor directories:

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

Use `example-package==1.2.3` in the project dependency list. Every transitive
package must also have a lock entry. Wheel metadata names, versions and dependency
constraints are checked. Only purelib `py*-none-any` wheels are accepted.
Requirements may attach `--hash=sha256:...`, which must agree with the lock.
Stable numeric comparisons, wildcard `==`/`!=` and `~=` constraints in transitive
metadata are supported. Extras, environment markers, prereleases, URLs, editable
installs, source distributions and dynamic project dependencies fail explicitly.
No pip or package setup code runs. No dependency is downloaded.

A vendor entry uses a directory `path`, a `files` map of relative `.py` filenames
to SHA-256 values and a `sha256` of that map's UTF-8 JSON encoding with sorted
keys and separators `,` and `:` without whitespace. Its optional `requires` list
declares transitive constraints. All files must match the inventory. Original
vendor roots are excluded from the project scan and bundled once from verified
scratch copies. Dependency pins and original input hashes appear in the receipt.

Native extensions, package data, relocated wheel contents, symlinks, overlapping
roots and ambiguous module names fail. Standard-library availability is still
limited by the snapshot. A pure wheel is not a promise that every import it uses
is present in the selected runtime.

## Bootstrap and validation

The base package bootstrap targets Linux x86_64 and needs native Python, Cargo
with `wasm32-wasip1`, and the native binaries pinned in `toolchain.lock.json`.
It downloads only the pinned componentize-py wheel and prepares the shared WASI
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

Aggregate bundled Python source plus its loader is limited to 1 MiB, and finished
Components to 64 MiB. There are at most 64 public functions, 16 parameters per
function and 32 levels of inferred type expansion. Builds deny guest networking,
inherited environment and I/O, and filesystem preopens. A consuming host chooses
its own capabilities. Runtime initialization is time-limited and failed builds
preserve existing outputs. Imports and initialization are retried on each fresh
Component instance. Source and template hashes are recorded in build receipts.
