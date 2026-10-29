"""Build user Python against a provisioned contract with Wasm-only build stages."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from scripts.common import LOCK, MAX_COMPONENT_BYTES, ROOT, digest, guest_tools, run
from scripts.contracts import CONTROL, contract
from scripts.source_bundle import LOADER_VERSION


def limited(command):
    return [*command[:2], '-O', 'opt-level=0', '-C', 'parallel-compilation=n', *command[2:]]


def verify_template(package, key, exports):
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
    if manifest.get('sourceModules') not in (None, 1, LOADER_VERSION):
        raise ValueError('unsupported Python template source-loader version: ' + str(manifest['sourceModules']))
    return manifest


def compile_contract(args, source, output):
    if args.entry != 'add':
        raise ValueError('--entry is only supported by the legacy scalar Python adapter')
    if not args.world:
        raise ValueError('--world is required with --wit')
    wit = args.wit.resolve(strict=True)
    document = json.loads(run(limited(guest_tools.command('component', 'wit', '--json', wit)), capture=True).stdout)
    key, exports = contract(document, args.world)
    installed = ROOT / 'dist/contracts' / key
    installed_manifest = verify_template(installed, key, exports) if installed.is_dir() else None
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.python-contract-', dir=output.parent) as directory:
        from scripts.analysis import inputs, analyze
        from scripts.source_bundle import encode
        from scripts.provision import template
        work = Path(directory)
        program, dependency_details, inventory, settings = inputs(source, args.package_root,
                args.python_path, getattr(args, 'dependency_root', None), work=work,
                excluded_paths=[output, args.cache_dir])
        analysis = analyze(program, work, metadata_only=not getattr(args, 'inferred', False))
        automatic = analysis['stdlib']
        strict = settings.get('preload-modules', [])
        available = set((installed_manifest or {}).get('preloadModules', [])) | set((installed_manifest or {}).get('automaticPreloads', []))
        if (installed_manifest and installed_manifest.get('sourceModules') == LOADER_VERSION
                and set(automatic) <= available and set(strict) <= available):
            package, manifest = installed, installed_manifest
        else:
            strict = sorted(set(strict) | set((installed_manifest or {}).get('preloadModules', [])))
            package = template(key, exports, wit, args.world, args.cache_dir,
                               modules=strict, automatic_modules=automatic)
            manifest = verify_template(package, key, exports)
        payload, source_details = encode(program, exports=analysis.get('exports', {}))
        source_details['dependencies'] = dependency_details
        source_details['stdlibModules'] = automatic
        packaged_source = work / 'program.py'
        packaged_source.write_bytes(payload)
        candidate = work / 'candidate.wasm'
        run(limited(guest_tools.guest_command(package / 'python-packager.wasm',
                    package / 'template.component.wasm', packaged_source, '__wisp_inferred__' if getattr(args, 'inferred', False) else '', candidate)))
        capabilities = ('inherit-network=n,allow-ip-name-lookup=n,tcp=n,udp=n,inherit-env=n,'
                        'inherit-stdin=n,inherit-stdout=n,inherit-stderr=n')
        result = run(limited([str(guest_tools.runtime()), 'run', '-C', 'cache=y', '-S', capabilities,
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
