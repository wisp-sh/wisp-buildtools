#!/usr/bin/env python3
"""Build external shared Wasm LLVM libraries and their Clang/Nim consumers."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
LLVM_REVISION = "d297af3b3d7a086d64f1477cfa682a16fcfa3260"
PROFILE = "llvm24-wasip3"
MODULE_NAMES = {
    "llvm24.so.wasm": "wisp-llvm24",
    "libc.so.wasm": "wisp-llvm24-libc",
    "libcxx.so.wasm": "wisp-llvm24-libcxx",
    "libcxxabi.so.wasm": "wisp-llvm24-libcxxabi",
}
LEGACY_MODULE_NAMES = {name.replace("wisp-", "vise-", 1) for name in MODULE_NAMES.values()}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(argv: list[object]) -> None:
    subprocess.run([str(arg) for arg in argv], check=True)


def uleb(number: int) -> bytes:
    encoded = bytearray()
    while number >= 128:
        encoded.append((number & 127) | 128)
        number >>= 7
    encoded.append(number)
    return bytes(encoded)


def read_uleb(data: bytes, position: int) -> tuple[int, int]:
    number, shift = 0, 0
    for _ in range(5):
        if position >= len(data):
            raise ValueError("truncated Wasm integer")
        byte = data[position]
        position += 1
        number |= (byte & 127) << shift
        if byte < 128:
            return number, position
        shift += 7
    raise ValueError("oversized Wasm integer")


def sections(data: bytes):
    if data[:4] != b"\0asm":
        raise ValueError("not a Wasm binary")
    position = 8
    while position < len(data):
        kind = data[position]
        size, start = read_uleb(data, position + 1)
        end = start + size
        if end > len(data):
            raise ValueError("truncated Wasm section")
        yield kind, data[start:end]
        position = end


def code_digest(path: Path) -> str:
    """Ignore metadata removed/added by component linking, retaining every code byte."""
    data = path.read_bytes()
    hasher = hashlib.sha256(data[:8])
    for kind, payload in sections(data):
        if kind:
            hasher.update(bytes([kind]) + uleb(len(payload)) + payload)
    return hasher.hexdigest()


def rename_imports(path: Path, names: dict[str, str]) -> None:
    """Change only external core-module import names, preserving module indices."""
    data = path.read_bytes()
    if data[:8] != b"\0asm\x0d\0\x01\0":
        raise ValueError("expected a Component binary")
    result = bytearray(data[:8])
    replaced = set()
    for kind, payload in sections(data):
        if kind == 10:  # Component import section.
            for previous, canonical in names.items():
                old = b"\0" + uleb(len(previous)) + previous.encode()
                new = b"\0" + uleb(len(canonical)) + canonical.encode()
                count = payload.count(old)
                if count:
                    if count != 1 or previous in replaced:
                        raise ValueError(f"ambiguous module import: {previous}")
                    payload = payload.replace(old, new)
                    replaced.add(previous)
        result.extend(bytes([kind]) + uleb(len(payload)) + payload)
    if replaced != set(names):
        raise ValueError("an extracted module is missing its Component import")
    path.write_bytes(result)


def verify(directory: Path) -> dict:
    directory = directory.resolve()
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("schema") != 1 or manifest.get("profile") != PROFILE:
        raise ValueError("unsupported shared LLVM manifest")
    entries = manifest.get("coreModules")
    # Accept complete legacy inventories without rewriting their consumers'
    # embedded imports. Preload the declared names after verifying every file.
    if (not isinstance(entries, list) or len(entries) != len(MODULE_NAMES)
            or {item.get("name") for item in entries} not in (set(MODULE_NAMES.values()), LEGACY_MODULE_NAMES)):
        raise ValueError("shared LLVM module inventory is incomplete")
    for item in entries:
        path = directory / item["path"]
        if not path.resolve().is_relative_to(directory) or path.is_symlink() or not path.is_file():
            raise ValueError(f"unsafe or missing shared LLVM module: {item['path']}")
        if path.stat().st_size != item["sizeBytes"] or digest(path) != item["sha256"]:
            raise ValueError(f"shared LLVM module checksum mismatch: {item['path']}")
    return manifest


def preload_args(directory: Path) -> list[str]:
    return [argument for item in verify(directory)["coreModules"]
            for argument in ("--preload", f"{item['name']}={directory.resolve() / item['path']}")]


def compile_driver(sdk: Path, headers: Path, source: Path, output: Path) -> None:
    run([sdk / "bin/clang++", "--target=wasm32-wasip3", "-std=c++17", "-O1",
         "-fPIC", "-fno-rtti", "-fno-exceptions", "-DNDEBUG", "-D_GNU_SOURCE",
         "-D__STDC_CONSTANT_MACROS", "-D__STDC_FORMAT_MACROS", "-D__STDC_LIMIT_MACROS",
         "-I", headers, "-c", source, "-o", output])


def build(args: argparse.Namespace) -> None:
    sdk, llvm = args.wasi_sdk.resolve(), args.llvm_sdk.resolve()
    tool, destination = args.wasm_tools.resolve(), args.out.resolve()
    run([args.python, ROOT / "llvm/scripts/sdk.py", "verify", llvm])
    metadata = json.loads((llvm / "share/llvm/sdk.json").read_text())
    if metadata.get("host_target") != "wasm32-wasip3" or metadata.get("llvm_revision") != LLVM_REVISION:
        raise ValueError("shared LLVM requires the pinned LLVM 24 Wasip3 SDK")
    # Stage the entire provider and both consumers before replacing any working artifact.
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".llvm-shared-", dir=destination.parent) as temporary:
        staging = Path(temporary)
        provider, work = staging / "provider", staging / "work"
        (provider / "lib").mkdir(parents=True)
        work.mkdir()
        objects = {}
        sources = {"clang": ROOT / "clang/src/clang_wasm_main.cpp", "nim": ROOT / "nim/src/c_backend.cpp"}
        for name, source in sources.items():
            objects[name] = work / f"{name}.o"
            compile_driver(sdk, llvm / "include", source, objects[name])
        symbols = set()
        for obj in objects.values():
            for line in subprocess.check_output([sdk / "bin/llvm-nm", "--undefined-only", "--format=posix", obj], text=True).splitlines():
                symbol = line.split()[0]
                if symbol.startswith(("_ZN4llvm", "_ZNK4llvm", "_ZN5clang", "_ZNK5clang", "_ZN3lld", "_ZNK3lld", "LLVMInitialize", "_ZTVN5clang", "_ZTVN4llvm")):
                    symbols.add(symbol)
        if not symbols:
            raise ValueError("compiler drivers do not reference LLVM")
        library = provider / "lib/llvm24.so.wasm"
        run([sdk / "bin/wasm-ld", "--shared", "--no-entry", "--Bsymbolic", "--strip-all",
             "--soname=llvm24.so.wasm", "--export=__wasm_call_ctors",
             *[flag for symbol in sorted(symbols) for flag in (f"--undefined={symbol}", f"--export={symbol}")],
             *sorted((llvm / "lib").glob("*.a")),
             sdk / "lib/clang/23/lib/wasm32-unknown-wasip3/libclang_rt.builtins.a",
             "--allow-undefined", "-o", library])
        runtime = sdk / "share/wasi-sysroot/lib/wasm32-wasip3"
        runtime_files = {"libc.so": "libc.so.wasm", "noeh/libc++.so": "libcxx.so.wasm", "noeh/libc++abi.so": "libcxxabi.so.wasm", "libdl.so": "libdl.so.wasm"}
        for source, filename in runtime_files.items():
            run([tool, "strip", "-d", "^name$", runtime / source, "-o", work / filename])
        # The command startup, main function and C++ driver stay in each consumer.
        # LLVM, libc and libc++ are external core modules sharing one memory/table.
        known = {code_digest(library): ("llvm24.so.wasm", "wisp-llvm24")}
        for filename in ("libc.so.wasm", "libcxx.so.wasm", "libcxxabi.so.wasm"):
            known[code_digest(work / filename)] = (filename, MODULE_NAMES[filename])
        consumers = {}
        for name, obj in objects.items():
            driver, linked = work / f"{name}.driver.so", work / f"{name}.linked.wasm"
            run([sdk / "bin/clang++", "--target=wasm32-wasip3", "-nostartfiles", "-shared",
                 runtime / "crt1-command.o", obj, "-Wl,-Bdynamic", library,
                 "-Wl,--skip-wit-component,--no-entry,--export=__main_argc_argv", "-lrt", "-ldl", "-o", driver])
            run([tool, "component", "link", driver, library,
                 f"libc++.so={work / 'libcxx.so.wasm'}", f"libc++abi.so={work / 'libcxxabi.so.wasm'}",
                 f"libc.so={work / 'libc.so.wasm'}", f"libdl.so={work / 'libdl.so.wasm'}",
                 "--stack-size", "16777216", "-o", linked])
            extracted, consumer = work / f"{name}-modules", staging / f"{name}.component.wasm"
            run([tool, "component", "unbundle", linked, "--threshold", "524288", "--module-dir", extracted, "-o", consumer])
            names = {}
            for module in sorted(extracted.glob("*.wasm")):
                identity = known.get(code_digest(module))
                if identity is None:
                    raise ValueError(f"unexpected large module in {name}: {module.name}")
                filename, import_name = identity
                target = provider / "lib" / filename
                if name == "clang":
                    shutil.copy2(module, target)
                elif digest(target) != digest(module):
                    raise ValueError(f"{name} and clang produced different shared modules: {filename}")
                names[module.stem] = import_name
            if set(names.values()) != set(MODULE_NAMES.values()):
                raise ValueError(f"{name} is missing a shared-library import")
            rename_imports(consumer, names)
            run([tool, "validate", consumer])
            consumers[name] = {"sha256": digest(consumer), "sizeBytes": consumer.stat().st_size}
        manifest = {"schema": 1, "profile": PROFILE, "llvmRevision": LLVM_REVISION,
                    "hostTarget": "wasm32-wasip3", "linkage": "shared-everything-core-module-imports",
                    "sourceInputs": {str(path.relative_to(ROOT)): digest(path) for path in [*sources.values(), Path(__file__), ROOT / 'llvm/shared.py']},
                    "sdkChecksumManifest": digest(llvm / "SHA256SUMS"),
                    "coreModules": [{"name": name, "path": f"lib/{filename}", "sha256": digest(provider / "lib" / filename), "sizeBytes": (provider / "lib" / filename).stat().st_size} for filename, name in MODULE_NAMES.items()],
                    "consumers": consumers}
        (provider / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        if (llvm / "licenses").is_dir():
            shutil.copytree(llvm / "licenses", provider / "licenses")
        notices = ROOT / "llvm/licenses/sdk"
        notice_receipts = json.loads((notices / "receipts.json").read_text())
        for filename, receipt in notice_receipts.items():
            if digest(notices / filename) != receipt["sha256"]:
                raise ValueError(f"WASI SDK notice checksum mismatch: {filename}")
        shutil.copytree(notices, provider / "licenses/wasi-sdk")
        verify(provider)
        previous = staging / "previous"
        if destination.exists():
            destination.rename(previous)
        try:
            provider.rename(destination)
        except BaseException:
            if previous.exists():
                previous.rename(destination)
            raise
        for name, target in (("clang", args.clang_output), ("nim", args.nim_output)):
            if target is not None:
                target = target.resolve()
                target.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(prefix=".shared-consumer-", dir=target.parent, delete=False) as staged:
                    staged_path = Path(staged.name)
                try:
                    shutil.copy2(staging / f"{name}.component.wasm", staged_path)
                    os.replace(staged_path, target)
                finally:
                    staged_path.unlink(missing_ok=True)
        print(json.dumps(manifest, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("build")
    create.add_argument("--wasi-sdk", type=Path, default=ROOT / "clang/.toolchains/wasi-sdk-34.0-x86_64-linux")
    create.add_argument("--llvm-sdk", type=Path, default=ROOT / "llvm/artifacts/llvm-24-d297af3-wasip3-o1")
    create.add_argument("--wasm-tools", type=Path, default=ROOT / "clang/.toolchains/wasm-tools-1.262.0-x86_64-linux/wasm-tools")
    create.add_argument("--python", default=os.sys.executable)
    create.add_argument("--out", type=Path, default=ROOT / "llvm/dist/shared")
    create.add_argument("--clang-output", type=Path)
    create.add_argument("--nim-output", type=Path)
    check = commands.add_parser("verify")
    check.add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.command == "build":
        build(args)
    else:
        print(json.dumps(verify(args.directory), indent=2))


if __name__ == "__main__":
    main()
