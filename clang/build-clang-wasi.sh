#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")" && pwd)
BUILD_ROOT=$(cd "$ROOT/.." && pwd)
TOOLS="$ROOT/.toolchains"
JOBS=${BUILD_JOBS:-1}
COMPILER_OPT=${COMPILER_OPT:-O1}
export NINJA_STATUS="${NINJA_STATUS:-[%f/%t %es] }"
COMPILER_LAUNCHER=()
if CCACHE_BIN=$(command -v ccache); then
  COMPILER_LAUNCHER=("-DCMAKE_C_COMPILER_LAUNCHER=$CCACHE_BIN" "-DCMAKE_CXX_COMPILER_LAUNCHER=$CCACHE_BIN")
  printf 'Compiler cache: %s\n' "$CCACHE_BIN"
fi
case "$COMPILER_OPT" in
  O1) BUILD_DIR="$TOOLS/build-p3" ;;
  Os|Oz) BUILD_DIR="$TOOLS/build-p3-${COMPILER_OPT}" ;;
  *) echo "COMPILER_OPT must be O1, Os, or Oz" >&2; exit 2 ;;
esac
mkdir -p "$TOOLS" "$TOOLS/bin"

download() {
  local name=$1 url=$2 sha=$3
  if [[ ! -f "$TOOLS/$name" ]]; then
    curl -fL --retry 3 -o "$TOOLS/$name" "$url"
  fi
  printf '%s  %s\n' "$sha" "$TOOLS/$name" | sha256sum -c -
}

download llvm-project-d297af3.tar.gz \
  https://codeload.github.com/llvm/llvm-project/tar.gz/d297af3b3d7a086d64f1477cfa682a16fcfa3260 \
  a7a2aef19e40094ed4fb3115f76306dd1c54a0021f2254c9d312d47448165848
download wasi-sdk-34.0-x86_64-linux.tar.gz \
  https://github.com/WebAssembly/wasi-sdk/releases/download/wasi-sdk-34/wasi-sdk-34.0-x86_64-linux.tar.gz \
  b761e3a0721dbae9c09a0059e5fdb2bf917d1b4a8a7b430fb3b5aafb0984b2c4
download wasm-tools-1.262.0-x86_64-linux.tar.gz \
  https://github.com/bytecodealliance/wasm-tools/releases/download/v1.262.0/wasm-tools-1.262.0-x86_64-linux.tar.gz \
  655b8df67d54c3ef9b52fc237c4e94bbfbb5f5f43825245cb0940a7002a9ed65
download ninja-linux.zip \
  https://github.com/ninja-build/ninja/releases/download/v1.13.2/ninja-linux.zip \
  5749cbc4e668273514150a80e387a957f933c6ed3f5f11e03fb30955e2bbead6

if [[ ! -x "$TOOLS/bin/ninja" ]]; then
  python3 -m zipfile -e "$TOOLS/ninja-linux.zip" "$TOOLS/bin"
  chmod +x "$TOOLS/bin/ninja"
fi
if [[ ! -d "$TOOLS/llvm-project/llvm" ]]; then
  mkdir -p "$TOOLS/llvm-project"
  tar -xzf "$TOOLS/llvm-project-d297af3.tar.gz" -C "$TOOLS/llvm-project" --strip-components=1
  mkdir -p "$TOOLS/llvm-project/llvm/lib/Support/WASI"
  patch -d "$TOOLS/llvm-project" -p1 < "$ROOT/patches/0001-clang-wasip3-in-process.patch"
fi
if ! grep -Fq 'add_clang_tool(clang-wasm' "$TOOLS/llvm-project/clang/tools/driver/CMakeLists.txt"; then
  patch -d "$TOOLS/llvm-project" -p1 < "$ROOT/patches/0002-driverless-clang-wasm.patch"
fi
# Keep the repository source authoritative for incremental builds too.
if ! cmp -s "$ROOT/src/clang_wasm_main.cpp" "$TOOLS/llvm-project/clang/tools/driver/clang_wasm_main.cpp"; then
  cp "$ROOT/src/clang_wasm_main.cpp" "$TOOLS/llvm-project/clang/tools/driver/clang_wasm_main.cpp"
fi
if [[ ! -d "$TOOLS/wasi-sdk-34.0-x86_64-linux" ]]; then
  tar -xzf "$TOOLS/wasi-sdk-34.0-x86_64-linux.tar.gz" -C "$TOOLS"
fi
if [[ ! -d "$TOOLS/wasm-tools-1.262.0-x86_64-linux" ]]; then
  tar -xzf "$TOOLS/wasm-tools-1.262.0-x86_64-linux.tar.gz" -C "$TOOLS"
fi

