#!/usr/bin/env python3
"""Build the pinned LLVM Rust port, keeping it separate from the working bootstrap.

--plan writes the exact configuration and commands. --dry-run additionally
compiles/runs Rust's native bootstrap driver and asks it to plan the build.
Only --build starts the LLVM/compiler/sysroot build.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import tarfile
import tomllib
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
TOOL_INPUT_LIMIT = 256 * 1024 * 1024
CORE_WASM = b"\0asm\x01\0\0\0"
COMPONENT_WASM = b"\0asm\x0d\0\x01\0"
# Keep output within the Wasmtime 50 host's configured feature set.
# Compact imports are disabled in that profile; do not enable them via Binaryen.
OPTIMIZER_FEATURES = (
    "--mvp-features", "--enable-threads", "--enable-mutable-globals",
    "--enable-sign-ext", "--enable-nontrapping-float-to-int", "--enable-simd",
    "--enable-bulk-memory", "--enable-exception-handling", "--enable-tail-call",
    "--enable-reference-types", "--enable-multivalue", "--enable-extended-const",
    "--enable-multimemory",
)
MACRO_PACKAGES = {
    "wasmi", "wasmi_collections", "wasmi_core", "wasmi_ir", "wasmparser",
    "downcast-rs", "foldhash", "hashbrown", "multi-stash", "spin", "string-interner", "getrandom",
}


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def executable(value: str) -> Path:
    found = shutil.which(value)
    if not found:
        raise ValueError(f"missing executable: {value}")
    return Path(found).resolve()


def provision_bootstrap(lock: dict, *, offline: bool) -> Path:
    """Install the matching official release without executing installer scripts."""
    selected = lock["bootstrap"]
    prefix = ROOT / "cache/native-bootstrap" / selected["channel"]
    inventory = prefix / "SHA256SUMS.json"
    if inventory.is_file():
        records = json.loads(inventory.read_text())
        if not records or any(not (prefix / name).is_file() or digest(prefix / name) != expected
                              for name, expected in records.items()):
            raise ValueError("native bootstrap checksum mismatch")
        receipt = json.loads((prefix / "receipt.json").read_text())
        if receipt != selected:
            raise ValueError("native bootstrap receipt differs from the source lock")
        return prefix
    cache = ROOT / "cache/bootstrap" / selected["date"]
    cache.mkdir(parents=True, exist_ok=True)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".native-bootstrap-", dir=prefix.parent) as temporary:
        stage = Path(temporary)
        for item in selected["artifacts"]:
            archive = cache / item["url"].rsplit("/", 1)[1]
            if not archive.is_file():
                if offline:
                    raise ValueError(f"missing offline native bootstrap archive: {archive}")
                partial = archive.with_suffix(".partial")
                try:
                    urllib.request.urlretrieve(item["url"], partial)
                    if digest(partial) != item["sha256"]:
                        raise ValueError("native bootstrap download checksum mismatch")
                    partial.replace(archive)
                finally:
                    partial.unlink(missing_ok=True)
            if digest(archive) != item["sha256"]:
                raise ValueError("native bootstrap archive checksum mismatch")
            unpack = stage / "unpack"
            unpack.mkdir(exist_ok=True)
            with tarfile.open(archive) as stream:
                stream.extractall(unpack, filter="data")
            distribution = next(unpack.iterdir())
            component = next(path for path in distribution.iterdir()
                             if path.is_dir() and (path / "manifest.in").is_file())
            for directory in ("bin", "lib", "libexec", "share", "etc"):
                if (component / directory).is_dir():
                    shutil.copytree(component / directory, stage / directory, dirs_exist_ok=True)
            shutil.rmtree(unpack)
        for program in ("rustc", "cargo"):
            if not (stage / "bin" / program).is_file():
                raise ValueError(f"native bootstrap is missing {program}")
        write_json(stage / "receipt.json", selected)
        write_json(stage / "SHA256SUMS.json", {
            str(path.relative_to(stage)): digest(path)
            for path in sorted(stage.rglob("*")) if path.is_file()
        })
        if prefix.exists():
            raise ValueError("incomplete native bootstrap exists; select a fresh bootstrap cache")
        stage.rename(prefix)
    return prefix


def validate_llvm_sdk(directory: Path, selected: dict) -> dict:
    """Verify compiled LLVM inputs before selecting the external Wasm SDK."""
    metadata = json.loads((directory / selected["metadata"]).read_text())
    if (metadata.get("llvm_revision") != selected["revision"]
            or metadata.get("host_target") != selected["host_target"]
            or metadata.get("version") != selected["version"]):
        raise ValueError("external LLVM SDK revision, version or host ABI differs from the lock")
    inventory = directory / selected["inventory"]
    count = 0
    for line in inventory.read_text().splitlines():
        expected, relative = line.split(None, 1)
        path = directory / relative.lstrip(" *")
        if (not path.resolve().is_relative_to(directory.resolve()) or path.is_symlink()
                or not path.is_file() or digest(path) != expected):
            raise ValueError(f"external LLVM SDK checksum mismatch: {relative}")
        count += 1
    if not count:
        raise ValueError("external LLVM SDK checksum inventory is empty")
    return {"directory": str(directory), "revision": selected["revision"],
            "metadataSha256": digest(directory / selected["metadata"]),
            "inventorySha256": digest(inventory), "verifiedFiles": count}


def check_patch(source: Path, patch: Path) -> None:
    # --check changes nothing. Run outside a Git checkout so source paths are
    # resolved under --directory rather than the surrounding Rust toolchain directory.
    with tempfile.TemporaryDirectory(prefix="wisp-rust-patch-check-") as directory:
        checked = subprocess.run([str(executable("git")), "apply", "--check", "--reverse", "--unsafe-paths",
                                  "--directory", str(source), str(patch)], cwd=directory, text=True, capture_output=True)
    if checked.returncode:
        raise ValueError("selected macro backport is not applied exactly:\n" + checked.stderr)


def check_install(prefix: Path, targets: list[str]) -> Path:
    if not targets or targets[0] != "wasm32-wasip3":
        raise ValueError("Rust compiler host must be wasm32-wasip3")
    compiler = prefix / "bin/rustc.wasm"
    if compiler.stat().st_size > TOOL_INPUT_LIMIT:
        raise ValueError("source compiler exceeds Wisp's 256 MiB tool input limit")
    with compiler.open("rb") as stream:
        expected = COMPONENT_WASM
        if stream.read(8) != expected:
            raise ValueError("installed compiler has the wrong Wasm module/component format")
    for target in targets:
        libraries = prefix / "lib/rustlib" / target / "lib"
        if not list(libraries.glob("libstd-*.rlib")) or not list(libraries.glob("libproc_macro-*.rlib")):
            raise ValueError(f"installed target is missing std or proc_macro: {target}")
    return compiler


def stage_build_outputs(out: Path, lock: dict) -> None:
    """Stage the Wasm compiler and libraries built by native stage1.

    `x.py build --stage 1 compiler/rustc library` assembles a stage2 compiler
    for the Wasm host. Its libraries are built with the same native stage1
    compiler. Using `install --stage 2` would eagerly build native stage2 too.
    """
    objects = out / "obj"
    compiler_stage = lock.get("compiler_stage", 2)
    compiler = objects / lock["compiler_host_target"] / f"stage{compiler_stage}/bin/rustc.wasm"
    if not compiler.is_file() and lock.get("bootstrap_stage") == 0:
        # Building this cross compiler does not require assembling a runnable
        # stage1 sysroot: the matching native stage0 builds every library.
        compiler = (objects / lock["bootstrap"]["host"] / f"stage{compiler_stage}-rustc"
                    / lock["compiler_host_target"] / "release/rustc-main.wasm")
    with compiler.open("rb") as stream:
        expected = COMPONENT_WASM
        if stream.read(8) != expected:
            raise ValueError("built compiler is not a Wasm Component")
    bootstrap_stage = lock.get("bootstrap_stage", 1)
    stage_name = "stage0-sysroot" if bootstrap_stage == 0 else f"stage{bootstrap_stage}"
    sysroot = objects / lock["bootstrap"]["host"] / stage_name / "lib/rustlib"
    targets = [lock["compiler_host_target"], *lock["application_targets"]]
    for target in targets:
        libraries = sysroot / target / "lib"
        if not list(libraries.glob("libstd-*.rlib")) or not list(libraries.glob("libproc_macro-*.rlib")):
            raise ValueError(f"built stage1 target is missing std or proc_macro: {target}")
    with tempfile.TemporaryDirectory(prefix=".compiler-install-", dir=out) as temporary:
        stage = Path(temporary) / "install"
        (stage / "bin").mkdir(parents=True)
        shutil.copyfile(compiler, stage / "bin/rustc.wasm")
        for target in targets:
            shutil.copytree(sysroot / target / "lib", stage / "lib/rustlib" / target / "lib")
        prefix = out / "install"
        if prefix.exists():
            shutil.rmtree(prefix)
        stage.rename(prefix)


def optimizer_environment(environment: dict, profile: dict) -> dict:
    result = dict(environment, BINARYEN_CORES="1")
    if profile["libraryDirectory"]:
        result["LD_LIBRARY_PATH"] = profile["libraryDirectory"] + (os.pathsep + result["LD_LIBRARY_PATH"] if result.get("LD_LIBRARY_PATH") else "")
    return result


def optimize_large_compiler(compiler: Path, out: Path, profile: dict | None, environment: dict,
                            *, force: bool = False, flags: tuple[str, ...] = ("-Oz",)) -> dict:
    raw_hash = digest(compiler)
    if not force and compiler.stat().st_size <= TOOL_INPUT_LIMIT:
        return {"applied": False, "reason": "compiler-fits-wisp-input-limit", "inputSha256": raw_hash}
    if profile is None:
        raise ValueError("raw compiler exceeds Wisp's 256 MiB limit; select the verified --wasm-opt tool to optimize it")
    if digest(Path(profile["path"])) != profile["sha256"]:
        raise ValueError("wasm-opt changed during the compiler build")
    for path, expected in profile["libraries"].items():
        if digest(Path(path)) != expected:
            raise ValueError("wasm-opt shared library changed during the compiler build")
    optimized = out / "compiler-optimized.wasm"
    argv = [profile["path"], str(compiler), *flags, *OPTIMIZER_FEATURES,
            "--strip-debug", "--strip-producers", "-o", str(optimized)]
    memory_limit = profile["memoryLimitMiB"] * 1024 * 1024
    def limit_memory():
        resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
    with (out / "compiler-optimization.log").open("a") as log:
        print("Running: " + " ".join(argv), file=log, flush=True)
        process = subprocess.run(argv, env=optimizer_environment(environment, profile),
                                 stdout=log, stderr=subprocess.STDOUT,
                                 timeout=profile["timeoutSeconds"], preexec_fn=limit_memory)
    if process.returncode:
        raise ValueError(f"wasm-opt exited {process.returncode}; raw compiler retained, see compiler-optimization.log")
    with optimized.open("rb") as stream:
        if stream.read(8) != b"\0asm\x01\0\0\0":
            raise ValueError("wasm-opt did not emit a Core Wasm compiler")
    raw_directory = out / "raw"
    raw_directory.mkdir(exist_ok=True)
    raw_copy = raw_directory / ("rustc-" + raw_hash + ".wasm")
    if raw_copy.exists() and digest(raw_copy) != raw_hash:
        raise ValueError("preserved raw compiler checksum mismatch")
    if not raw_copy.exists():
        shutil.copy2(compiler, raw_copy)
    optimized.replace(compiler)
    return {"applied": True, "argv": argv, "exitCode": process.returncode,
            "inputSha256": raw_hash, "inputBytes": raw_copy.stat().st_size,
            "outputSha256": digest(compiler), "outputBytes": compiler.stat().st_size,
            "rawCompiler": str(raw_copy), "tool": profile}


def optimize_compiler_component(compiler: Path, out: Path, profile: dict | None,
                               environment: dict) -> dict:
    """Optimize the compiler's Core module while preserving its Component ABI."""
    if profile is None:
        raise ValueError("WASIp3 compiler optimization requires --wasm-opt")
    original = compiler.read_bytes()
    if original[:8] != COMPONENT_WASM:
        raise ValueError("compiler optimization requires a Component")

    def read_size(offset: int) -> tuple[int, int]:
        value = 0
        for shift in range(0, 35, 7):
            if offset >= len(original):
                break
            byte = original[offset]
            offset += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value, offset
        raise ValueError("malformed Component section length")

    def encode_size(value: int) -> bytes:
        result = bytearray()
        while value >= 128:
            result.append((value & 127) | 128)
            value >>= 7
        result.append(value)
        return bytes(result)

    modules = []
    offset = 8
    while offset < len(original):
        kind = original[offset]
        size, start = read_size(offset + 1)
        end = start + size
        if end > len(original):
            raise ValueError("truncated Component section")
        if kind == 1:
            modules.append((offset, start, end))
        offset = end
    if not modules:
        raise ValueError("compiler Component contains no Core module")
    section, start, end = max(modules, key=lambda item: item[2] - item[1])
    core = out / "component-compiler.core.wasm"
    core.write_bytes(original[start:end])
    if core.read_bytes()[:8] != CORE_WASM:
        raise ValueError("compiler Component's main module is not Core Wasm")
    passes = [optimize_large_compiler(core, out, profile, environment, force=True,
              flags=("--duplicate-function-elimination", "--remove-unused-module-elements", "--vacuum"))]
    passes.append(optimize_large_compiler(core, out, profile, environment, force=True, flags=("-O1",)))
    content = core.read_bytes()
    rebuilt = original[:section] + b"\x01" + encode_size(len(content)) + content + original[end:]
    raw = out / "raw" / ("rustc-component-" + hashlib.sha256(original).hexdigest() + ".wasm")
    raw.parent.mkdir(exist_ok=True)
    if raw.exists() and raw.read_bytes() != original:
        raise ValueError("preserved raw Component checksum mismatch")
    if not raw.exists():
        raw.write_bytes(original)
    compiler.write_bytes(rebuilt)
    core.unlink()
    return {"applied": True, "inputSha256": hashlib.sha256(original).hexdigest(),
            "inputBytes": len(original), "outputSha256": digest(compiler),
            "outputBytes": len(rebuilt), "rawCompiler": str(raw), "corePasses": passes}


