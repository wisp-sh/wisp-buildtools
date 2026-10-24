#!/usr/bin/env python3
"""Development check of compiler-generated macros using the source-built native rustc.

This never builds a compiler or standard library and never participates in the
application runner. A native compiler pass is not Wasm-hosted acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MACRO_TARGET = "wasm32-unknown-unknown"
APP_TARGET = "wasm32-wasip1"
HOST = "x86_64-unknown-linux-gnu"
EXPECTED_MACROS = {"Answer", "checked_answer", "answer_literal"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def readiness(compiler: Path, sysroot: Path) -> dict:
    missing = []
    if not compiler.is_file():
        missing.append("source-built native rustc executable")
    elif not os.access(compiler, os.X_OK):
        missing.append("executable permission on native rustc")
    else:
        with compiler.open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                missing.append("native ELF compiler; a Wasm compiler belongs in application acceptance")
    libraries = {}
    for target in (MACRO_TARGET, APP_TARGET):
        directory = sysroot / "lib/rustlib" / target / "lib"
        libraries[target] = {}
        for name in ("std", "proc_macro"):
            files = sorted(directory.glob(f"lib{name}-*.rlib"))
            libraries[target][name] = [str(path) for path in files]
            if not files:
                missing.append(f"source-built {target} {name} rlib in the selected sysroot")
    return {"ready": not missing, "compiler": str(compiler), "sysroot": str(sysroot),
            "missing": missing, "targetLibraries": libraries}


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rustc", type=Path,
                        default=ROOT / f"build/compiler-source-build-1.99.1/obj/{HOST}/stage1/bin/rustc")
    parser.add_argument("--sysroot", type=Path, help="matching assembled sysroot; defaults to the rustc prefix")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "build/native-macro-integration-check")
    parser.add_argument("--wasmtime", type=Path, help="optionally execute the resulting WASIp1 command")
    parser.add_argument("--linker", type=Path, help="optional native wasm-ld for this development check")
    parser.add_argument("--readiness-only", action="store_true", help="inspect inputs without executing any compiler")
    parser.add_argument("--timeout", type=int, default=300, help="seconds per compiler or runtime process")
    args = parser.parse_args()
    compiler = args.rustc.resolve()
    sysroot = (args.sysroot or compiler.parent.parent).resolve()
    state = readiness(compiler, sysroot)
    if args.readiness_only or not state["ready"]:
        print(json.dumps(state, indent=2))
        return 0 if args.readiness_only else 2
    out = args.out_dir.resolve()
    if out.exists() and any(out.iterdir()):
        parser.error("--out-dir must be fresh")
    runtime = args.wasmtime.resolve() if args.wasmtime else None
    linker = args.linker.resolve() if args.linker else None
    if linker is not None and not linker.is_file():
        parser.error("--linker must name an installed native wasm-ld")
    if runtime is not None and not runtime.is_file():
        parser.error("--wasmtime must name an installed runtime executable")
    out.mkdir(parents=True, exist_ok=True)
    for name in ("macro", "runtime", "traces", "logs"):
        (out / name).mkdir()
    report = {"schemaVersion": 1, "kind": "native-compiler-development-check",
              "wasmHostedCompilerAcceptance": False, "passed": False,
              "readiness": state, "compiler": {"path": str(compiler), "sha256": sha256(compiler)},
              "executions": [], "macroInvocations": [], "applicationExecuted": False}
    report_path = out / "result.json"
    write_json(report_path, report)
    environment = {key: value for key, value in os.environ.items()
                   if key not in ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "RUSTC_WRAPPER",
                                  "RUSTC_WORKSPACE_WRAPPER", "WISP_WASM_PROC_MACRO_LOG")}

    def execute(label: str, command: list[str], *, trace: Path | None = None) -> str:
        process_environment = dict(environment)
        if trace is not None:
            process_environment["WISP_WASM_PROC_MACRO_LOG"] = str(trace)
        try:
            process = subprocess.run(command, cwd=out, env=process_environment, text=True,
                                     capture_output=True, timeout=args.timeout)
        except subprocess.TimeoutExpired as error:
            def text_output(value: str | bytes | None) -> str:
                return value.decode(errors="replace") if isinstance(value, bytes) else value or ""

            (out / "logs" / (label + ".stdout")).write_text(text_output(error.stdout))
            (out / "logs" / (label + ".stderr")).write_text(text_output(error.stderr))
            report["executions"].append({"label": label, "argv": command, "cwd": str(out),
                                         "exitCode": None, "timedOut": True,
                                         "timeoutSeconds": args.timeout})
            write_json(report_path, report)
            raise RuntimeError(f"{label} exceeded {args.timeout} seconds; partial logs preserved") from error
        (out / "logs" / (label + ".stdout")).write_text(process.stdout)
        (out / "logs" / (label + ".stderr")).write_text(process.stderr)
        record = {"label": label, "argv": command, "cwd": str(out), "exitCode": process.returncode}
        if trace is not None:
            record["trace"] = str(trace)
        report["executions"].append(record)
        write_json(report_path, report)
        if process.returncode:
            raise RuntimeError(f"{label} failed with exit {process.returncode}:\n{process.stderr}")
        return process.stdout

    version = execute("compiler-version", [str(compiler), "--version"]).strip()
    expected_version = json.loads((ROOT / "toolchain.json").read_text())["rustVersion"]
    if not version.startswith("rustc " + expected_version):
        raise RuntimeError(f"selected native compiler is not the pinned source version: {version}")
    report["compiler"]["version"] = version
    flags = execute("compiler-unstable-options", [str(compiler), "-Zhelp"])
    if "wasm-proc-macros" not in flags:
        raise RuntimeError("selected native compiler does not expose the macro backport flag")
    report["targetLibrarySha256"] = {
        target: {name: {path: sha256(Path(path)) for path in paths} for name, paths in libraries.items()}
        for target, libraries in state["targetLibraries"].items()
    }
    fixture = ROOT / "examples/derive"
    macro_source = fixture / "acceptance-derive/src/lib.rs"
    caller_source = fixture / "src/lib.rs"
    application_source = fixture / "src/main.rs"
    report["sourceSha256"] = {str(path): sha256(path) for path in (macro_source, caller_source, application_source)}
    module = out / "macro/acceptance_derive-probe.wasm"
    library = out / "runtime/libacceptance_macro-probe.rlib"
    application = out / "application.wasm"
    common = ["--sysroot", str(sysroot), "--edition", "2021", "-Zwasm-proc-macros", "-Cpanic=abort", "-Copt-level=1"]
    if linker is not None:
        common += ["-Clinker=" + str(linker)]
        report["linker"] = {"path": str(linker), "sha256": sha256(linker)}
    execute("compile-proc-macro", [str(compiler), str(macro_source), "--crate-name", "acceptance_derive",
            "--crate-type", "proc-macro", "--target", MACRO_TARGET, *common,
            "--extern", "proc_macro",
            "-Cmetadata=wisp_native_development_macro", "-o", str(module)],
            trace=out / "traces/compile-proc-macro.jsonl")
    if module.read_bytes()[:8] != b"\0asm\x01\0\0\0":
        raise RuntimeError("proc-macro compiler output is not a Core Wasm module")
    caller_trace = out / "traces/compile-caller.jsonl"
    execute("compile-caller", [str(compiler), str(caller_source), "--crate-name", "acceptance_macro",
            "--crate-type", "rlib", "--target", APP_TARGET, *common,
            "-Cmetadata=wisp_native_development_caller", "--extern", "acceptance_derive=" + str(module),
            "-Ldependency=" + str(module.parent), "-o", str(library)], trace=caller_trace)
    invocations = []
    for line in caller_trace.read_text().splitlines():
        record = json.loads(line)
        if record.get("event") == "invoke" and record.get("status") == "succeeded":
            if (record.get("schemaVersion") != 1 or record.get("runtime") != "wasmi"
                    or record.get("macroTarget") != MACRO_TARGET or record.get("module") != str(module)):
                raise RuntimeError("successful macro invocation names an unexpected interpreter/module")
            invocations.append(record)
    if {record["name"] for record in invocations} != EXPECTED_MACROS:
        raise RuntimeError("native compiler trace does not prove derive, helper, attribute and function macros ran")
    report["macroInvocations"] = invocations
    execute("compile-application", [str(compiler), str(application_source), "--crate-name", "acceptance_macro_bin",
            "--crate-type", "bin", "--target", APP_TARGET, *common,
            "--extern", "acceptance_macro=" + str(library), "-Ldependency=" + str(library.parent),
            "-Ldependency=" + str(module.parent), "-o", str(application)],
            trace=out / "traces/compile-application.jsonl")
    if application.read_bytes()[:8] != b"\0asm\x01\0\0\0":
        raise RuntimeError("application compiler output is not a Core Wasm module")
    report["artifacts"] = {"macro": {"path": str(module), "sha256": sha256(module)},
                           "callerLibrary": {"path": str(library), "sha256": sha256(library)},
                           "application": {"path": str(application), "sha256": sha256(application)}}
    if runtime is not None:
        output = execute("run-application", [str(runtime), "run", str(application)])
        if output.strip() != "42":
            raise RuntimeError("the macro-generated application did not print 42")
        report["applicationExecuted"] = True
        report["runtime"] = {"path": str(runtime), "sha256": sha256(runtime)}
    report["passed"] = True
    write_json(report_path, report)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"native macro development check: {error}", file=sys.stderr)
        raise SystemExit(2)
