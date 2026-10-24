#!/usr/bin/env python3
"""Install the pinned Zig WASI frontend and hosted C backend."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parent
LOCK = json.loads((ROOT / "toolchain.lock.json").read_text())


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def check(path: Path, expected: str) -> None:
    actual = sha256(path)
    if actual != expected:
        raise RuntimeError(f"SHA-256 mismatch for {path}: expected {expected}, got {actual}")


def backend_provenance(backend: Path, receipt_path: Path | None = None) -> dict:
    """Accept either the pinned release or an explicitly verified source build."""
    with backend.open("rb") as stream:
        if stream.read(8) != b"\0asm\x0d\0\x01\0":
            raise RuntimeError("C/C++ backend must be a Wasm Component")
    if receipt_path is None:
        check(backend, LOCK["clang_wasm"]["sha256"])
        return {"kind": "pinned-artifact", "sha256": sha256(backend),
                "llvm_revision": LOCK["clang_wasm"]["llvm_revision"]}
    specification = LOCK["clang_wasm"]["source_build"]
    recipe_path = (ROOT / specification["recipe"]).resolve()
    workspace = recipe_path.parent
    recipe = json.loads(recipe_path.read_text())["tools"]["clang"]
    receipt = json.loads(receipt_path.read_text())
    if (receipt.get("schema") != specification["receipt_schema"] or receipt.get("tool") != "clang"
            or receipt.get("operation") != "build" or receipt.get("passed") is not True):
        raise RuntimeError("C/C++ source-build receipt is not a successful compiler build")
    if receipt.get("recipeSha256") != sha256(recipe_path):
        raise RuntimeError("C/C++ source-build receipt does not match the current build recipe")
    if receipt.get("source") != recipe["source"] or recipe["source"]["revision"] != LOCK["clang_wasm"]["llvm_revision"]:
        raise RuntimeError("C/C++ source-build receipt has a different LLVM revision")
    inputs = receipt.get("sourceInputs", {})
    if set(inputs) != set(recipe["sourceInputs"]):
        raise RuntimeError("C/C++ source-build receipt has an incomplete source inventory")
    for relative, expected in inputs.items():
        path = workspace / relative
        if not path.resolve().is_relative_to(workspace) or not path.is_file():
            raise RuntimeError("C/C++ source-build input is missing or escapes the repository")
        check(path, expected)
    expected_backend = workspace / specification["artifact"]
    if backend.resolve() != expected_backend.resolve():
        raise RuntimeError("C/C++ source-build receipt names another compiler artifact")
    expected = receipt.get("artifacts", {}).get(specification["artifact"])
    if not expected:
        raise RuntimeError("C/C++ source-build receipt does not checksum the compiler artifact")
    check(backend, expected)
    validation = receipt.get("validation", {})
    if (validation.get("componentInterface") != specification["required_interface"]
            or validation.get("smokeResult") != specification["required_smoke_result"]):
        raise RuntimeError("C/C++ source-build receipt lacks the required Component and compile checks")
    return {"kind": "verified-source-build", "sha256": expected,
            "llvm_revision": recipe["source"]["revision"], "build_receipt_sha256": sha256(receipt_path),
            "recipe_sha256": receipt["recipeSha256"], "source_inputs": inputs, "validation": validation}


def archive(spec: dict, name: str) -> Path:
    target = ROOT / ".downloads" / name
    target.parent.mkdir(exist_ok=True)
    if not target.exists():
        sibling = ROOT.parent / "clang" / ".toolchains" / name
        if sibling.is_file():
            shutil.copyfile(sibling, target)
        else:
            print(f"Downloading {spec['url']}", flush=True)
            temporary = target.with_suffix(target.suffix + ".part")
            with urllib.request.urlopen(spec["url"], timeout=120) as source, temporary.open("wb") as output:
                shutil.copyfileobj(source, output)
            temporary.replace(target)
    check(target, spec["sha256"])
    return target


def extract(path: Path, dirname: str) -> Path:
    destination = ROOT / ".sources"
    destination.mkdir(exist_ok=True)
    directory = destination / dirname
    if not directory.is_dir():
        with tarfile.open(path) as bundle:
            bundle.extractall(destination, filter="data")
    return directory


def install(args: argparse.Namespace) -> None:
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    zig = LOCK["zig"]
    zig_source = extract(archive(zig, f"zig-{zig['version']}.tar.xz"), f"zig-{zig['version']}")
    check(zig_source / "stage1/zig1.wasm", zig["stage1_sha256"])
    check(zig_source / "stage1/zig.h", zig["stage1_header_sha256"])
    shutil.copy2(zig_source / "stage1/zig1.wasm", dist / "zig.wasm")
    shutil.copy2(zig_source / "LICENSE", dist / "LICENSE.zig")
    lib = dist / "lib"
    lib.mkdir(exist_ok=True)
    # This is a freestanding target. Omit native libc, C++, and platform runtimes.
    for name in ("std", "compiler", "compiler_rt", "compiler_rt.zig", "c", "c.zig", "ubsan_rt.zig"):
        source = zig_source / "lib" / name
        if source.is_dir():
            shutil.copytree(source, lib / name, dirs_exist_ok=True)
        else:
            shutil.copy2(source, lib / name)
    headers = dist / "include"
    if headers.exists():
        shutil.rmtree(headers)
    headers.mkdir(exist_ok=True)
    shutil.copy2(zig_source / "stage1/zig.h", headers / "zig.h")
    backend = Path(args.clang_wasm).resolve() if args.clang_wasm else (ROOT / LOCK["clang_wasm"]["default_artifact"]).resolve()
    if not backend.is_file():
        raise RuntimeError(f"Build the clang toolchain first: missing {backend}")
    receipt = Path(args.clang_build_receipt).resolve() if args.clang_build_receipt else None
    provenance = backend_provenance(backend, receipt)
    # The Clang compiler is a package dependency, not part of Zig's payload.
    (dist / "clang-wasm.component").unlink(missing_ok=True)
    (dist / "clang-build-receipt.json").unlink(missing_ok=True)
    provenance.update(provider="toolchain:clang", embedded=False, environment="WISP_CLANG")
    provenance["resource_headers"] = {"provider": "toolchain:clang", "environment": "WISP_CLANG_RESOURCE_DIR", "default_directory": "clang/include"}
    (dist / "LICENSE.llvm").unlink(missing_ok=True)
    # Wasmtime belongs to Wisp's host, never to a language compiler package.
    for previous in ("wasmtime", "LICENSE.wasmtime"):
        (dist / previous).unlink(missing_ok=True)
    files = {}
    for path in sorted(dist.rglob("*")):
        if path.is_file() and path.name != "manifest.json" and ".work" not in path.relative_to(dist).parts:
            files[path.relative_to(dist).as_posix()] = {"sha256": sha256(path), "bytes": path.stat().st_size}
    manifest = {"schema_version": 1, "zig_version": zig["version"], "target": "wasm32-freestanding", "frontend_host": "wasi_snapshot_preview1", "backend_host": "wasip3", "files": files}
    manifest["fullyWasmHostedBuild"] = True
    manifest["clang_backend"] = provenance
    manifest["host_requirements"] = {"wasmtime": {"provider": "wisp", "tested_version": "49.0.1", "interfaces": ["wasi_snapshot_preview1", "wasi:cli/run@0.3.0"]}}
    (dist / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Installed {len(files)} files, {sum(item['bytes'] for item in files.values()):,} bytes in {dist}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clang-wasm", help="Path to the pinned Clang compiler Component")
    parser.add_argument("--clang-build-receipt", help="Explicit source-built backend receipt from the root build.py; default still requires the pinned artifact hash")
    args = parser.parse_args()
    try:
        install(args)
    except (OSError, RuntimeError, ValueError, KeyError, tarfile.TarError) as exc:
        print(f"zig bootstrap: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
