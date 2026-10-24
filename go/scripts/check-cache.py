#!/usr/bin/env python3
"""Exercise package-level cache invalidation with real Wasmtime-hosted tools."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def build(script: Path, project: Path, toolchain: Path, wasmtime: Path,
          runtime_cache: Path, build_cache: Path, output: Path,
          flags: tuple[str, ...] = ()) -> dict:
    command = [sys.executable, str(script), "--project", str(project),
               "--toolchain", str(toolchain), "--wasmtime", str(wasmtime),
               "--cache", str(runtime_cache), "--build-cache", str(build_cache),
               "--output", str(output)]
    for flag in flags:
        command.append(f"--compiler-flag={flag}")
    return json.loads(subprocess.check_output(command, text=True))


def misses(result: dict) -> set[str]:
    return {name for name, details in result["nodes"].items()
            if details["cache"] == "miss"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toolchain", type=Path, required=True)
    parser.add_argument("--wasmtime", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    args = parser.parse_args()
    repository = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="go-cache-check-") as temporary:
        root = Path(temporary)
        project = root / "project"
        shutil.copytree(repository / "examples/multipkg", project)
        toolchain = args.toolchain.resolve()
        wasmtime = args.wasmtime.resolve()
        runtime_cache = args.cache.resolve()
        build_cache = root / "build-cache"
        output = root / "main.wasm"
        script = repository / "scripts/build.py"

        def run(flags: tuple[str, ...] = (), tc: Path = toolchain) -> dict:
            return build(script, project, tc, wasmtime, runtime_cache,
                         build_cache, output, flags)

        baseline = run()
        all_nodes = set(baseline["nodes"])
        checks = {"initial": misses(baseline)}
        checks["identical rebuild"] = misses(run())
        modifications = [
            ("one main source", "message.go", "Hello, WebAssembly", "Hello, cache",
             {"compile:project", "link"}),
            ("leaf package", "internal/format/json.go", '"message":', '"text":',
             {"compile:project/internal/format", "compile:project", "link"}),
            ("shared dependency", "internal/shared/normalize.go", "strings.ToLower", "strings.ToUpper",
             {"compile:project/internal/shared", "compile:project/internal/format",
              "compile:project/internal/hash", "link"}),
        ]
        for label, relative, old, new, expected in modifications:
            path = project / relative
            original = path.read_text()
            if old not in original:
                raise RuntimeError(f"test fixture changed: {relative}")
            path.write_text(original.replace(old, new))
            try:
                checks[label] = misses(run())
            finally:
                path.write_text(original)
            if checks[label] != expected:
                raise AssertionError(f"{label}: expected {sorted(expected)}, got {sorted(checks[label])}")
        checks["compiler flags"] = misses(run(("-N",)))

        variant = root / "toolchain-variant"
        shutil.copytree(toolchain, variant)
        manifest = variant / "manifest.toml"
        manifest.chmod(0o644)
        manifest.write_text(manifest.read_text().replace('version = "1.27.2"',
                                                         'version = "1.27.2+cache-test"'))
        sums = variant / "SHA256SUMS"
        sums.chmod(0o644)
        lines = sums.read_text().splitlines()
        changed = hashlib.sha256(manifest.read_bytes()).hexdigest()
        sums.write_text("\n".join(
            f"{changed}  manifest.toml" if line.endswith("  manifest.toml") else line
            for line in lines) + "\n")
        checks["toolchain hash"] = misses(run(tc=variant))
        expected = {"initial": all_nodes, "identical rebuild": set(),
                    "compiler flags": all_nodes, "toolchain hash": all_nodes}
        for label, nodes in expected.items():
            if checks[label] != nodes:
                raise AssertionError(f"{label}: expected {sorted(nodes)}, got {sorted(checks[label])}")
        print(json.dumps({"baseline_sha256": baseline["sha256"],
                          "misses": {name: sorted(nodes) for name, nodes in checks.items()}}, indent=2))


if __name__ == "__main__":
    main()
