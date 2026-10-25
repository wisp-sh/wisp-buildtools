#!/usr/bin/env python3
"""Bootstrap Roslyn/Mono compiler guests and a fixed IL-interpreting reactor."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from toolchain import ROOT, SOURCES, digest, verified_dist

CACHE = ROOT / '.toolchains'


def fetch(spec, offline):
    destination = CACHE / 'downloads' / spec['url'].rsplit('/', 1)[-1]
    destination.parent.mkdir(parents=True, exist_ok=True)
    def valid():
        return destination.is_file() and destination.stat().st_size == spec['bytes'] and digest(destination) == spec['sha256']
    if not valid():
        if offline:
            raise ValueError(f"offline bootstrap needs {destination.name}")
        pending = destination.with_suffix(destination.suffix + '.tmp')
        try:
            with urllib.request.urlopen(spec['url'], timeout=90) as source, pending.open('wb') as output:
                total = 0
                while chunk := source.read(1024 * 1024):
                    total += len(chunk)
                    if total > spec['bytes']:
                        raise ValueError('download exceeds pinned size')
                    output.write(chunk)
            if pending.stat().st_size != spec['bytes'] or digest(pending) != spec['sha256']:
                raise ValueError(f"download checksum mismatch: {destination.name}")
            pending.replace(destination)
        finally:
            pending.unlink(missing_ok=True)
    return destination


def unpack(spec, name, offline):
    archive = fetch(spec, offline)
    target = CACHE / name
    stamp = target / '.archive-sha256'
    if not stamp.is_file() or stamp.read_text() != spec['sha256']:
        if target.exists():
            shutil.rmtree(target)
        target.mkdir()
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as bundle:
                for item in bundle.infolist():
                    if not (target / item.filename).resolve().is_relative_to(target):
                        raise ValueError('archive member escapes extraction directory')
                bundle.extractall(target)
        else:
            with tarfile.open(archive) as bundle:
                bundle.extractall(target, filter='data')
        stamp.write_text(spec['sha256'])
    return target


def native(command, environment):
    result = subprocess.run([str(x) for x in command], env=environment,
                            capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise ValueError((result.stdout + result.stderr).strip())


def csc(sdk, source, output, refs, environment, *, library=False, unsafe=False):
    native([sdk / 'dotnet', sdk / 'sdk/8.0.100/Roslyn/bincore/csc.dll', '-nologo',
            '-target:library' if library else '-target:exe', '-unsafe+' if unsafe else '-unsafe-', '-deterministic+', '-parallel-', '-nostdlib+', f'-out:{output}',
            *[f'-r:{p}' for p in refs], *[ROOT / p for p in ([source] if isinstance(source, str) else source)]], environment)


def build(args):
    # At most 3 GiB of disk growth and 4 GiB process memory are budgeted here.
    for cmd in (['free', '-h'], ['df', '-h', str(ROOT)], ['df', '-i', str(ROOT)],
                ['findmnt', '-T', str(CACHE)], ['ps', '-eo', 'pid,rss,comm']):
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL if cmd[0] == 'ps' else None)
    if shutil.disk_usage(ROOT).free < 8 * 1024**3:
        raise ValueError('bootstrap needs 3 GiB growth plus the 5 GiB disk reserve')
    available = next(int(l.split()[1]) * 1024 for l in Path('/proc/meminfo').read_text().splitlines() if l.startswith('MemAvailable:'))
    if available < 7 * 1024**3:
        raise ValueError('bootstrap needs 4 GiB memory budget plus 3 GiB host headroom')
    lock = json.loads((ROOT / 'toolchain.lock.json').read_text())
    sdk = unpack(lock['artifacts']['dotnet-sdk'], 'dotnet', args.offline)
    runtime = unpack(lock['artifacts']['mono-wasi'], 'mono-wasi', args.offline)
    tasks = unpack(lock['artifacts']['wasi-sdk-pack'], 'wasi-sdk-pack', args.offline)
    browser = unpack(lock['artifacts']['browser-crypto'], 'browser-crypto', args.offline)
    if args.wasi_sdk:
        wasi = args.wasi_sdk.resolve(strict=True)
    else:
        wasi = unpack(lock['artifacts']['wasi-sdk'], 'wasi-sdk', args.offline) / 'wasi-sdk-34.0-x86_64-linux'
    clang = wasi / 'bin/clang'
    scratch = CACHE / 'scratch'
    scratch.mkdir(exist_ok=True)
    environment = {**os.environ, 'TMPDIR': str(scratch), 'DOTNET_PROCESSOR_COUNT': '1',
                   'DOTNET_CLI_TELEMETRY_OPTOUT': '1', 'DOTNET_SKIP_FIRST_TIME_EXPERIENCE': '1',
                   'DOTNET_CLI_HOME': str(CACHE / 'cli-home')}
    n = runtime / 'runtimes/wasi-wasm/native'
    refdir = sdk / 'packs/Microsoft.NETCore.App.Ref/8.0.0/ref/net8.0'
    refs = sorted(refdir.glob('*.dll'))
    with tempfile.TemporaryDirectory(prefix='bootstrap-', dir=CACHE) as directory:
        work = Path(directory)
        dist = work / 'dist'
        managed = dist / 'compiler/managed'
        managed.mkdir(parents=True)
        support = work / 'Wisp.RuntimeSupport.dll'
        csc(sdk, 'src/Canonical.cs', support, refs, environment, library=True, unsafe=True)
        bcl = sorted((runtime / 'runtimes/wasi-wasm/lib/net8.0').glob('*.dll')) + [n / 'System.Private.CoreLib.dll', support]
        for p in bcl:
            shutil.copyfile(p, managed / p.name)
        shutil.copyfile(browser / 'runtimes/browser-wasm/lib/net8.0/System.Security.Cryptography.dll', managed / 'System.Security.Cryptography.dll')
        for name in ['Microsoft.CodeAnalysis.dll', 'Microsoft.CodeAnalysis.CSharp.dll']:
            shutil.copyfile(sdk / 'sdk/8.0.100/Roslyn/bincore' / name, managed / name)
        csc(sdk, ['src/Compiler.cs', 'src/Bindings.cs', 'src/WasmModule.cs'], managed / 'Compiler.dll', refs + [support] + [managed / name for name in
            ['Microsoft.CodeAnalysis.dll', 'Microsoft.CodeAnalysis.CSharp.dll']], environment)
        shutil.copyfile(n / 'dotnet.wasm', dist / 'compiler/dotnet.wasm')
        shutil.copyfile(n / 'icudt.dat', dist / 'compiler/icudt.dat')
        shutil.copytree(refdir, dist / 'references')
        shutil.copyfile(support, dist / 'references/Wisp.RuntimeSupport.dll')
        helper = work / 'helper'
        helper.mkdir()
        for p in (tasks / 'tasks/net8.0').glob('*.dll'):
            shutil.copyfile(p, helper / p.name)
        for name in ['Microsoft.Build.Framework.dll', 'Microsoft.Build.Utilities.Core.dll', 'Microsoft.NET.StringTools.dll']:
            shutil.copyfile(sdk / 'sdk/8.0.100' / name, helper / name)
        csc(sdk, 'src/Generate.cs', helper / 'Generate.dll', refs + [helper / name for name in
            ['WasmAppBuilder.dll', 'Microsoft.Build.Framework.dll', 'Microsoft.Build.Utilities.Core.dll']], environment)
        (helper / 'Generate.runtimeconfig.json').write_text(json.dumps({'runtimeOptions': {
            'tfm': 'net8.0', 'framework': {'name': 'Microsoft.NETCore.App', 'version': '8.0.0'}}}))
        # Scan only the fixed runtime, never application IL, during bootstrap.
        original = work / 'bcl'
        original.mkdir()
        for p in bcl:
            shutil.copyfile(p, original / p.name)
        native([sdk / 'dotnet', helper / 'Generate.dll', original, work], environment)
        assembly, header, registrations = [], [], []
        for i, p in enumerate(bcl):
            symbol = f'bundle_{i}'
            assembly += [f'.section .rodata.{symbol},"",@', f'.globl {symbol}',
                         f'.type {symbol},@object', f'{symbol}:', f'.incbin "{p}"', f'.size {symbol}, .-{symbol}']
            header.append(f'extern const unsigned char {symbol}[];')
            registrations.append(f'mono_wasm_add_assembly("{p.name}", {symbol}, {p.stat().st_size});')
        (work / 'bundle.s').write_text('\n'.join(assembly) + '\n')
        (work / 'bundle.h').write_text('\n'.join(header) + '\nstatic void register_bundled_assemblies(void) {\n' + '\n'.join(registrations) + '\n}\n')
        native([clang, '-c', work / 'bundle.s', '-o', work / 'bundle.o'], environment)
        libraries = ['libmonosgen-2.0.a', 'libmono-ee-interp.a', 'libmono-icall-table.a', 'libmono-wasm-nosimd.a',
                     'libmono-component-hot_reload-stub-static.a', 'libmono-component-debugger-stub-static.a',
                     'libmono-component-diagnostics_tracing-stub-static.a', 'libmono-component-marshal-ilgen-static.a',
                     'libSystem.Native.a', 'libSystem.IO.Compression.Native.a']
        native([clang, '-Oz', f'-ffile-prefix-map={ROOT.parent}=/wisp-buildtools',
                f'-ffile-prefix-map={wasi}=/wasi-sdk', f'-ffile-prefix-map={CACHE}=/bootstrap',
                '-mexec-model=reactor', '-DGEN_PINVOKE=1', '-DINVARIANT_GLOBALIZATION',
                '-DINVARIANT_TIMEZONE', '-D_WASI_EMULATED_SIGNAL', '-D_WASI_EMULATED_MMAN', '-D_WASI_EMULATED_PROCESS_CLOCKS',
                f'-I{work}', f'-I{n}/include', f'-I{n}/include/mono-2.0', f'-I{n}/include/wasm', ROOT / 'src/runtime.c',
                *[n / 'src' / name for name in ['driver.c', 'pinvoke.c', 'stubs.c', 'synthetic-pthread.c']],
                work / 'bundle.o', *[n / name for name in libraries], '-lc++', '-lc++abi',
                '-lwasi-emulated-process-clocks', '-lwasi-emulated-signal', '-lwasi-emulated-mman',
                '-Wl,-z,stack-size=8388608', '-Wl,--initial-memory=52428800', '-Wl,--no-stack-first',
                '-Wl,--strip-all', '-Wl,--threads=1', '-o', dist / 'runtime-template.wasm'], environment)
        licenses = dist / 'licenses'
        licenses.mkdir()
        for relative, spec in lock['licenses'].items():
            p = ROOT / 'licenses' / relative
            if p.stat().st_size != spec['bytes'] or digest(p) != spec['sha256']:
                raise ValueError(f'license checksum mismatch: {relative}')
            destination = licenses / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, destination)
        for p, name in [(sdk / 'LICENSE.txt', 'dotnet-LICENSE.txt'), (sdk / 'ThirdPartyNotices.txt', 'dotnet-NOTICES.txt'),
                        (runtime / 'LICENSE.TXT', 'mono-LICENSE.txt'), (runtime / 'THIRD-PARTY-NOTICES.TXT', 'mono-NOTICES.txt'),
                        (browser / 'LICENSE.TXT', 'browser-LICENSE.txt'), (browser / 'THIRD-PARTY-NOTICES.TXT', 'browser-NOTICES.txt')]:
            shutil.copyfile(p, licenses / name)
        # Managed PE assemblies are interpreter data, like JVM JAR contents.
        # Keep them in a ZIP so portable packages contain no loose PE binaries.
        assemblies = {}
        with zipfile.ZipFile(dist / 'assemblies.zip', 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for directory, prefix in [(managed, 'managed'), (dist / 'references', 'references')]:
                for p in sorted(directory.glob('*.dll')):
                    name = prefix + '/' + p.name
                    archive.writestr(zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)), p.read_bytes(),
                                     compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
                    assemblies[name] = {'bytes': p.stat().st_size, 'sha256': digest(p)}
                shutil.rmtree(directory)
        manifest = {'schemaVersion': 1, 'toolchain': 'csharp', 'version': 'roslyn-4.8.0-mono-8.0.0-bindings-v2',
                    'upstream': lock, 'sourceCompilationWasmHosted': True, 'fullyWasmHostedBuild': True,
                    'bootstrapInputs': {p: digest(ROOT / p) for p in SOURCES},
                    'bootstrapClangSha256': digest(clang),
                    'assemblies': assemblies, 'runtimeAssemblies': [p.stem for p in bcl],
                    'payloadFormat': 2,
                    'sha256': {p.relative_to(dist).as_posix(): digest(p) for p in sorted(dist.rglob('*')) if p.is_file()}}
        (dist / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        previous = ROOT / '.toolchains/previous-dist'
        if previous.exists():
            shutil.rmtree(previous)
        if (ROOT / 'dist').exists():
            (ROOT / 'dist').rename(previous)
        dist.rename(ROOT / 'dist')
        if previous.exists():
            shutil.rmtree(previous)
    verified_dist()
    print('C# compiler and runtime guests bootstrapped; native .NET is bootstrap-only')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--rebuild', action='store_true')
    parser.add_argument('--wasi-sdk', type=Path, default=os.environ.get('WISP_CSHARP_WASI_SDK'))
    args = parser.parse_args()
    if args.check and args.rebuild:
        parser.error('--check and --rebuild cannot be combined')
    CACHE.mkdir(exist_ok=True)
    if not args.rebuild:
        try:
            verified_dist()
            print('C# distribution integrity verified')
            return
        except (OSError, ValueError):
            if args.check:
                raise
    build(args)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError, zipfile.BadZipFile) as error:
        raise SystemExit(f'csharp bootstrap: {error}') from error
