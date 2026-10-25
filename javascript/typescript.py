"""Bundle JavaScript/TypeScript with esbuild running as a WASI guest."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import subprocess
import uuid

ROOT = Path(__file__).resolve().parent
ESBUILD_VERSION = "0.28.2"


def bundle(source: Path, module_root: Path, temp_dir: Path, *,
           esbuild_wasm: Path, wasmtime: Path, timeout: int = 120) -> dict:
    """Return emitted JS, exact input hashes, and guest compiler provenance.

    The source tree is the guest root. The guest sees no other host files except
    its fresh output directory, and receives no inherited environment variables.
    """
    source, module_root = source.resolve(strict=True), module_root.resolve(strict=True)
    relative = source.relative_to(module_root)
    temp_dir = temp_dir.resolve()
    temp_dir.mkdir(parents=True, exist_ok=True)
    esbuild_wasm, wasmtime = esbuild_wasm.resolve(strict=True), wasmtime.resolve(strict=True)
    token = uuid.uuid4().hex
    guest_output = f"/.wisp-esbuild-output-{token}"
    host_output = temp_dir / f"esbuild-{token}"
    host_output.mkdir(mode=0o700)
    command = [
        str(wasmtime), "run", "-C", "cache=y", "-O", "opt-level=0", "-C", "parallel-compilation=n", "--dir", f"{module_root}::/",
        "--dir", f"{host_output}::{guest_output}", "--env", "PWD=/",
        "--env", "GOMAXPROCS=1", str(esbuild_wasm),
        "--entry", "/" + relative.as_posix(),
        "--out", guest_output + "/bundle.mjs",
        "--metafile", guest_output + "/metafile.json",
    ]
    # Native runtime configuration is intentionally small. Child guest variables
    # are passed explicitly above, including the Go WASI working directory.
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP",
                      "WISP_CWASM_RUNTIME", "WISP_CWASM_CACHE", "WISP_CWASM_COMPILER", "WISP_CWASM_BACKEND"}}
    env["RAYON_NUM_THREADS"] = "1"
    completed = subprocess.run(command, text=True, capture_output=True, env=env, timeout=timeout)
    if completed.returncode:
        raise ValueError("WASI esbuild failed:\n" + (completed.stderr or completed.stdout).strip())
    metafile = json.loads((host_output / "metafile.json").read_text())
    guest_digests = metafile.get("wispInputSha256")
    if not isinstance(guest_digests, dict):
        raise ValueError("WASI esbuild did not report hashes of parsed input bytes")
    inputs = []
    for name in sorted(metafile["inputs"]):
        guest_absolute = posixpath.normpath("/" + name.lstrip("/"))
        guest_digest = guest_digests.get(guest_absolute)
        if not isinstance(guest_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", guest_digest):
            raise ValueError(f"WASI esbuild did not hash a bundled input: {name}")
        guest_path = Path(guest_absolute).relative_to("/")
        path = (module_root / guest_path).resolve()
        path.relative_to(module_root)
        inputs.append({"path": str(path), "sha256": guest_digest})
    return {
        "js": (host_output / "bundle.mjs").read_text(encoding="utf-8"),
        "inputs": inputs,
        "compiler": {
            "name": f"esbuild@{ESBUILD_VERSION}",
            "target": "wasip1/wasm",
            "sha256": hashlib.sha256(esbuild_wasm.read_bytes()).hexdigest(),
            "sourceCompilerWasmHosted": True,
            "nodeRequired": False,
        },
    }
