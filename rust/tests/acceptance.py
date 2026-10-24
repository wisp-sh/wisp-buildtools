#!/usr/bin/env python3
"""Compile ordinary Rust through an explicit Wasm-hosted compiler runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CATALOG = Path(__file__).with_name("cases.json")


class AcceptanceError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise AcceptanceError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise AcceptanceError(f"{path} must contain a JSON object")
    return value


def absolute_file(value: object, label: str) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise AcceptanceError(f"{label} must be an absolute local file path")
    path = Path(value).resolve()
    if not path.is_file():
        raise AcceptanceError(f"{label} does not exist: {path}")
    return path


def check_wasm(path: Path, expected: object, label: str) -> str:
    with path.open("rb") as handle:
        header = handle.read(8)
    if header not in (b"\0asm\x01\0\0\0", b"\0asm\x0d\0\x01\0"):
        raise AcceptanceError(f"{label} is not a core Wasm module or Component: {path}")
    actual = sha256(path)
    if not isinstance(expected, str) or actual != expected:
        raise AcceptanceError(f"{label} SHA-256 does not match the local Wasm file")
    return actual


def executable(value: str, cwd: Path | None = None) -> Path:
    if "/" in value:
        path = Path(value)
        if not path.is_absolute():
            path = (cwd or Path.cwd()) / path
        path = path.resolve()
    else:
        found = shutil.which(value)
        if found is None:
            raise AcceptanceError(f"executable is unavailable: {value}")
        path = Path(found).resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise AcceptanceError(f"executable is unavailable: {path}")
    return path


def validate_macro_trace(record: dict, executions: list[dict], program: Path, out: Path) -> None:
    """Require completed interpreter calls, rather than merely a compiled macro."""
    if record.get("runtime") != "wasmi":
        raise AcceptanceError("in-process procedural macro must identify its Wasmi runtime")
    trace = absolute_file(record.get("trace"), "procedural macro trace")
    if not trace.is_relative_to(out.resolve()) or record.get("traceSha256") != sha256(trace):
        raise AcceptanceError("procedural macro trace must match its hash inside the case output")
    invocation = record["execution"]
    if "WISP_WASM_PROC_MACRO_LOG=" + str(trace) not in executions[invocation]["argv"]:
        raise AcceptanceError("procedural macro trace is not attached to its compiler execution")
    compilation = record.get("compilation")
    if type(compilation) is not int or not 0 <= compilation < invocation:
        raise AcceptanceError("procedural macro has no earlier compilation record")
    build = executions[compilation]
    argv = build["argv"]
    if (build["kind"] != "compiler" or "-Zwasm-proc-macros" not in argv
            or not any(argv[index:index + 2] == ["--crate-type", "proc-macro"] for index in range(len(argv)))
            or not any(argv[index:index + 2] == ["--target", "wasm32-unknown-unknown"] for index in range(len(argv)))
            or not any(argv[index:index + 2] == ["-o", str(program)] for index in range(len(argv)))):
        raise AcceptanceError("procedural macro compilation did not produce the reported Wasm target")
    identity = (record.get("macroIndex"), record.get("macroName"), record.get("macroKind"))
    if (type(identity[0]) is not int or not 0 <= identity[0] < 2**32
            or not isinstance(identity[1], str) or not identity[1]
            or identity[2] not in ("function", "attribute", "derive")):
        raise AcceptanceError("procedural macro invocation identity is invalid")
    loaded, started, completed = False, False, False
    try:
        for line in trace.read_text().splitlines():
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("trace event must be an object")
            if (event.get("module") != str(program)
                    or (event.get("index"), event.get("name"), event.get("kind")) != identity):
                continue
            if (event.get("schemaVersion") != 1 or event.get("runtime") != "wasmi"
                    or event.get("macroTarget") != "wasm32-unknown-unknown"):
                raise ValueError("trace event has an unsupported runtime or target")
            action, status = event.get("event"), event.get("status")
            if action == "load" and status == "succeeded":
                loaded = True
            elif action == "invoke" and status == "started" and loaded and not started:
                started = True
            elif action == "invoke" and status == "succeeded" and started:
                started, completed = False, True
            else:
                raise ValueError("trace event did not complete a loaded macro invocation")
    except (OSError, ValueError, TypeError) as error:
        raise AcceptanceError(f"invalid procedural macro trace: {error}") from error
    if started or not completed:
        raise AcceptanceError("procedural macro trace contains no completed invocation")


def validate_report(report: dict, case: dict, manifest: Path, out: Path, runtime: Path) -> Path:
    if report.get("schemaVersion") != 1:
        raise AcceptanceError("compiler runner must emit report schemaVersion 1")
    if report.get("target") != "wasm32-wasip1":
        raise AcceptanceError("compiler runner reported the wrong application target")
    if report.get("manifestPath") != str(manifest):
        raise AcceptanceError("compiler runner reported a different source manifest")

    compiler = report.get("compiler")
    if not isinstance(compiler, dict) or compiler.get("host") != "wasm":
        raise AcceptanceError("compiler runner must report a Wasm-hosted compiler")
    compiler_path = absolute_file(compiler.get("path"), "compiler.path")
    check_wasm(compiler_path, compiler.get("sha256"), "compiler")

    executions = report.get("executions")
    if not isinstance(executions, list) or not executions:
        raise AcceptanceError("compiler runner did not record any Wasm executions")
    checked_executions = []
    for index, execution in enumerate(executions):
        if not isinstance(execution, dict):
            raise AcceptanceError(f"executions[{index}] must be an object")
        kind = execution.get("kind")
        if kind not in ("compiler", "build-script", "procedural-macro"):
            raise AcceptanceError(f"executions[{index}] has an unknown execution kind")
        if type(execution.get("exitCode")) is not int or execution["exitCode"] != 0:
            raise AcceptanceError(f"executions[{index}] did not exit successfully")
        program = absolute_file(execution.get("program"), f"executions[{index}].program")
        if kind == "compiler" and program != compiler_path:
            raise AcceptanceError(f"executions[{index}] used a different compiler")
        check_wasm(program, execution.get("sha256"), f"executions[{index}]")
        argv = execution.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) for arg in argv):
            raise AcceptanceError(f"executions[{index}].argv must contain actual command arguments")
        cwd_value = execution.get("cwd")
        if not isinstance(cwd_value, str) or not Path(cwd_value).is_absolute():
            raise AcceptanceError(f"executions[{index}].cwd must be an absolute directory")
        cwd = Path(cwd_value).resolve()
        if not cwd.is_dir() or executable(argv[0], cwd) != runtime:
            raise AcceptanceError(f"executions[{index}] did not invoke the configured Wasmtime")
        if not any(Path(arg).is_absolute() and Path(arg).resolve() == program for arg in argv[1:]):
            raise AcceptanceError(f"executions[{index}].argv does not name its Wasm program")
        checked_executions.append((kind, program))
    if not any(kind == "compiler" for kind, _ in checked_executions):
        raise AcceptanceError("compiler runner never invoked the compiler through Wasmtime")

    dependencies = report.get("dependencies")
    if not isinstance(dependencies, list):
        raise AcceptanceError("compiler runner must record dependencies, including an empty list")
    for expected in case["dependencies"]:
        if not any(
            isinstance(item, dict)
            and item.get("name") == expected["name"]
            and item.get("version") == expected["version"]
            and item.get("source") == "vendored"
            for item in dependencies
        ):
            raise AcceptanceError(f"missing vendored dependency {expected['name']} {expected['version']}")

    for field, kind in (("buildScripts", "build-script"), ("proceduralMacros", "procedural-macro")):
        records = report.get(field)
        if not isinstance(records, list):
            raise AcceptanceError(f"compiler runner must record {field}, including an empty list")
        packages = set()
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("package"), str):
                raise AcceptanceError(f"{field} contains an invalid package record")
            program = absolute_file(record.get("program"), f"{field}.program")
            check_wasm(program, record.get("sha256"), f"{field} {record['package']}")
            execution_index = record.get("execution")
            if type(execution_index) is not int or not 0 <= execution_index < len(checked_executions):
                raise AcceptanceError(f"{field} {record['package']} has no execution record")
            execution_kind, execution_program = checked_executions[execution_index]
            if field == "proceduralMacros" and record.get("loader") == "compiler-wasm-runtime":
                if execution_kind != "compiler":
                    raise AcceptanceError("in-process procedural macro must run inside the Wasm compiler")
                if not program.is_relative_to(out.resolve()):
                    raise AcceptanceError("procedural macro artifact must be inside its case output")
                validate_macro_trace(record, executions, program, out)
            elif execution_kind != kind or execution_program != program:
                raise AcceptanceError(f"{field} {record['package']} did not execute through Wasmtime")
            packages.add(record["package"])
        for package in case[field]:
            if package not in packages:
                raise AcceptanceError(f"missing Wasm execution record for {field} package {package}")
    for expected in case.get("macroInvocations", []):
        if not any(record.get("package") == expected["package"]
                   and record.get("macroName") == expected["name"]
                   and record.get("macroKind") == expected["kind"]
                   for record in report["proceduralMacros"]):
            raise AcceptanceError(f"missing completed {expected['kind']} macro {expected['name']}")

    artifact = report.get("artifact")
    if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
        raise AcceptanceError("compiler runner must report its output Wasm artifact")
    artifact_path = Path(artifact["path"])
    if not artifact_path.is_absolute():
        artifact_path = out / artifact_path
    artifact_path = artifact_path.resolve()
    if not artifact_path.is_relative_to(out.resolve()) or not artifact_path.is_file():
        raise AcceptanceError("application artifact must exist inside its output directory")
    check_wasm(artifact_path, artifact.get("sha256"), "application artifact")
    return artifact_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    runner = parser.add_mutually_exclusive_group()
    runner.add_argument("--runner", help="explicit compiler runner executable")
    runner.add_argument("--runner-command", help='explicit runner argv as JSON, e.g. ["python3", "/path/runner.py"]')
    parser.add_argument("--wasmtime", default=os.environ.get("WISP_WASMTIME"), help="Wasmtime executable")
    parser.add_argument("--vendor-dir", type=Path, help="prepared offline Cargo vendor directory")
    parser.add_argument("--out", type=Path, help="fresh evidence output directory")
    parser.add_argument("--case", action="append", dest="cases", help="case name; may be repeated")
    parser.add_argument("--list", action="store_true", help="list cases without compiling anything")
    parser.add_argument("--timeout", type=int, default=1800, help="per-case compilation timeout in seconds")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    catalog = read_json(CATALOG)
    known_cases = {case["name"]: case for case in catalog["cases"]}
    if args.list:
        for case in catalog["cases"]:
            print(f"{case['name']}: {', '.join(case['checks'])}")
        if catalog["pendingWispFlows"]:
            print("Pending Wisp adapter: " + "; ".join(catalog["pendingWispFlows"]))
        elif "wispIntegration" in catalog:
            print("Wisp integration uses its separate registry verifier: " + catalog["wispIntegration"]["verifier"])
        return 0
    if not args.runner and not args.runner_command:
        raise AcceptanceError("no compiler runner configured; pass --runner or --runner-command, no compilation was attempted")
    if not args.wasmtime:
        raise AcceptanceError("Wasmtime is required; pass --wasmtime or set WISP_WASMTIME")
    if args.out is None:
        raise AcceptanceError("--out must name a fresh evidence directory")
    if args.timeout <= 0:
        raise AcceptanceError("--timeout must be positive")
    runtime = executable(args.wasmtime)
    if args.runner_command:
        try:
            runner = json.loads(args.runner_command)
        except ValueError as error:
            raise AcceptanceError(f"--runner-command must be a JSON argument array: {error}") from error
        if not isinstance(runner, list) or not runner or any(not isinstance(arg, str) for arg in runner):
            raise AcceptanceError("--runner-command must be a nonempty JSON array of strings")
    else:
        runner = [args.runner]
    runner[0] = str(executable(runner[0]))
    selected = args.cases or list(known_cases)
    if len(set(selected)) != len(selected):
        raise AcceptanceError("--case selections must not contain duplicates")
    unknown = set(selected) - known_cases.keys()
    if unknown:
        raise AcceptanceError("unknown case: " + ", ".join(sorted(unknown)))
    if args.vendor_dir is not None:
        args.vendor_dir = args.vendor_dir.resolve()
        if not args.vendor_dir.is_dir():
            raise AcceptanceError(f"vendor directory does not exist: {args.vendor_dir}")
    out = args.out.resolve()
    if out.exists():
        raise AcceptanceError(f"output directory already exists: {out}")
    out.mkdir(parents=True)
    result = {
        "schemaVersion": 1,
        "target": catalog["target"],
        "status": "running",
        "cases": [],
        "pendingWispFlows": catalog["pendingWispFlows"],
    }
    exit_code = 0
    for name in selected:
        case = known_cases[name]
        case_out = out / name
        case_out.mkdir()
        manifest = (ROOT / case["project"] / "Cargo.toml").resolve()
        report_path = case_out / "build-report.json"
        command = runner + [
            "build", "--manifest-path", str(manifest), "--target", catalog["target"],
            "--out-dir", str(case_out), "--offline", "--report", str(report_path),
            "--wasmtime", str(runtime),
        ]
        if args.vendor_dir is not None:
            command += ["--vendor-dir", str(args.vendor_dir)]
        entry = {"name": name, "status": "running", "runnerCommand": command}
        result["cases"].append(entry)
        print(f"[{name}] compiling with the explicit Wasm compiler runner", flush=True)
        try:
            with (case_out / "runner.stdout.log").open("wb") as stdout, (case_out / "runner.stderr.log").open("wb") as stderr:
                compiled = subprocess.run(command, cwd=ROOT, stdout=stdout, stderr=stderr, timeout=args.timeout, check=False)
            if compiled.returncode != 0:
                entry["status"] = "unavailable" if compiled.returncode == 2 else "failed"
                raise AcceptanceError(f"runner exited {compiled.returncode}; see {case_out / 'runner.stderr.log'}")
            report = read_json(report_path)
            artifact = validate_report(report, case, manifest, case_out, runtime)
            invoked = subprocess.run([str(runtime), "run", str(artifact)], cwd=case_out, capture_output=True, timeout=60, check=False)
            (case_out / "application.stdout.log").write_bytes(invoked.stdout)
            (case_out / "application.stderr.log").write_bytes(invoked.stderr)
            if invoked.returncode != 0:
                raise AcceptanceError(f"application Wasm exited {invoked.returncode}")
            if invoked.stdout != case["expectedStdout"].encode():
                raise AcceptanceError(f"application output differs: expected {case['expectedStdout']!r}, got {invoked.stdout!r}")
            entry.update(status="passed", artifact=str(artifact), sha256=sha256(artifact), stdout=invoked.stdout.decode())
            print(f"[{name}] passed", flush=True)
        except (AcceptanceError, OSError, subprocess.TimeoutExpired) as error:
            if entry["status"] != "unavailable":
                entry["status"] = "failed"
            entry["error"] = str(error)
            exit_code = 2 if entry["status"] == "unavailable" else 1
            print(f"[{name}] {entry['status']}: {error}", file=sys.stderr)
            break
    executed = {entry["name"] for entry in result["cases"]}
    for name in selected:
        if name not in executed:
            result["cases"].append({"name": name, "status": "not-run"})
    result["status"] = "passed" if exit_code == 0 else "incomplete"
    (out / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (AcceptanceError, OSError) as error:
        print(f"unavailable: {error}", file=sys.stderr)
        sys.exit(2)
