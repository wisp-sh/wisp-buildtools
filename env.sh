#!/usr/bin/env bash
# Source this file to select the existing compiler artifacts in this workspace.
_wisp_buildtools_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export WISP_BUILDTOOLS_ROOT="$_wisp_buildtools_dir"
# Infer the sibling checkout without requiring its directory to be renamed.
_wisp_project_dir="${_wisp_buildtools_dir%-buildtools}"
if [[ -d "$_wisp_buildtools_dir/../wisp/.tools/shared-hosts" ]]; then
    _wisp_project_dir="$_wisp_buildtools_dir/../wisp"
fi
_wisp_hosts_dir="${WISP_HOSTS_DIR:-$_wisp_project_dir/.tools/shared-hosts}"
_wisp_host_runtime="$_wisp_hosts_dir/wisp-wasmtime-49.0.1/wasmtime"
# Existing local caches retain the checkout's old directory name.
if [[ ! -x "$_wisp_host_runtime" && -x "$_wisp_hosts_dir/${_wisp_project_dir##*/}-wasmtime-49.0.1/wasmtime" ]]; then
    _wisp_host_runtime="$_wisp_hosts_dir/${_wisp_project_dir##*/}-wasmtime-49.0.1/wasmtime"
fi
export WISP_WASMTIME="${WISP_WASMTIME:-$_wisp_host_runtime}"
export WISP_BOOTSTRAP_WASMTIME="$_wisp_hosts_dir/wasmtime-49.0.1/wasmtime"
export WISP_LLVM="$_wisp_buildtools_dir/llvm/dist/shared"
export WISP_CLANG_RESOURCE_DIR="$_wisp_buildtools_dir/clang/include"
export WISP_WASMTIME_PYTHON="$_wisp_hosts_dir/wasmtime-python-49.0.0"
export WISP_CLANG="$_wisp_buildtools_dir/clang/.toolchains/build-p3/bin/clang-wasm.component"
export WISP_GO_TOOLCHAIN="$_wisp_buildtools_dir/go/dist/go-toolchain-full-1.27.2"
export WISP_GO="$_wisp_buildtools_dir/go/.bootstrap/go-host/go/bin/go"
export WISP_RUST="${WISP_RUST:-$_wisp_buildtools_dir/rust/dist/compiler}"
export WISP_RUST_WASMTIME="${WISP_RUST_WASMTIME:-$WISP_WASMTIME}"
unset _wisp_hosts_dir _wisp_project_dir _wisp_host_runtime
# Workspace adapters build new language artifacts before Wisp imports them.
export WISP_TOOLCHAINS="$_wisp_buildtools_dir/toolchains.py"
export WISP_OCAML_TOOLCHAIN="$_wisp_buildtools_dir/ocaml"
export WISP_GOOSE_TOOLCHAIN="$_wisp_buildtools_dir/goose"
export WISP_GRAIN_TOOLCHAIN="$_wisp_buildtools_dir/grain"
export WISP_JAVA_TOOLCHAIN="$_wisp_buildtools_dir/java"
export WISP_KOTLIN_TOOLCHAIN="$_wisp_buildtools_dir/kotlin"
export WISP_SWIFT_TOOLCHAIN="$_wisp_buildtools_dir/swift"
export WISP_ZIG_TOOLCHAIN="$_wisp_buildtools_dir/zig"
export WISP_ASSEMBLYSCRIPT_TOOLCHAIN="$_wisp_buildtools_dir/assemblyscript"
export WISP_NIM_TOOLCHAIN="$_wisp_buildtools_dir/nim"
export WISP_JAVASCRIPT_TOOLCHAIN="$_wisp_buildtools_dir/javascript"
export WISP_PYTHON_TOOLCHAIN="$_wisp_buildtools_dir/python"
unset _wisp_buildtools_dir
