#!/usr/bin/env python3
"""Build, test and package the Wasm compilers in this repository.

These commands build compiler packages. Use toolchains.py or the language
adapters to compile an application with those packages.
"""
from __future__ import annotations

import argparse
import hashlib
import fnmatch
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile

import toolchain_packages as packages

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "buildtools.json"
WASM_HEADERS = {b"\0asm\x01\0\0\0", b"\0asm\x0d\0\x01\0"}
EXCLUDED = {".git", "__pycache__", "node_modules", "target", ".DS_Store",
            ".work", ".cache", ".sources", ".downloads", ".bootstrap",
            "evidence", ".evidence", "private", ".private", "benchmarks"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".log", ".jsonl", ".time", ".dmp"}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def relative_path(value: str) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or any(part in {".", ".."} for part in value.split("/")):
        raise ValueError(f"expected a safe repository-relative path: {value!r}")
    return path


def load_manifest(path: Path = MANIFEST) -> dict:
    document = json.loads(path.read_text())
    if document.get("schema") != 1 or not isinstance(document.get("tools"), dict) or not document["tools"]:
        raise ValueError("unsupported or empty buildtools manifest")
    tools = document["tools"]
    for name, item in tools.items():
        if not name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in name):
            raise ValueError(f"invalid tool name: {name!r}")
        relative_path(item["directory"])
        for key in ("dependencies", "runtimeDependencies"):
            if any(dependency not in tools for dependency in item[key]):
                raise ValueError(f"unknown dependency in {name}.{key}")
        for value in [*item["artifacts"], *item["package"]["include"], *item["package"].get("optional", []), *item.get("sourceInputs", [])]:
            relative_path(value)
        for step in [*item["build"], *item["test"]]:
            if ("argv" in step) == ("action" in step):
                raise ValueError(f"{name} step needs exactly one of argv or action")
            if "argv" in step and (not isinstance(step["argv"], list) or not step["argv"]):
                raise ValueError(f"{name} step needs nonempty argv")
    dependency_order(document, list(tools))
    dependency_order(document, list(tools), key="runtimeDependencies")
    return document


def dependency_order(document: dict, selected: list[str], *, key="dependencies") -> list[str]:
    tools, result, visiting = document["tools"], [], set()
    def visit(name):
        if name not in tools:
            raise ValueError(f"unknown tool {name!r}; choose one of {', '.join(tools)} or all")
        if name in visiting:
            raise ValueError(f"dependency cycle through {name}")
        if name in result:
            return
        visiting.add(name)
        for dependency in tools[name][key]:
            visit(dependency)
        visiting.remove(name)
        result.append(name)
    for name in selected:
        visit(name)
    return result


def configuration(root: Path, jobs: int) -> dict[str, str]:
    runtime = os.environ.get("WISP_WASMTIME") or os.environ.get("WASMTIME") or shutil.which("wasmtime")
    if not runtime:
        runtime = "wasmtime"
    optimizer = os.environ.get("WISP_WASM_OPT") or shutil.which("wasm-opt")
    if not optimizer:
        optimizer = str(root / "clang/.toolchains/binaryen-version_133/bin/wasm-opt")
    optimizer = str(Path(optimizer).absolute()) if "/" in optimizer else optimizer
    optimizer_lib_root = Path(optimizer).parent.parent / "lib"
    optimizer_lib_default = optimizer_lib_root / "x86_64-linux-gnu"
    if not optimizer_lib_default.is_dir():
        optimizer_lib_default = optimizer_lib_root
    optimizer_lib = os.environ.get("WISP_WASM_OPT_LIBRARY_DIR", str(optimizer_lib_default))
    return {"root": str(root), "python": sys.executable, "jobs": str(jobs),
            "wasmtime": runtime, "bootstrap_wasmtime": os.environ.get("WISP_BOOTSTRAP_WASMTIME", runtime),
            "wasm_opt": optimizer, "wasm_opt_lib": optimizer_lib}


def expand(value, settings):
    if isinstance(value, str):
        return value.format_map(settings)
    if isinstance(value, list):
        return [expand(item, settings) for item in value]
    if isinstance(value, dict):
        return {key: expand(item, settings) for key, item in value.items()}
    return value


