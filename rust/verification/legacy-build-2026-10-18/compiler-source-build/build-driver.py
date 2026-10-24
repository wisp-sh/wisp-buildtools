#!/usr/bin/env python3
"""Build the pinned LLVM Rust port, keeping it separate from the working bootstrap.

--plan writes the exact configuration and commands. --dry-run additionally
compiles/runs Rust's native bootstrap driver and asks it to plan installation.
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
MACRO_PACKAGES = {
    "wasmi", "wasmi_collections", "wasmi_core", "wasmi_ir", "wasmparser",
    "downcast-rs", "foldhash", "hashbrown", "multi-stash", "spin", "string-interner",
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


def check_patch(source: Path, patch: Path) -> None:
    # --check changes nothing. Run outside a Git checkout so source paths are
    # resolved under --directory rather than the surrounding rust-wasm repo.
    with tempfile.TemporaryDirectory(prefix="vise-rust-patch-check-") as directory:
        checked = subprocess.run([str(executable("git")), "apply", "--check", "--reverse", "--unsafe-paths",
                                  "--directory", str(source), str(patch)], cwd=directory, text=True, capture_output=True)
    if checked.returncode:
        raise ValueError("selected macro backport is not applied exactly:\n" + checked.stderr)


def check_install(prefix: Path, targets: list[str]) -> Path:
    compiler = prefix / "bin/rustc.wasm"
    if compiler.stat().st_size > TOOL_INPUT_LIMIT:
        raise ValueError("source compiler exceeds Vise's 256 MiB tool input limit")
    with compiler.open("rb") as stream:
        if stream.read(8) != b"\0asm\x01\0\0\0":
            raise ValueError("installed compiler is not a Core Wasm module")
    for target in targets:
        libraries = prefix / "lib/rustlib" / target / "lib"
        if not list(libraries.glob("libstd-*.rlib")) or not list(libraries.glob("libproc_macro-*.rlib")):
            raise ValueError(f"installed target is missing std or proc_macro: {target}")
    return compiler


def optimizer_environment(environment: dict, profile: dict) -> dict:
    result = dict(environment, BINARYEN_CORES="1")
    if profile["libraryDirectory"]:
        result["LD_LIBRARY_PATH"] = profile["libraryDirectory"] + (os.pathsep + result["LD_LIBRARY_PATH"] if result.get("LD_LIBRARY_PATH") else "")
    return result


def optimize_large_compiler(compiler: Path, out: Path, profile: dict | None, environment: dict) -> dict:
    raw_hash = digest(compiler)
    if compiler.stat().st_size <= TOOL_INPUT_LIMIT:
        return {"applied": False, "reason": "compiler-fits-vise-input-limit", "inputSha256": raw_hash}
    if profile is None:
        raise ValueError("raw compiler exceeds Vise's 256 MiB limit; select the verified --wasm-opt tool to optimize it")
    if digest(Path(profile["path"])) != profile["sha256"]:
        raise ValueError("wasm-opt changed during the compiler build")
    for path, expected in profile["libraries"].items():
        if digest(Path(path)) != expected:
            raise ValueError("wasm-opt shared library changed during the compiler build")
    optimized = out / "compiler-optimized.wasm"
    argv = [profile["path"], str(compiler), "-Oz", "--all-features", "--enable-threads",
            "--strip-debug", "--strip-producers", "-o", str(optimized)]
    memory_limit = profile["memoryLimitMiB"] * 1024 * 1024
    def limit_memory():
        resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
    with (out / "compiler-optimization.log").open("w") as log:
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
    bundle = b"Additional interpreter dependencies in the Vise Rust macro backport.\nCrate archives are checked against the patched Cargo.lock.\n" + b"\n".join(sections)
    if len(bundle) > 256 * 1024:
        raise ValueError("macro notice bundle exceeds Vise's per-notice limit")
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
    runtime_dir = f'wasmtime-v{manifest["runtime"]["version"]}-x86_64-linux'
    installed = ROOT / "build/bootstrap"
    inventory = json.loads((installed / "SHA256SUMS.json").read_text())
    if runtime_dir + "/wasmtime" not in inventory:
        raise ValueError("working bootstrap has no pinned Wasmtime runtime")
    for relative, expected in inventory.items():
        if relative.startswith(runtime_dir + "/") and digest(installed / relative) != expected:
            raise ValueError("pinned Wasmtime runtime changed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".rust-macro-candidate-", dir=destination.parent) as temporary:
        stage = Path(temporary) / "candidate"
        prefix = stage / "build/bootstrap"
        prefix.mkdir(parents=True)
        shutil.copyfile(compiler, prefix / "rustc.wasm")
        # Keep exactly the three library targets used by the compiler runner.
        # Native bootstrap libraries and native helper executables stay outside
        # the candidate application's sysroot.
        for target in [report["compilerHost"], *report["applicationTargets"]]:
            shutil.copytree(out / "install/lib/rustlib" / target / "lib", prefix / "sysroot/lib/rustlib" / target / "lib")
        shutil.copytree(installed / runtime_dir, prefix / runtime_dir)
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
        manifest["licenses"] = {path.name: digest(path) for path in sorted((stage / "licenses").iterdir()) if path.is_file()}
        write_json(stage / "toolchain.json", manifest)
        files = {str(path.relative_to(prefix)): digest(path) for path in sorted(prefix.rglob("*")) if path.is_file()}
        if len(files) > 2048 or any(path.stat().st_size > 256 * 1024 * 1024 for path in prefix.rglob("*") if path.is_file()):
            raise ValueError("candidate exceeds Vise's toolchain file count or per-file size limit")
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
        (stage / "README.txt").write_text("Source-built macro compiler candidate.\nRun derive and Serde acceptance before treating this as a usable release.\n")
        stage.rename(destination)


def configuration(source: Path, out: Path, sdk: Path, native: dict, jobs: int, lock: dict) -> str:
    quote = lambda value: json.dumps(str(value))
    targets = [lock["compiler_host_target"], *lock["application_targets"]]
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
codegen-backends = ["llvm"]
deny-warnings = false
download-rustc = false
llvm-bitcode-linker = false
lld = false
llvm-tools = false
debug = false
debug-logging = false
debuginfo-level = 0
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
linker = {quote(sdk / "bin/clang")}
codegen-backends = ["llvm"]
'''
    # Parse our generated configuration before executing any upstream code.
    tomllib.loads(text)
    return text


def validate_inputs(source: Path, sdk: Path, lock: dict) -> dict:
    receipt = json.loads((source / "vise-source-receipt.json").read_text())
    assembled = json.loads((source / "vise-source-lock.json").read_text())
    expected = {item["name"]: item["revision"] for item in lock["sources"]}
    if receipt.get("sourceRevisions") != expected or not receipt.get("archiveChecksumsVerified"):
        raise ValueError("source preparation receipt does not select the locked revisions")
    for item in lock["sources"]:
        previous = next((old for old in assembled["sources"] if old["name"] == item["name"]), {})
        if any(previous.get(key) != item[key] for key in ("revision", "sha256", "destination")):
            raise ValueError("prepared source pins differ from source-lock.json")
    stage0 = (source / "src/stage0").read_text()
    for item in lock["bootstrap"]["artifacts"]:
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
              "src/bootstrap/src/core/build_steps/compile.rs", "src/bootstrap/src/core/builder.rs",
              "compiler/rustc_llvm/build.rs", "library/sysroot/Cargo.toml",
              "src/llvm-project/cmake/Modules/LLVMVersion.cmake"]
    return {"sourceRevisions": expected, "sourcePreparationReceiptSha256": digest(source / "vise-source-receipt.json"),
            "sourceInputs": {name: digest(source / name) for name in inputs},
            "wasiSdkArchiveSha256": digest(archive),
            "verification": "locked source-preparation receipt plus current configuration input hashes; source tree may contain the selected backport"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="write configuration/commands without running upstream code; default")
    mode.add_argument("--dry-run", action="store_true", help="compile/run native bootstrap, then x.py install --dry-run")
    mode.add_argument("--build", action="store_true", help="perform the real compiler and sysroot build")
    mode.add_argument("--package", action="store_true", help="package an already successful source build as an unverified candidate")
    parser.add_argument("--source", type=Path, default=ROOT / "build/source-rust-cf327c206854")
    parser.add_argument("--out", type=Path, default=ROOT / "build/compiler-source-build")
    parser.add_argument("--wasi-sdk", type=Path, default=ROOT / "downloads/wasi-sdk-24.0-x86_64-linux")
    parser.add_argument("--macro-patch", type=Path, help="adapted patch already applied to --source; checked in reverse before executing bootstrap")
    parser.add_argument("--candidate-out", type=Path, default=ROOT / "dist/rust-wasm-macro")
    parser.add_argument("--jobs", type=int, choices=range(1, 9), default=2,
                        help="parallel compile jobs, default 2; LLVM linking stays limited to one job")
    parser.add_argument("--ninja", default=shutil.which("ninja") or str(ROOT.parent / "wlang/.toolchains/bin/ninja"))
    parser.add_argument("--cmake", default="cmake")
    parser.add_argument("--cc", default="cc")
    parser.add_argument("--cxx", default="c++")
    parser.add_argument("--wasm-opt", help="optional optimizer, used only if the raw compiler exceeds Vise's 256 MiB limit")
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
    source, out, sdk = args.source.resolve(strict=True), args.out.resolve(), args.wasi_sdk.resolve(strict=True)
    if out == source or out.is_relative_to(source) or source.is_relative_to(out):
        raise ValueError("source and build output must be separate directories")
    if args.package:
        package_candidate(source, out, args.candidate_out.resolve(), args.offline)
        print(args.candidate_out.resolve())
        return 0
    native = {name: executable(getattr(args, name)) for name in ("ninja", "cmake", "cc", "cxx")}
    inputs = validate_inputs(source, sdk, lock)
    out.mkdir(parents=True, exist_ok=True)
    config_path = out / "config.toml"
    config = configuration(source, out, sdk, native, args.jobs, lock)
    if config_path.is_file() and config_path.read_text() != config and (out / "obj" / lock["bootstrap"]["host"] / "llvm/build/CMakeCache.txt").is_file():
        raise ValueError("configuration differs from an existing build; select a fresh --out")
    config_path.write_text(config)
    command = [str(Path(sys.executable).resolve()), "-I", str(source / "x.py"), "install",
               "--stage", "2", "--config", str(config_path), "-j", str(args.jobs), "compiler/rustc", "library/std"]
    if args.dry_run:
        command.append("--dry-run")
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("RUST", "CARGO_BUILD_", "CARGO_PROFILE_", "CARGO_TARGET_", "CC_", "CXX_", "CFLAGS", "CXXFLAGS", "LDFLAGS"))
                   and key not in ("CC", "CXX", "AR", "RANLIB", "LLVM_CONFIG", "DESTDIR", "CARGO_ENCODED_RUSTFLAGS", "CARGOFLAGS", "CARGO", "CARGO_HOME", "CARGO_NET_OFFLINE")}
    environment.update(WASI_SDK_PATH=str(sdk), WASI_SYSROOT=str(sdk / "share/wasi-sysroot"),
                       CARGO_INCREMENTAL="0", CARGO_BUILD_JOBS=str(args.jobs), CMAKE_BUILD_PARALLEL_LEVEL=str(args.jobs),
                       CMAKE=str(native["cmake"]), RUSTFLAGS="-Wwarnings",
                       CARGO_HOME=str(ROOT / "cache/compiler-cargo"))
    environment["PATH"] = os.pathsep.join(dict.fromkeys([str(native["ninja"].parent), str(native["cmake"].parent), environment.get("PATH", "")]))
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
              "applicationTargets": lock["application_targets"], "bootstrapStage": 2,
              "nativeTools": {name: {"path": str(path), "sha256": digest(path)} for name, path in native.items()},
              "command": command, "cwd": str(source),
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
    if args.offline:
        for artifact in lock["bootstrap"]["artifacts"]:
            cached = ROOT / "cache/bootstrap" / lock["bootstrap"]["date"] / artifact["url"].rsplit("/", 1)[1]
            if not cached.is_file() or digest(cached) != artifact["sha256"]:
                raise ValueError(f"missing checksum-verified offline stage0 archive: {cached}")
    check_patch(source, patch)
    report["macroPatch"]["reverseApplyCheckPassed"] = True
    # Fail before a long LLVM build if the SDK cannot link a threaded C++ guest.
    probe = out / "sdk-probe.cpp"
    probe.write_text("#include <mutex>\nint main() { std::mutex m; m.lock(); m.unlock(); return 0; }\n")
    sdk_probe = [str(sdk / "bin/wasm32-wasip1-threads-clang++"), "--sysroot=" + str(sdk / "share/wasi-sysroot"),
                 "-pthread", "-D_WASI_EMULATED_MMAN", str(probe), "-lwasi-emulated-mman",
                 "-Wl,--max-memory=4294967296", "-Wl,-z,stack-size=1048576", "-Wl,--stack-first",
                 "-o", str(out / "sdk-probe.wasm")]
    preflight = subprocess.run(sdk_probe, env=environment, text=True, capture_output=True)
    report["sdkProbe"] = {"argv": sdk_probe, "exitCode": preflight.returncode, "stderr": preflight.stderr}
    write_json(receipt_path, report)
    if preflight.returncode or (out / "sdk-probe.wasm").read_bytes()[:8] != b"\0asm\x01\0\0\0":
        raise ValueError("WASI SDK C++ threads probe failed; see build-report.json")
    report["status"] = "running"
    write_json(receipt_path, report)
    print("Running: " + " ".join(command), flush=True)
    log_path = out / ("bootstrap.log" if args.build else "dry-run.log")
    with log_path.open("w") as log:
        process = subprocess.run(command, cwd=source, env=environment, stdout=log, stderr=subprocess.STDOUT)
    report["exitCode"] = process.returncode
    report["status"] = "dry-run-passed" if args.dry_run and process.returncode == 0 else "build-passed" if process.returncode == 0 else "failed"
    if args.build and process.returncode == 0:
        targets = [lock["compiler_host_target"], *lock["application_targets"]]
        try:
            report["compilerOptimization"] = optimize_large_compiler(out / "install/bin/rustc.wasm", out, optimizer, environment)
            compiler = check_install(out / "install", targets)
            check_patch(source, patch)
            if digest(patch) != report["macroPatch"]["sha256"]:
                raise ValueError("macro backport changed during compiler build")
            if check_build_patches(source, lock) != report["buildPatches"]:
                raise ValueError("build patches changed during compiler build")
            if any(digest(source / name) != expected for name, expected in inputs["sourceInputs"].items()):
                raise ValueError("compiler source configuration inputs changed during build")
            runtime_manifest = json.loads((ROOT / "toolchain.json").read_text())
            runtime = ROOT / "build/bootstrap" / f'wasmtime-v{runtime_manifest["runtime"]["version"]}-x86_64-linux/wasmtime'
            probe_command = [str(runtime), "run", "-Ccache=y", "-Sthreads=y", "--dir", str(out / "install"),
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
