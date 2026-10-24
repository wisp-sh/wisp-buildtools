#!/usr/bin/env python3
"""Build the pinned Goose frontend as WASI and package its shared C backend."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from runner import ROOT, digest, manifest

LOCK = json.loads((ROOT / 'toolchain.lock.json').read_text())


def inputs():
    return {str(p.relative_to(ROOT)): digest(p) for p in sorted((ROOT / 'src').glob('*')) if p.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wasi-sdk', type=Path, default=Path(os.environ.get(
        'WASI_SDK_PATH', ROOT.parent / LOCK['wasiSdk']['provider'])))
    parser.add_argument('--c-backend', type=Path, default=ROOT.parent / 'nim/dist/bin/c-backend.wasm')
    parser.add_argument('--compiler', type=Path, help='package an already built frontend')
    parser.add_argument('--jobs', type=int, default=2)
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    if not args.force and not args.compiler:
        try:
            if manifest()['sourceInputs'] == inputs():
                print('Goose WASI toolchain is ready.'); return
        except (OSError, ValueError, KeyError):
            pass
    sdk = args.wasi_sdk.resolve(strict=True)
    backend = args.c_backend.resolve(strict=True)
    work = ROOT / '.toolchains'
    work.mkdir(exist_ok=True)
    archive = work / 'source.tar.gz'
    if not archive.exists():
        pending = archive.with_suffix('.partial')
        try:
            with urllib.request.urlopen(LOCK['goose']['url'], timeout=120) as source, pending.open('wb') as out:
                shutil.copyfileobj(source, out)
            if digest(pending) != LOCK['goose']['sha256']:
                raise ValueError('Goose source archive checksum mismatch.')
            pending.replace(archive)
        finally:
            pending.unlink(missing_ok=True)
    if digest(archive) != LOCK['goose']['sha256']:
        raise ValueError('Goose source archive checksum mismatch.')
    with tempfile.TemporaryDirectory(prefix='goose-bootstrap-', dir=work) as directory:
        staging = Path(directory)
        with tarfile.open(archive) as source:
            source.extractall(staging / 'source', filter='data')
        tree, = (staging / 'source').iterdir()
        for name, before, after in json.loads((ROOT / 'src/frontend-patches.json').read_text()):
            path = tree / name
            text = path.read_text()
            if text.count(before) != 1:
                raise ValueError(f'Goose frontend patch no longer applies: {name}')
            path.write_text('// Modified by Wisp for the single-threaded wasm32 frontend.\n' + text.replace(before, after))
        compiler = args.compiler.resolve(strict=True) if args.compiler else staging / 'goose.wasm'
        if not args.compiler:
            env = {**os.environ, 'TMPDIR': str(staging)}
            subprocess.run([str(sdk / 'bin/clang++'), '--target=wasm32-wasip1', '-std=c++20',
                            '-O1', '-fwasm-exceptions', '-mllvm', '-wasm-use-legacy-eh=false',
                            '-I', str(tree / 'src'), str(ROOT / 'src/compiler.cpp'),
                            '-Wl,-z,stack-size=67108864', '-Wl,--max-memory=2147483648',
                            '-lunwind', '-o', str(compiler)], env=env, check=True)
        for path in (compiler, backend):
            if path.read_bytes()[:8] not in (b'\0asm\x01\0\0\0', b'\0asm\x0d\0\x01\0'):
                raise ValueError(f'Expected a Wasm compiler: {path}')
        dist = staging / 'dist'
        (dist / 'bin').mkdir(parents=True)
        shutil.copyfile(compiler, dist / 'bin/goose.wasm')
        shutil.copyfile(backend, dist / 'bin/c-backend.wasm')
        shutil.copytree(tree / 'stdlib', dist / 'stdlib')
        (dist / 'runtime').mkdir()
        for name in ('runtime.h', 'runtime_ext.h'):
            shutil.copyfile(tree / 'src/runtime' / name, dist / 'runtime' / name)
        shutil.copyfile(ROOT / 'src/support.h', dist / 'runtime/support.h')
        sysroot = sdk / 'share/wasi-sysroot'
        shutil.copytree(sysroot / 'include/wasm32-wasip1', dist / 'sysroot/include',
                        ignore=shutil.ignore_patterns('c++', 'eh'))
        (dist / 'sysroot/lib').mkdir()
        for name in ('crt1-reactor.o', 'libc.a', 'libm.a'):
            shutil.copyfile(sysroot / 'lib/wasm32-wasip1' / name, dist / 'sysroot/lib' / name)
        resources = sdk / 'lib/clang/23'
        shutil.copyfile(resources / 'lib/wasm32-unknown-wasip1/libclang_rt.builtins.a',
                        dist / 'sysroot/lib/libclang_rt.builtins.a')
        (dist / 'licenses').mkdir()
        shutil.copyfile(tree / 'LICENSE', dist / 'licenses/Goose-Apache-2.0.txt')
        for name in ('LICENSE', 'LICENSE.TXT'):
            if (sdk / name).is_file(): shutil.copyfile(sdk / name, dist / 'licenses' / f'wasi-sdk-{name}')
        for path in (ROOT.parent / 'nim/dist/licenses').iterdir():
            if path.is_file() and path.name.startswith('LLVM'):
                shutil.copyfile(path, dist / 'licenses' / path.name)
        # Bind the copied backend to its actual shared LLVM provider.
        shared = json.loads((ROOT.parent / 'llvm/dist/shared/manifest.json').read_text())
        if digest(backend) != shared['consumers']['nim']['sha256']:
            raise ValueError('The C backend does not match the shared LLVM provider.')
        record = {'schema':1, 'language':'goose', 'version':LOCK['goose']['revision'],
                  'profile':LOCK['profile'], 'bootstrap':LOCK, 'sourceInputs':inputs(),
                  'compilerHost':'wasm32-wasip1', 'cBackendHost':'wasm32-wasip3',
                  'fullyWasmHostedBuild':True, 'llvmLinkage':'shared-everything-core-module-imports',
                  'clangResources':{'provider':'clang', 'sha256':{
                      str(p.relative_to(resources / 'include')):digest(p)
                      for p in sorted((resources / 'include').rglob('*')) if p.is_file()}},
                  'sha256':{str(p.relative_to(dist)):digest(p) for p in sorted(dist.rglob('*')) if p.is_file()}}
        (dist / 'manifest.json').write_text(json.dumps(record, indent=2)+'\n')
        previous = staging / 'previous'
        if (ROOT / 'dist').exists(): (ROOT / 'dist').rename(previous)
        try: dist.rename(ROOT / 'dist')
        except BaseException:
            if previous.exists(): previous.rename(ROOT / 'dist')
            raise
    print('Goose WASI toolchain is ready.')

if __name__ == '__main__': main()