def build_plan(document: dict, selected: list[str], settings: dict) -> dict:
    order = dependency_order(document, selected)
    records = []
    for name in order:
        item = document["tools"][name]
        records.append({"name": name, **expand(item, settings)})
    return {"schema": 1, "requested": selected, "order": order, "tools": records,
            "applicationCompilation": "Compiler stages run in Wasm; Python and Wasmtime are native hosts.",
            "compilerBootstrap": "Native tools are used only while creating compiler packages."}


def run(argv, *, root: Path, environment=None, capture=False):
    command = [str(value) for value in argv]
    print("+ " + shlex.join(command), file=sys.stderr, flush=True)
    return subprocess.run(command, cwd=root, env={**os.environ, **(environment or {})},
                          check=True, text=True, capture_output=capture)


def verify_checksum_list(root: Path, inventory: Path) -> None:
    if inventory.suffix == ".json":
        entries = json.loads(inventory.read_text()).items()
    else:
        entries = []
        for line in inventory.read_text().splitlines():
            expected, separator, name = line.partition("  ")
            if not separator:
                raise ValueError(f"malformed checksum inventory: {inventory}")
            entries.append((name, expected))
    seen = set()
    for name, expected in entries:
        path = root / relative_path(name)
        if name in seen or path.is_symlink() or not path.is_file() or digest(path) != expected:
            raise ValueError(f"package checksum mismatch: {path}")
        seen.add(name)


