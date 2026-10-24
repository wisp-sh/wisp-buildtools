#!/usr/bin/env python3
"""Package the pinned Kotlin compiler bytecode for the shared WASI JVM."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / '.cache'


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inputs():
    return {name: digest(ROOT / name) for name in ['bootstrap.py', 'compile.py', 'toolchain.lock.json', 'src/PerformancePatch.java']}


def fetch(item, path, offline):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.stat().st_size == item['bytes'] and digest(path) == item['sha256']:
        return path
    if offline:
        raise ValueError(f'missing or changed bootstrap input: {path}')
    with urllib.request.urlopen(item['url'], timeout=120) as response:
        data = response.read(item['bytes'] + 1)
    if len(data) != item['bytes'] or hashlib.sha256(data).hexdigest() != item['sha256']:
        raise ValueError(f'bootstrap input checksum mismatch: {path.name}')
    temporary = path.with_name(path.name + '.downloading')
    temporary.write_bytes(data)
    temporary.replace(path)
    return path


def package_jar(source, destination, patched=None):
    """Remove native helpers and apply the recorded optional-profiler guard."""
    removed = []
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        for name in sorted(original.namelist()):
            if name.endswith('/'):
                continue
            if name.lower().endswith(('.so', '.dll', '.dylib', '.jnilib', '.exe')):
                removed.append(name)
                continue
            data = original.read(name)
            if patched is not None and name == 'org/jetbrains/kotlin/util/PerformanceManager.class':
                data = patched.read_bytes()
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            output.writestr(info, data)
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(destination) as packaged:
        for name in original.namelist():
            if name.endswith('.class') and packaged.read(name) != (patched.read_bytes() if patched is not None and name == 'org/jetbrains/kotlin/util/PerformanceManager.class' else original.read(name)):
                raise ValueError(f'compiler bytecode changed while packaging: {name}')
    return removed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if args.check:
        specification = importlib.util.spec_from_file_location('kotlin_compile_integrity', ROOT / 'compile.py')
        helper = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(helper)
        _, manifest = helper.verified_dist()
        if manifest.get('bootstrapInputs') != inputs():
            raise ValueError('Kotlin bootstrap inputs changed; rebuild the distribution')
        print('Kotlin distribution integrity verified')
        return
    lock = json.loads((ROOT / 'toolchain.lock.json').read_text())
    jars = {item['path']: fetch(item, CACHE / ('kotlin-' + lock['version']) / item['path'], args.offline)
            for item in lock['jars']}
    licenses = {item['name']: fetch(item, CACHE / 'licenses' / item['name'], args.offline)
                for item in lock['licenses']}
    with tempfile.TemporaryDirectory(prefix='.bootstrap-', dir=ROOT) as temporary:
        work = Path(temporary)
        compiler = jars[f'kotlin-compiler-embeddable-{lock["version"]}.jar']
        original, patched = work / 'PerformanceManager.class', work / 'PerformanceManager.patched.class'
        with zipfile.ZipFile(compiler) as archive:
            original.write_bytes(archive.read('org/jetbrains/kotlin/util/PerformanceManager.class'))
        java = os.environ.get('WISP_BOOTSTRAP_JAVA', 'java')
        subprocess.run([java, 'com.sun.tools.javac.Main', '-cp', str(compiler), '-d', str(work),
                        str(ROOT / 'src/PerformancePatch.java')], check=True)
        subprocess.run([java, '-cp', str(work) + os.pathsep + os.pathsep.join(map(str, jars.values())),
                        'wisp.bootstrap.PerformancePatch', str(original), str(patched)], check=True)
        dist = work / 'dist'
        (dist / 'jars').mkdir(parents=True)
        removed = {}
        for name, path in jars.items():
            removed[name] = package_jar(path, dist / 'jars' / name, patched if path == compiler else None)
        for name, path in licenses.items():
            target = dist / 'licenses' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        (dist / 'NOTICE.txt').write_text(
            'Kotlin compiler bytecode and libraries: JetBrains, Apache-2.0.\n'
            'Coroutines: Kotlin contributors, Apache-2.0. Trove4j: LGPL-2.1.\n'
            'Upstream notices and third-party license texts are in licenses/.\n'
            'Optional native resources were removed. The performance manager skips CPU-profiling setup when detailed profiling is disabled.\n'
            'Exact artifact sources, checksums and licenses are pinned in toolchain.lock.json.\n')
        manifest = {'schemaVersion': 1, 'toolchain': 'kotlin', 'version': lock['version'],
                    'upstream': lock, 'bootstrapInputs': inputs(), 'jvmTarget': lock['jvmTarget'],
                    'compilerMain': 'org.jetbrains.kotlin.cli.jvm.K2JVMCompiler',
                    'runtimeDependencies': ['wisp:jvm', 'wisp:teavm'],
                    'sourceCompilationWasmHosted': True, 'nodeRequired': False,
                    'nativeResourcesRemoved': removed,
                    'bytecodeTransformations': [{'class': 'org/jetbrains/kotlin/util/PerformanceManager',
                        'source': 'src/PerformancePatch.java', 'originalSha256': digest(original),
                        'patchedSha256': digest(patched), 'reason': 'guard optional CPU profiling setup'}],
                    'sha256': {str(p.relative_to(dist)): digest(p) for p in sorted(dist.rglob('*')) if p.is_file()}}
        (dist / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        target, previous = ROOT / 'dist', ROOT / '.dist.previous'
        if previous.exists():
            shutil.rmtree(previous)
        if target.exists():
            target.rename(previous)
        try:
            dist.rename(target)
        except BaseException:
            if previous.exists():
                previous.rename(target)
            raise
        if previous.exists():
            shutil.rmtree(previous)
    print(f'Kotlin {lock["version"]}: {sum(p.stat().st_size for p in (ROOT / "dist").rglob("*") if p.is_file())} bytes')


if __name__ == '__main__':
    main()
