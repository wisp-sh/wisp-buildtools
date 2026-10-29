"""Execution profiles for compiler guests, separate from Wasm used as input data."""
from __future__ import annotations

import importlib.util
from pathlib import Path

# These files are consumed by component assembly, never instantiated as tools.
SUPPORTED = {'clang', 'go', 'llvm', 'rust', 'zig', 'assemblyscript', 'nim', 'javascript',
             'python', 'wasm-tools', 'grain', 'swift', 'llvm-swift', 'jvm', 'teavm',
             'java', 'kotlin', 'ocaml', 'goose', 'tsrs', 'wizer'}

DATA = {
    'wasm-tools/dist/wasi_snapshot_preview1.reactor.wasm',
    'python/dist/template.component.wasm',
}


def execution_profile(tool, relative):
    if relative in DATA or relative.startswith('javascript/.toolchains/runtimes/') or relative == 'javascript/.toolchains/starlingmonkey/runtime.wasm':
        return None
    if tool == 'wizer':
        return None  # Explicit native host capability; no compiler guest.
    if tool == 'tsrs':
        return {'backend': 'python', 'host': 'tsrs'}
    if tool in {'assemblyscript', 'swift', 'llvm-swift'}:
        return {'backend': 'python', 'host': 'assemblyscript' if tool == 'assemblyscript' else 'swift'}
    flags = ['-Oopt-level=0']
    if tool in {'wasm-tools', 'python', 'go'}:
        flags = []
    elif tool in {'clang', 'llvm'} or (tool in {'nim', 'ocaml', 'goose', 'teavm'} and relative.endswith('c-backend.wasm')):
        flags += ['-Sp3=y']
    elif tool == 'rust':
        flags += ['-Sp3=y', '-Wmax-wasm-stack=16777216', '-Wasync-stack-size=33554432',
                  '-Oregalloc-algorithm=single-pass', '-Cinlining=n']
    elif tool in {'ocaml', 'goose'}:
        flags += ['-Wexceptions=y', '-Wmax-wasm-stack=33554432']
    elif tool == 'grain':
        flags += ['-Wmax-wasm-stack=16777216']
    elif tool == 'jvm':
        flags += ['-Wall-proposals=y', '-Wexceptions=y']
    return {'backend': 'cli', 'flags': flags}


def python_host(root, host):
    if host == 'tsrs':
        path = Path(root) / 'tsrs/wasmtime_host.py'
        spec = importlib.util.spec_from_file_location('_wisp_cwasm_tsrs', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        wt = module._wasmtime()
        engine = module.compilation_engine(wt)
        return lambda source: module.load_module(wt, engine, source, artifact_only=True)
    path = Path(root) / ('assemblyscript/src/runtime.py' if host == 'assemblyscript' else 'swift/runtime.py')
    spec = importlib.util.spec_from_file_location('_wisp_cwasm_' + host, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if host == 'assemblyscript':
        engine = module.compilation_engine()
        load = lambda source: module.load_module(engine, source, artifact_only=True)
    else:
        wasmtime = module.binding()
        engine, _ = module.compilation_engine(wasmtime)
        load = lambda source: module.load_module(wasmtime, engine, source, artifact_only=True)
    return load
