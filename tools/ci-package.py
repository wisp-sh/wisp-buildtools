#!/usr/bin/env python3
"""Provision pinned compiler packages for wisp-pkgs without publication credentials."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def load_driver():
    # Composite actions execute from the recipe checkout. run_path does not add
    # the buildtools checkout to Python's import path as executing build.py does.
    original_path = sys.path[:]
    try:
        sys.path.insert(0, str(ROOT))
        return runpy.run_path(str(ROOT / 'build.py'))
    finally:
        sys.path[:] = original_path


def targets(toolchain):
    allowed = {'clang', 'go', 'rust', 'zig', 'nim', 'assemblyscript', 'javascript',
               'python', 'grain', 'swift', 'java', 'kotlin'}
    if toolchain not in allowed:
        raise ValueError('unsupported package compiler: ' + toolchain)
    # Python's additional string reactor uses Clang runtime/bootstrap tooling.
    return list(dict.fromkeys((['clang'] if toolchain == 'python' else []) +
                             ['llvm-swift' if toolchain == 'swift' else toolchain, 'wasm-tools']))


def provision(toolchain, recipe_root):
    manifest = json.loads((ROOT / 'buildtools.json').read_text())
    driver = load_driver()
    prerequisites = runpy.run_path(str(ROOT / 'tools/ci-prerequisites.py'))
    prerequisites['check_lock']()
    order = driver['dependency_order'](manifest, targets(toolchain))
    os.environ['WISP_BUILDTOOLS_ROOT'] = str(ROOT)
    os.environ['WISP_COMPILER_MEMORY_LOCK'] = str(recipe_root / '.cache/compiler-memory.lock')
    for tool in order:
        # Apply each result immediately: dependencies run in this process, rather
        # than waiting for GITHUB_ENV to be applied to the next Actions step.
        report = prerequisites['install_for'](tool)
        os.environ.update(report['environment'])
        os.environ['PATH'] = os.pathsep.join([*report['paths'], os.environ['PATH']])
        subprocess.run([sys.executable, str(ROOT / 'build.py'), 'build', tool,
                        '--no-deps', '--jobs', '2'], cwd=ROOT, check=True)
    exported = {
        'WISP_BUILDTOOLS_ROOT': ROOT,
        'WISP_COMPILER_MEMORY_LOCK': os.environ['WISP_COMPILER_MEMORY_LOCK'],
        'WISP_PREVIEW1_ADAPTER': ROOT / 'wasm-tools/dist/wasi_snapshot_preview1.reactor.wasm',
        'WISP_WASM_TOOLS': ROOT / 'wasm-tools/dist/wasm-tools.wasm',
        'WISP_CLANG': ROOT / 'clang/.toolchains/build-p3/bin/clang-wasm.component',
        'WISP_CLANG_RESOURCE_DIR': ROOT / 'clang/include',
        'WISP_LLVM': ROOT / 'llvm/dist/shared',
        'WISP_GO_TOOLCHAIN': ROOT / 'go/dist/go-toolchain-full-1.27.2',
        'WISP_RUST': ROOT / 'rust/dist/compiler',
        'WISP_WASI_SDK': ROOT / 'clang/.toolchains/wasi-sdk-34.0-x86_64-linux',
    }
    # Later prerequisite installers may select the upstream host. Compiler
    # families needing Wisp's host must keep the derived host after provisioning.
    host = ROOT / '.ci/hosts/wisp-wasmtime-50.0.0/wasmtime'
    exported['WISP_BOOTSTRAP_WASMTIME'] = (
        ROOT / '.ci/toolchains/wasmtime' / prerequisites['LOCK']['downloads']['wasmtime']['executable']
        if toolchain == 'python' else host)
    if toolchain == 'go':
        # Verify the application execution host before exporting it to recipes.
        prerequisites['execution_host']('build.py', host.parent, ROOT / '.ci/host-build')
    if host.is_file():
        exported.update(WISP_CWASM_RUNTIME=host, WISP_WASMTIME=ROOT / 'cwasm.py',
                        WISP_RUST_WASMTIME=ROOT / 'cwasm.py')
    if toolchain == 'swift':
        shards = recipe_root / '.cache/swift-small/frontend-reviewed'
        subprocess.run(['cargo', 'fetch', '--locked', '--manifest-path',
                        str(ROOT / 'swift/shard_tool/Cargo.toml')], check=True)
        subprocess.run([sys.executable, str(ROOT / 'swift/shard.py'), '--input',
                        str(ROOT / 'llvm/dist/swift-shared/swift-frontend-llvm.so.wasm'),
                        '--out', str(shards), '--shards', '8'], check=True)
        exported.update(WISP_SWIFT_HOST_BACKEND='streamed', WISP_SWIFT_SHARDS=shards)
    prerequisites['github_environment']({key: str(value) for key, value in exported.items()}, [])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--toolchain', required=True)
    parser.add_argument('--recipe-root', type=Path, required=True)
    args = parser.parse_args()
    provision(args.toolchain, args.recipe_root.resolve())
