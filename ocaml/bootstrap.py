#!/usr/bin/env python3
"""Bootstrap OCaml 4.14.2 and its WASI bytecode runtime, then package them."""
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

BUILD_ENV = None

def run(*args, cwd=None, env=None):
    subprocess.run(list(map(str, args)), cwd=cwd, env=env if env is not None else BUILD_ENV, check=True)


def build_environment(work):
    # OCaml's reproducible-build format is TARGET=SOURCE, with %-escaped paths.
    def encode(value):
        return str(value).replace('%', '%#').replace('=', '%+').replace(':', '%.')
    mappings = [(Path.home(), '/build-user'), (ROOT.parent, '/wisp-buildtools'),
                (work, '/wisp-buildtools/ocaml/build')]
    environment = dict(os.environ)
    entries = [environment['BUILD_PATH_PREFIX_MAP']] if environment.get('BUILD_PATH_PREFIX_MAP') else []
    entries.extend(encode(target) + '=' + encode(source.resolve()) for source, target in mappings)
    environment['BUILD_PATH_PREFIX_MAP'] = ':'.join(entries)
    return environment


def main():
    global BUILD_ENV
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wasi-sdk', type=Path, default=Path(os.environ.get(
        'WISP_OCAML_WASI_SDK', ROOT.parent / LOCK['wasiSdk']['provider'])))
    parser.add_argument('--work-dir', type=Path, default=ROOT / '.toolchains/build')
    parser.add_argument('--package-from', type=Path, help='package an existing build directory with native/ and target/')
    parser.add_argument('--host-tools', type=Path, help='native ocamlrun and sak for an existing build on a noexec filesystem')
    parser.add_argument('--jobs', type=int, default=2)
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    if not args.force and not args.package_from:
        try:
            manifest()
            print('OCaml WASI toolchain is ready.')
            return
        except (OSError, ValueError, KeyError):
            pass
    sdk = args.wasi_sdk.resolve(strict=True)
    work = (args.package_from or args.work_dir).resolve()
    BUILD_ENV = build_environment(work)
    work.mkdir(parents=True, exist_ok=True)
    cache = ROOT / '.toolchains'
    cache.mkdir(exist_ok=True)
    archive = cache / 'ocaml-4.14.2.tar.gz'
    if not archive.exists():
        with urllib.request.urlopen(LOCK['ocaml']['url']) as response, archive.open('wb') as output:
            shutil.copyfileobj(response, output)
    if digest(archive) != LOCK['ocaml']['sha256']:
        raise ValueError('OCaml source archive checksum mismatch.')
    native, target = work / 'native', work / 'target'
    if not args.package_from:
        for directory in (native, target):
            if directory.exists():
                raise ValueError(f'Build directory already exists; select a fresh --work-dir: {directory}')
            directory.mkdir()
            with tarfile.open(archive) as source:
                for member in source.getmembers():
                    member.name = '/'.join(member.name.split('/')[1:])
                    if member.name:
                        source.extract(member, directory, filter='data')
        disabled = ['--disable-native-compiler', '--disable-ocamldoc', '--disable-debugger',
                    '--disable-ocamltest', '--disable-debug-runtime', '--disable-instrumented-runtime']
        run('sh', './configure', '--prefix=/ocaml', *disabled, cwd=native)
        run('make', f'-j{max(1,args.jobs)}', 'coldstart', cwd=native)
        run('make', f'-j{max(1,args.jobs)}', 'ocamlc', 'library', cwd=native)
        run('make', '-C', 'tools', 'stripdebug', cwd=native)
        run('patch', '-p1', '-i', ROOT / 'src/wasi-runtime.patch', cwd=target)
        cc = str(sdk / 'bin/clang')
        run('sh', './configure', '--prefix=/ocaml', '--host=wasm32-unknown-wasi', *disabled,
            '--disable-shared', '--disable-systhreads', '--disable-unix-lib',
            f'CC={cc} -mllvm -wasm-enable-sjlj -mexception-handling -mllvm -wasm-use-legacy-eh=false',
            f'AR={sdk / "bin/llvm-ar"}', f'RANLIB={sdk / "bin/llvm-ranlib"}',
            'LIBS=-lsetjmp',
            f'CFLAGS=-O1 -D_WASI_EMULATED_SIGNAL -D_WASI_EMULATED_PROCESS_CLOCKS -ffile-prefix-map={ROOT.parent}=/wisp-buildtools', cwd=target)
        header = target / 'runtime/caml/s.h'
        header.write_text(header.read_text().replace('#define HAS_SYSTEM 1', '/* #undef HAS_SYSTEM */'))
        makefile = target / 'runtime/Makefile'
        makefile.write_text(makefile.read_text().replace('$(shell ./sak$(EXE) encode-C-literal',
                           f'$(shell {native / "runtime/sak"} encode-C-literal'))
        run('make', '-C', 'runtime', f'-j{max(1,args.jobs)}', 'libcamlrun.a', 'prims.o', cwd=target)
        run(sdk / 'bin/clang', '-Wl,-z,stack-size=16777216', '-Wl,--strip-debug',
            '-o', 'runtime/ocamlrun', 'runtime/main.b.o', 'runtime/prims.o',
            'runtime/libcamlrun.a', '-lm', '-lsetjmp', '-lwasi-emulated-signal',
            '-lwasi-emulated-process-clocks', cwd=target)
    host = args.host_tools.resolve() if args.host_tools else native / 'runtime'
    with tempfile.TemporaryDirectory(prefix='.ocaml-package-', dir=ROOT) as temporary:
        dist = Path(temporary) / 'dist'
        for directory in ('bin', 'lib/caml', 'sysroot/lib', 'src', 'licenses'):
            (dist / directory).mkdir(parents=True, exist_ok=True)
        run(host / 'ocamlrun', native / 'tools/stripdebug', native / 'ocamlc', dist / 'bin/ocamlc.byte')
        bytecode = dist / 'bin/ocamlc.byte'
        contents = bytecode.read_bytes()
        if contents.startswith(b'#!'):
            bytecode.write_bytes(contents.split(b'\n', 1)[1])
        shutil.copy2(target / 'runtime/ocamlrun', dist / 'bin/ocamlrun.wasm')
        shutil.copy2(ROOT.parent / LOCK['cBackend']['provider'], dist / 'bin/c-backend.wasm')
        shutil.copy2(target / 'runtime/libcamlrun.a', dist / 'lib/libcamlrun.a')
        run(sdk / 'bin/llvm-strip', '--strip-debug', dist / 'lib/libcamlrun.a')
        for pattern in ('*.h', 'domain_state.tbl'):
            for path in (target / 'runtime/caml').glob(pattern):
                shutil.copy2(path, dist / 'lib/caml' / path.name)
        for pattern in ('*.cmi', '*.cma', '*.mli'):
            for path in (native / 'stdlib').glob(pattern):
                shutil.copy2(path, dist / 'lib' / path.name)
        for name in ('command.c', 'reactor.c'):
            shutil.copy2(ROOT / 'src' / name, dist / 'src' / name)
        shutil.copytree(sdk / 'share/wasi-sysroot/include/wasm32-wasip1', dist / 'sysroot/include',
                        ignore=shutil.ignore_patterns('c++'))
        for name in ('crt1-command.o', 'crt1-reactor.o', 'libc.a', 'libm.a', 'libsetjmp.a',
                     'libwasi-emulated-signal.a', 'libwasi-emulated-process-clocks.a'):
            shutil.copy2(sdk / 'share/wasi-sysroot/lib/wasm32-wasip1' / name, dist / 'sysroot/lib' / name)
        shutil.copy2(sdk / 'lib/clang/23/lib/wasm32-unknown-wasip1/libclang_rt.builtins.a',
                     dist / 'sysroot/lib/libclang_rt.builtins.a')
        shutil.copy2(native / 'LICENSE', dist / 'licenses/OCaml-LICENSE')
        for path in (ROOT.parent / 'nim/licenses').iterdir():
            if path.is_file():
                shutil.copy2(path, dist / 'licenses' / path.name)
        inputs = {str(p.relative_to(ROOT)): digest(p) for p in sorted((ROOT / 'src').rglob('*')) if p.is_file()}
        record = {'schema': 1, 'language': 'ocaml', 'version': LOCK['ocaml']['version'],
                  'compilerHost': 'wasm32-wasip1', 'fullyWasmHostedBuild': True,
                  'bootstrap': LOCK, 'bootstrapInputs': inputs,
                  'sha256': {str(p.relative_to(dist)): digest(p) for p in sorted(dist.rglob('*')) if p.is_file()}}
        (dist / 'manifest.json').write_text(json.dumps(record, indent=2) + '\n')
        previous = Path(temporary) / 'previous'
        if (ROOT / 'dist').exists():
            (ROOT / 'dist').rename(previous)
        try:
            dist.rename(ROOT / 'dist')
        except BaseException:
            if previous.exists():
                previous.rename(ROOT / 'dist')
            raise
    print(f'Packaged OCaml {LOCK["ocaml"]["version"]} at {ROOT / "dist"}')


if __name__ == '__main__':
    main()