def action(name: str, *, root: Path, settings: dict) -> dict | None:
    python = settings["python"]
    if name == "clang-build":
        directory = root / "clang"
        sdk = root / "llvm/artifacts/llvm-24-d297af3-wasip3-o1"
        reused = sdk.is_dir()
        if reused:
            run([python, root / "llvm/scripts/sdk.py", "verify", sdk], root=root)
            resource_dirs = list((directory / ".toolchains/wasi-sdk-34.0-x86_64-linux/lib/clang").glob("*/include"))
            if len(resource_dirs) != 1:
                raise ValueError("verified shared-library bootstrap requires the pinned WASI SDK resource headers")
            if (directory / "include").exists():
                shutil.rmtree(directory / "include")
            shutil.copytree(resource_dirs[0], directory / "include")
        else:
            run(["bash", directory / "build-clang-wasi.sh"], root=root,
                environment={"BUILD_JOBS": settings["jobs"]})
        action("llvm-sdk", root=root, settings=settings)
        run([python, root / "llvm/scripts/shared.py", "build", "--out", root / "llvm/dist/shared",
             "--clang-output", root / "clang/.toolchains/build-p3/bin/clang-wasm.component",
             "--nim-output", root / "nim/work/backend-build/c-backend"], root=root)
        from llvm.shared import preload_args
        backend = root / "clang/.toolchains/build-p3/bin/clang-wasm.component"
        wasm_tools = root / "clang/.toolchains/wasm-tools-1.262.0-x86_64-linux/wasm-tools"
        run([wasm_tools, "validate", backend], root=root)
        wit = run([wasm_tools, "component", "wit", backend], root=root, capture=True).stdout
        if "export wasi:cli/run@0.3.0;" not in wit or re.search(r"wasi_snapshot_preview1|wasi:[^\s]*@0\.[012]\.", wit):
            raise ValueError("source-built C/C++ compiler does not expose the required WASI 0.3 command contract")
        parent = root / "build/clang"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="compiler-check-", dir=parent) as temporary:
            work = Path(temporary)
            (work / "add.cpp").write_text('extern "C" int add(int a, int b) { return a + b; }\n')
            run([settings["wasmtime"], "run", "-S", "p3=y", *preload_args(root / "llvm/dist/shared"), "--dir", f"{work}::/work", backend,
                 "/work/add.cpp", "-o", "/work/add.wasm", "-O1"], root=root)
            value = run([settings["wasmtime"], "run", "--invoke", "add", work / "add.wasm", "20", "22"],
                        root=root, capture=True).stdout.strip()
            if value != "42":
                raise ValueError("source-built C/C++ compiler smoke output differs from 42")
            return {"componentInterface": "wasi:cli/run@0.3.0", "interfaceSha256": hashlib.sha256(wit.encode()).hexdigest(),
                    "sourceBootstrap": "reused-verified-sdk" if reused else "native-source-build",
                    "sdkInventorySha256": digest(sdk / "SHA256SUMS"),
                    "smokeResult": 42, "smokeOutputSha256": digest(work / "add.wasm")}
    elif name == "go-package":
        destination = root / "go/dist/go-toolchain-full-1.27.2"
        if (destination / "SHA256SUMS").is_file():
            verify_checksum_list(destination, destination / "SHA256SUMS")
            return
        configured = os.environ.get("WISP_GO_HOST_ROOT")
        native = Path(configured) if configured else root / "go/.bootstrap/go-host/go"
        if not (native / "bin/go").is_file():
            raise ValueError("set WISP_GO_HOST_ROOT to an extracted official Go 1.27.2 distribution")
        run([python, root / "go/scripts/build-toolchain.py", "--go-root", native,
             "--output", destination, "--cache", root / "build/go/bootstrap-cache"], root=root)
    elif name == "llvm-sdk":
        destination = root / "llvm/artifacts/llvm-24-d297af3-wasip3-o1"
        if destination.is_dir():
            run([python, root / "llvm/scripts/sdk.py", "verify", destination], root=root)
            return
        archive = root / "llvm/.sources/llvm-project-d297af3.tar.gz"
        if not archive.is_file():
            archive.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / "clang/.toolchains/llvm-project-d297af3.tar.gz", archive)
        run([python, root / "llvm/scripts/sdk.py", "package", "--source-dir",
             root / "clang/.toolchains/llvm-project", "--build-dir", root / "clang/.toolchains/build-p3",
             "--out", destination], root=root)
    elif name == "llvm-shared":
        run([python, root / "llvm/scripts/shared.py", "verify", root / "llvm/dist/shared"], root=root)
    elif name == "llvm-smoke":
        parent = root / "build/llvm"
        parent.mkdir(parents=True, exist_ok=True)
        output = Path(tempfile.mkdtemp(prefix="sdk-smoke-", dir=parent)) / "result"
        run([python, root / "llvm/scripts/verify-sdk-smoke.py", "--sdk",
             root / "llvm/artifacts/llvm-24-d297af3-wasip3-o1", "--wasi-sdk",
             root / "clang/.toolchains/wasi-sdk-34.0-x86_64-linux", "--wasmtime",
             settings["wasmtime"], "--out", output], root=root)
    elif name == "rust-source":
        lock = json.loads((root / "rust/source-lock.json").read_text())
        source = root / ("rust/build/source-rust-" + lock["sources"][0]["revision"][:12])
        if not source.exists():
            run([python, root / "rust/scripts/prepare-source.py", "--jobs", settings["jobs"]], root=root)
        patches = []
        for specification in [lock["macro_backport"], *lock.get("build_patches", [])]:
            patch = root / "rust" / relative_path(specification["path"])
            if digest(patch) != specification["sha256"]:
                raise ValueError(f"compiler patch checksum mismatch: {patch}")
            patches.append(patch)
        for patch in patches:
            # The reviewed patches can be reused after a successful source
            # preparation. --batch alone lets GNU patch silently ignore -R on
            # fresh sources, so force the requested direction for every check.
            command = ["patch", "--batch", "--force", "--fuzz=0", "-d", str(source), "-p1"]
            with patch.open("rb") as stream:
                reverse = subprocess.run([*command, "--dry-run", "--reverse"], stdin=stream,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if reverse.returncode == 0:
                continue
            # Reject partial or divergent patches before writing any hunks.
            with patch.open("rb") as stream:
                forward = subprocess.run([*command, "--dry-run", "--forward"], stdin=stream,
                                         capture_output=True, text=True)
            if forward.returncode != 0:
                raise ValueError(f"cannot apply compiler patch cleanly: {patch}\n"
                                 + forward.stdout + forward.stderr)
            with patch.open("rb") as stream:
                subprocess.run([*command, "--forward"], stdin=stream, check=True)
    elif name == "rust-package":
        destination = root / "rust/dist/compiler"
        if destination.exists():
            inventory = destination / "build/bootstrap/SHA256SUMS.json"
            verify_checksum_list(inventory.parent, inventory)
            report = json.loads((root / "rust/build/compiler-source-build-1.99.1/build-report.json").read_text())
            manifest = json.loads((destination / "toolchain.json").read_text())
            if manifest["compiler"]["moduleSha256"] != report["compiler"]["sha256"]:
                if manifest["compiler"].get("sourceModuleSha256") != report["compiler"]["sha256"]:
                    raise ValueError("existing Rust candidate differs from source build; preserve or move rust/dist/compiler before repackaging")
                action("llvm-rust-verify", root=root, settings=settings)
            return
        run([python, "-I", root / "rust/scripts/build-compiler.py", "--package",
             "--candidate-out", destination], root=root)
    elif name == "rust-shared":
        run([python, root / "llvm/scripts/rust-shared.py", "--input",
             root / "rust/build/compiler-source-build-1.99.1/install/bin/rustc.wasm",
             "--primary", root / "rust/dist/compiler/build/bootstrap/rustc.wasm",
             "--output", root / "llvm/dist/rust-shared", "--wasm-opt", settings["wasm_opt"],
             "--wasm-split", Path(settings["wasm_opt"]).with_name("wasm-split"),
             "--binaryen-lib", settings["wasm_opt_lib"]], root=root)
    elif name == "llvm-rust-verify":
        run([python, root / "llvm/scripts/rust-shared.py", "--check", "--primary",
             root / "rust/dist/compiler/build/bootstrap/rustc.wasm",
             "--output", root / "llvm/dist/rust-shared"], root=root)
    elif name == "rust-acceptance":
        parent = root / "build/rust"
        parent.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="acceptance-", dir=parent))
        vendor = work / "vendor"
        run(["cargo", "vendor", "--locked", "--manifest-path", root / "rust/examples/serde/Cargo.toml",
             "--sync", root / "rust/examples/base64/Cargo.toml", vendor], root=root)
        candidate = root / "rust/dist/compiler"
        run([python, root / "rust/tests/acceptance.py", "--runner-command",
             json.dumps([python, "-I", str(candidate / "scripts/runner.py")]), "--wasmtime",
             os.environ.get("WISP_RUST_WASMTIME", "wasmtime"),
             "--vendor-dir", vendor, "--out", work / "results"], root=root)
        record_rust_acceptance(candidate, work / "results")
    else:
        raise ValueError(f"unknown build action: {name}")