NATIVE_STARTED=$SECONDS
cmake --fresh -S "$TOOLS/llvm-project/llvm" -B "$TOOLS/build-native" -G Ninja \
  "${COMPILER_LAUNCHER[@]}" \
  -DCMAKE_MAKE_PROGRAM="$TOOLS/bin/ninja" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER=/usr/bin/gcc -DCMAKE_CXX_COMPILER=/usr/bin/g++ \
  -DLLVM_ENABLE_PROJECTS=clang -DLLVM_TARGETS_TO_BUILD=WebAssembly \
  -DLLVM_INCLUDE_TESTS=OFF -DLLVM_INCLUDE_BENCHMARKS=OFF \
  -DLLVM_INCLUDE_EXAMPLES=OFF -DLLVM_BUILD_TOOLS=OFF \
  -DLLVM_ENABLE_LIBXML2=OFF -DLLVM_ENABLE_ZLIB=OFF \
  -DLLVM_ENABLE_ZSTD=OFF -DLLVM_ENABLE_CURL=OFF \
  -DLLVM_ENABLE_BINDINGS=OFF -DLLVM_ENABLE_ASSERTIONS=OFF \
  -DCLANG_INCLUDE_TESTS=OFF
cmake --build "$TOOLS/build-native" --target llvm-tblgen clang-tblgen -j "$JOBS"
printf 'Native table generators completed in %ss\n' "$((SECONDS - NATIVE_STARTED))"

# The SDK default 64 KiB stack traps in SelectionDAG with the -Oz build.
WASM_STARTED=$SECONDS
cmake --fresh -S "$TOOLS/llvm-project/llvm" -B "$BUILD_DIR" -G Ninja \
  "${COMPILER_LAUNCHER[@]}" \
  -DCMAKE_MAKE_PROGRAM="$TOOLS/bin/ninja" \
  -DCMAKE_TOOLCHAIN_FILE="$TOOLS/wasi-sdk-34.0-x86_64-linux/share/cmake/wasi-sdk-p3.cmake" \
  -DCMAKE_BUILD_TYPE=Release \
  "-DCMAKE_C_FLAGS_RELEASE=-$COMPILER_OPT -DNDEBUG -ffile-prefix-map=\"$HOME\"=/build-user -ffile-prefix-map=\"$BUILD_ROOT\"=/wisp-buildtools" \
  "-DCMAKE_CXX_FLAGS_RELEASE=-$COMPILER_OPT -DNDEBUG -ffile-prefix-map=\"$HOME\"=/build-user -ffile-prefix-map=\"$BUILD_ROOT\"=/wisp-buildtools" \
  "-DCMAKE_EXE_LINKER_FLAGS=-Wl,-z,stack-size=1048576" \
  -DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY \
  '-DLLVM_ENABLE_PROJECTS=clang;lld' \
  -DLLVM_TARGETS_TO_BUILD=WebAssembly \
  -DLLVM_DEFAULT_TARGET_TRIPLE=wasm32-wasip3 \
  -DLLVM_TABLEGEN="$TOOLS/build-native/bin/llvm-tblgen" \
  -DCLANG_TABLEGEN="$TOOLS/build-native/bin/clang-tblgen" \
  -DLLVM_INCLUDE_TESTS=OFF -DLLVM_INCLUDE_BENCHMARKS=OFF \
  -DLLVM_INCLUDE_EXAMPLES=OFF -DLLVM_BUILD_TOOLS=OFF \
  -DLLVM_ENABLE_LIBXML2=OFF -DLLVM_ENABLE_ZLIB=OFF \
  -DLLVM_ENABLE_ZSTD=OFF -DLLVM_ENABLE_CURL=OFF \
  -DLLVM_ENABLE_LIBEDIT=OFF -DLLVM_ENABLE_BINDINGS=OFF \
  -DLLVM_ENABLE_THREADS=OFF -DLLVM_ENABLE_ASSERTIONS=OFF \
  -DBUILD_SHARED_LIBS=OFF -DLLVM_BUILD_LLVM_DYLIB=OFF \
  -DLLVM_LINK_LLVM_DYLIB=OFF \
  -DCLANG_INCLUDE_TESTS=OFF -DCLANG_ENABLE_OBJC_REWRITER=OFF \
  -DCLANG_ENABLE_STATIC_ANALYZER=OFF -DCLANG_PLUGIN_SUPPORT=OFF \
  -DLLD_BUILD_TOOLS=OFF
cmake --build "$BUILD_DIR" --target clang-wasm -j "$JOBS"
printf 'Wasm compiler completed in %ss\n' "$((SECONDS - WASM_STARTED))"
"$TOOLS/wasm-tools-1.262.0-x86_64-linux/wasm-tools" strip -a \
  "$BUILD_DIR/bin/clang-wasm" \
  -o "$BUILD_DIR/bin/clang-wasm.component"

# Generated Zig C uses these Clang resource headers. They belong to Clang's
# package and are shared with its consumers rather than copied into each one.
python3 - "$TOOLS/wasi-sdk-34.0-x86_64-linux/lib/clang/23/include" "$ROOT/include" <<'PY'
from pathlib import Path
import shutil
import sys
import tempfile
source, target = map(Path, sys.argv[1:])
with tempfile.TemporaryDirectory(prefix='.clang-headers-', dir=target.parent) as temporary:
    staging = Path(temporary) / 'include'
    shutil.copytree(source, staging)
    if target.exists(): target.rename(Path(temporary) / 'previous')
    staging.rename(target)
PY

printf 'Built: %s\n' "$BUILD_DIR/bin/clang-wasm.component"
python3 "$ROOT/target_sdk.py" package
