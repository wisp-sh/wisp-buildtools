#!/usr/bin/env python3
"""Package upstream Go's WASIp1 compiler tools and complete target stdlib."""

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from wasmbuild.wit_runtime import SUPPORT_ROOT, verified_runtime

GO_VERSION = "go1.27.2"
SOURCE_SHA256 = "03495da2ba64894d40f5c4992e49454fa78b50690604ff92b6afff5081b76e62"


def run(command: list[str], env: dict[str, str]) -> str:
    result = subprocess.run(command, env=env, text=True, capture_output=True)
    if result.returncode:
        raise SystemExit(result.stderr.strip() or result.stdout.strip() or
                         f"Go bootstrap command failed: {command}")
    return result.stdout


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--go-root", type=Path, required=True, help="extracted official Go 1.27.2 Linux archive")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, help="optional isolated Go build cache")
    args = parser.parse_args()
    root = args.go_root.resolve()
    destination = args.output.resolve()
    go = root / "bin/go"
    if not go.is_file():
        parser.error(f"missing {go}")
    if destination.exists() and any(destination.iterdir()):
        parser.error(f"output directory is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)

    runtime_sources = verified_runtime(SUPPORT_ROOT / "wit-runtime")
    with tempfile.TemporaryDirectory(prefix="go-wasm-package-", dir=destination.parent) as temporary:
        work = Path(temporary)
        cache = args.cache.resolve() if args.cache else work / "gocache"
        cache.mkdir(parents=True, exist_ok=True)
        env = {
            "GOROOT": str(root),
            "GOPATH": str(work / "gopath"),
            "GOCACHE": str(cache),
            "GOTMPDIR": str(work),
            "GOOS": "wasip1",
            "GOARCH": "wasm",
            "CGO_ENABLED": "0",
            "GO111MODULE": "off",
            "GOTOOLCHAIN": "local",
            "GOMAXPROCS": "1",
            "PATH": "/usr/bin:/bin",
            "HOME": str(work),
        }
        version = run([str(go), "version"], env).strip()
        if not version.startswith(f"go version {GO_VERSION} "):
            raise SystemExit(f"expected {GO_VERSION}, got {version}")

        bin_dir = destination / "bin"
        bin_dir.mkdir()
        for tool in ("compile", "asm", "link"):
            print(f"building {tool}.wasm", flush=True)
            run([str(go), "build", "-p", "1", "-trimpath", "-o", str(bin_dir / f"{tool}.wasm"), f"cmd/{tool}"], env)

        # Resolve every standard package during packaging. User builds never call Go.
        exports = run([str(go), "list", "-p", "1", "-export", "-f",
                       "{{if .Export}}{{.ImportPath}} {{.Export}}{{end}}", "std"], env)
        archive_root = destination / "goroot/pkg/wasip1_wasm"
        names = []
        for line in exports.splitlines():
            if not line:
                continue
            name, source = line.split(" ", 1)
            archive = archive_root / f"{name}.a"
            archive.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, archive)
            names.append(name)
        shutil.copytree(root / "pkg/include", destination / "goroot/pkg/include")
        shutil.copyfile(root / "LICENSE", destination / "LICENSE")
        for name, data in runtime_sources.items():
            path = destination / "wit-runtime" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (destination / "manifest.toml").write_text(
            f'''[package]
name = "go/toolchain"
version = "1.27.2"
upstream_revision = "{GO_VERSION}"
upstream_source_sha256 = "{SOURCE_SHA256}"
stdlib_archive_count = {len(names)}

[host]
kind = "wasip1"
arch = "wasm32"

[tools.compile]
path = "bin/compile.wasm"

[tools.asm]
path = "bin/asm.wasm"

[tools.link]
path = "bin/link.wasm"

[resources]
goroot = "goroot"
wit_runtime = "wit-runtime"

[capabilities]
filesystem = true
network = false
'''
        )
        # SHA256SUMS covers every immutable package input except itself.
        files = sorted(path for path in destination.rglob("*") if path.is_file())
        (destination / "SHA256SUMS").write_text(
            "".join(f"{sha256(path)}  {path.relative_to(destination)}\n" for path in files)
        )
        for path in files:
            path.chmod(0o444)
        for path in sorted((path for path in destination.rglob("*") if path.is_dir()), reverse=True):
            path.chmod(0o555)
        destination.chmod(0o555)
        print(f"packaged {len(names)} archives at {destination}", flush=True)


if __name__ == "__main__":
    main()
