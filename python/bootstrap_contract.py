#!/usr/bin/env python3
"""Provision a reusable Python compiler template for an authored WIT world.

This bootstrap reads contracts and generated bindings, never application source.
Automatic provisioning receives only WIT, never application source.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import bootstrap
from scripts.common import LOCK, MAX_COMPONENT_BYTES, ROOT, digest, run, tool, verified_package
from scripts.contracts import CONTROL, contract, generate_runtime, preload_modules, selected_world
from scripts.source_bundle import LOADER_VERSION
from scripts.wasm import read_uleb, section, sections, source_component, uleb


def public_template(component, allowed):
    result = component[:8]
    seen = set()
    for kind, payload in sections(component):
        if kind == 11:
            count, position = read_uleb(payload, 0)
            entries = []
            for _ in range(count):
                start = position
                if payload[position] != 0:
                    raise ValueError('qualified Component export unsupported')
                length, position = read_uleb(payload, position + 1)
                exported = payload[position:position + length].decode()
                position += length + 1  # external kind
                _, position = read_uleb(payload, position)
                if payload[position] != 0:
                    raise ValueError('explicit export type unsupported')
                position += 1
                if exported in allowed:
                    entries.append(payload[start:position])
                    seen.add(exported)
            if position != len(payload):
                raise ValueError('trailing Component exports')
            payload = uleb(len(entries)) + b''.join(entries)
        result += section(kind, payload)
    if seen != set(allowed):
        raise ValueError(f'missing template exports: {set(allowed) - seen}')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache-dir', type=Path, help='contract cache directory, separate from the installed toolchain')
    parser.add_argument('--offline', action='store_true', help='use only bundled, pinned provisioning tools')
    parser.add_argument('--wit', required=True, type=Path)
    parser.add_argument('--world', required=True)
    parser.add_argument('--auto-preload-module', action='append', default=[], help='attempt a statically discovered stdlib import in guest CPython')
    parser.add_argument('--preload-module', action='append', default=[], metavar='MODULE',
                        help='import a stdlib module into the template during provisioning; repeatable')
    parser.add_argument('--cargo', default='cargo', help='deprecated; export helper is supplied by the base package')
    parser.add_argument('--rebuild', action='store_true', help='atomically replace an existing contract template')
    args = parser.parse_args()
    modules = preload_modules(args.preload_module)
    automatic = preload_modules(args.auto_preload_module)
    if not verified_package(ROOT / 'dist'):
        raise ValueError('provision the base Python toolchain with bootstrap.py first')
    tools = {key: tool(key) for key in ('wasm-tools', 'wasm-opt', 'wac')}
    for key, path in tools.items():
        expected = LOCK['tools'][key]['sha256']
        if digest(path) != expected:
            raise ValueError(f'{key} differs from toolchain.lock.json')
    wit = args.wit.resolve(strict=True)
    document = json.loads(run([tools['wasm-tools'], 'component', 'wit', '--json', wit], capture=True).stdout)
    key, exports = contract(document, args.world)
    world = selected_world(document, args.world)
    package = document['packages'][world['package']]['name']
    identity, separator, version = package.partition('@')
    selected = identity + '/' + args.world + (separator + version if separator else '')
    destination = (args.cache_dir or ROOT / 'dist/contracts') / key
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not args.rebuild:
        manifest = json.loads((destination / 'manifest.json').read_text())
        valid = (manifest.get('schema') == 1 and manifest.get('contractSha256') == key
                 and manifest.get('exports') == exports
                 and manifest.get('componentizePy') == LOCK['componentize_py']
                 and manifest.get('sourceModules') == LOADER_VERSION
                 and manifest.get('preloadModules', []) == modules
                 and manifest.get('automaticPreloads', []) == automatic)
        for name, expected in manifest.get('bootstrapInputs', {}).items():
            path = ROOT / name
            valid = valid and path.resolve().is_relative_to(ROOT.resolve()) and path.is_file() and digest(path) == expected
        for name, expected in manifest.get('sha256', {}).items():
            path = destination / name
            valid = valid and path.resolve().is_relative_to(destination.resolve()) and path.is_file() and digest(path) == expected
        if valid and {'template.component.wasm', 'python-packager.wasm', 'public-exports.wasm'} <= manifest.get('sha256', {}).keys():
            print(f'Python contract template ready: {destination}')
            return
        raise ValueError(f'existing template differs; use --rebuild to replace it atomically: {destination}')
    environment = dict(os.environ, PYTHONSAFEPATH="1", BINARYEN_CORES='1',
                       RAYON_NUM_THREADS='1', TOKIO_WORKER_THREADS='1', CARGO_BUILD_JOBS='1')
    environment.pop('VIRTUAL_ENV', None)
    with tempfile.TemporaryDirectory(prefix='.contract-bootstrap-', dir=destination.parent) as directory:
        work = Path(directory)
        installed = bootstrap.install_componentize_py(work, offline=args.offline)
        environment['PYTHONPATH'] = str(installed)
        environment['LD_LIBRARY_PATH'] = str(ROOT / 'dist/provision')
        dependencies = work / 'wit/deps'
        dependencies.mkdir(parents=True)
        # Preserve the complete dependency graph in individual package documents.
        run([tools['wasm-tools'], 'component', 'wit', wit, '--out-dir', dependencies])
        (dependencies / 'wisp-source.wit').write_text('package wisp:python@0.1.0; interface source { get: func() -> string; }\n')
        (work / 'wit/runtime.wit').write_text(
            'package wisp:python-contract@0.1.0;\nworld runtime {\n'
            f'  include {selected};\n  import wisp:python/source@0.1.0;\n'
            f'  export {CONTROL}: func() -> result<_, string>;\n}}\n')
        command = [sys.executable, '-P', '-c', 'import componentize_py; componentize_py.script()',
                   '-d', work / 'wit', '-w', 'runtime']
        bindings = work / 'bindings'
        run([*command, 'bindings', bindings], env=environment, cwd=work)
        app = work / 'app'
        app.mkdir()
        runtime_source = generate_runtime(bindings, modules, exports, automatic)
        (app / '_wisp_runtime.py').write_text(runtime_source)
        runtime = work / 'runtime.wasm'
        run([*command, 'componentize', '-p', app, '_wisp_runtime', '-o', runtime], env=environment, timeout=600, cwd=work)
        runtime.write_bytes(bootstrap.optimize_runtime(runtime.read_bytes(), work, tools['wasm-opt'], cache=work))
        provider = work / 'source.wasm'
        provider.write_bytes(source_component(b'# application source is supplied at build time\n', ''))
        staged = work / 'package'
        staged.mkdir()
        template = staged / 'template.component.wasm'
        run([tools['wac'], 'plug', runtime, '--plug', provider, '-o', template], env=environment)
        template.write_bytes(public_template(template.read_bytes(), [*exports, CONTROL]))
        if template.stat().st_size > MAX_COMPONENT_BYTES:
            raise ValueError(f'Python template exceeds the 64 MiB admission limit: {template.stat().st_size}')
        run([tools['wasm-tools'], 'validate', template])
        shutil.copyfile(ROOT / 'dist/public-exports.wasm', staged / 'public-exports.wasm')
        shutil.copyfile(ROOT / 'dist/python-packager.wasm', staged / 'python-packager.wasm')
        shutil.copytree(bindings / 'wit_world', staged / 'wit_world')
        shutil.copyfile(bindings / 'componentize_py_types.py', staged / 'componentize_py_types.py')
        shutil.copytree(ROOT / 'dist/licenses', staged / 'licenses')
        (staged / 'runtime.py').write_text(runtime_source)
        manifest = {'schema': 1, 'contractSha256': key, 'exports': exports,
                    'componentizePy': LOCK['componentize_py'], 'sourceModules': LOADER_VERSION,
                    'preloadModules': modules, 'automaticPreloads': automatic,
                    'bootstrapInputs': {p: digest(ROOT / p) for p in
                                        ('bootstrap_contract.py', 'scripts/contracts.py', 'scripts/runtime_contract.py',
                                         'scripts/source_loader.py', 'scripts/source_bundle.py', 'toolchain.lock.json')},
                    'sha256': {str(path.relative_to(staged)): digest(path) for path in sorted(staged.rglob('*')) if path.is_file()}}
        (staged / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        destination.parent.mkdir(parents=True, exist_ok=True)
        previous = work / 'previous-template'
        if destination.exists():
            destination.rename(previous)
        try:
            staged.rename(destination)
        except BaseException:
            if previous.exists():
                previous.rename(destination)
            raise
    print(f'Python contract template ready: {destination}')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from error