def check_macro_pins(source: Path, patch: Path, lock: dict) -> None:
    selected = lock.get("macro_backport", {})
    if not selected or digest(patch) != selected.get("sha256"):
        raise ValueError("selected macro patch checksum differs from source-lock.json")
    if digest(source / "Cargo.lock") != selected.get("patched_cargo_lock_sha256"):
        raise ValueError("patched Cargo.lock checksum differs from source-lock.json")
    if selected.get("macro_target") != "wasm32-unknown-unknown":
        raise ValueError("unsupported procedural macro target in source-lock.json")


def patch_source_hashes(source: Path, patch: Path) -> dict[str, str]:
    paths = []
    for line in patch.read_text().splitlines():
        if not line.startswith("diff --git "):
            continue
        match = re.fullmatch(r"diff --git a/(\S+) b/(\S+)", line)
        if match is None or match[1] != match[2]:
            raise ValueError("build patches must name ordinary unchanged source paths")
        relative = Path(match[2])
        if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
            raise ValueError("unsafe source path in build patch")
        path = source / relative
        if not path.is_file() or not path.resolve().is_relative_to(source):
            raise ValueError("patched compiler source is missing or escapes the source tree")
        paths.append(str(relative))
    if not paths or len(paths) != len(set(paths)):
        raise ValueError("empty or duplicate source paths in build patch")
    return {name: digest(source / name) for name in sorted(paths)}