def record_rust_acceptance(candidate: Path, results: Path) -> None:
    result = json.loads((results / "results.json").read_text())
    required = {"scalar", "std", "base64", "build-script", "derive", "serde"}
    if (result.get("status") != "passed"
            or {item["name"] for item in result["cases"] if item["status"] == "passed"} != required):
        raise ValueError("Rust acceptance requires all six successful application cases")
    manifest_path = candidate / "toolchain.json"
    manifest = json.loads(manifest_path.read_text())
    # Keep execution logs and metadata in the caller's results directory. The
    # runtime package needs the tested identities and a compact result only.
    adapter = {"schemaVersion": 1, "status": "passed",
               "scope": "All six standalone application acceptance cases passed.",
               "runnerSha256": digest(candidate / "scripts/runner.py"),
               "compilerSha256": digest(candidate / "build/bootstrap/rustc.wasm"),
               "standaloneCases": sorted(required), "receiptFiles": {},
               "acceptanceResultSha256": digest(results / "results.json")}
    (candidate / "runtime-adapter.json").write_text(json.dumps(adapter, indent=2) + "\n")
    manifest["status"] = "standalone-verified"
    manifest["verification"].update(deriveAcceptancePassed=True, serdeAcceptancePassed=True,
                                    standaloneAcceptancePassed=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    (candidate / "README.txt").write_text(
        f"Rust {manifest['rustVersion']} compiler hosted on {manifest['compilerHost']}.\n"
        "All six standalone application acceptance cases passed, including build scripts and Serde macros.\n"
        "runtime-adapter.json records the tested identities and acceptance result hash.\n"
        "Wisp registry interop is verified separately.\n")


def check_artifacts(item: dict, root: Path) -> None:
    for relative in item["artifacts"]:
        path = root / relative_path(relative)
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"missing compiler artifact: {relative}; run build.py build first")
        if path.suffix in {".wasm", ".component"}:
            with path.open("rb") as stream:
                if stream.read(8) not in WASM_HEADERS:
                    raise ValueError(f"compiler artifact is not Wasm: {relative}")


