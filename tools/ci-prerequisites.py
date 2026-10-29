#!/usr/bin/env python3
"""Install checksum-pinned Linux CI bootstrap tools independently of Wisp."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
LOCK = json.loads((ROOT / "tools/ci-tools.lock.json").read_text())
TOOLS = ("clang", "go", "grain", "ocaml", "goose", "llvm", "rust", "zig", "assemblyscript", "nim",
         "javascript", "python", "wasm-tools", "swift", "llvm-swift", "jvm", "teavm", "java", "kotlin",
         "tsrs", "wizer", "csharp")
SHARED_LLVM_CONSUMERS = ("clang", "llvm", "zig", "nim", "ocaml", "goose", "teavm", "java", "kotlin")
SHARED_RUST_CONSUMERS = ("rust",)
CACHE = ROOT / ".ci/downloads"
INSTALL = ROOT / ".ci/toolchains"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def requirements(tool: str) -> list[str]:
    # Python's bootstrap and the Wizer host adapter pin the exact stock 50 binary.
    # All compiler execution uses the Wisp host from shared-hosts/wasmtime.lock.json.
    selected = ["wasmtime"] if tool in ("python", "wizer", "javascript") else []
    selected += {
        "go": ["go-1.27.2"],
        "grain": ["wasi-sdk-34"],
        "csharp": ["wasi-sdk-34", "wasmtime-python"],
        "ocaml": ["wasi-sdk-34"],
        "goose": ["wasi-sdk-34"],
        **{name: ["wasi-sdk-34"] for name in ("jvm", "teavm", "java", "kotlin")},
        "swift": ["binaryen", "wasmtime-python"],
        "llvm-swift": ["binaryen", "wasmtime-python"],
        "javascript": ["go-1.27.2", "wasmtime-python"],
        "python": ["wasm-tools", "wac", "binaryen"],
        "rust": ["binaryen", "wasi-sdk-34"],
        "assemblyscript": ["wasmtime-python"],
        "tsrs": ["wasmtime-python"],
    }.get(tool, [])
    return selected


def rust_bootstrap(tool: str) -> dict | None:
    if tool not in ("javascript", "python", "rust", *SHARED_LLVM_CONSUMERS):
        return None
    bootstrap = dict(LOCK["rustBootstrap"])
    if tool == "javascript":
        # Reproducing the checksum-pinned guest requires its original compiler.
        manifest = json.loads((ROOT / "javascript/checksums/wasm-tools.json").read_text())
        compiler = manifest["bootstrapRustc"].split()
        if len(compiler) < 2 or compiler[0] != "rustc" or not re.fullmatch(r"\d+\.\d+\.\d+", compiler[1]):
            raise ValueError("invalid JavaScript bootstrap Rust compiler pin")
        bootstrap["version"] = compiler[1]
    return bootstrap


def check_lock() -> None:
    if LOCK["schema"] != 1:
        raise ValueError("unsupported CI tool lock")
    for name, spec in LOCK["downloads"].items():
        hashes = [spec["sha256"], *spec.get("additionalFiles", {}).values()]
        if "executableSha256" in spec:
            hashes.append(spec["executableSha256"])
        if any(not re.fullmatch(r"[a-f0-9]{64}", value) for value in hashes):
            raise ValueError(f"invalid SHA-256 in CI tool lock: {name}")
        if not spec["url"].startswith("https://"):
            raise ValueError(f"CI download must use HTTPS: {name}")
    # Python's bootstrap intentionally checks the exact native tool bytes.
    python_lock = json.loads((ROOT / "python/toolchain.lock.json").read_text())
    for name in ("wasmtime", "wasm-tools", "wac"):
        if LOCK["downloads"][name]["executableSha256"] != python_lock["tools"][name]["sha256"]:
            raise ValueError(f"CI and Python bootstrap hashes differ: {name}")
    if LOCK["downloads"]["binaryen"]["executableSha256"] != python_lock["tools"]["wasm-opt"]["sha256"]:
        raise ValueError("CI and Python wasm-opt hashes differ")
    wizer_lock = json.loads((ROOT / 'wizer/toolchain.lock.json').read_text())
    if LOCK['downloads']['wasmtime']['executableSha256'] != wizer_lock['hosts']['official-linux-x86_64']['sha256']:
        raise ValueError('CI and Wizer execution host hashes differ')
    rust_lock = json.loads((ROOT / "rust/source-lock.json").read_text())
    if LOCK["downloads"]["wasi-sdk-34"]["sha256"] != rust_lock["wasi_sdk"]["sha256"]:
        raise ValueError("CI and Rust WASI SDK hashes differ")
    grain_lock = json.loads((ROOT / "grain/toolchain.lock.json").read_text())
    if LOCK["downloads"]["wasi-sdk-34"]["sha256"] != grain_lock["bootstrapWasiSdk"]["sha256"]:
        raise ValueError("CI and Grain WASI SDK hashes differ")
    jvm_lock = json.loads((ROOT / "jvm/toolchain.lock.json").read_text())
    if LOCK["downloads"]["wasi-sdk-34"]["sha256"] != jvm_lock["bootstrapWasiSdk"]["sha256"]:
        raise ValueError("CI and JVM WASI SDK hashes differ")
    teavm_lock = json.loads((ROOT / "teavm/toolchain.lock.json").read_text())
    if LOCK["downloads"]["wasi-sdk-34"]["sha256"] != teavm_lock["bootstrapWasiSdk"]["sha256"]:
        raise ValueError("CI and TeaVM WASI SDK hashes differ")
    if LOCK["downloads"]["wasm-tools"]["executableSha256"] != teavm_lock["bootstrapWasmTools"]["sha256"]:
        raise ValueError("CI and TeaVM native wasm-tools hashes differ")
    swift_lock = json.loads((ROOT / "swift/toolchain.lock.json").read_text())
    binaryen = LOCK["downloads"]["binaryen"]
    for name, expected in swift_lock["binaryen"].items():
        if name == "version":
            continue
        filename = {"wasm-optSha256": binaryen["executable"],
                    "wasm-splitSha256": str(Path(binaryen["executable"]).with_name("wasm-split"))}[name]
        actual = (binaryen["executableSha256"] if name == "wasm-optSha256"
                  else binaryen["additionalFiles"].get(filename))
        if actual != expected:
            raise ValueError(f"CI and Swift Binaryen hashes differ: {name}")
    host = ROOT / "tools/shared-hosts"
    origin = json.loads((host / "origin.json").read_text())
    if origin["owner"] != "wisp":
        raise ValueError("compiler execution hosts must belong to Wisp")
    for name, expected in origin["files"].items():
        if digest(host / name) != expected:
            raise ValueError(f"Wisp host source copy differs from its origin inventory: {name}")
        original = ROOT / origin["sourceDirectory"] / name
        if original.is_file() and digest(original) != expected:
            raise ValueError(f"refresh the CI copy of the Wisp host recipe: {name}")
    host_lock = json.loads((host / "wasmtime.lock.json").read_text())
    if python_lock['tools']['wasmtime']['version'] != host_lock['version']:
        raise ValueError('Python bootstrap and compiler execution host Wasmtime versions differ')
    stock = LOCK['downloads']['wasmtime']
    if stock['executable'] != f"wasmtime-v{host_lock['version']}-x86_64-linux/wasmtime":
        raise ValueError('CI bootstrap and compiler execution host Wasmtime versions differ')
    adapter_lock = json.loads((ROOT / 'wasm-tools/adapter.lock.json').read_text())
    if adapter_lock['version'] != host_lock['version']:
        raise ValueError('Preview 1 adapter and compiler execution host Wasmtime versions differ')
    if tuple(map(int, host_lock["rustVersion"].split("."))) > tuple(map(int, LOCK["rustBootstrap"]["version"].split("."))):
        raise ValueError("CI and Wisp execution host Rust versions differ")
    rust_host_lock = json.loads((host / "rust-wasmtime.lock.json").read_text())
    if tuple(map(int, rust_host_lock["rustVersion"].split("."))) > tuple(map(int, LOCK["rustBootstrap"]["version"].split("."))):
        raise ValueError("CI and Wisp Rust execution host bootstrap versions differ")


def download(name: str) -> Path:
    spec = LOCK["downloads"][name]
    CACHE.mkdir(parents=True, exist_ok=True)
    archive = CACHE / spec["url"].rsplit("/", 1)[-1]
    if archive.is_file() and digest(archive) == spec["sha256"]:
        return archive
    with tempfile.NamedTemporaryFile(dir=CACHE, delete=False) as output:
        partial = Path(output.name)
        try:
            print(f"Download {name}: {spec['url']}", flush=True)
            with urllib.request.urlopen(spec["url"], timeout=120) as response:
                shutil.copyfileobj(response, output)
            output.close()
            if digest(partial) != spec["sha256"]:
                raise ValueError(f"download SHA-256 mismatch: {name}")
            partial.replace(archive)
        finally:
            partial.unlink(missing_ok=True)
    return archive


def install(name: str) -> Path:
    spec = LOCK["downloads"][name]
    archive = download(name)
    INSTALL.mkdir(parents=True, exist_ok=True)
    destination = INSTALL / name
    # Re-extract verified archives. A cache marker cannot validate extracted bytes.
    with tempfile.TemporaryDirectory(prefix=f".{name}-", dir=INSTALL) as temporary:
        staging = Path(temporary)
        kind = spec.get("format", "tar")
        if kind == "deb":
            subprocess.run(["dpkg-deb", "--extract", str(archive), str(staging)], check=True)
        elif kind == "wheel":
            subprocess.run([sys.executable, "-I", "-m", "pip", "install", "--no-index", "--no-deps",
                            "--disable-pip-version-check", "--target", str(staging), str(archive)], check=True)
        elif kind == "file":
            shutil.copyfile(archive, staging / spec["executable"])
            (staging / spec["executable"]).chmod(0o755)
        else:
            with tarfile.open(archive) as source:
                source.extractall(staging, filter="data")
        executable = staging / spec["executable"]
        if not executable.is_file():
            raise ValueError(f"download is missing its executable: {name}")
        files = dict(spec.get("additionalFiles", {}))
        if "executableSha256" in spec:
            files[spec["executable"]] = spec["executableSha256"]
        for relative, expected in files.items():
            if digest(staging / relative) != expected:
                raise ValueError(f"extracted SHA-256 mismatch: {name}/{relative}")
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(staging, destination, symlinks=True)
    return destination / spec["executable"]


def github_environment(environment: dict[str, str], paths: list[str]) -> None:
    env_file, path_file = os.environ.get("GITHUB_ENV"), os.environ.get("GITHUB_PATH")
    for value in [*environment.keys(), *environment.values(), *paths]:
        if "\n" in value or "\r" in value:
            raise ValueError("CI environment values must occupy one line")
    if env_file:
        with Path(env_file).open("a") as stream:
            for key, value in environment.items():
                stream.write(f"{key}={value}\n")
    if path_file:
        with Path(path_file).open("a") as stream:
            for path in paths:
                stream.write(path + "\n")


def execution_host(recipe: str, prefix: Path, cache: Path) -> None:
    command = [sys.executable, str(ROOT / "tools/shared-hosts" / recipe), "--prefix", str(prefix)]
    started = time.monotonic()
    if (prefix / "wasmtime").is_file() and (prefix / "receipt.json").is_file():
        try:
            # The current trusted recipe verifies the lock, patch, binary hash,
            # and real provider behavior before any cached host is accepted.
            subprocess.run([*command, "--check"], check=True)
        except (OSError, subprocess.CalledProcessError) as error:
            print(f"Rebuild execution host {prefix.name}: cached host check failed: {error}",
                  file=sys.stderr, flush=True)
        else:
            print(f"Reused verified execution host {prefix.name} in {time.monotonic() - started:.1f}s",
                  file=sys.stderr, flush=True)
            return
    subprocess.run([*command, "--cache", str(cache), "--jobs", os.environ.get("BUILD_JOBS", "2")],
                   check=True)
    print(f"Built execution host {prefix.name} in {time.monotonic() - started:.1f}s",
          file=sys.stderr, flush=True)


def install_for(tool: str) -> dict:
    binaries = {name: install(name) for name in requirements(tool)}
    host_receipts = []
    environment = {}
    paths = []
    if tool == "go":
        environment["WISP_GO_HOST_ROOT"] = str(binaries["go-1.27.2"].parents[1])
    if tool == "goose":
        environment["WASI_SDK_PATH"] = str(binaries["wasi-sdk-34"].parents[1])
    if tool == "ocaml":
        environment["WISP_OCAML_WASI_SDK"] = str(binaries["wasi-sdk-34"].parents[1])
    if tool == "grain":
        environment["WISP_GRAIN_WASI_SDK"] = str(binaries["wasi-sdk-34"].parents[1])
    if tool == "csharp":
        environment["WISP_CSHARP_WASI_SDK"] = str(binaries["wasi-sdk-34"].parents[1])
    if tool in ("jvm", "teavm", "java", "kotlin"):
        environment["WISP_JVM_WASI_SDK"] = str(binaries["wasi-sdk-34"].parents[1])
    if tool == "javascript":
        environment["WISP_JAVASCRIPT_BOOTSTRAP_GO"] = str(binaries["go-1.27.2"])
    if tool == "python":
        environment.update({"WISP_BOOTSTRAP_WASM_TOOLS": str(binaries["wasm-tools"]),
                            "WISP_WAC": str(binaries["wac"])})
    if tool in ("wizer", "javascript"):
        environment["WISP_WIZER_WASMTIME"] = str(binaries["wasmtime"])
    if "binaryen" in binaries:
        environment["WISP_WASM_OPT"] = str(binaries["binaryen"])
    if tool in SHARED_RUST_CONSUMERS:
        sdk = binaries["wasi-sdk-34"].parents[1]
        environment["WISP_RUST_WASI_SDK"] = str(sdk)
        # The source driver also uses this conventional location without an override.
        expected = ROOT / "rust/downloads" / sdk.name
        expected.parent.mkdir(parents=True, exist_ok=True)
        if expected.is_symlink():
            expected.unlink()
        if not expected.exists():
            expected.symlink_to(sdk, target_is_directory=True)
        # Rust verifies the original SDK archive's size and checksum as well as
        # the extracted tools. Keep the verified download at its expected path.
        archive = download("wasi-sdk-34")
        expected_archive = expected.parent / archive.name
        if expected_archive.is_symlink():
            expected_archive.unlink()
        if not expected_archive.exists():
            expected_archive.symlink_to(archive.resolve())
    if tool in ("assemblyscript", "swift", "llvm-swift", "tsrs", "javascript", "csharp"):
        environment["WISP_WASMTIME_PYTHON"] = str(binaries["wasmtime-python"].parents[1])
    bootstrap = rust_bootstrap(tool)
    if bootstrap is not None:
        subprocess.run(["rustup", "toolchain", "install", bootstrap["version"], "--profile", "minimal",
                        "--target", bootstrap["target"]], check=True)
        rustc = subprocess.check_output(["rustup", "which", "--toolchain", bootstrap["version"],
                                        "rustc"], text=True).strip()
        paths.insert(0, str(Path(rustc).parent))
    host_lock = json.loads((ROOT / 'tools/shared-hosts/wasmtime.lock.json').read_text())
    subprocess.run(['rustup', 'toolchain', 'install', host_lock['rustVersion'], '--profile', 'minimal'], check=True)
    prefix = ROOT / '.ci/hosts' / ('wisp-wasmtime-' + host_lock['version'])
    execution_host('build.py', prefix, ROOT / '.ci/host-build')
    host_receipts.append(prefix / 'receipt.json')
    launcher = str(ROOT / 'cwasm.py')
    environment.update(WISP_CWASM_RUNTIME=str(prefix / 'wasmtime'), WISP_WASMTIME=launcher,
                       WASMTIME=launcher, WISP_RUST_WASMTIME=launcher,
                       WISP_OCAML_WASMTIME=launcher, WISP_GOOSE_FRONTEND_HOST=launcher,
                       WISP_BOOTSTRAP_WASMTIME=str(binaries.get('wasmtime', prefix / 'wasmtime')),
                       WISP_LLVM=str(ROOT / 'llvm/dist/shared'))
    paths.insert(0, str(prefix))
    github_environment(environment, paths)
    report = {"schema": 1, "tool": tool, "environment": environment, "paths": paths,
              "downloads": {name: LOCK["downloads"][name]["sha256"] for name in binaries},
              "executionHosts": {path.parent.name: json.loads(path.read_text())
                                 for path in host_receipts}}
    (ROOT / ".ci/prerequisites.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("install", "plan"):
        child = commands.add_parser(command)
        child.add_argument("tool", choices=TOOLS)
    commands.add_parser("check")
    commands.add_parser("lint")
    args = parser.parse_args()
    check_lock()
    if args.command == "check":
        print("CI bootstrap locks match the toolchain locks")
        return
    if args.command == "plan":
        print(json.dumps({"tool": args.tool, "downloads": requirements(args.tool),
                          "rustBootstrap": rust_bootstrap(args.tool),
                          "wispSharedHost": json.loads((ROOT / "tools/shared-hosts/wasmtime.lock.json").read_text())
                              if args.tool in (*SHARED_LLVM_CONSUMERS, *SHARED_RUST_CONSUMERS) else None},
                         indent=2))
        return
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("CI bootstrap currently supports Linux x86_64")
    if args.command == "lint":
        executable = install("actionlint")
        subprocess.run([str(executable), "-shellcheck=", "-pyflakes=", *map(str, sorted(
            (ROOT / ".github/workflows").glob("*.yml")))], cwd=ROOT, check=True)
        return
    print(json.dumps(install_for(args.tool), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as error:
        sys.exit(f"ci-prerequisites: {error}")
