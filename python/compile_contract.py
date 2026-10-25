"""Build user Python against a provisioned contract with Wasm-only build stages."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from scripts.common import LOCK, MAX_COMPONENT_BYTES, ROOT, digest, guest_tools, run
from scripts.contracts import CONTROL, contract
from scripts.source_bundle import LOADER_VERSION, bundle


def limited(command):
    return [*command[:2], '-O', 'opt-level=0', '-C', 'parallel-compilation=n', *command[2:]]


def compile_contract(args, source, output):
    if args.entry != 'add':
        raise ValueError('--entry is only supported by the legacy scalar Python adapter')
    if not args.world:
        raise ValueError('--world is required with --wit')
    wit = args.wit.resolve(strict=True)
    document = json.loads(run(limited(guest_tools.command('component', 'wit', '--json', wit)), capture=True).stdout)
    key, exports = contract(document, args.world)
    package = ROOT / 'dist/contracts' / key
    if not package.is_dir():
        from scripts.provision import template
        package = template(key, exports, wit, args.world, args.cache_dir)
    manifest = json.loads((package / 'manifest.json').read_text())
    if (manifest.get('schema') != 1 or manifest.get('contractSha256') != key
            or manifest.get('exports') != exports or manifest.get('componentizePy') != LOCK['componentize_py']):
        raise ValueError('Python contract template manifest differs from selected WIT')
    inventory = manifest.get('sha256')
    if not isinstance(inventory, dict):
        raise ValueError('Python contract template inventory is missing')
    if not {'template.component.wasm', 'python-packager.wasm', 'public-exports.wasm'} <= inventory.keys():
        raise ValueError('Python contract template inventory is incomplete')
    for name, expected in inventory.items():
        path = package / name
        if not path.resolve().is_relative_to(package.resolve()) or digest(path) != expected:
            raise ValueError(f'Python contract template checksum mismatch: {name}')
    loader_version = manifest.get('sourceModules')
    if loader_version is not None and loader_version != LOADER_VERSION:
        raise ValueError(f'unsupported Python template source-loader version: {loader_version}')
    use_bundle = loader_version == LOADER_VERSION
    if not use_bundle and (args.package_root or args.python_path):
        raise ValueError('Python template lacks package imports; reprovision with bootstrap_contract.py --rebuild')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.python-contract-', dir=output.parent) as directory:
        work = Path(directory)
        source_details = {}
        packaged_source = source
        if use_bundle:
            from scripts.dependencies import prepare
            dependency_root = args.package_root or source.parent
            dependency_roots, dependency_details = prepare(dependency_root, work / 'dependencies')
            payload, source_details = bundle(source, args.package_root, [*args.python_path, *dependency_roots],
                                              excluded_paths=[*dependency_roots, *(dependency_root / p for p in dependency_details['excludedPaths'])])
            source_details['dependencies'] = dependency_details
            packaged_source = work / 'program.py'
            packaged_source.write_bytes(payload)
        candidate = work / 'candidate.wasm'
        run(limited(guest_tools.guest_command(package / 'python-packager.wasm',
                    package / 'template.component.wasm', packaged_source, '', candidate)))
        capabilities = ('inherit-network=n,allow-ip-name-lookup=n,tcp=n,udp=n,inherit-env=n,'
                        'inherit-stdin=n,inherit-stdout=n,inherit-stderr=n')
        result = run(limited([str(guest_tools.runtime()), 'run', '-S', capabilities,
                             '--invoke', CONTROL + '()', candidate]), capture=True, timeout=args.timeout).stdout.strip()
        if result != 'ok':
            raise ValueError('guest CPython rejected the program: ' + result)
        public = work / 'public.wasm'
        run(limited(guest_tools.guest_command(package / 'public-exports.wasm', candidate, public)))
        if public.stat().st_size > MAX_COMPONENT_BYTES:
            raise ValueError('Python Component exceeds the 64 MiB admission limit')
        run(limited(guest_tools.command('validate', public)))
        # Verify the actual exported ABI before publishing any bytes.
        actual = json.loads(run(limited(guest_tools.command('component', 'wit', '--json', public)), capture=True).stdout)
        if len(actual['worlds']) != 1:
            raise ValueError('Python Component must decode to one public world')
        actual_world = actual['worlds'][0]
        for imported in actual_world['imports'].values():
            interface = imported.get('interface')
            if interface is None:
                raise ValueError('unexpected non-interface Python host import')
            owner = actual['interfaces'][interface['id']]['package']
            if owner is None or not actual['packages'][owner]['name'].startswith('wasi:'):
                raise ValueError('unexpected non-WASI Python host import')
        actual_world['imports'] = {}  # WASI capabilities are supplied by the execution host.
        actual_key, _ = contract(actual, actual_world['name'])
        if actual_key != key:
            raise ValueError('Python Component exports differ from the selected WIT contract')
        details = {'schema': 1, 'contractSha256': key, 'templateManifestSha256': digest(package / 'manifest.json'),
                   'sourceSha256': digest(source), 'sourceBundle': source_details, 'componentSha256': digest(public),
                   'fullyWasmHostedBuild': True,
                   'templateBootstrap': {'implementation': 'native componentize-py, WIT only',
                                         'applicationSourceRead': False},
                   'stages': ['WASI source-provider packaging', 'guest CPython compile/exec and export validation',
                              'WASI public export projection', 'WASI wasm-tools validation']}
        os.replace(public, output)
    print(json.dumps(details, sort_keys=True))
