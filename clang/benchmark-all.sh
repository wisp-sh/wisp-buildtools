#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")" && pwd)
TOOLS="$ROOT/.toolchains"
mkdir -p "$ROOT/work/benchmarks"
python3 "$ROOT/benchmark.py" \
  "$TOOLS/build-p3/bin/clang-p3.wasm" "$TOOLS/build-p3/bin/clang-p3" \
  original "$ROOT/work/benchmarks/original.json"
for variant in O1 Os Oz; do
  build_dir="$TOOLS/build-p3"
  if [[ "$variant" != O1 ]]; then build_dir="$TOOLS/build-p3-$variant"; fi
  python3 "$ROOT/benchmark.py" \
    "$build_dir/bin/clang-wasm.component" "$build_dir/bin/clang-wasm" \
    "compiler-$variant" "$ROOT/work/benchmarks/compiler-$variant.json"
done
for variant in O1 Os Oz; do
  component="$ROOT/work/clang-wasm-opt-$variant.component"
  python3 "$ROOT/benchmark.py" "$component" "$component" \
    "wasm-opt-$variant" "$ROOT/work/benchmarks/wasm-opt-$variant.json"
done
