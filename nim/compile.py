#!/usr/bin/env python3
"""Compile a Nim library with the WASI hosted Nim, Clang and LLD binaries."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent

def runtime() -> Path:
    explicit = os.environ.get('WISP_WASMTIME')
    if explicit:
        found = shutil.which(explicit)
        if found: return Path(found)
        raise SystemExit(f'WISP_WASMTIME does not name an executable: {explicit}')
    found = shutil.which('wasmtime')
    if found: return Path(found)
    raise SystemExit('Wisp must provide Wasmtime with shared module imports; set WISP_WASMTIME.')

def llvm_preload_args() -> list[str]:
    helper = Path(os.environ.get('WISP_LLVM_TOOLCHAIN', ROOT.parent / 'llvm')) / 'shared.py'
    if not helper.is_file(): raise SystemExit('Install the shared LLVM toolchain beside Nim.')
    specification = importlib.util.spec_from_file_location('wisp_llvm_shared', helper)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module.preload_args()

def clang_resource_directory() -> Path:
    directory = Path(os.environ.get('WISP_CLANG_RESOURCE_DIR') or ROOT.parent / 'clang/include').resolve()
    metadata = json.loads((ROOT / 'dist/manifest.json').read_text()).get('clangResources', {})
    files = metadata.get('sha256', {})
    if metadata.get('provider') != 'clang' or not files or not (directory / 'stdarg.h').is_file() or directory.is_relative_to(ROOT):
        raise SystemExit('Install Clang resource headers separately; set WISP_CLANG_RESOURCE_DIR.')
    for name, expected in files.items():
        path = directory / name
        if not path.resolve().is_relative_to(directory) or path.is_symlink() or not path.is_file():
            raise SystemExit(f'Clang resource header is missing: {name}')
        with path.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != expected: raise SystemExit(f'Clang resource headers do not match Nim: {name}')
    return directory

def compile_nim(source: Path, output: Path, *, project_root: Path | None = None, work_dir: Path | None = None, exports: list[str] | None = None, nim_args: list[str] | None = None) -> None:
    source, output = source.resolve(), output.resolve()
    project = (project_root or source.parent).resolve()
    if not source.is_file(): raise SystemExit(f'Nim source does not exist: {source}')
    try: relative = source.relative_to(project)
    except ValueError: raise SystemExit('The source must be inside --project-root.')
    dist = ROOT / 'dist'
    for name in ('bin/nim.wasm', 'bin/c-backend.wasm', 'sysroot/lib/crt1-reactor.o'):
        if not (dist / name).is_file(): raise SystemExit('Run python3 bootstrap.py before compiling.')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    if work_dir is None:
        temporary = tempfile.TemporaryDirectory(prefix='.nim-', dir=output.parent)
        work = Path(temporary.name)
    else:
        work = work_dir.resolve()
        work.mkdir(parents=True, exist_ok=True)
        if (work / 'nimcache').exists(): shutil.rmtree(work / 'nimcache')
    wasmtime = runtime()
    headers = clang_resource_directory()
    mounts = ['--dir', f'{dist}::/nim', '--dir', f'{headers}::/clang/include', '--dir', f'{project}::/project', '--dir', f'{work}::/work']
    def run(args: list[str | Path]) -> None:
        subprocess.run([str(a) for a in args], check=True)
    try:
        run([wasmtime, 'run', '-O', 'opt-level=0', '-C', 'cache=y', *mounts, dist / 'bin/nim.wasm', 'c', '--skipCfg:on', '--skipUserCfg:on', '--skipParentCfg:on', '--lib:/nim/lib', '--path:/nim/lib/core', '--path:/nim/lib/pure', '--path:/nim/lib/pure/collections', '--path:/nim/lib/pure/concurrency', '--path:/nim/lib/posix', '--path:/nim/lib/impure', '--path:/nim/lib/wrappers', '--cpu:wasm32', '--os:linux', '--cc:clang', '--threads:off', '--mm:orc', '-d:release', '-d:useMalloc', '-d:useFork', '-d:noSignalHandler', '-d:wasi', '--exceptions:goto', '--compileOnly', '--noMain:on', '--nimcache:/work/nimcache', '--colors:off', *(nim_args or []), '/project/' + relative.as_posix()])
        sources = sorted((work / 'nimcache').glob('*.c'))
        if not sources: raise SystemExit('Nim succeeded without emitting C files.')
        # WASI reactor startup calls NimMain before the first library call.
        (work / 'initialize.c').write_text('extern void NimMain(void);\n__attribute__((constructor)) static void initialize_nim(void) { NimMain(); }\n')
        sources.append(work / 'initialize.c')
        sources.append(dist / 'unsupported_processes.c')
        common = ['-triple', 'wasm32-unknown-wasi', '-emit-obj', '-x', 'c', '-O1', '-fgnuc-version=4.2.1', '-nostdsysteminc', '-nobuiltininc', '-internal-isystem', '/clang/include', '-isystem', '/nim/sysroot/include', '-I', '/nim/lib', '-I', '/nim/compat', '-include', '/nim/compat.h', '-D_WASI_EMULATED_SIGNAL', '-D_WASI_EMULATED_PROCESS_CLOCKS']
        units, objects = [], []
        for index, path in enumerate(sources):
            guest = '/nim/unsupported_processes.c' if path.parent == dist else '/work/' + path.relative_to(work).as_posix()
            obj = f'/work/unit-{index}.o'
            units.append([*common, guest, '-o', obj])
            objects.append(obj)
        link = ['--no-entry', '--strip-all', '--export=_initialize', '--export=NimMain', '-z', 'stack-size=1048576']
        if exports:
            link += [f'--export={name}' for name in exports]
        else:
            link.append('--export-all')
        link += ['/nim/sysroot/lib/crt1-reactor.o', *objects, '-L/nim/sysroot/lib', '-lc', '-lm', '-ldl', '-lwasi-emulated-signal', '-lwasi-emulated-process-clocks', '-lwasi-emulated-getpid', '/nim/sysroot/lib/libclang_rt.builtins.a', '-o', '/work/module.wasm']
        (work / 'backend.json').write_text(json.dumps({'compile': units, 'link': link}, indent=2) + '\n')
        run([wasmtime, 'run', '-O', 'opt-level=0', '-C', 'cache=y', '-S', 'p3=y', *llvm_preload_args(), *mounts, dist / 'bin/c-backend.wasm', 'build', '/work/backend.json'])
        # Keep an existing artifact intact if copying fails, even when a retained
        # work directory is on another filesystem from the output.
        with tempfile.NamedTemporaryFile(prefix='.nim-output-', dir=output.parent, delete=False) as staged:
            staged_path = Path(staged.name)
        try:
            shutil.copy2(work / 'module.wasm', staged_path)
            os.replace(staged_path, output)
        finally:
            staged_path.unlink(missing_ok=True)
    finally:
        if temporary is not None: temporary.cleanup()

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('-o', '--output', required=True, type=Path)
    parser.add_argument('--project-root', type=Path, help='root mounted for local Nim imports; defaults to the source directory')
    parser.add_argument('--work-dir', type=Path, help='keep generated C, objects and backend invocation JSON')
    parser.add_argument('--export', action='append', dest='exports', help='export this C symbol; repeat for more symbols, otherwise all symbols are exported')
    parser.add_argument('--nim-arg', action='append', default=[], help='additional Nim frontend argument, passed as --nim-arg=VALUE')
    args = parser.parse_args()
    try:
        compile_nim(args.source, args.output, project_root=args.project_root, work_dir=args.work_dir, exports=args.exports, nim_args=args.nim_arg)
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode if error.returncode > 0 else 1)

if __name__ == '__main__':
    main()
