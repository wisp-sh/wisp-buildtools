#!/usr/bin/env python3
"""Compile Goose libraries with the packaged WASI frontend and shared LLVM guest."""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from runner import ROOT, digest, manifest, runtime

SCALARS = {'int8_t', 'uint8_t', 'int16_t', 'uint16_t', 'int32_t', 'uint32_t',
           'int64_t', 'uint64_t', 'float', 'double', 'void'}


def exports(header: str) -> list[str]:
    names = []
    for result, name, parameters in re.findall(r'^([\w]+)\s+(\w+)\(([^()]*)\);$', header, re.M):
        if name == 'goose_init': continue
        if result not in SCALARS or any(
                not re.fullmatch(r'(?:u?int(?:8|16|32|64)_t|float|double)\s+\w+', p.strip())
                for p in parameters.split(',') if p.strip() != 'void'):
            raise ValueError('Goose scalar-v1 requires scalar parameters and return values.')
        names.append(name)
    if not names or len(names) != len(set(names)):
        raise ValueError('Goose did not emit a valid scalar export header.')
    return names


def compile_goose(source: Path, output: Path, *, project_root: Path | None = None,
                  work_dir: Path | None = None, optimization: str = 'O1') -> dict:
    record = manifest()
    source = source.resolve(strict=True)
    project = (project_root or source.parent).resolve(strict=True)
    if not source.is_file() or source.suffix != '.goose' or not source.is_relative_to(project):
        raise ValueError('Provide a .goose entrypoint inside --project-root.')
    if optimization not in {'O0', 'O1', 'O2'}: raise ValueError('Unsupported Goose optimization level.')
    output = output.absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    host = runtime()
    helper = Path(os.environ.get('WISP_LLVM_TOOLCHAIN', ROOT.parent / 'llvm')) / 'shared.py'
    spec = importlib.util.spec_from_file_location('goose_llvm_shared', helper)
    if not spec or not spec.loader: raise ValueError('Install the shared LLVM toolchain.')
    shared = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shared)
    headers = Path(os.environ.get('WISP_CLANG_RESOURCE_DIR', ROOT.parent / 'clang/include')).resolve(strict=True)
    for name, expected in record['clangResources']['sha256'].items():
        path = headers / name
        if path.is_symlink() or not path.resolve().is_relative_to(headers) or digest(path) != expected:
            raise ValueError(f'Clang resource headers do not match Goose: {name}')
    env = dict(os.environ)
    env.setdefault('XDG_CACHE_HOME', str(Path.home() / '.cache'))
    commands = []
    def run(argv):
        argv = list(map(str, argv))
        result = subprocess.run(argv, env=env, capture_output=True, text=True)
        commands.append({'arguments': argv, 'status':result.returncode,
                         'stdout':result.stdout, 'stderr':result.stderr})
        if result.returncode:
            raise ValueError(result.stderr.strip() or result.stdout.strip() or 'Goose guest failed.')
    temporary = None
    if work_dir is None:
        temporary = tempfile.TemporaryDirectory(prefix='.goose-', dir=output.parent)
        work = Path(temporary.name)
    else:
        work = work_dir.resolve()
        work.mkdir(parents=True, exist_ok=True)
    try:
        mounts = ['--dir', f'{ROOT / "dist"}::/goose', '--dir', f'{project}::/project',
                  '--dir', f'{work}::/work', '--dir', f'{headers}::/clang/include']
        run([runtime(frontend=True), 'run', '-C', 'cache=y', '-O', 'opt-level=0', '-W', 'exceptions=y',
             '-W', 'max-wasm-stack=33554432', *mounts, ROOT / 'dist/bin/goose.wasm',
             '/project/' + source.relative_to(project).as_posix(),
             '--header', '/work/exports.h', '-o', '/work/program.c', '-' + optimization])
        names = exports((work / 'exports.h').read_text())
        # Reactor constructors run once before any exported library function.
        (work / 'initialize.c').write_text('extern void goose_init(int, char **);\n'
            '__attribute__((constructor)) static void initialize_goose(void) { goose_init(0, 0); }\n')
        common = ['-triple', 'wasm32-unknown-wasi', '-emit-obj', '-x', 'c', '-' + optimization,
                  '-fgnuc-version=4.2.1', '-relaxed-aliasing', '-nostdsysteminc', '-nobuiltininc',
                  '-internal-isystem', '/clang/include', '-isystem', '/goose/sysroot/include',
                  '-Werror=implicit-function-declaration']
        units = [[*common, f'/work/{name}.c', '-o', f'/work/{name}.o'] for name in ('program', 'initialize')]
        # Place the downward-growing C shadow stack before static data. With
        # memory capped below 4 GiB, underflow traps instead of wrapping into data.
        link = ['--no-entry', '--strip-all', '--stack-first', '--max-memory=2147483648',
                '--export=_initialize', '-z', 'stack-size=1048576',
                *(f'--export={name}' for name in names), '/goose/sysroot/lib/crt1-reactor.o',
                '/work/program.o', '/work/initialize.o', '-L/goose/sysroot/lib', '-lc', '-lm',
                '/goose/sysroot/lib/libclang_rt.builtins.a', '-o', '/work/output.wasm']
        (work / 'backend.json').write_text(json.dumps({'compile':units, 'link':link}, indent=2)+'\n')
        run([host, 'run', '-S', 'p3=y', *shared.preload_args(), *mounts,
             ROOT / 'dist/bin/c-backend.wasm', 'build', '/work/backend.json'])
        candidate = work / 'output.wasm'
        if candidate.read_bytes()[:8] != b'\0asm\x01\0\0\0':
            raise ValueError('Goose backend did not emit Core Wasm.')
        receipt = {'schema':1, 'compiler':'goose', 'revision':record['version'],
                   'profile':record['profile'], 'fullyWasmHostedBuild':True,
                   'compilerSha256':record['sha256']['bin/goose.wasm'],
                   'sourceSha256':digest(source), 'outputSha256':digest(candidate),
                   'exports':names, 'initialization':'WASI reactor constructor -> goose_init',
                   'commands':commands}
        # Stage on the output filesystem to retain an existing artifact on failure.
        with tempfile.NamedTemporaryFile(prefix='.goose-output-', dir=output.parent, delete=False) as f:
            staged = Path(f.name)
        try:
            shutil.copyfile(candidate, staged)
            os.replace(staged, output)
        finally: staged.unlink(missing_ok=True)
        Path(str(output)+'.build.json').write_text(json.dumps(receipt, indent=2)+'\n')
        return receipt
    finally:
        if temporary: temporary.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('-o', '--output', required=True, type=Path)
    parser.add_argument('--project-root', type=Path)
    parser.add_argument('--work-dir', type=Path)
    parser.add_argument('-O0', action='store_const', dest='optimization', const='O0')
    parser.add_argument('-O1', action='store_const', dest='optimization', const='O1')
    parser.add_argument('-O2', action='store_const', dest='optimization', const='O2')
    args = parser.parse_args()
    try:
        compile_goose(args.source, args.output, project_root=args.project_root,
                      work_dir=args.work_dir, optimization=args.optimization or 'O1')
    except (OSError, ValueError) as error:
        print(f'goose: {error}', file=sys.stderr); return 1
    return 0

if __name__ == '__main__': sys.exit(main())
