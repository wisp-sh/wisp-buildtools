#!/usr/bin/env python3
"""Build upstream AssemblyScript Wasm modules for the Wisp-provided host."""

import argparse
import hashlib
import json
import pathlib
import re
import shutil
import subprocess
import tarfile
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
SOURCE_VERSION = "0.28.20"
SOURCE_SHA256 = "b019ab552cbecc84db4e25352b471f14aac2a7ea4d2ca573470132ed8e59332f"
SOURCE_URL = f"https://codeload.github.com/AssemblyScript/assemblyscript/tar.gz/refs/tags/v{SOURCE_VERSION}"


def fetch(name, url, expected):
    path = ROOT / ".cache" / name
    if not path.exists():
        print(f"Downloading {name}", flush=True)
        urllib.request.urlretrieve(url, path)
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise RuntimeError(
            f"{name}: SHA-256 mismatch, expected {expected}, got {actual}"
        )
    return path


def generate_diagnostics(source):
    messages = json.loads((source / "src/diagnosticMessages.json").read_text())
    lines = [
        "// Generated from upstream diagnosticMessages.json.",
        "export enum DiagnosticCode {",
    ]
    for text, code in messages.items():
        key = re.sub(r"_+$", "", re.sub(r"[^\w]+", "_", text))
        lines.append(f"  {key} = {code},")
    lines += [
        "}",
        "export function diagnosticCodeToString(code: DiagnosticCode): string {",
        "  switch (code) {",
    ]
    for text, code in messages.items():
        lines.append(f"    case {code}: return {json.dumps(text)};")
    lines += ['    default: return "";', "  }", "}"]
    (source / "src/diagnosticMessages.generated.ts").write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scan-source",
        action="store_true",
        help="Run is-malicious on downloaded compiler sources before bootstrap",
    )
    args = parser.parse_args()
    for directory in (".cache", ".sources", "dist"):
        (ROOT / directory).mkdir(exist_ok=True)
    archive = fetch(
        f"assemblyscript-{SOURCE_VERSION}.tar.gz", SOURCE_URL, SOURCE_SHA256
    )
    source = ROOT / ".sources" / f"assemblyscript-{SOURCE_VERSION}"
    if not source.exists():
        with tarfile.open(archive) as tar:
            tar.extractall(ROOT / ".sources", filter="data")
    if args.scan_source:
        subprocess.run(
            ["npx", "--yes", "is-malicious", str(source)], cwd=ROOT, check=True
        )
    subprocess.run(
        ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=ROOT,
        check=True,
    )
    generate_diagnostics(source)
    subprocess.run(["node", "src/extract-binaryen.mjs"], cwd=ROOT, check=True)
    subprocess.run(
        [
            "node",
            "node_modules/assemblyscript/bin/asc.js",
            str(source / "src/glue/wasm/index.ts"),
            str(source / "src/index-wasm.ts"),
            "--exportStart",
            "_initialize",
            "--exportRuntime",
            "--initialMemory",
            "768",
            "--runtime",
            "incremental",
            "-o",
            "dist/assemblyscript.wasm",
            "-O1",
        ],
        cwd=ROOT,
        check=True,
    )
    std = ROOT / "dist/std"
    if std.exists():
        shutil.rmtree(std)
    shutil.copytree(ROOT / "node_modules/assemblyscript/std/assembly", std)
    licenses = ROOT / "dist/licenses"
    licenses.mkdir(exist_ok=True)
    for label, origin in [
        ("assemblyscript", source),
        ("binaryen", ROOT / "node_modules/binaryen"),
    ]:
        for name in ("LICENSE", "NOTICE"):
            if (origin / name).exists():
                shutil.copyfile(origin / name, licenses / f"{label}-{name}")
    artifacts = {}
    from src.bootstrap_shims import build_shims
    build_shims()
    for path in sorted((ROOT / "dist").rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            artifacts[str(path.relative_to(ROOT / "dist"))] = {
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
    manifest = {
        "assemblyscript": SOURCE_VERSION,
        "binaryen": "131.0.0-nightly.20260721",
        "host": "Wisp with Python 3.10+ and its Wasmtime Python API",
        "host_requirements": {"wasmtime_python": {"provider": "wisp", "tested_version": "49.0.0", "environment": "WISP_WASMTIME_PYTHON", "interface": "Python Wasmtime API with Core Wasm host callbacks"}},
        "compiler_execution": "AssemblyScript compiler and Binaryen backend both execute as Wasm in Wasmtime",
        "fullyWasmHostedBuild": True,
        "abi_adapters": "prebuilt Wasm modules, generated only during bootstrap",
        "source": {"url": SOURCE_URL, "sha256": SOURCE_SHA256},
        "npm_integrity": "package-lock.json",
        "artifacts": artifacts,
    }
    (ROOT / "dist/manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("Bootstrap complete. Run python3 verify.py.")


if __name__ == "__main__":
    main()
