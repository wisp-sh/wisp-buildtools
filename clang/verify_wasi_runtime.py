#!/usr/bin/env python3
"""Exercise target resource headers, libc++, malloc and reactor initialization."""
from __future__ import annotations

import os
import argparse
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from llvm.shared import preload_args


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wit-bindgen", default=os.environ.get("WISP_WIT_BINDGEN"),
                        help="optional upstream wit-bindgen 0.61.1 executable")
    args = parser.parse_args()
    host = os.environ.get("WISP_WASMTIME")
    if not host:
        raise SystemExit("set WISP_WASMTIME to Wisp's Wasm execution host")
    compiler = Path(os.environ.get("WISP_CLANG", ROOT / ".toolchains/build-p3/bin/clang-wasm.component"))
    default_sdk = ROOT / "wasi-sdk"
    if not (default_sdk / "manifest.json").is_file():
        default_sdk = ROOT / ".toolchains/wasi-sdk-34.0-x86_64-linux"
    sysroot = Path(os.environ.get("WISP_CLANG_WASI_SYSROOT", default_sdk / "share/wasi-sysroot")).resolve()
    sdk = sysroot.parent.parent
    parent = ROOT / "work"
    parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wasi-runtime-", dir=parent) as temporary:
        work = Path(temporary)
        (work / "test.cpp").write_bytes((ROOT / "examples/wasi-runtime.cpp").read_bytes())
        link_args = []
        export = "test"
        if args.wit_bindgen:
            version = subprocess.check_output([args.wit_bindgen, "--version"], text=True).strip()
            if version != "wit-bindgen-cli 0.61.1":
                raise SystemExit(f"expected wit-bindgen 0.61.1, got {version}")
            (work / "runtime.wit").write_text('''package wisp:sdk;
interface values {
  answer: func() -> u32;
  echo: func(value: option<list<string>>) -> result<list<string>, string>;
}
world runtime { export values; }
''')
            subprocess.run([args.wit_bindgen, "cpp", str(work / "runtime.wit"),
                            "--world", "runtime", "--out-dir", str(work)], check=True)
            with (work / "test.cpp").open("a") as source:
                source.write('''
#include "runtime.cpp"
namespace exports::wisp::sdk::values {
uint32_t Answer() { return ::test(); }
std::expected<wit::vector<wit::string>, wit::string> Echo(
    std::optional<wit::vector<wit::string>> value) {
  if (value) return std::move(*value);
  return std::unexpected(wit::string::from_view("missing"));
}
}
''')
            link_args = ["--link-object", "/work/runtime_component_type.o"]
            export = "wisp:sdk/values#answer"
        command = [host, "run", "-S", "p3=y", *preload_args(),
                        "--dir", f"{work}::/work", "--dir", f"{sdk}::/wasi-sdk", str(compiler),
                        "/work/test.cpp", "-o", "/work/test.wasm", "-O1", "-std=c++23",
                        "--wasi-sysroot", "/wasi-sdk/share/wasi-sysroot", *link_args]
        subprocess.run(command, check=True)
        result = subprocess.run([host, "run", "--invoke", export, str(work / "test.wasm")],
                                check=True, text=True, capture_output=True)
        if result.stdout.strip() != "42":
            raise SystemExit(f"WASI runtime smoke result differs from 42: {result.stdout!r}")
        print("Wasm-hosted C++23: resource headers, libc++, malloc, _initialize: 42")
        if args.wit_bindgen:
            if b"component-type" not in (work / "test.wasm").read_bytes():
                raise SystemExit("upstream component metadata object was not retained")
            print("Upstream C++ bindings: option/list/string/result, metadata object, answer: 42")
            # Argument validation happens before compiling, so these negative
            # cases do not incur another Wasm application compilation.
            for invalid in ["/work/missing.o", "/work/test.cpp", "/work/directory.o"]:
                (work / "directory.o").mkdir(exist_ok=True)
                rejected = subprocess.run([*command, "--link-object", invalid],
                                          capture_output=True, text=True)
                if rejected.returncode == 0 or "requires a regular .o file" not in rejected.stderr:
                    raise SystemExit(f"invalid link object was not rejected: {invalid}: {rejected.stderr}")
            print("Link-object rejects missing files, wrong suffixes and directories")


if __name__ == "__main__":
    main()
