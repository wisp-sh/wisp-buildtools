#!/usr/bin/env python3
"""Combine a hash-pinned Wisp native kit and compiler release into a CI runtime."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess

from runtime_format import download, extract_runtime, fail, json_bytes, strict_json, validate_catalog
from package_runtime import package

REQUIRED = {'clang', 'go', 'rust', 'llvm', 'wasm-tools'}


def compiler_bundles(value):
    if not isinstance(value, dict) or value.get('schema') != 2 or value.get('platform') != 'linux-x86_64':
        fail('expected a schema 2 compiler release manifest')
    entries = value.get('bundles')
    if not isinstance(entries, list) or not 1 <= len(entries) <= 64:
        fail('invalid compiler release bundle list')
    by_tool = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('tool'), str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', entry['tool']):
            fail('invalid tool name')
        if entry['tool'] in by_tool:
            fail('duplicate compiler release bundle')
        if entry.get('platform') not in ('wasm32-wasi', 'linux-x86_64'):
            fail('library runtimes require Linux x86_64 compatible compiler bundles')
        if not isinstance(entry.get('version'), str) or not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', entry['version']):
            fail('invalid compiler version')
        if type(entry.get('size')) is not int or not 0 < entry['size'] <= 2 * 1024**3:
            fail('invalid compiler size')
        requirements = entry.get('requires')
        if not isinstance(requirements, list) or any(not isinstance(name, str) or not re.fullmatch(r'wisp:[a-z][a-z0-9-]{0,63}', name) for name in requirements):
            fail('invalid shared compiler requirements')
        by_tool[entry['tool']] = entry
    selected = set(REQUIRED)
    pending = list(selected)
    while pending:
        tool = pending.pop()
        if tool not in by_tool:
            fail(f'compiler release is missing {tool}')
        for name in by_tool[tool]['requires']:
            dependency = name.removeprefix('wisp:')
            if dependency not in selected:
                selected.add(dependency)
                pending.append(dependency)
    return [by_tool[tool] for tool in sorted(selected)]


def compiler_environment(text, prefix):
    exports = {}
    for line in text.splitlines():
        tokens = shlex.split(line)
        if len(tokens) != 2 or tokens[0] != 'export' or '=' not in tokens[1]:
            fail('unexpected toolchain environment output')
        key, path = tokens[1].split('=', 1)
        if not re.fullmatch(r'WISP_[A-Z0-9_]+', key) or '..' in Path(path).parts or not Path(path).is_relative_to(prefix):
            fail('toolchain environment escapes the runtime')
        exports[key] = Path(path).relative_to(prefix).as_posix()
    return exports


def npm_environment(prepared, exports):
    """Older native kits omitted the npm archives used to authenticate JCO."""
    exports = dict(exports)
    if 'WISP_NPM_CACHE' not in exports:
        cache = prepared / 'npm-cache'
        environment = dict(os.environ)
        environment['PATH'] = str(prepared / 'node/bin') + os.pathsep + environment.get('PATH', '')
        subprocess.run([prepared / 'node/bin/npm', 'ci', '--prefix', prepared / 'js',
                        '--cache', cache, '--ignore-scripts', '--no-audit', '--no-fund'],
                       env=environment, check=True)
        if not (cache / '_cacache').is_dir():
            fail('npm setup did not retain its authenticated archive cache')
        exports['WISP_NPM_CACHE'] = 'npm-cache'
    return exports


def assemble(args):
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    native_file = download(args.native_manifest_url, args.native_manifest_sha256, work / 'native.json', limit=65536)
    native = validate_catalog(strict_json(native_file.read_bytes()))
    compiler_file = download(args.compiler_manifest_url, args.compiler_manifest_sha256, work / 'compilers.json', limit=65536)
    bundles = compiler_bundles(strict_json(compiler_file.read_bytes()))
    spec = native['runtime']
    archive = download(spec['url'], spec['sha256'], work / 'native.tar.gz', size=spec['size'])
    prepared = work / 'prepared'
    prepared.mkdir()
    extract_runtime(archive, prepared)
    archive.unlink()
    native['env'] = npm_environment(prepared, native['env'])
    cli = prepared / 'bin/wisp'
    for entry in bundles:
        archive = download(entry['url'], entry['sha256'], work / (entry['tool'] + '.wisp.zip'), size=entry['size'])
        # Check declared identity before installation, even when the hash matches.
        record = json.loads(subprocess.check_output([cli, 'toolchain', 'validate', archive], text=True))
        if record.get('tool') != entry['tool'] or record.get('version') != entry['version'] or record.get('platform') != entry['platform']:
            fail('compiler bundle differs from its manifest identity')
        subprocess.run([cli, 'toolchain', 'install', archive, '--prefix', prepared / 'toolchains', '--sha256', entry['sha256']], check=True)
        archive.unlink()
    report = json.loads(subprocess.check_output([cli, 'toolchain', 'doctor', '--prefix', prepared / 'toolchains'], text=True))
    if report.get('valid') is not True or report.get('hosts', {}).get('available') is not True or report.get('adapter', {}).get('available') is not True:
        fail('assembled runtime failed compiler, host or adapter checks')
    text = subprocess.check_output([cli, 'toolchain', 'env', '--prefix', prepared / 'toolchains'], text=True)
    exports = {**native['env'], **compiler_environment(text, prepared)}
    (prepared / 'release-inputs.json').write_bytes(json_bytes({
        'nativeManifest': {'url': args.native_manifest_url, 'sha256': args.native_manifest_sha256},
        'compilerManifest': {'url': args.compiler_manifest_url, 'sha256': args.compiler_manifest_sha256},
    }))
    args.manifest.write_bytes(json_bytes(package(prepared, args.archive, exports, args.url)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('native', 'compiler'):
        parser.add_argument(f'--{name}-manifest-url', required=True)
        parser.add_argument(f'--{name}-manifest-sha256', required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--url', required=True)
    assemble(parser.parse_args())


if __name__ == '__main__':
    main()
