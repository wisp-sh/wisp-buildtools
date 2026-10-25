#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")" && pwd)
TOOLS="$ROOT/.toolchains"
COMPILER=${CLANG_WASM_COMPONENT:-$TOOLS/build-p3/bin/clang-wasm.component}
WASMTIME=${WISP_WASMTIME:-$(command -v wasmtime || true)}
if [[ ! -x "$WASMTIME" ]]; then
  echo 'Wisp must provide Wasmtime with shared module imports; set WISP_WASMTIME.' >&2
  exit 1
fi
WASM_TOOLS="$TOOLS/wasm-tools-1.262.0-x86_64-linux/wasm-tools"
SHARED_ARGUMENT_FILE=$(mktemp)
trap 'rm -f "$SHARED_ARGUMENT_FILE"' EXIT
PYTHONPATH="$ROOT/.." python3 - "$SHARED_ARGUMENT_FILE" <<'PY'
from llvm.shared import preload_args
from pathlib import Path
import sys
Path(sys.argv[1]).write_bytes(b''.join(argument.encode() + b'\0' for argument in preload_args()))
PY
mapfile -d '' -t SHARED_ARGS < "$SHARED_ARGUMENT_FILE"
mkdir -p "$ROOT/work/driverless"
"$WASM_TOOLS" validate "$COMPILER"
"$WASM_TOOLS" component wit "$COMPILER" > "$ROOT/work/driverless/compiler.wit"
rg -q '@0\.3\.0' "$ROOT/work/driverless/compiler.wit"
if rg -q 'wasi_snapshot_preview1|wasi:[^[:space:]]*@0\.[012]\.' "$ROOT/work/driverless/compiler.wit"; then
  echo 'compiler imports an older WASI interface' >&2; exit 1
fi
for sample in add template struct constexpr c include; do
  extension=cpp
  if [[ "$sample" == c ]]; then extension=c; fi
  cp "$ROOT/examples/$sample.$extension" "$ROOT/work/driverless/$sample.$extension"
  extra_args=()
  if [[ "$sample" == include ]]; then
    mkdir -p "$ROOT/work/driverless/includes"
    cp "$ROOT/examples/include.hpp" "$ROOT/work/driverless/includes/include.hpp"
    rm -f "$ROOT/work/driverless/include.hpp"
    extra_args=(-I /work/includes)
  fi
  "$WASMTIME" run -O opt-level=0 -S p3=y "${SHARED_ARGS[@]}" --dir "$ROOT/work/driverless::/work" "$COMPILER" \
    "/work/$sample.$extension" -o "/work/$sample.wasm" -O1 "${extra_args[@]}"
  "$WASM_TOOLS" validate "$ROOT/work/driverless/$sample.wasm"
  export_name=test
  if [[ "$sample" == add ]]; then export_name=add; fi
  invoke_args=()
  if [[ "$sample" == add ]]; then invoke_args=(20 22); fi
  result=$("$WASMTIME" run --invoke "$export_name" \
    "$ROOT/work/driverless/$sample.wasm" "${invoke_args[@]}" 2>/dev/null)
  test "$result" = 42
  "$WASM_TOOLS" print "$ROOT/work/driverless/$sample.wasm" > "$ROOT/work/driverless/$sample.wat"
  if rg -q '^  \(import ' "$ROOT/work/driverless/$sample.wat"; then
    echo "unexpected import in $sample" >&2; exit 1
  fi
  echo "$sample: $result"
done
printf 'extern "C" int broken( {\n' > "$ROOT/work/driverless/invalid.cpp"
if "$WASMTIME" run -O opt-level=0 -S p3=y "${SHARED_ARGS[@]}" --dir "$ROOT/work/driverless::/work" "$COMPILER" \
  /work/invalid.cpp -o /work/invalid.wasm 2> "$ROOT/work/driverless/invalid.log"; then
  echo 'invalid source compiled' >&2; exit 1
fi
rg -q 'error:' "$ROOT/work/driverless/invalid.log"
cp "$ROOT/examples/exceptions.cpp" "$ROOT/work/driverless/exceptions.cpp"
if "$WASMTIME" run -O opt-level=0 -S p3=y "${SHARED_ARGS[@]}" --dir "$ROOT/work/driverless::/work" "$COMPILER" \
  /work/exceptions.cpp -o /work/exceptions.wasm \
  2> "$ROOT/work/driverless/exceptions.log"; then
  echo 'exceptions unexpectedly enabled' >&2; exit 1
fi
rg -q "exceptions disabled" "$ROOT/work/driverless/exceptions.log"
if "$WASMTIME" run -O opt-level=0 -S p3=y "${SHARED_ARGS[@]}" --dir "$ROOT/work/driverless::/work" "$COMPILER" \
  /work/add.cpp -o /work/invalid-target.wasm --target=x86_64 \
  2> "$ROOT/work/driverless/invalid-target.log"; then
  echo 'target override accepted' >&2; exit 1
fi
rg -q 'unsupported argument' "$ROOT/work/driverless/invalid-target.log"
if command -v strace >/dev/null; then
  strace -f -e trace=execve -o "$ROOT/work/driverless/execve.log" \
    "$WASMTIME" run -O opt-level=0 -S p3=y "${SHARED_ARGS[@]}" --dir "$ROOT/work/driverless::/work" "$COMPILER" \
    /work/add.cpp -o /work/traced.wasm
  python3 - "$ROOT/work/driverless/execve.log" "$WASMTIME" "$ROOT/.." <<'PY'
import json
from pathlib import Path
import re
import sys

launcher = Path(sys.argv[2]).resolve()
allowed = {launcher}
root = Path(sys.argv[3]).resolve()
if launcher == root / 'cwasm.py':
    sys.path.insert(0, str(root))
    import cwasm
    allowed.add(cwasm.default_runtime())
executions = re.findall(r'execve\(("(?:[^"\\]|\\.)*")', Path(sys.argv[1]).read_text())
if not executions:
    raise SystemExit('subprocess trace contains no execve calls')
for encoded in executions:
    program = Path(json.loads(encoded)).resolve()
    if program not in allowed:
        raise SystemExit(f'unexpected host executable during guest compilation: {program}')
print('subprocess trace: only the compiler launcher and Wasmtime executed')
PY
else
  echo "subprocess trace: skipped because strace is unavailable"
fi
echo "driverless verification passed"
