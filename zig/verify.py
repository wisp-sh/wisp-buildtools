#!/usr/bin/env python3
"""Exercise Zig syntax, standard library use, imports, and diagnostics in Wasm."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from compile import clang_backend, host_runtime

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def uleb(data: bytes, offset: int) -> tuple[int, int]:
    value, shift = 0, 0
    while True:
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if not byte & 128:
            return value, offset
        shift += 7
        if shift > 35:
            raise ValueError("invalid Wasm length")


def no_imports(path: Path) -> None:
    data = path.read_bytes()
    assert data[:8] == b"\x00asm\x01\x00\x00\x00", "expected Core Wasm"
    offset = 8
    while offset < len(data):
        section = data[offset]
        length, start = uleb(data, offset + 1)
        if section == 2:
            count, _ = uleb(data, start)
            assert count == 0, f"unexpected runtime imports in {path}"
        offset = start + length
    assert offset == len(data), "truncated Wasm section"


def main() -> int:
    started = time.monotonic()
    runtime = host_runtime()
    backend = clang_backend()
    manifest = json.loads((DIST / "manifest.json").read_text())
    for filename, expected in manifest["files"].items():
        path = DIST / filename
        assert path.stat().st_size == expected["bytes"], f"size mismatch: {path}"
        assert sha256(path) == expected["sha256"], f"checksum mismatch: {path}"
    checks = ["runtime manifest checksums"]
    artifacts = {}
    # Absolute runtime and Python paths make PATH unavailable to both compiler
    # stages. Neither stage can silently fall back to native compiler binaries.
    environment = dict(os.environ, PATH="/nonexistent", WISP_WASMTIME=runtime, WISP_CLANG=str(backend))
    with tempfile.TemporaryDirectory(prefix="verification with spaces-", dir=ROOT) as temporary:
        work = Path(temporary)
        for sample, function, arguments, expected in (
            ("add", "add", ["20", "22"], "42"),
            ("features", "answer", [], "42"),
            ("imports", "answer", [], "42"),
        ):
            output = work / f"{sample}.wasm"
            compile_started = time.monotonic()
            result = subprocess.run([sys.executable, str(ROOT / "compile.py"), str(ROOT / f"examples/{sample}.zig"), "-o", str(output)], env=environment, capture_output=True, text=True)
            assert result.returncode == 0, result.stdout + result.stderr
            no_imports(output)
            invocation = subprocess.run([runtime, "run", "--invoke", function, str(output), *arguments], env=environment, capture_output=True, text=True, check=True)
            assert invocation.stdout.strip() == expected, invocation.stdout + invocation.stderr
            checks.append(f"{sample}: {function}({', '.join(arguments)}) == {expected}, no imports, PATH unavailable")
            artifacts[sample] = {"sha256": sha256(output), "bytes": output.stat().st_size, "compile_and_invoke_seconds": round(time.monotonic() - compile_started, 3)}
            if sample == "features":
                floating = subprocess.run([runtime, "run", "--invoke", "twice", str(output), "21"], capture_output=True, text=True, check=True)
                assert floating.stdout.strip() == "42", floating.stdout + floating.stderr
                checks.append("f64 export: twice(21) == 42")
        invalid_output = work / "invalid.wasm"
        invalid = subprocess.run([sys.executable, str(ROOT / "compile.py"), str(ROOT / "examples/invalid.zig"), "-o", str(invalid_output)], env=environment, capture_output=True, text=True)
        assert invalid.returncode != 0, "invalid source compiled"
        assert "undeclared identifier" in invalid.stderr, invalid.stderr
        assert not invalid_output.exists(), "failed compilation published an artifact"
        checks.append("invalid source rejected by Zig semantic analysis with no output")
        retained = work / "retained.wasm"
        retained.write_bytes(b"previous successful build")
        invalid = subprocess.run([sys.executable, str(ROOT / "compile.py"), str(ROOT / "examples/invalid.zig"), "-o", str(retained)], env=environment, capture_output=True, text=True)
        assert invalid.returncode != 0
        assert retained.read_bytes() == b"previous successful build"
        checks.append("failed compilation preserves previous output")
    receipt = {"schema_version": 1, "zig_version": manifest["zig_version"], "frontend_sha256": manifest["files"]["zig.wasm"]["sha256"], "backend_sha256": manifest["clang_backend"]["sha256"], "checks": checks, "artifacts": artifacts, "seconds": round(time.monotonic() - started, 3)}
    receipt["fullyWasmHostedBuild"] = True
    receipt_path = ROOT / "receipts/verification.json"
    receipt_path.parent.mkdir(exist_ok=True)
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    for check in checks:
        print(f"PASS {check}")
    print(f"Receipt: {receipt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