def adapter_repackage(name: str, root: Path) -> dict | None:
    """Keep compiler provenance when repackaging changed host-only adapters."""
    allowed = {"teavm": {"compile.py", "c_backend.py"}, "kotlin": {"compile.py"}}
    if name not in allowed:
        return None
    manifest_path = root / name / "dist/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    inputs = manifest.get("bootstrapInputs")
    if not isinstance(inputs, dict) or not inputs:
        raise ValueError(f"{name} compiler bootstrap input inventory is missing")
    changed = {}
    for relative, expected in inputs.items():
        path = root / name / relative_path(relative)
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"{name} compiler bootstrap input is missing or a symlink: {relative}")
        current = digest(path)
        if current != expected:
            if relative not in allowed[name]:
                raise ValueError(f"{name} compiler bootstrap input changed; rebuild the distribution: {relative}")
            changed[relative] = {"compilerBootstrapSha256": expected, "packageSha256": current}
    if not changed:
        return None
    return {**manifest, "adapterRepackage": {
        "originalManifestSha256": digest(manifest_path), "hostScripts": changed,
    }}


def verify_package_inputs(name: str, root: Path) -> None:
    """Keep each compiler's existing checksum contract when redistributing it."""
    if name in {"jvm", "teavm", "java", "kotlin"}:
        if adapter_repackage(name, root) is None:
            run([sys.executable, root / name / "bootstrap.py", "--check"], root=root)
        else:
            # The compiler's original recipe remains in its manifest. Only known
            # host adapters may change without rebuilding the verified guests.
            run([sys.executable, "-B", "-c",
                 "import runpy,sys; runpy.run_path(sys.argv[1], run_name='wisp_package_integrity')['verified_dist']()",
                 root / name / "compile.py"], root=root)
        return
    if name in {"swift", "llvm-swift"}:
        run([sys.executable, root / "swift/bootstrap.py", "--check"], root=root)
        return
    inventories = {
        "go": "go/dist/go-toolchain-full-1.27.2/SHA256SUMS",
        "rust": "rust/dist/compiler/build/bootstrap/SHA256SUMS.json",
    }
    if name in inventories:
        inventory = root / inventories[name]
        verify_checksum_list(inventory.parent, inventory)
        return
    if name == "llvm":
        from llvm.shared import manifest
        manifest(root / "llvm/dist/shared")
        return
    if name == "llvm-rust":
        run([sys.executable, root / "llvm/scripts/rust-shared.py", "--check", "--primary",
             root / "rust/dist/compiler/build/bootstrap/rustc.wasm",
             "--output", root / "llvm/dist/rust-shared"], root=root)
        return
    if name == "javascript":
        base = root / "javascript"
        entries = json.loads((base / "checksums/wasm-tools.json").read_text())["files"]
    elif name == "wasm-tools":
        base = root / "wasm-tools/dist"
        lock = json.loads((root / "wasm-tools/toolchain.lock.json").read_text())
        entries = {"wasm-tools.wasm": lock["module_sha256"]}
        adapter_lock = root / "wasm-tools/adapter.lock.json"
        if adapter_lock.is_file():
            adapter = json.loads(adapter_lock.read_text())
            entries.update({adapter[key]["file"]: adapter[key]["sha256"] for key in ("module", "license")})
    elif name in {"zig", "assemblyscript", "nim", "python", "grain", "ocaml", "goose"}:
        base = root / name / "dist"
        manifest = json.loads((base / "manifest.json").read_text())
        key = {"zig": "files", "assemblyscript": "artifacts", "nim": "sha256", "python": "sha256", "grain": "sha256", "ocaml": "sha256", "goose": "sha256"}[name]
        entries = manifest[key]
    else:
        return
    for relative, specification in entries.items():
        path = base / relative_path(relative)
        expected = specification["sha256"] if isinstance(specification, dict) else specification
        if path.is_symlink() or not path.is_file() or digest(path) != expected:
            raise ValueError(f"compiler package checksum mismatch: {path}")


