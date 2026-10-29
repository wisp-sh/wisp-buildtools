#!/usr/bin/env python3
"""Verify the Wasm-only JS/TS build with real Component calls in Wasmtime."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
from unittest.mock import patch

from toolchain import ROOT, COMPONENT_HEADER, check_integrity, digest, tool_path, wasmtime_path
import typescript as typescript_compiler

sys.path.insert(0, str(ROOT.parent))
import wasm_tools


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wasmtime", type=Path)
    parser.add_argument("--engine", choices=("quickjs", "starlingmonkey"), default="quickjs")
    parser.add_argument("--compiler-root", type=Path, default=ROOT, help="installed JavaScript package to exercise")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "build/verify")
    args = parser.parse_args()
    if args.wasmtime:
        os.environ["WISP_WASMTIME"] = str(args.wasmtime.resolve())
    compiler_root = args.compiler_root.resolve()
    engine_args = ["--engine", args.engine]
    runtime = wasmtime_path()
    os.environ["WISP_WASMTIME"] = str(runtime)
    directory = args.output_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    guard = directory / "blocked-native-tools"
    guard.mkdir(exist_ok=True)
    log = guard / "attempts.log"
    log.unlink(missing_ok=True)
    for name in ("node", "nodejs", "npm", "npx", "esbuild", "componentize-qjs", "jco", "wasm-tools", "cargo", "rustc", "go"):
        blocked = guard / name
        blocked.write_text("#!/bin/sh\nprintf '%s\\n' " + shlex.quote(name) +
                           " >> " + shlex.quote(str(log)) + "\nexit 97\n")
        blocked.chmod(0o755)
    env = {**os.environ, "PATH": str(guard) + os.pathsep + os.environ.get("PATH", ""),
           "RAYON_NUM_THREADS": "1", "TOKIO_WORKER_THREADS": "1"}
    os.environ["PATH"] = env["PATH"]
    checks, artifacts = [], []
    report = {"schemaVersion": 2, "passed": False, "checks": checks, "artifacts": artifacts,
              "directory": str(directory), "engine": args.engine, "fullyWasmHostedBuild": args.engine == "quickjs", "nodeRequired": False,
              "wasmtime": {"path": str(runtime)}}

    def run(command, *, cwd=ROOT, success=True):
        result = subprocess.run([str(value) for value in command], cwd=cwd, env=env,
                                text=True, capture_output=True, timeout=180)
        if success and result.returncode:
            raise ValueError((result.stderr or result.stdout).strip())
        return result

    def check(name, action):
        start = time.monotonic()
        try:
            detail = action()
            checks.append({"name": name, "passed": True,
                           "milliseconds": round((time.monotonic() - start) * 1000), "detail": detail})
            print(f"PASS {name}", flush=True)
            return detail
        except Exception as error:
            checks.append({"name": name, "passed": False,
                           "milliseconds": round((time.monotonic() - start) * 1000), "error": str(error)})
            print(f"FAIL {name}: {error}", file=sys.stderr, flush=True)
            return None

    def compile_component(source, wit, world, name, extra=()):
        output = directory / f"{name}.wasm"
        for path in (output, Path(str(output) + ".wit"), Path(str(output) + ".build.json")):
            path.unlink(missing_ok=True)
        run([sys.executable, compiler_root / "compile.py", *engine_args, source, "--wit", wit, "--world", world, "-o", output, *extra])
        assert output.read_bytes()[:8] == COMPONENT_HEADER
        receipt = json.loads(Path(str(output) + ".build.json").read_text())
        assert receipt["output"]["sha256"] == digest(output)
        assert receipt["output"]["bytes"] == output.stat().st_size < 16 * 1024 * 1024
        assert receipt["sourceCompilerWasmHosted"] is True
        assert receipt["fullyWasmHostedBuild"] is (args.engine == "quickjs")
        assert receipt["engine"]["name"] == args.engine
        if args.engine == "starlingmonkey":
            assert receipt["engine"]["receipt"]["snapshot"]["nativeSnapshotHostApproved"] is True
        assert receipt["nodeRequired"] is False
        wit_text = Path(str(output) + ".wit").read_text()
        assert not re.search(r"export (init|wisp-validate):", wit_text)
        artifact = {"name": name, "path": str(output), "bytes": output.stat().st_size,
                    "sha256": digest(output), "unstrippedBytes": receipt["output"]["unstrippedBytes"]}
        artifacts.append(artifact)
        return {"path": output, "receipt": receipt, "wit": wit_text, "artifact": artifact}

    def invoke(path, expression):
        try:
            return run([runtime, "run", "-C", "cache=y", "--invoke", expression, path]).stdout.strip()
        except ValueError as error:
            raise ValueError(f"{expression}: {error}") from error

    def rejects(source, wit, world, name, pattern, extra=()):
        output = directory / f"{name}.wasm"
        targets = [output, Path(str(output) + ".wit"), Path(str(output) + ".build.json")]
        for target in targets:
            target.unlink(missing_ok=True)
        result = run([sys.executable, compiler_root / "compile.py", *engine_args, source, "--wit", wit, "--world", world,
                      "-o", output, *extra], success=False)
        diagnostic = (result.stderr or result.stdout).strip()
        assert result.returncode, "invalid build succeeded"
        assert re.search(pattern, diagnostic, re.I | re.S), diagnostic
        assert not any(target.exists() for target in targets), "failed build published output"
        return {"diagnostic": diagnostic[-4000:]}

    def wat(stem, text):
        source, output = directory / f"{stem}.wat", directory / f"{stem}.wasm"
        source.write_text(text)
        run(wasm_tools.command("parse", source, "-o", output))
        return output

    try:
        report["dependencies"] = check("checksum-pinned Wasm compiler inputs", lambda: check_integrity(artifact_root=compiler_root))
        if compiler_root == ROOT:
            check("bootstrap check uses no Node or native compiler", lambda: json.loads(
                run([sys.executable, ROOT / "bootstrap.py", "--check", "--json"]).stdout))
        js = check("plain JS emits a stripped typed Component", lambda: compile_component(
            ROOT / "examples/add.js", ROOT / "examples/add.wit", "arithmetic", "add-js", ["--stub-wasi"])["artifact"])
        if js is None:
            raise ValueError("basic Component compilation failed; see recorded check")
        js_path = Path(js["path"])

        def numeric():
            results = {call: invoke(js_path, call) for call in ("add(20,22)", "add(-3.5,4.25)", "add(1000,-1001)")}
            assert list(results.values()) == ["42", "0.75", "-1"], results
            return results
        check("JavaScript executes in Wasm with varying arguments", numeric)

        def typescript():
            component = compile_component(ROOT / "examples/add.ts", ROOT / "examples/add.wit", "arithmetic", "add-ts", ["--stub-wasi"])
            assert any(item["path"].endswith("/sum.ts") for item in component["receipt"]["bundleInputs"])
            assert "WASIp1" in component["receipt"]["stages"]["typescript"]
            assert invoke(component["path"], "add(20,22)") == "42"
            assert invoke(component["path"], "add(1.25,2.5)") == "3.75"
            return component["artifact"]
        check("TypeScript and local imports compile inside esbuild WASI", typescript)

        if (compiler_root.parent / "tsrs/dist/manifest.json").is_file():
            def typechecked():
                component = compile_component(ROOT / "examples/typecheck/pass/add.ts", ROOT / "examples/add.wit",
                                              "arithmetic", "typecheck-pass", ["--typecheck", "--stub-wasi"])
                assert component["receipt"]["typecheck"]["passed"] is True
                assert component["receipt"]["options"]["typecheck"] is True
                assert invoke(component["path"], "add(20,22)") == "42"
                return component["artifact"]
            check("optional tsrs typecheck accepts a valid project", typechecked)
            check("optional tsrs typecheck rejects errors before bundling", lambda: rejects(
                ROOT / "examples/typecheck/fail/add.ts", ROOT / "examples/add.wit", "arithmetic",
                "typecheck-fail", r"tsrs typecheck failed.*2322", ["--typecheck", "--stub-wasi"]))

        else:
            raise ValueError("tsrs package missing; real TypeScript acceptance is mandatory")

        def input_digests():
            project = directory / "input-digests"
            project.mkdir(exist_ok=True)
            source = project / "add.ts"
            original = 'import meta from "./value.json"; export function add(a:number,b:number){return a+b+meta.offset;}'
            source.write_text(original)
            (project / "value.json").write_text('{"offset":0}')
            real_run = subprocess.run

            def after_guest(*arguments, **keywords):
                result = real_run(*arguments, **keywords)
                source.write_text(original.replace("a+b", "a-b"))
                return result

            with patch.object(typescript_compiler.subprocess, "run", after_guest):
                result = typescript_compiler.bundle(source, project, directory / "digest-output",
                    esbuild_wasm=compiler_root / ".toolchains/wasm/esbuild.wasm", wasmtime=runtime)
            expected = hashlib.sha256(original.encode()).hexdigest()
            recorded = next(item["sha256"] for item in result["inputs"] if item["path"] == str(source))
            assert recorded == expected and recorded != digest(source)
            assert len(result["inputs"]) == 2
            assert "a + b" in result["js"] and "a - b" not in result["js"]
            return {"sameSizeEditAfterGuest": True, "sha256": recorded, "jsonDependency": True}
        check("guest hashes exact parsed bytes despite later source edits", input_digests)

        def imported():
            component = compile_component(ROOT / "examples/imports.ts", ROOT / "examples/imports.wit", "imported-arithmetic", "imports", ["--stub-wasi"])
            provider = wat("host-math", '''(component
              (core module $m (func (export "add") (param f64 f64) (result f64)
                local.get 0 f64.const 3 f64.mul local.get 1 f64.add))
              (core instance $m (instantiate $m))
              (func $add (param "a" f64) (param "b" f64) (result f64)
                (canon lift (core func $m "add")))
              (instance $math (export "add" (func $add)))
              (export "toolchain:javascript/host-math" (instance $math)))''')
            composed = directory / "imports-composed.wasm"
            run(wasm_tools.command("compose", component["path"], "-d", provider, "-o", composed))
            results = [invoke(composed, call) for call in ("add(20,22)", "add(-3.5,4.25)")]
            assert results == ["82", "-6.25"], results
            if args.engine == "starlingmonkey":
                source = directory / "named-imports.ts"
                source.write_text('import {add as hostAdd} from "toolchain:javascript/host-math"; '
                                  'import * as host from "toolchain:javascript/host-math"; '
                                  'export function add(a:number,b:number) { return hostAdd(a,b)+host.add(a,b); }\n')
                named = compile_component(source, ROOT / "examples/imports.wit", "imported-arithmetic",
                                          "named-imports", ["--stub-wasi"])
                named_composed = directory / "named-imports-composed.wasm"
                run(wasm_tools.command("compose", named["path"], "-d", provider, "-o", named_composed))
                assert invoke(named_composed, "add(20,22)") == "164"
            return {"provider": "3*a+b", "results": results, **component["artifact"]}
        check("typed WIT imports call a composed Wasm provider", imported)

        def rich_types():
            component = compile_component(ROOT / "examples/types.ts", ROOT / "examples/types.wit", "typed-data", "types", ["--stub-wasi"])
            summary = invoke(component["path"], 'summarize([20,22,-0.5],"λ🌍")')
            assert re.search(r"count:\s*3", summary), summary
            assert re.search(r"total:\s*41\.5", summary), summary
            assert 'Summary: λ🌍' in summary, summary
            empty = invoke(component["path"], 'summarize([],"")')
            assert re.search(r"count:\s*0", empty) and re.search(r"total:\s*0", empty), empty
            success, failure = invoke(component["path"], "safe-divide(84,2)"), invoke(component["path"], "safe-divide(1,0)")
            assert success == "ok(42)", success
            assert failure == 'err("division by zero")', failure
            return {"summary": summary, "empty": empty, "success": success, "failure": failure}
        check("lists, records, Unicode and WIT results cross the ABI", rich_types)

        def state_once():
            source = directory / "state.js"
            source.write_text("let calls = 0; export function add(a,b) { return a+b+calls++; }\n")
            compile_component(source, ROOT / "examples/add.wit", "arithmetic", "under-test", ["--stub-wasi"])
            harness = wat("state-harness", '''(component
              (type $add (func (param "a" f64) (param "b" f64) (result f64)))
              (import "under-test" (instance $app (export "add" (func (type $add)))))
              (alias export $app "add" (func $add))
              (core func $call (canon lower (func $add)))
              (core module $m
                (import "app" "add" (func $add (param f64 f64) (result f64)))
                (func (export "twice") (result f64)
                  f64.const 20 f64.const 22 call $add drop
                  f64.const 20 f64.const 22 call $add))
              (core instance $imports (export "add" (func $call)))
              (core instance $m (instantiate $m (with "app" (instance $imports))))
              (func $twice (result f64) (canon lift (core func $m "twice")))
              (export "twice" (func $twice)))''')
            composed = directory / "state-composed.wasm"
            run(wasm_tools.command("compose", harness, "-p", directory, "-o", composed))
            assert invoke(composed, "twice()") == "43"
            return {"firstCall": 42, "secondCall": 43}
        check("guest module state initializes once per instance", state_once)

        for extension in ("js", "ts"):
            source = directory / f"invalid.{extension}"
            source.write_text("export function add(a" + (": number" if extension == "ts" else "") + " {\n")
            check(f"malformed {extension.upper()} fails atomically in a Wasm compiler", lambda source=source, extension=extension:
                  rejects(source, ROOT / "examples/add.wit", "arithmetic", f"invalid-{extension}", r"Expected|Unexpected"))

        initialization = directory / "initialization-error.js"
        initialization.write_text('throw new Error("INITIALIZATION_TEST"); export function add(a,b){return a+b}\n')
        check("guest initialization failures reject publication", lambda: rejects(
            initialization, ROOT / "examples/add.wit", "arithmetic", "initialization-error", r"initialization.*failed|INITIALIZATION_TEST"))
        check("unknown WIT worlds reject publication", lambda: rejects(
            ROOT / "examples/add.js", ROOT / "examples/add.wit", "absent", "invalid-world", r"world.*absent|absent.*world"))
        malformed_wit = directory / "invalid.wit"
        malformed_wit.write_text("package invalid:syntax; world arithmetic { export add: func( }\n")
        check("malformed WIT rejects publication", lambda: rejects(
            ROOT / "examples/add.js", malformed_wit, "arithmetic", "invalid-wit", r"parsing WIT|expected"))
        check("output size caps reject publication", lambda: rejects(
            ROOT / "examples/add.js", ROOT / "examples/add.wit", "arithmetic", "oversized", r"exceeds output cap 32", ["--max-bytes", "32"]))

        def caller_relative():
            caller = directory / "caller"
            caller.mkdir(exist_ok=True)
            shutil.copyfile(ROOT / "examples/add.js", caller / "add.js")
            shutil.copyfile(ROOT / "examples/add.wit", caller / "add.wit")
            for target in caller.glob("output.wasm*"):
                target.unlink()
            run([sys.executable, compiler_root / "compile.py", *engine_args, "add.js", "-o", "output.wasm", "--stub-wasi"], cwd=caller)
            assert invoke(caller / "output.wasm", "add(20,22)") == "42"
            return {"source": "add.js", "output": "output.wasm", "adjacentWit": True}
        check("CLI resolves caller paths and adjacent WIT", caller_relative)

        def existing_output():
            before = digest(js_path)
            result = run([sys.executable, compiler_root / "compile.py", *engine_args, ROOT / "examples/add.js", "-o", js_path], success=False)
            assert result.returncode and "already exists" in result.stderr, result.stderr
            assert digest(js_path) == before
        check("existing artifacts are preserved", existing_output)

        def default_wasi():
            component = compile_component(ROOT / "examples/add.js", ROOT / "examples/add.wit", "arithmetic", "default-wasi")
            assert "import wasi:" in component["wit"], component["wit"]
            assert invoke(component["path"], "add(20,22)") == "42"
            return component["artifact"]
        check("default WASI Component executes without Node", default_wasi)

        if args.engine == "starlingmonkey":
            def promises():
                source = directory / "promises.js"
                source.write_text("export async function add(a,b) { return await Promise.resolve(a+b); }\n")
                component = compile_component(source, ROOT / "examples/add.wit", "arithmetic", "promises", ["--stub-wasi"])
                assert invoke(component["path"], "add(20,22)") == "42"
                assert invoke(component["path"], "add(-3.5,4.25)") == "0.75"
                return {"settledMicrotasks": True, "asyncWit": False}
            check("Promise-returning exports settle microtasks", promises)

            def wide_integers():
                source = directory / "wide.js"
                source.write_text('''import host from "test:wide/host";
export function wide(value) { return value; }
export function signed(value) {
  if (value >= 0n) throw new Error('signed argument lost its sign');
  return value + 1n;
}
export function viaUnsigned(value) { return host.echoUnsigned(value); }
export function viaSigned(value) { return signed(host.echoSigned(value)); }
export function echoRecord(value) { return value; }
''')
                wit = directory / "wide.wit"
                wit.write_text('''package test:wide;
interface host { echo-unsigned: func(value:u64)->u64; echo-signed: func(value:s64)->s64; }
interface types { record pair { unsigned: u64, signed: s64 } }
world numbers {
  import host;
  use types.{pair};
  export wide: func(value:u64)->u64;
  export signed: func(value:s64)->s64;
  export via-unsigned: func(value:u64)->u64;
  export via-signed: func(value:s64)->s64;
  export echo-record: func(value:pair)->pair;
}
''')
                component = compile_component(source, wit, "numbers", "wide")
                provider = wat("wide-host", '''(component
                  (core module $m (func (export "echo") (param i64) (result i64) local.get 0))
                  (core instance $m (instantiate $m))
                  (func $unsigned (param "value" u64) (result u64) (canon lift (core func $m "echo")))
                  (func $signed (param "value" s64) (result s64) (canon lift (core func $m "echo")))
                  (instance $host (export "echo-unsigned" (func $unsigned)) (export "echo-signed" (func $signed)))
                  (export "test:wide/host" (instance $host)))''')
                composed = directory / "wide-composed.wasm"
                run(wasm_tools.command("compose", component["path"], "-d", provider, "-o", composed))
                values = {
                    "wide(18446744073709551615)": "18446744073709551615",
                    "wide(0)": "0",
                    "signed(-9223372036854775808)": "-9223372036854775807",
                    "via-unsigned(18446744073709551615)": "18446744073709551615",
                    "via-signed(-9223372036854775808)": "-9223372036854775807",
                }
                for expression, expected in values.items():
                    actual = invoke(composed, expression)
                    assert actual == expected, (expression, actual, expected)
                record = invoke(composed, "echo-record({unsigned:18446744073709551615,signed:-9223372036854775808})")
                assert "18446744073709551615" in record and "-9223372036854775808" in record, record
                return {"calls": values, "record": record, "coreProvider": "i64 identity"}
            check("StarlingMonkey BigInt bindings preserve 64-bit boundaries", wide_integers)

            def rejected_promise():
                source = directory / "rejected-promise.js"
                source.write_text("export async function add() { throw new Error('PROMISE_REJECTED'); }\n")
                component = compile_component(source, ROOT / "examples/add.wit", "arithmetic", "rejected-promise")
                result = run([runtime, "run", "--invoke", "add(20,22)", component["path"]], success=False)
                assert result.returncode and "PROMISE_REJECTED" in result.stderr, result.stderr
                pure = compile_component(source, ROOT / "examples/add.wit", "arithmetic", "rejected-promise-pure", ["--stub-wasi"])
                result = run([runtime, "run", "--invoke", "add(20,22)", pure["path"]], success=False)
                assert result.returncode and "wasm trap" in result.stderr, result.stderr
                return {"rejectionTraps": True, "diagnosticWithStdio": True, "pureWorldHasNoStderr": True}
            check("rejected Promise produces an invocation error", rejected_promise)

        def dependency_project():
            project = directory / "dependency-project"
            (project / "node_modules/offset").mkdir(parents=True, exist_ok=True)
            (project / "tsconfig.json").write_text(json.dumps({"compilerOptions": {
                "strict": True, "target": "ES2022", "module": "ESNext", "moduleResolution": "Bundler"},
                "include": ["*.ts"]}))
            (project / "node_modules/offset/package.json").write_text(json.dumps({
                "name": "offset", "version": "1.0.0", "main": "index.js", "types": "index.d.ts"}))
            (project / "node_modules/offset/index.d.ts").write_text("export const offset: number;\n")
            dependency = project / "node_modules/offset/index.js"
            dependency.write_text("export const offset = 1;\n")
            source = project / "main.ts"
            source.write_text('import {offset} from "offset"; export function add(a:number,b:number):number {return a+b+offset;}\n')
            first = compile_component(source, ROOT / "examples/add.wit", "arithmetic", "dependency-first",
                                      ["--typecheck", "--stub-wasi"])
            assert invoke(first["path"], "add(20,22)") == "43"
            dependency.write_text("export const offset = 2;\n")
            second = compile_component(source, ROOT / "examples/add.wit", "arithmetic", "dependency-second",
                                       ["--typecheck", "--stub-wasi"])
            assert invoke(second["path"], "add(20,22)") == "44"
            assert first["receipt"]["javascriptSha256"] != second["receipt"]["javascriptSha256"]
            (project / "broken.ts").write_text('const value: number = "wrong";\n')
            rejects(source, ROOT / "examples/add.wit", "arithmetic", "dependency-invalid", r"2322", ["--typecheck"])
            (project / "broken.ts").write_text('const value: number = 1;\n')
            (project / "tsconfig.json").write_text(json.dumps({"compilerOptions": {
                "strict": True, "target": "ES2022"}, "files": ["broken.ts"]}))
            rejects(source, ROOT / "examples/add.wit", "arithmetic", "excluded-entrypoint",
                    r"entrypoint is excluded", ["--typecheck"])
            return {"offlineNpm": True, "tsconfig": True, "dependencyEdit": [43, 44],
                    "projectErrorRejected": True, "excludedEntrypointRejected": True}
        check("offline npm modules, tsconfig and changed dependency bytes", dependency_project)

        def guards():
            assert not log.exists(), log.read_text()
            return {"blocked": 11, "attempted": 0}
        check("native compiler and Node guards were never invoked", guards)
        report["passed"] = all(item["passed"] for item in checks)
    except Exception as error:
        report["error"] = str(error)
        print(f"verify: {error}", file=sys.stderr)
    finally:
        report["finishedAt"] = datetime.now(timezone.utc).isoformat()
        (directory / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
        print(f"{sum(item['passed'] for item in checks)}/{len(checks)} checks passed; {directory / 'verification.json'}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        raise SystemExit(f"verify: {error}")
