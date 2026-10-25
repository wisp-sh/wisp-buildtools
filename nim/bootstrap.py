#!/usr/bin/env python3
"""Build the WASI Nim frontend and package its external LLVM consumer."""
from __future__ import annotations
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parent
LOCK = json.loads((ROOT / 'toolchain.lock.json').read_text())
NIM_VERSION = LOCK['nim']['version']
NIM_URL = LOCK['nim']['url']
NIM_SHA256 = LOCK['nim']['sha256']

def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def verified_package(dist: Path) -> bool:
    try:
        manifest = json.loads((dist / 'manifest.json').read_text())
        files = manifest['sha256']
        required = {'bin/nim.wasm', 'bin/c-backend.wasm', 'lib/system.nim', 'sysroot/lib/crt1-reactor.o'}
        if manifest['schema'] != 1 or manifest['version'] != NIM_VERSION or manifest['bootstrap'] != LOCK or manifest.get('llvmLinkage') != 'shared-everything-core-module-imports' or manifest.get('clangResources', {}).get('provider') != 'clang' or not required.issubset(files):
            return False
        for name, sha in files.items():
            path = dist / name
            if not path.resolve().is_relative_to(dist.resolve()) or not path.is_file() or digest(path) != sha:
                return False
        return True
    except (OSError, KeyError, TypeError, ValueError):
        return False

def package(dist: Path, nim: Path, sdk: Path, llvm: Path, work: Path, backend: Path) -> None:
    (dist / 'bin').mkdir(parents=True)
    shutil.copy2(work / 'nim.wasm', dist / 'bin/nim.wasm')
    shutil.copy2(backend, dist / 'bin/c-backend.wasm')
    shutil.copytree(nim / 'lib', dist / 'lib', ignore=shutil.ignore_patterns('*.exe', '*.so', '*.a', '*.o'))
    include = dist / 'sysroot/include'
    shutil.copytree(sdk / 'share/wasi-sysroot/include/wasm32-wasip1', include, ignore=shutil.ignore_patterns('c++'))
    clang_resources = sdk / 'lib/clang/23'
    libraries = dist / 'sysroot/lib'
    libraries.mkdir(parents=True)
    for name in ('crt1-reactor.o', 'libc.a', 'libm.a', 'libdl.a', 'libwasi-emulated-signal.a', 'libwasi-emulated-process-clocks.a', 'libwasi-emulated-getpid.a'):
        shutil.copy2(sdk / 'share/wasi-sysroot/lib/wasm32-wasip1' / name, libraries / name)
    shutil.copy2(clang_resources / 'lib/wasm32-unknown-wasip1/libclang_rt.builtins.a', libraries / 'libclang_rt.builtins.a')
    shutil.copytree(ROOT / 'src/compat', dist / 'compat')
    shutil.copy2(ROOT / 'src/compat.h', dist / 'compat.h')
    shutil.copy2(ROOT / 'src/unsupported_processes.c', dist / 'unsupported_processes.c')
    shutil.copytree(ROOT / 'licenses', dist / 'licenses')
    shutil.copy2(nim / 'copying.txt', dist / 'licenses/Nim.txt')
    for name in ('LICENSE', 'LICENSE.TXT'):
        if (sdk / name).exists(): shutil.copy2(sdk / name, dist / 'licenses' / f'wasi-sdk-{name}')
    for item in (llvm / 'licenses').iterdir():
        if item.is_file(): shutil.copy2(item, dist / 'licenses' / item.name)
    files = {str(p.relative_to(dist)): digest(p) for p in sorted(dist.rglob('*')) if p.is_file()}
    manifest = {'schema': 1, 'language': 'nim', 'version': NIM_VERSION, 'compiler_host': 'wasm32-wasip1', 'c_backend_host': 'wasm32-wasip3', 'output': 'wasm32-wasip1 core reactor', 'bootstrap': LOCK, 'sha256': files}
    manifest['fullyWasmHostedBuild'] = True
    manifest['llvmLinkage'] = 'shared-everything-core-module-imports'
    manifest['clangResources'] = {'provider': 'clang', 'sha256': {str(path.relative_to(clang_resources / 'include')): digest(path) for path in sorted((clang_resources / 'include').rglob('*')) if path.is_file()}}
    (dist / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'Packaged {len(files)} files')

def run(args: list[str | Path], **kwargs) -> None:
    subprocess.run([str(a) for a in args], check=True, **kwargs)