def execute(document: dict, selected: list[str], command: str, *, root: Path, settings: dict,
            dependencies=True) -> None:
    order = dependency_order(document, selected) if command == "build" and dependencies else selected
    for name in order:
        item = document["tools"][name]
        print(f"{command}: {name}", file=sys.stderr, flush=True)
        source_inputs = {path: digest(root / path) for path in item.get("sourceInputs", [])}
        document_path = root / "buildtools.json"
        recipe_sha = digest(document_path) if document_path.is_file() else None
        validation = {}
        for step in expand(item[command], settings):
            if "action" in step:
                validation.update(action(step["action"], root=root, settings=settings) or {})
            else:
                run(step["argv"], root=root, environment=step.get("environment"))
        check_artifacts(item, root)
        if source_inputs != {path: digest(root / path) for path in source_inputs}:
            raise ValueError(f"{name} source inputs changed while {command} ran; rerun before recording a successful receipt")
        if recipe_sha is not None and digest(document_path) != recipe_sha:
            raise ValueError("compiler build recipe changed during execution; rerun before recording a successful receipt")
        receipt = root / "build/receipts" / name / f"{command}.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        record = {"schema": 1, "tool": name, "operation": command,
                  "passed": True, "artifacts": {path: digest(root / path) for path in item["artifacts"]},
                  "sourceInputs": source_inputs,
                  "source": item.get("source"), "validation": validation}
        if recipe_sha is not None:
            record["recipeSha256"] = recipe_sha
        receipt.write_text(json.dumps(record, indent=2) + "\n")


def package_files(document: dict, selected: str, root: Path) -> dict[str, Path]:
    files = {}
    # Validate the runtime graph, but publish each provider's bytes only once.
    dependency_order(document, [selected], key="runtimeDependencies")
    name = selected
    item = document["tools"][name]
    check_artifacts(item, root)
    verify_package_inputs(name, root)
    specification = {**item["package"], **packages.profile(root, name)}
    includes = specification["include"] + [value for value in specification.get("optional", [])
                                            if (root / value).exists()]
    for relative in includes:
        source = root / relative_path(relative)
        if source.is_symlink() or not source.exists():
            raise ValueError(f"package input is missing or a symlink: {relative}")
        candidates = sorted(source.rglob("*")) if source.is_dir() else [source]
        for path in candidates:
            relative_file = path.relative_to(root)
            if (EXCLUDED.intersection(relative_file.parts) or path.suffix in EXCLUDED_SUFFIXES
                    or any(fnmatch.fnmatchcase(relative_file.as_posix(), pattern) for pattern in specification.get("exclude", []))):
                continue
            if path.is_symlink():
                raise ValueError(f"package input contains a symlink: {relative_file}")
            if path.is_file():
                if not path.resolve().is_relative_to(root.resolve()):
                    raise ValueError(f"package input escapes repository: {relative_file}")
                files[relative_file.as_posix()] = path
    return dict(sorted(files.items()))


