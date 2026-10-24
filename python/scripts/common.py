"""Host orchestration helpers. User Python is never evaluated on the host."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))
if os.environ.get("WISP_WASM_TOOLS_TOOLCHAIN"):
    sys.path.insert(0, str(Path(os.environ["WISP_WASM_TOOLS_TOOLCHAIN"]).resolve().parent))
import wasm_tools as guest_tools
LOCK = json.loads((ROOT / "toolchain.lock.json").read_text())
MAX_COMPONENT_BYTES = 16 * 1024 * 1024


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def tool(name: str) -> Path:
    variable = "WISP_BOOTSTRAP_WASM_TOOLS" if name == "wasm-tools" else "WISP_" + name.upper().replace("-", "_")
    sibling = ROOT.parent / "clang/.toolchains"
    candidates = [os.environ.get(variable), shutil.which(name)]
    candidates += {
        "wasmtime": [],
        "wasm-tools": [sibling / "wasm-tools-1.262.0-x86_64-linux/wasm-tools"],
        "wasm-opt": [sibling / "binaryen-version_133/bin/wasm-opt"],
        "wac": [ROOT.parent.parent / "wisp/.tools/build/wac"],
    }[name]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    raise ValueError(f"{name} is missing; set {variable} to the executable")


def run(command, *, capture=False, timeout=300, env=None):
    return subprocess.run([str(value) for value in command], check=True,
                          text=True, capture_output=capture, timeout=timeout, env=env)


def invoke(component: Path, expression: str, *, timeout=30) -> str:
    # No preopens, environment inheritance or network capabilities for the guest.
    try:
        capabilities = ("inherit-network=n,allow-ip-name-lookup=n,tcp=n,udp=n,inherit-env=n,"
                        "inherit-stdin=n,inherit-stdout=n,inherit-stderr=n")
        result = run([tool("wasmtime"), "run", "-S", capabilities, "--invoke", expression, component],
                     capture=True, timeout=timeout)
    except subprocess.CalledProcessError as error:
        raise ValueError(f"guest invocation failed: {error.stderr.strip() or error.stdout.strip()}") from error
    return result.stdout.strip()


def verified_package(dist: Path) -> bool:
    try:
        manifest = json.loads((dist / "manifest.json").read_text())
        if manifest["schema"] != 1 or manifest["bootstrap"] != LOCK:
            return False
        for name, expected in manifest["inputs"].items():
            if digest(ROOT / name) != expected:
                return False
        files = manifest["sha256"]
        if not {"template.component.wasm", "runtime.wit", "runtime/app.py", "python-packager.wasm"}.issubset(files):
            return False
        for name, expected in files.items():
            path = dist / name
            if not path.resolve().is_relative_to(dist.resolve()) or digest(path) != expected:
                return False
        # Editing the wrapper or WIT requires rebuilding its CPython snapshot.
        return (digest(ROOT / "runtime/app.py") == files["runtime/app.py"] and
                digest(ROOT / "wit/runtime.wit") == files["runtime.wit"])
    except (OSError, KeyError, TypeError, ValueError):
        return False