def portable_diagnostics(nim: Path) -> None:
    # These compile-time tuples describe the compiler's own implementation,
    # rather than user source. Keep filenames without baking in the builder.
    patches = {
        'compiler/msgs.nim': ('instantiationInfo(-2, fullPaths = true)', 'instantiationInfo(-2, fullPaths = false)'),
        'compiler/vm.nim': ('instantiationInfo(-2, fullPaths = true)', 'instantiationInfo(-2, fullPaths = false)'),
    }
    for relative, (before, after) in patches.items():
        path = nim / relative
        text = path.read_text()
        if after in text and before not in text:
            continue
        if text.count(before) != 1:
            raise ValueError(f'Nim diagnostic path patch no longer applies: {relative}')
        path.write_text(text.replace(before, after))

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wasi-sdk', type=Path, default=Path(os.environ.get('WASI_SDK_PATH', ROOT.parent / 'clang/.toolchains/wasi-sdk-34.0-x86_64-linux')))
    parser.add_argument('--llvm-sdk', type=Path, default=Path(os.environ.get('WISP_LLVM_SDK', ROOT.parent / 'llvm/artifacts/llvm-24-d297af3-wasip3-o1')))
    parser.add_argument('--llvm-shared', type=Path, default=Path(os.environ.get('WISP_LLVM', ROOT.parent / 'llvm/dist/shared')))
    parser.add_argument('--shared-backend', type=Path, default=ROOT / 'work/backend-build/c-backend')
    parser.add_argument('--jobs', type=int, default=2)
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--package-only', action='store_true', help='package an existing work/nim.wasm and work/backend-build/c-backend')
    args = parser.parse_args()
    sdk, llvm = args.wasi_sdk.resolve(), args.llvm_sdk.resolve()
    shared, backend = args.llvm_shared.resolve(), args.shared_backend.resolve()
    dist, work, tools = ROOT / 'dist', ROOT / 'work', ROOT / '.toolchains'
    if (dist / 'manifest.json').exists() and not args.force and not args.package_only:
        if verified_package(dist):
            print('Nim WASI toolchain is ready.')
            return
    for path in (work, tools): path.mkdir(exist_ok=True)
    archive = tools / f'nim-{NIM_VERSION}-linux_x64.tar.xz'
    if not archive.exists():
        with tempfile.NamedTemporaryFile(prefix='nim-download-', dir=tools, delete=False) as download:
            downloaded = Path(download.name)
        try:
            run(['curl', '--fail', '--location', '--retry', '3', '--user-agent', 'Mozilla/5.0', NIM_URL, '-o', downloaded])
            if digest(downloaded) != NIM_SHA256:
                raise SystemExit('SHA-256 mismatch for the downloaded Nim archive.')
            os.replace(downloaded, archive)
        finally:
            downloaded.unlink(missing_ok=True)
    if digest(archive) != NIM_SHA256:
        raise SystemExit(f'SHA-256 mismatch: {archive}')
    nim = tools / f'nim-{NIM_VERSION}'
    if not nim.exists():
        with tarfile.open(archive) as source:
            source.extractall(tools, filter='data')
    if not args.package_only:
        portable_diagnostics(nim)
        cc = sdk / 'bin/clang'
        if not cc.is_file(): raise SystemExit(f'WASI SDK compiler missing: {cc}')
        cache = work / 'nimcache'
        # WASI has no spawn.h. Keep osproc on the fork/exec path covered by
        # unsupported_processes.c rather than Nim's default POSIX spawn path.
        run([nim / 'bin/nim', 'c', '--compileOnly', '--genScript', '--forceBuild:on', '--cpu:wasm32', '--os:linux', '--cc:clang', '--threads:off', '--mm:orc', '-d:release', f'-d:gitHash={LOCK["nim"]["git_commit"]}', '-d:useMalloc', '-d:useFork', '-d:noSignalHandler', '-d:nimNoReadline', '-d:nimNoLinenoise', '--exceptions:goto', f'--nimcache:{cache}', nim / 'compiler/nim.nim'], cwd=ROOT)
        main_c = cache / '@mnim.nim.c'
        text = main_c.read_text().replace('int main(int argc, char** args, char** env) {', 'extern char **environ;\nint main(int argc, char** args) {\n  char **env = environ;')
        main_c.write_text(text)
        generated = json.loads((cache / 'nim.json').read_text())['compile']
        objects = [Path(entry[0]).with_suffix('.o') for entry in generated]
        common = [cc, '--target=wasm32-wasip1', '-O1', '-w', '-ferror-limit=8',
                  f'-ffile-prefix-map={Path.home()}=/build-user', f'-ffile-prefix-map={ROOT}=/wisp-buildtools/nim',
                  '-I' + str(ROOT / 'src/compat'), '-include', ROOT / 'src/compat.h', '-Dreadlink=nim_wasi_readlink', '-D_WASI_EMULATED_SIGNAL', '-D_WASI_EMULATED_PROCESS_CLOCKS', '-I' + str(nim / 'lib')]
        def compile_unit(pair: tuple[list, Path]) -> None:
            entry, output = pair
            run([*common, '-c', entry[0], '-o', output], cwd=ROOT)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(args.jobs, 2))) as pool:
            list(pool.map(compile_unit, zip(generated, objects)))
        support = work / 'unsupported_processes.o'
        run([cc, '--target=wasm32-wasip1', '-O1', '-c', ROOT / 'src/unsupported_processes.c', '-o', support])
        run([cc, '--target=wasm32-wasip1', '-Wl,-z,stack-size=16777216', *objects, support, '-lwasi-emulated-signal', '-lwasi-emulated-process-clocks', '-o', work / 'nim.wasm'])
    # Link the C++ backend against the one external LLVM provider, never archives
    # inside this package. Native tools run only while bootstrapping the provider.
    try:
        shared_manifest = json.loads((shared / 'manifest.json').read_text())
        backend_ready = digest(backend) == shared_manifest['consumers']['nim']['sha256']
    except (OSError, KeyError, TypeError, ValueError):
        backend_ready = False
    if not backend_ready:
        run([os.sys.executable, ROOT.parent / 'llvm/scripts/shared.py', 'build',
             '--wasi-sdk', sdk, '--llvm-sdk', llvm, '--out', shared,
             '--clang-output', ROOT.parent / 'clang/.toolchains/build-p3/bin/clang-wasm.component',
             '--nim-output', backend])
    # Build the complete package before replacing a previously working dist.
    with tempfile.TemporaryDirectory(prefix='.nim-package-', dir=ROOT) as directory:
        staging = Path(directory) / 'dist'
        package(staging, nim, sdk, llvm, work, backend)
        previous = Path(directory) / 'previous'
        if dist.exists(): dist.rename(previous)
        try:
            staging.rename(dist)
        except BaseException:
            if previous.exists(): previous.rename(dist)
            raise
    print(f'Toolchain ready at {dist}')

if __name__ == '__main__':
    main()
