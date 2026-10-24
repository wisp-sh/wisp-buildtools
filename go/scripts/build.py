#!/usr/bin/env python3
"""Build a local Go project with packaged WASIp1 Go tools under Wasmtime."""

import argparse
import json
from pathlib import Path
import shutil
import time

from wasmbuild.go_adapter import construct, module_prefix
from wasmbuild.graph import Engine, file_digest
from wasmbuild.runtime import WasmtimeRuntime
from wasmbuild.toolchain import Toolchain


def main() -> None:
    started = time.perf_counter()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toolchain", type=Path, required=True)
    parser.add_argument("--wasmtime", type=Path, required=True)
    parser.add_argument("--project", type=Path, help="project root containing package main")
    parser.add_argument("--source", type=Path, help="single-source input")
    parser.add_argument("--import-prefix", help="local import prefix, defaults to go.mod or project")
    parser.add_argument("--compiler-flag", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, help="Wasmtime compiled-module cache")
    parser.add_argument("--build-cache", type=Path, help="content-addressed node output cache")
    args = parser.parse_args()
    if (args.project is None) == (args.source is None):
        parser.error("give exactly one of --project or --source")
    project = args.project.resolve(strict=True) if args.project else args.source.resolve(strict=True).parent
    if args.source and len(list(project.glob("*.go"))) != 1:
        parser.error("--source requires a directory with exactly one Go file; use --project")
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    toolchain = Toolchain(args.toolchain)
    runtime_cache = args.cache.resolve() if args.cache else output.parent / ".wasmtime-cache"
    build_cache = args.build_cache.resolve() if args.build_cache else output.parent / ".build-cache"
    runtime = WasmtimeRuntime(args.wasmtime, runtime_cache)
    engine = Engine(runtime, toolchain.tools, toolchain.digest, build_cache)
    graph = construct(project, toolchain, module_prefix(project, args.import_prefix),
                      tuple(args.compiler_flag))
    graph_started = time.perf_counter()
    report, produced = engine.execute(graph)
    shutil.copyfile(produced["link"]["main.wasm"], output)
    print(json.dumps({"output": str(output), "sha256": file_digest(output),
                      "output_bytes": output.stat().st_size,
                      "toolchain_sha256": toolchain.digest,
                      "graph_seconds": round(time.perf_counter() - graph_started, 3),
                      "total_seconds": round(time.perf_counter() - started, 3),
                      "nodes": report}, indent=2))


if __name__ == "__main__":
    main()