def check_build_patches(source: Path, lock: dict) -> list[dict]:
    records = []
    for selected in lock.get("build_patches", []):
        relative = Path(selected["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("unsafe build patch path in source lock")
        patch = ROOT / relative
        if digest(patch) != selected["sha256"]:
            raise ValueError("build patch checksum differs from source-lock.json")
        hashes = patch_source_hashes(source, patch)
        if hashes != selected["source_files"]:
            raise ValueError("patched build source hashes differ from source-lock.json")
        check_patch(source, patch)
        records.append({"path": str(patch), "sha256": digest(patch),
                        "sourceFiles": hashes, "reverseApplyCheckPassed": True})
    return records


def macro_notices(source: Path, offline: bool) -> tuple[bytes, list[dict]]:
    """Retain licenses from checksum-verified crates, with pinned VCS fallbacks."""
    packages = tomllib.loads((source / "Cargo.lock").read_text())["package"]
    records = []
    sections = []
    seen = set()
    cache = ROOT / "cache/compiler-licenses"
    cache.mkdir(parents=True, exist_ok=True)
    for package in packages:
        if package["name"] not in MACRO_PACKAGES:
            continue
        if package["name"] == "getrandom" and package["version"] != "0.4.3":
            continue
        name, version, expected = (package[key] for key in ("name", "version", "checksum"))
        # This backport adds wasmparser 0.221.3; older existing wasmparser
        # packages remain covered by Rust's original COPYRIGHT inventory.
        if name == "wasmparser" and version != "0.221.3":
            continue
        filename = f"{name}-{version}.crate"
        archive = cache / filename
        if not archive.is_file():
            for registry in (ROOT / "cache/compiler-cargo/registry/cache", Path.home() / ".cargo/registry/cache"):
                matches = list(registry.glob("*/" + filename))
                found = next((path for path in matches if digest(path) == expected), None)
                if found:
                    shutil.copyfile(found, archive)
                    break
        if not archive.is_file():
            if offline:
                raise ValueError(f"missing offline macro license crate: {filename}")
            urllib.request.urlretrieve(f"https://static.crates.io/crates/{name}/{filename}", archive)
        if digest(archive) != expected:
            raise ValueError(f"macro license crate checksum mismatch: {filename}")
        with tarfile.open(archive, "r:gz") as crate:
            prefix = f"{name}-{version}/"
            texts = {}
            for member in crate:
                relative = member.name.removeprefix(prefix)
                if member.name.startswith(prefix) and "/" not in relative and member.isfile() and relative.lower().startswith(("license", "copying", "notice")):
                    if member.size > 256 * 1024:
                        raise ValueError("unexpectedly large macro license")
                    texts[relative] = crate.extractfile(member).read()
            vcs = json.loads(crate.extractfile(prefix + ".cargo_vcs_info.json").read())
        record = {"name": name, "version": version, "crateSha256": expected, "licenses": []}
        if not texts:
            revision = vcs["git"]["sha1"]
            if not re.fullmatch(r"[0-9a-f]{40}", revision):
                raise ValueError("invalid macro crate VCS revision")
            repository = "wasmi-labs/wasmi" if name.startswith("wasmi") else "bytecodealliance/wasm-tools" if name == "wasmparser" else None
            if repository is None:
                raise ValueError(f"missing packaged license for {filename}")
            url = f"https://raw.githubusercontent.com/{repository}/{revision}/LICENSE-MIT"
            local = cache / (hashlib.sha256(url.encode()).hexdigest() + ".txt")
            if not local.is_file():
                if offline:
                    raise ValueError(f"missing offline VCS license: {url}")
                urllib.request.urlretrieve(url, local)
            texts["LICENSE-MIT"] = local.read_bytes()
            record["vcsLicenseUrl"] = url
        for filename, content in sorted(texts.items()):
            record["licenses"].append({"name": filename, "sha256": hashlib.sha256(content).hexdigest()})
            sections.append(f"\n===== {name} {version}: {filename} =====\n\n".encode() + content)
        records.append(record)
        seen.add(name)
    if seen != MACRO_PACKAGES:
        raise ValueError("macro dependency lock differs from the reviewed backport")
    bundle = b"Additional interpreter dependencies in the Wisp Rust macro backport.\nCrate archives are checked against the patched Cargo.lock.\n" + b"\n".join(sections)
    if len(bundle) > 256 * 1024:
        raise ValueError("macro notice bundle exceeds Wisp's per-notice limit")
    return bundle, records


def snapshot_build_provenance(out: Path, report: dict, lock_path: Path) -> None:
    if report.get("mode") != "build":
        raise ValueError("only a real build may replace compiler provenance snapshots")
    snapshots = {}
    for name, path, field in (("build-driver.py", Path(__file__).resolve(), "buildDriverSha256"),
                              ("source-lock.json", lock_path, "sourceLockSha256")):
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != report.get(field):
            raise ValueError(f"compiler build input changed before snapshot: {name}")
        snapshots[name] = (content, field)
    # Validate both inputs before replacing either earlier build snapshot.
    with tempfile.TemporaryDirectory(prefix=".build-provenance-", dir=out) as temporary:
        stage = Path(temporary)
        for name, (content, field) in snapshots.items():
            (stage / name).write_bytes(content)
            if digest(stage / name) != report[field]:
                raise ValueError(f"compiler build snapshot checksum mismatch: {name}")
        for name in snapshots:
            (stage / name).replace(out / name)


def package_candidate(source: Path, out: Path, destination: Path, offline: bool) -> None:
    report_path = out / "build-report.json"
    report = json.loads(report_path.read_text())
    if (report.get("schemaVersion") != 1 or report.get("mode") != "build"
            or report.get("compilerBuildCompleted") is not True or report.get("status") != "build-passed"):
        raise ValueError("candidate packaging requires a completed source build")
    if (report.get("sourceRoot") != str(source)
            or type(report.get("compilerProbe", {}).get("exitCode")) is not int
            or report["compilerProbe"]["exitCode"] != 0):
        raise ValueError("candidate packaging requires a successful installed-compiler probe")
    provenance = {}
    for name, field in (("build-driver.py", "buildDriverSha256"), ("source-lock.json", "sourceLockSha256")):
        path = out / name
        if not path.is_file() or digest(path) != report.get(field):
            raise ValueError(f"original compiler build provenance missing or changed: {name}")
        provenance[name] = {"sha256": digest(path)}
    if digest(out / "config.toml") != report["configSha256"]:
        raise ValueError("compiler configuration changed since the successful build")
    compiler = check_install(out / "install", [report["compilerHost"], *report["applicationTargets"]])
    if digest(compiler) != report["compiler"]["sha256"]:
        raise ValueError("installed compiler changed since the successful build")
    if digest(source / "Cargo.lock") != report["inputs"]["sourceInputs"]["Cargo.lock"]:
        raise ValueError("compiler dependency lock changed since the successful build")
    if digest(ROOT / "scripts/runner.py") != report["runnerSha256"]:
        raise ValueError("compiler runner changed since the successful build; verify the new runner before packaging")
    if destination.exists():
        raise ValueError("candidate output already exists; select a fresh --candidate-out")
    manifest = json.loads((ROOT / "toolchain.json").read_text())
    if manifest["rustRevision"] != report["inputs"]["sourceRevisions"]["rust"] or manifest["llvmRevision"] != report["inputs"]["sourceRevisions"]["llvm"]:
        raise ValueError("working bootstrap source revisions differ from the source build")
    for name, expected in manifest["licenses"].items():
        if digest(ROOT / "licenses" / name) != expected:
            raise ValueError(f"working Rust license notice changed: {name}")
    notices, notice_records = macro_notices(source, offline)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".rust-candidate-", dir=destination.parent) as temporary:
        stage = Path(temporary) / "candidate"
        prefix = stage / "build/bootstrap"
        prefix.mkdir(parents=True)
        shutil.copyfile(compiler, prefix / "rustc.wasm")
        # Keep exactly the three library targets used by the compiler runner.
        # Native bootstrap libraries and native helper executables stay outside
        # the candidate application's sysroot.
        for target in [report["compilerHost"], *report["applicationTargets"]]:
            shutil.copytree(out / "install/lib/rustlib" / target / "lib", prefix / "sysroot/lib/rustlib" / target / "lib")
        (stage / "scripts").mkdir()
        shutil.copyfile(ROOT / "scripts/runner.py", stage / "scripts/runner.py")
        shutil.copytree(ROOT / "licenses", stage / "licenses")
        (stage / "licenses/WASMI-THIRD-PARTY.txt").write_bytes(notices)
        manifest.update(status="candidate-unverified", release="source-macro-candidate",
                        releaseRepository=manifest["rustRepository"], releaseRevision=manifest["rustRevision"],
                        proceduralMacroTarget="wasm32-unknown-unknown",
                        capabilities={"std": True, "vendoredDependencies": True, "basicBuildScripts": True,
                                      "proceduralMacros": True},
                        verification={"compilerBuildCompleted": True, "deriveAcceptancePassed": False,
                                      "serdeAcceptancePassed": False, "firstUsableRelease": False})
        manifest["compiler"] = {"module": "rustc.wasm", "moduleSha256": digest(compiler),
                                "sourceBuildReportSha256": digest(report_path)}
        manifest["sysroot"] = {"targets": [manifest["compilerHost"], manifest["applicationTarget"], manifest["proceduralMacroTarget"]]}
        manifest["runtime"] = {"provider": "wisp", "version": manifest["runtime"]["version"], "interface": "wasi:cli/run", "environment": "WISP_RUST_WASMTIME"}
        manifest["licenses"] = {path.name: digest(path) for path in sorted((stage / "licenses").iterdir()) if path.is_file()}
        write_json(stage / "toolchain.json", manifest)
        files = {str(path.relative_to(prefix)): digest(path) for path in sorted(prefix.rglob("*")) if path.is_file()}
        if len(files) > 2048 or any(path.stat().st_size > 256 * 1024 * 1024 for path in prefix.rglob("*") if path.is_file()):
            raise ValueError("candidate exceeds Wisp's toolchain file count or per-file size limit")
        write_json(prefix / "SHA256SUMS.json", files)
        # Ship the exact bytes referenced by compiler.sourceBuildReportSha256.
        # The original driver identifies the compiler build; this driver may
        # contain later packaging-only fixes and has a separate provenance hash.
        shutil.copyfile(report_path, stage / "build-report.json")
        for name in provenance:
            shutil.copyfile(out / name, stage / name)
        write_json(stage / "source-build.json", {"buildReport": report, "buildReportSha256": digest(report_path),
                                               "buildProvenance": provenance,
                                               "packagingDriverSha256": digest(Path(__file__).resolve()),
                                               "runnerSha256": digest(stage / "scripts/runner.py"),
                                               "macroNotices": notice_records})
        # Splitting consumes this receipt on a clean CI build too. Acceptance
        # fills in execution evidence only after the candidate has been tested.
        write_json(stage / "runtime-adapter.json", {
            "schemaVersion": 1, "status": "pending",
            "scope": "Source-built compiler; application acceptance has not run.",
            "compilerSha256": digest(compiler),
            "runnerSha256": digest(stage / "scripts/runner.py"),
            "standaloneCases": [], "receiptFiles": {},
        })
        (stage / "verification").mkdir()
        (stage / "README.txt").write_text("Source-built macro compiler candidate.\nRun derive and Serde acceptance before treating this as a usable release.\n")
        stage.rename(destination)


def path_remaps(source: Path, out: Path) -> list[str]:
    # Target flags also apply to stage-0 stdlib builds. Bootstrap's debug-info
    # remapping alone does not cover every metadata artifact we redistribute.
    return [f"--remap-path-prefix={path.resolve()}={public}" for path, public in (
        (Path.home(), '/build-user'), (ROOT.parent, '/wisp-buildtools'),
        (source, '/rustc/source'), (out, '/wisp-buildtools/rust/build'))]


def configuration(source: Path, out: Path, sdk: Path, native: dict, jobs: int, lock: dict) -> str:
    remaps = path_remaps(source, out)
    if lock.get("external_llvm"):
        quote = lambda value: json.dumps(str(value))
        targets = [lock["compiler_host_target"], *lock["application_targets"]]
        text = f'''# Rust {lock["rust_version"]}; reuse the existing LLVM 24/WASIp3 SDK.
profile = "compiler"
change-id = 9999999
[build]
description = {quote(lock["bootstrap"]["compiler_description"])}
build = {quote(lock["bootstrap"]["host"])}
host = [{quote(lock["compiler_host_target"])}]
target = {json.dumps(targets)}
build-dir = {quote(out / "obj")}
rustc = {quote(native["rustc"])}
cargo = {quote(native["cargo"])}
local-rebuild = true
optimized-compiler-builtins = false
submodules = false
locked-deps = true
docs = false
compiler-docs = false
extended = false
tools = []
full-bootstrap = false
[rust]
channel = "stable"
codegen-backends = ["llvm"]
download-rustc = false
deny-warnings = false
debug = false
debug-assertions = false
debug-logging = false
debuginfo-level = 0
remap-debuginfo = true
incremental = false
lto = "off"
omit-git-hash = true
lld = false
llvm-tools = false
llvm-bitcode-linker = false
[llvm]
download-ci-llvm = false
static-libstdcpp = true
link-shared = false
assertions = false
tests = false
plugins = false
ninja = true
[target.{lock["compiler_host_target"]}]
llvm-config = {quote(native["llvm-config"])}
'''
        for target in dict.fromkeys([lock["compiler_host_target"], *lock["application_targets"]]):
            if target != lock["compiler_host_target"]:
                text += f"\n[target.{target}]\n"
            if not target.startswith("wasm32-wasi"):
                text += 'rustflags = ' + json.dumps(remaps) + '\n'
                continue
            text += f'''wasi-root = {quote(sdk / "share/wasi-sysroot")}
cc = {quote(sdk / "bin" / (target + "-clang"))}
cxx = {quote(sdk / "bin" / (target + "-clang++"))}
ar = {quote(sdk / "bin/llvm-ar")}
ranlib = {quote(sdk / "bin/llvm-ranlib")}
linker = {quote(sdk / "bin" / (target + "-clang"))}
rustflags = {json.dumps(["-Clink-self-contained=no", "-Clink-arg=-Wl,-z,stack-size=16777216", *remaps])}
'''
        # Bootstrap still consults p2 when preparing the p3 sysroot.
        text += f'''
[target.wasm32-wasip2]
wasi-root = {quote(sdk / "share/wasi-sysroot")}
[target.{lock["bootstrap"]["host"]}]
cc = {quote(native["cc"])}
cxx = {quote(native["cxx"])}
'''
        tomllib.loads(text)
        return text
    quote = lambda value: json.dumps(str(value))
    targets = [lock["compiler_host_target"], *lock["application_targets"]]
    llvm_cache = f'ccache = {quote(native["ccache"])}\n' if "ccache" in native else ""
    text = f'''# Generated by scripts/build-compiler.py. No external LLVM SDK is selected.
profile = "compiler"
change-id = 9999999

[build]
build = {quote(lock["bootstrap"]["host"])}
host = [{quote(lock["compiler_host_target"])}]
target = {json.dumps(targets)}
build-dir = {quote(out / "obj")}
bootstrap-cache-path = {quote(ROOT / "cache/bootstrap")}
docs = false
compiler-docs = false
extended = false
tools = []
submodules = false
locked-deps = true
full-bootstrap = false
cargo-native-static = true

[rust]
channel = "stable"
codegen-backends = ["llvm"]
deny-warnings = false
download-rustc = false
llvm-bitcode-linker = false
lld = false
llvm-tools = false
debug = false
debug-assertions = false
debug-logging = false
debuginfo-level = 0
remap-debuginfo = true
incremental = false
lto = "off"
omit-git-hash = true

[llvm]
static-libstdcpp = true
ninja = true
download-ci-llvm = false
link-shared = false
link-jobs = 1
targets = "WebAssembly;X86"
experimental-targets = ""
assertions = false
tests = false
plugins = false
libzstd = false
build-config = {{ CMAKE_MAKE_PROGRAM = {quote(native["ninja"])} }}
{llvm_cache}
[install]
prefix = {quote(out / "install")}
sysconfdir = "etc"

[target.{lock["bootstrap"]["host"]}]
cc = {quote(native["cc"])}
cxx = {quote(native["cxx"])}
'''
    for target in ("wasm32-wasip1", "wasm32-wasip1-threads"):
        text += f'''
[target.{target}]
wasi-root = {quote(sdk / "share/wasi-sysroot")}
cc = {quote(sdk / "bin" / (target + "-clang"))}
cxx = {quote(sdk / "bin" / (target + "-clang++"))}
ar = {quote(sdk / "bin/llvm-ar")}
ranlib = {quote(sdk / "bin/llvm-ranlib")}
linker = {quote(sdk / "bin" / (target + "-clang"))}
codegen-backends = ["llvm"]
rustflags = {json.dumps(["-Clink-self-contained=no", "-Clink-arg=-lwasi-emulated-mman", "-Clink-arg=-Wl,-z,stack-size=16777216", "-Clink-arg=-Wl,--stack-first", *remaps])}
'''
    # Parse our generated configuration before executing any upstream code.
    tomllib.loads(text)
    return text


def validate_inputs(source: Path, sdk: Path, lock: dict) -> dict:
    receipt = json.loads((source / "wisp-source-receipt.json").read_text())
    assembled = json.loads((source / "wisp-source-lock.json").read_text())
    expected = {item["name"]: item["revision"] for item in lock["sources"]}
    if receipt.get("sourceRevisions") != expected or not receipt.get("archiveChecksumsVerified"):
        raise ValueError("source preparation receipt does not select the locked revisions")
    for item in lock["sources"]:
        previous = next((old for old in assembled["sources"] if old["name"] == item["name"]), {})
        if any(previous.get(key) != item[key] for key in ("revision", "sha256", "destination")):
            raise ValueError("prepared source pins differ from source-lock.json")
    stage0 = (source / "src/stage0").read_text()
    for item in lock.get("source_bootstrap", lock["bootstrap"])["artifacts"]:
        key = "dist/" + item["url"].split("/dist/", 1)[1]
        if f"{key}={item['sha256']}\n" not in stage0:
            raise ValueError("bootstrap archive is not pinned by the prepared src/stage0")
    if (source / "src/version").read_text().strip() != lock["rust_version"].removesuffix("-dev"):
        raise ValueError("source Rust version differs from the lock")
    archive = ROOT / "downloads" / lock["wasi_sdk"]["url"].rsplit("/", 1)[1]
    if archive.stat().st_size != lock["wasi_sdk"]["archive_bytes"] or digest(archive) != lock["wasi_sdk"]["sha256"]:
        raise ValueError("WASI SDK archive checksum/size mismatch")
    for name in ("clang", "clang++", "wasm32-wasip1-threads-clang", "wasm32-wasip1-threads-clang++",
                 "wasm32-wasip1-clang", "wasm32-wasip1-clang++", "llvm-ar", "llvm-ranlib", "ar", "ranlib", "wasm-ld"):
        if not (sdk / "bin" / name).is_file():
            raise ValueError(f"WASI SDK is missing the tool required by the pinned fork: {name}")
    # These files determine bootstrap/CMake configuration and compiler linkage.
    inputs = ["Cargo.lock", "src/stage0", "src/version", "x.py", "src/bootstrap/bootstrap.py",
              "src/bootstrap/src/core/build_steps/llvm.rs", "src/bootstrap/src/core/build_steps/install.rs",
              "src/bootstrap/src/core/build_steps/compile.rs", "src/bootstrap/src/core/builder/mod.rs",
              "compiler/rustc_llvm/build.rs", "library/sysroot/Cargo.toml",
              "src/version"]
    return {"sourceRevisions": expected, "sourcePreparationReceiptSha256": digest(source / "wisp-source-receipt.json"),
            "sourceInputs": {name: digest(source / name) for name in inputs},
            "wasiSdkArchiveSha256": digest(archive),
            "verification": "locked source-preparation receipt plus current configuration input hashes; source tree may contain the selected backport"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="write configuration/commands without running upstream code; default")
    mode.add_argument("--dry-run", action="store_true", help="compile/run native bootstrap, then x.py build --dry-run")
    mode.add_argument("--build", action="store_true", help="perform the real compiler and sysroot build")
    mode.add_argument("--package", action="store_true", help="package an already successful source build as an unverified candidate")
    parser.add_argument("--source", type=Path, help="defaults to the source revision in source-lock.json")
    parser.add_argument("--out", type=Path, default=ROOT / "build/compiler-source-build-1.99.1")
    parser.add_argument("--wasi-sdk", type=Path, default=ROOT / "downloads/wasi-sdk-34.0-x86_64-linux")
    parser.add_argument("--llvm-sdk", type=Path, help="verified LLVM 24/WASIp3 SDK; defaults to the source lock")
    parser.add_argument("--bootstrap-prefix", type=Path, help="use a matching native Rust release already installed at this prefix")
    parser.add_argument("--macro-patch", type=Path, help="adapted patch already applied to --source; checked in reverse before executing bootstrap")
    parser.add_argument("--candidate-out", type=Path, default=ROOT / "dist/compiler")
    parser.add_argument("--jobs", type=int, choices=range(1, 9), default=2,
                        help="parallel compile jobs, default 2; LLVM linking stays limited to one job")
    parser.add_argument("--ninja", default=shutil.which("ninja") or str(ROOT.parent / "clang/.toolchains/bin/ninja"))
    parser.add_argument("--cmake", default="cmake")
    parser.add_argument("--cc", default="cc")
    parser.add_argument("--cxx", default="c++")
    parser.add_argument("--ccache", default=os.environ.get("WISP_RUST_CCACHE") or None,
                        help="LLVM compiler cache executable; also selected by WISP_RUST_CCACHE")
    parser.add_argument("--wasm-opt", help="optimizer for the compiler Core module; required for the WASIp3 profile")
    parser.add_argument("--wasm-opt-library-dir", type=Path, help="directory containing this optimizer's packaged libbinaryen.so")
    parser.add_argument("--wasm-opt-memory-mib", type=int, choices=range(1024, 8193), default=4096)
    parser.add_argument("--wasm-opt-timeout", type=int, default=1800)
    parser.add_argument("--offline", action="store_true", help="Cargo registry access disabled; provision locked dependencies and stage0 cache first")
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "AMD64"):
        raise ValueError("this source lock currently provisions Linux x86_64 bootstrap tools only")
    lock_path = ROOT / "source-lock.json"
    lock = json.loads(lock_path.read_text())
    if lock.get("schema_version") != 1:
        raise ValueError("unsupported source lock schema")
    if lock.get("compiler_host_target") != "wasm32-wasip3":
        raise ValueError("Rust compiler host must be wasm32-wasip3")
    source_path = args.source or ROOT / ("build/source-rust-" + lock["sources"][0]["revision"][:12])
    source, out, sdk = source_path.resolve(strict=True), args.out.resolve(), args.wasi_sdk.resolve(strict=True)
    if out == source or out.is_relative_to(source) or source.is_relative_to(out):
        raise ValueError("source and build output must be separate directories")
    if args.package:
        package_candidate(source, out, args.candidate_out.resolve(), args.offline)
        print(args.candidate_out.resolve())
        return 0
    native = {name: executable(getattr(args, name)) for name in ("ninja", "cmake", "cc", "cxx")}
    if args.ccache:
        native["ccache"] = executable(args.ccache)
    inputs = validate_inputs(source, sdk, lock)
    if lock.get("external_llvm"):
        llvm_sdk = (args.llvm_sdk or ROOT.parent / lock["external_llvm"]["directory"]).resolve(strict=True)
        inputs["externalLlvm"] = validate_llvm_sdk(llvm_sdk, lock["external_llvm"])
        inputs["sourceRevisions"]["llvm"] = lock["external_llvm"]["revision"]
        bootstrap = (args.bootstrap_prefix.resolve(strict=True) if args.bootstrap_prefix else
                     provision_bootstrap(lock, offline=args.offline) if args.build or args.dry_run else
                     ROOT / "cache/native-bootstrap" / lock["bootstrap"]["channel"])
        native["rustc"] = bootstrap / "bin/rustc"
        native["cargo"] = bootstrap / "bin/cargo"
        native["llvm-config"] = llvm_sdk / "bin/llvm-config"
        if args.build or args.dry_run:
            version = subprocess.check_output([str(native["rustc"]), "-vV"], text=True)
            if ("release: " + lock["rust_version"] + "\n" not in version
                    or "commit-hash: " + lock["sources"][0]["revision"] + "\n" not in version):
                raise ValueError("native bootstrap must be the exact matching official Rust release")
            description = lock["bootstrap"].get("compiler_description")
            if description and version.splitlines()[0] != "rustc " + lock["rust_version"] + " (" + description + ")":
                raise ValueError("native bootstrap version description differs from the sysroot metadata identity")
    out.mkdir(parents=True, exist_ok=True)
    config_path = out / "config.toml"
    config = configuration(source, out, sdk, native, args.jobs, lock)
    if config_path.is_file() and config_path.read_text() != config and (out / "obj" / lock["bootstrap"]["host"] / "llvm/build/CMakeCache.txt").is_file():
        raise ValueError("configuration differs from an existing build; select a fresh --out")
    config_path.write_text(config)
    base_command = [str(Path(sys.executable).resolve()), "-I", str(source / "x.py"), "build",
                    "--config", str(config_path), "-j", str(args.jobs)]
    if lock.get("external_llvm"):
        commands = [base_command + ["--stage", "1", "compiler/rustc"],
                    base_command + ["--stage", "0", "library"]]
    else:
        commands = [base_command + ["--stage", "1", "compiler/rustc", "library"]]
    if args.dry_run:
        for argv in commands:
            argv.append("--dry-run")
    command = commands[0]
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("RUST", "CARGO_BUILD_", "CARGO_PROFILE_", "CARGO_TARGET_", "CC_", "CXX_", "CFLAGS", "CXXFLAGS", "LDFLAGS"))
                   and key not in ("CC", "CXX", "AR", "RANLIB", "LLVM_CONFIG", "DESTDIR", "CARGO_ENCODED_RUSTFLAGS", "CARGOFLAGS", "CARGO", "CARGO_HOME", "CARGO_NET_OFFLINE")}
    environment.update(WASI_SDK_PATH=str(sdk), WASI_SYSROOT=str(sdk / "share/wasi-sysroot"),
                       CARGO_INCREMENTAL="0", CARGO_BUILD_JOBS=str(args.jobs), CMAKE_BUILD_PARALLEL_LEVEL=str(args.jobs),
                       CMAKE=str(native["cmake"]), RUSTFLAGS="-Wwarnings",
                       CARGO_HOME=str(ROOT / "cache/compiler-cargo"))
    environment["PATH"] = os.pathsep.join(dict.fromkeys([str(native["ninja"].parent), str(native["cmake"].parent), str(sdk / "bin"), environment.get("PATH", "")]))
    if args.offline:
        environment["CARGO_NET_OFFLINE"] = "true"
    optimizer = None
    if args.wasm_opt_library_dir and not args.wasm_opt:
        raise ValueError("--wasm-opt-library-dir requires --wasm-opt")
    if args.wasm_opt:
        path = executable(args.wasm_opt)
        libraries = args.wasm_opt_library_dir.resolve(strict=True) if args.wasm_opt_library_dir else None
        optimizer = {"path": str(path), "sha256": digest(path), "version": None,
                     "libraryDirectory": str(libraries) if libraries else None,
                     "libraries": {str(file): digest(file) for file in sorted(libraries.glob("libbinaryen.so*"))} if libraries else {},
                     "memoryLimitMiB": args.wasm_opt_memory_mib, "timeoutSeconds": args.wasm_opt_timeout,
                     "cores": 1}
        if args.wasm_opt_timeout <= 0:
            raise ValueError("--wasm-opt-timeout must be positive")
        if args.build or args.dry_run:
            version = subprocess.run([str(path), "--version"], env=optimizer_environment(environment, optimizer),
                                     text=True, capture_output=True, timeout=10)
            if version.returncode or not version.stdout.startswith("wasm-opt version "):
                raise ValueError("selected wasm-opt cannot execute: " + version.stderr)
            optimizer["version"] = version.stdout.strip()
    report = {"schemaVersion": 1, "mode": "build" if args.build else "dry-run" if args.dry_run else "plan",
              "status": "planned", "sourceRoot": str(source), "sourceLockSha256": digest(lock_path),
              "buildDriverSha256": digest(Path(__file__).resolve()),
              "runnerSha256": digest(ROOT / "scripts/runner.py"),
              "configSha256": digest(config_path), "inputs": inputs,
              "macroPatch": None, "compilerHost": lock["compiler_host_target"],
              "applicationTargets": lock["application_targets"], "bootstrapStage": lock.get("bootstrap_stage", 1), "compilerStage": lock.get("compiler_stage", 2),
              "nativeTools": {name: {"path": str(path), "sha256": digest(path) if path.is_file() else None} for name, path in native.items()},
              "command": command, "commands": commands, "cwd": str(source),
              "environment": {key: environment[key] for key in ("WASI_SDK_PATH", "WASI_SYSROOT", "CARGO_INCREMENTAL", "CARGO_BUILD_JOBS", "CMAKE_BUILD_PARALLEL_LEVEL", "RUSTFLAGS", "CARGO_HOME")},
              "installPrefix": str(out / "install"), "compilerBuildCompleted": False,
              "proceduralMacroAcceptancePassed": False}
    report["optimizer"] = optimizer
    if args.macro_patch:
        patch = args.macro_patch.resolve(strict=True)
        check_macro_pins(source, patch, lock)
        hashes = patch_source_hashes(source, patch)
        report["macroPatch"] = {"path": str(patch), "sha256": digest(patch), "sourceFiles": hashes}
        inputs["sourceInputs"].update(hashes)
    report["buildPatches"] = check_build_patches(source, lock)
    for applied in report["buildPatches"]:
        inputs["sourceInputs"].update(applied["sourceFiles"])
    receipt_path = out / ("build-report.json" if args.build else "dry-run-report.json" if args.dry_run else "build-plan.json")
    if args.build:
        snapshot_build_provenance(out, report, lock_path)
    write_json(receipt_path, report)
    if not args.build and not args.dry_run:
        print(json.dumps(report, indent=2))
        return 0
    if not args.macro_patch:
        raise ValueError("--dry-run/--build requires --macro-patch selecting the reviewed backport already applied to --source")
    if args.build and optimizer is None:
        raise ValueError("the WASIp3 compiler build requires --wasm-opt to keep startup practical")
    if args.offline:
        for artifact in lock["bootstrap"]["artifacts"]:
            cached = ROOT / "cache/bootstrap" / lock["bootstrap"]["date"] / artifact["url"].rsplit("/", 1)[1]
            if not cached.is_file() or digest(cached) != artifact["sha256"]:
                raise ValueError(f"missing checksum-verified offline stage0 archive: {cached}")
    check_patch(source, patch)
    report["macroPatch"]["reverseApplyCheckPassed"] = True
    probe = out / "sdk-probe.cpp"
    probe.write_text("#include <string>\nint main() { return std::string(\"rust\").size() == 4 ? 0 : 1; }\n")
    sdk_probe = [str(sdk / "bin/wasm32-wasip3-clang++"), "-fno-exceptions", str(probe), "-o", str(out / "sdk-probe.wasm")]
    probe_header = COMPONENT_WASM
    preflight = subprocess.run(sdk_probe, env=environment, text=True, capture_output=True)
    report["sdkProbe"] = {"argv": sdk_probe, "exitCode": preflight.returncode, "stderr": preflight.stderr}
    write_json(receipt_path, report)
    if preflight.returncode or (out / "sdk-probe.wasm").read_bytes()[:8] != probe_header:
        raise ValueError("WASI SDK C++ Component probe failed; see build-report.json")
    report["status"] = "running"
    write_json(receipt_path, report)
    log_path = out / ("bootstrap.log" if args.build else "dry-run.log")
    with log_path.open("w") as log:
        for argv in commands:
            print("Running: " + " ".join(argv), flush=True)
            process = subprocess.run(argv, cwd=source, env=environment, stdout=log, stderr=subprocess.STDOUT)
            if process.returncode:
                break
    report["exitCode"] = process.returncode
    report["status"] = "dry-run-passed" if args.dry_run and process.returncode == 0 else "build-passed" if process.returncode == 0 else "failed"
    if args.build and process.returncode == 0:
        targets = [lock["compiler_host_target"], *lock["application_targets"]]
        try:
            stage_build_outputs(out, lock)
            report["compilerOptimization"] = optimize_compiler_component(out / "install/bin/rustc.wasm", out, optimizer, environment)
            compiler = check_install(out / "install", targets)
            check_patch(source, patch)
            if digest(patch) != report["macroPatch"]["sha256"]:
                raise ValueError("macro backport changed during compiler build")
            if check_build_patches(source, lock) != report["buildPatches"]:
                raise ValueError("build patches changed during compiler build")
            if any(digest(source / name) != expected for name, expected in inputs["sourceInputs"].items()):
                raise ValueError("compiler source configuration inputs changed during build")
            configured_runtime = os.environ.get("WISP_RUST_WASMTIME")
            runtime = shutil.which(configured_runtime) if configured_runtime else None
            if not runtime:
                raise ValueError("Wisp must provide the pinned Wasmtime host through WISP_RUST_WASMTIME")
            probe_command = [str(runtime), "run", "-Ccache=y", *["-Sp3=y", "-Oopt-level=0", "-Oregalloc-algorithm=single-pass", "-Cinlining=n"], "--dir", str(out / "install"),
                             "--env", "RUST_MIN_STACK=16777216", str(compiler), "--version"]
            invoked = subprocess.run(probe_command, env=environment, text=True, capture_output=True, timeout=600)
            report["compilerProbe"] = {"argv": probe_command, "exitCode": invoked.returncode,
                                       "stdout": invoked.stdout, "stderr": invoked.stderr}
            if invoked.returncode or not invoked.stdout.startswith("rustc " + lock["rust_version"]):
                raise ValueError("source-built compiler did not report its expected version through pinned Wasmtime")
            report["compilerBuildCompleted"] = True
            report["compiler"] = {"path": str(compiler), "sha256": digest(compiler), "bytes": compiler.stat().st_size}
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            report["status"] = "artifact-check-failed"
            report["artifactError"] = str(error)
            write_json(receipt_path, report)
            raise ValueError(str(error)) from error
    write_json(receipt_path, report)
    print(json.dumps({"status": report["status"], "report": str(receipt_path), "log": str(log_path)}, indent=2))
    return process.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as error:
        print(f"build-compiler: {error}", file=sys.stderr)
        raise SystemExit(2)
