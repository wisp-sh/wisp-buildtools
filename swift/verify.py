#!/usr/bin/env python3
"""Exercise source compilation and shared-backend execution without native compilers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from runtime import ROOT, host, integrity, provider, sha


def main():
    manifest = integrity()
    environment = {**os.environ, "PATH": "/nonexistent"}
    cases = []
    with tempfile.TemporaryDirectory(prefix="swift-verify-") as directory:
        work = Path(directory)

        def compile(source, output, *arguments, success=True):
            result = subprocess.run([sys.executable, str(ROOT / "compile.py"), str(source),
                                     "-o", str(output), *map(str, arguments)],
                                    env=environment, text=True, capture_output=True, timeout=1200)
            if (result.returncode == 0) != success:
                raise AssertionError(result.stdout + result.stderr)
            return result

        def invoke(path, call=None):
            argv = [str(host()), "run"]
            if call:
                argv += ["--invoke", call]
            result = subprocess.run([*argv, str(path)], env=environment, text=True,
                                    capture_output=True, timeout=120)
            if result.returncode:
                raise AssertionError(result.stdout + result.stderr)
            return result.stdout.strip()

        command = work / "command.wasm"
        compile(ROOT / "examples/main.swift", command)
        assert invoke(command) == "squares: [1, 4, 9, 16, 25]"
        cases.append("generic-array-string-command")
        math = work / "math.wasm"
        compile(ROOT / "examples/add.swift", math, "--component", "--wit", ROOT / "examples/add.wit", "--world", "math")
        for call, expected in [("add(20,22)", "42"), ("add(4294967295,1)", "0"),
                               ("widen(9007199254740993)", "9007199254740994"),
                               ("widen(18446744073709551615)", "0"), ("scale(-3)", "-4.5")]:
            assert invoke(math, call) == expected
            cases.append(call)
        receipt = json.loads(Path(str(math) + ".build.json").read_text())
        assert receipt["fullyWasmHostedBuild"] and receipt["nodeRequired"] is False
        assert receipt["assembly"]["initialization"] == "WASI reactor CRT _initialize"
        cases.append("reactor-crt-and-shared-libc-adapter-allocator")
        startup = work / "startup.wasm"
        compile(ROOT / "examples/startup.swift", startup, "--component", "--wit", ROOT / "examples/startup.wit", "--world", "startup")
        actual = invoke(startup, "stress()")
        assert actual == "swift-ready\nswift-allocation-ready\n42", repr(actual)
        cases.append("wasi-io-and-8400000-byte-array-growth")
        bad = work / "invalid.swift"
        bad.write_text("this is not Swift source\n")
        failed = work / "failed.wasm"
        compile(bad, failed, success=False)
        assert not failed.exists() and not Path(str(failed) + ".build.json").exists()
        cases.append("invalid-source-fails-without-partial-output")
        original = sha(math)
        compile(ROOT / "examples/add.swift", math, "--component", "--wit", ROOT / "examples/add.wit", success=False)
        assert sha(math) == original
        cases.append("existing-output-preserved")
        identity = work / "wrong-provider"
        identity.mkdir()
        (identity / "manifest.json").write_text('{}')
        result = subprocess.run([sys.executable, str(ROOT / "compile.py"), str(ROOT / "examples/main.swift"),
                                 "-o", str(work / "wrong.wasm")], env={**environment, "WISP_LLVM_SWIFT": str(identity)},
                                capture_output=True, text=True, timeout=120)
        assert result.returncode and "backend provider does not match" in result.stderr
        assert not (work / "wrong.wasm").exists()
        cases.append("unrelated-backend-provider-rejected")
    integrity()
    report = {"passed": True, "version": manifest["version"], "fullyWasmHostedBuild": True,
              "nodeRequired": False, "nativeCompilerUsed": False, "path": "/nonexistent",
              "compilerManifestSha256": sha(ROOT / "dist/manifest.json"),
              "providerManifestSha256": sha(provider() / "manifest.json"),
              "distributionUnchanged": True, "cases": cases}
    (ROOT / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
