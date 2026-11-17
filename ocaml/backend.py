"""Compile and link C stubs and embedded OCaml bytecode using Wasm LLVM."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess

from runner import ROOT, runtime


def build(work: Path, sources: list[str], output: str, *, objects: list[str] | None = None,
          includes: list[str] | None = None, reactor: bool = False,
          exports: list[str] | None = None) -> None:
    helper = Path(os.environ.get('WISP_LLVM_TOOLCHAIN', ROOT.parent / 'llvm')) / 'shared.py'
    spec = importlib.util.spec_from_file_location('ocaml_llvm_shared', helper)
    shared = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shared)
    headers = Path(os.environ.get('WISP_CLANG_RESOURCE_DIR', ROOT.parent / 'clang/include')).resolve()
    common = ['-triple', 'wasm32-unknown-wasi', '-emit-obj', '-x', 'c', '-O1',
              '-fgnuc-version=4.2.1', '-relaxed-aliasing', '-fwrapv',
              '-nostdsysteminc', '-nobuiltininc', '-internal-isystem', '/clang/include',
              '-isystem', '/ocaml/sysroot/include', '-I', '/ocaml/lib',
              '-D_WASI_EMULATED_SIGNAL', '-D_WASI_EMULATED_PROCESS_CLOCKS',
              '-target-feature', '+exception-handling', '-Wno-deprecated-non-prototype',
              '-Wno-constant-conversion']
    for directory in includes or []:
        common += ['-I', directory]
    units, compiled = [], []
    for index, source in enumerate(sources):
        obj = f'/work/backend-{index}.o'
        units.append([*common, source, '-o', obj])
        compiled.append(obj)
    startup = 'crt1-reactor.o' if reactor else 'crt1-command.o'
    link = ['--strip-debug', '-z', 'stack-size=16777216']
    if reactor:
        link += ['--no-entry', '--export=_initialize', '--export-memory']
    for name in exports or []:
        link.append(f'--export={name}')
    link += [f'/ocaml/sysroot/lib/{startup}', *compiled, *(objects or []),
             '/ocaml/lib/libcamlrun.a', '-L/ocaml/sysroot/lib', '-lc', '-lm', '-lsetjmp',
             '-lwasi-emulated-signal', '-lwasi-emulated-process-clocks',
             '/ocaml/sysroot/lib/libclang_rt.builtins.a', '-o', output]
    (work / 'backend.json').write_text(json.dumps({'compile': units, 'link': link}, indent=2) + '\n')
    subprocess.run([str(runtime(backend=True)), 'run', '-S', 'p3=y', *shared.preload_args(),
                    '--dir', f'{ROOT / "dist"}::/ocaml', '--dir', f'{headers}::/clang/include',
                    '--dir', f'{work.resolve()}::/work',
                    str(ROOT / 'dist/bin/c-backend.wasm'), 'build', '/work/backend.json'], check=True)