def package_tool(document: dict, name: str, *, root: Path, output_dir: Path) -> dict:
    files = package_files(document, name, root)
    manifest = adapter_repackage(name, root)
    # Bootstrap tool locations are local diagnostics, not runtime paths. Keep
    # their identities and hashes without publishing a developer's home path.
    public_metadata = {}
    if name == "python":
        for relative in ("python/dist/manifest.json", "python/dist/strings/manifest.json"):
            if relative not in files:
                continue
            metadata = json.loads(files[relative].read_text())
            for key in ("native_bootstrap_tools", "bootstrapOnlyNativeTools"):
                for tool in metadata.get(key, {}).values():
                    if "path" in tool:
                        tool["path"] = Path(tool["path"]).name
            public_metadata[relative] = metadata
    if manifest is not None:
        public_metadata[f"{name}/dist/manifest.json"] = manifest
    if not public_metadata:
        return packages.pack(document, name, files, root=root, output_dir=output_dir)
    with tempfile.TemporaryDirectory(prefix="wisp-adapter-repackage-") as temporary:
        for relative, metadata in public_metadata.items():
            staged = Path(temporary) / relative
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(packages.canonical(metadata))
            files[relative] = staged
        return packages.pack(document, name, files, root=root, output_dir=output_dir)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="list compiler packages")
    listing.add_argument("--json", action="store_true")
    for name in ("plan", "build", "test", "package", "sizes"):
        command = commands.add_parser(name)
        command.add_argument("tool", help="tool name or all")
        command.add_argument("--jobs", type=int, choices=range(1, 9), default=2)
        if name == "plan":
            command.add_argument("--json", action="store_true")
        if name == "build":
            command.add_argument("--no-deps", action="store_true", help="reuse compiler dependencies already built separately")
        if name == "package":
            command.add_argument("--output-dir", type=Path, default=ROOT / "dist")
        if name == "sizes":
            command.add_argument("--package-dir", type=Path, default=ROOT / "dist")
            command.add_argument("--output-dir", type=Path)
            command.add_argument("--json", action="store_true")
    validation = commands.add_parser("validate", help="verify a Wisp toolchain bundle without executing it")
    validation.add_argument("bundle", type=Path)
    installation = commands.add_parser("install", help="verify and install a bundle into a new prefix")
    installation.add_argument("bundle", type=Path, nargs="+")
    installation.add_argument("--prefix", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command in {"validate", "install"}:
        result = packages.validate(args.bundle) if args.command == "validate" else packages.install(args.bundle, args.prefix)
        print(json.dumps(result, indent=2))
        return 0
    document = load_manifest()
    if args.command == "list":
        if args.json:
            print(json.dumps({"schema": 1, "tools": [{"name": name, **item}
                                                      for name, item in document["tools"].items()]}, indent=2))
        else:
            for name, item in document["tools"].items():
                print(f"{name:15} {item['description']}")
        return 0
    selected = list(document["tools"]) if args.tool == "all" else [args.tool]
    dependency_order(document, selected)
    settings = configuration(ROOT, args.jobs)
    if args.command == "plan":
        plan = build_plan(document, selected, settings)
        if args.json:
            print(json.dumps(plan, indent=2))
        else:
            print("Build order: " + " -> ".join(plan["order"]))
            for item in plan["tools"]:
                print(f"\n{item['name']}: {item['description']}")
                print("  bootstrap: " + "; ".join(item["bootstrapRequirements"]))
                for step in item["build"]:
                    print("  " + (shlex.join(step["argv"]) if "argv" in step else step["description"]))
        return 0
    if args.command == "package":
        providers = dependency_order(document, selected, key="runtimeDependencies")
        results = [package_tool(document, name, root=ROOT, output_dir=args.output_dir.resolve()) for name in providers]
        data = packages.report([Path(record["archive"]) for record in results])
        packages.write_reports(data, args.output_dir.resolve())
        for record in results:
            print(json.dumps(record), flush=True)
    elif args.command == "sizes":
        providers = dependency_order(document, selected, key="runtimeDependencies")
        data = packages.report([args.package_dir / f"{name}.wisp.zip" for name in providers])
        packages.write_reports(data, args.output_dir or args.package_dir)
        print(json.dumps(data, indent=2) if args.json else (args.output_dir or args.package_dir).joinpath("sizes.md").read_text())
    else:
        execute(document, selected, args.command, root=ROOT, settings=settings,
                dependencies=not getattr(args, "no_deps", False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, tarfile.TarError, packages.zipfile.BadZipFile, packages.zlib.error) as error:
        print(f"build: {error}", file=sys.stderr)
        raise SystemExit(1)
