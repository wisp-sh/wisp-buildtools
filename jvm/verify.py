#!/usr/bin/env python3
"""Run the installed shared JVM fixture without a native Java runtime."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile

from runner import ROOT, command, integrity


def main():
    before = integrity()
    with tempfile.TemporaryDirectory(prefix="jvm-verify-") as directory:
        work = Path(directory)
        invocation = command("JvmSmoke", [ROOT / "dist/fixtures"], mounts={work: "/proof"},
                             arguments=["/proof/result.txt"])
        result = subprocess.run(invocation, env={**os.environ, "PATH": "/nonexistent"},
                                capture_output=True, text=True, timeout=120)
        if result.returncode or result.stdout.strip() != "jvm-wasm-ok":
            raise AssertionError(result.stdout + result.stderr)
        assert (work / "result.txt").read_text() == "jvm-wasm-io\n"
    assert integrity() == before
    report = {"passed": True, "nativeJavaUsed": False, "nodeRequired": False,
              "execution": "Wasm JVM guest", "path": "/nonexistent", "distributionUnchanged": True,
              "cases": ["virtual-reference-return-with-tail-dispatch", "java-base-module", "java-management-module",
                        "application-unnamed-module-and-loader", "nest-host-loader", "exception-dispatch", "file-io",
                        "random-file-read-seek-length-and-channel-size", "unsigned-single-byte-file-input",
                        "interface-initialization-order-and-parent-exclusion", "most-specific-interface-default",
                        "static-member-initializes-declarer", "missing-file-exists-returns-false",
                        "interface-field-inherited-through-class", "virtual-call-does-not-initialize-interface"]}
    (ROOT / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
