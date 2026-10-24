#!/usr/bin/env python3
"""Bootstrap the pinned esbuild WASI compiler without Node or npm."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile

ESBUILD_VERSION = "0.28.2"
GO_VERSION = "go1.27.2"
SOURCE_URL = f"https://proxy.golang.org/github.com/evanw/esbuild/@v/v{ESBUILD_VERSION}.zip"
SOURCE_SHA256 = "db0fc2918ef7d1769b1746ed123d40db4f92636e2536dda4e40f5d080ae30de2"
SOURCE_MODULE_SUM = "h1:A2uETn4jrQTcXaT/shwTDTYBxDjl7fV7nXmUrJxfA2w="
ROOT = Path(__file__).resolve().parent.parent


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(output: Path, receipt: Path, go: str = "go", offline: bool = False) -> dict:
    output, receipt = output.resolve(), receipt.resolve()
    go_path = shutil.which(go)
    if not go_path:
        raise ValueError(f"Go {GO_VERSION.removeprefix('go')} is required only to bootstrap esbuild.wasm")
    env = os.environ.copy()
    env.update({"GOTOOLCHAIN": "local", "GOWORK": "off", "GOFLAGS": "", "GOMAXPROCS": "4"})
    version = subprocess.check_output([go_path, "version"], text=True, env=env).strip()
    if version.split()[2] != GO_VERSION:
        raise ValueError(f"expected bootstrap compiler {GO_VERSION}; found {version}")
    wrapper = ROOT / "tools/esbuild-wasi.go"
    cache = ROOT / ".toolchains/downloads"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"esbuild-{ESBUILD_VERSION}.zip"
    if not archive.is_file() or sha256(archive) != SOURCE_SHA256:
        if offline:
            raise ValueError("pinned esbuild source archive is not cached for offline bootstrap")
        with urllib.request.urlopen(SOURCE_URL, timeout=60) as response:
            payload = response.read(64 * 1024 * 1024 + 1)
        if hashlib.sha256(payload).hexdigest() != SOURCE_SHA256:
            raise ValueError("esbuild source archive checksum mismatch")
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as downloaded:
            downloaded.write(payload)
            downloaded_path = Path(downloaded.name)
        os.replace(downloaded_path, archive)
    output.parent.mkdir(parents=True, exist_ok=True)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="esbuild-wasi-", dir=output.parent) as temporary:
        work = Path(temporary)
        source = work / "source"
        prefix = f"github.com/evanw/esbuild@v{ESBUILD_VERSION}/"
        with zipfile.ZipFile(archive) as entries:
            for entry in entries.infolist():
                if not entry.filename.startswith(prefix):
                    raise ValueError("unexpected esbuild source archive member")
                relative = Path(entry.filename.removeprefix(prefix))
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("unsafe esbuild source archive member")
                target = source / relative
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(entries.read(entry))
        main = source / "cmd/wisp-esbuild/main.go"
        main.parent.mkdir(parents=True)
        shutil.copyfile(wrapper, main)
        # Upstream's module declares x/sys for Linux/macOS terminal handling.
        # Its WASI files use only the standard library. Keep the original Go
        # language version while removing that unused native-only module edge.
        guest_module = "module github.com/evanw/esbuild\n\ngo 1.13\n"
        (source / "go.mod").write_text(guest_module)
        built = work / "esbuild.wasm"
        # The WASI source graph uses no third-party modules. Disable network
        # resolution so source compilation depends only on the pinned archive.
        env.update({"GOOS": "wasip1", "GOARCH": "wasm", "CGO_ENABLED": "0", "GOPROXY": "off"})
        command = [go_path, "build", "-mod=readonly", "-trimpath", "-buildvcs=false",
                   "-ldflags=-s -w", "-o", str(built), "./cmd/wisp-esbuild"]
        subprocess.run(command, cwd=source, env=env, check=True)
        if built.read_bytes()[:8] != b"\x00asm\x01\x00\x00\x00":
            raise ValueError("Go did not emit a WebAssembly core module")
        result = {
            "schemaVersion": 1,
            "compiler": f"esbuild@{ESBUILD_VERSION}",
            "sourceUrl": SOURCE_URL,
            "sourceSha256": SOURCE_SHA256,
            "sourceModuleSum": SOURCE_MODULE_SUM,
            "goVersion": GO_VERSION,
            "target": "wasip1/wasm",
            "wrapperSha256": sha256(wrapper),
            "guestGoModSha256": hashlib.sha256(guest_module.encode()).hexdigest(),
            "artifactSha256": sha256(built),
            "artifactBytes": built.stat().st_size,
            "nativeBootstrapOnly": True,
            "nodeRequired": False,
        }
        receipt_stage = work / "receipt.json"
        receipt_stage.write_text(json.dumps(result, indent=2) + "\n")
        os.replace(built, output)
        os.replace(receipt_stage, receipt)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".toolchains/wasm/esbuild.wasm")
    parser.add_argument("--receipt", type=Path, default=ROOT / ".toolchains/esbuild-build.json")
    parser.add_argument("--go", default=os.environ.get("WISP_JAVASCRIPT_BOOTSTRAP_GO", "go"))
    parser.add_argument("--offline", action="store_true", help="require the pinned source archive in the local cache")
    args = parser.parse_args()
    print(json.dumps(build(args.output, args.receipt, args.go, args.offline), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"build-esbuild-wasi: {error}")
