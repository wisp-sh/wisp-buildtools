#!/usr/bin/env python3
"""Compile an authored scalar C# contract using only Wasm compiler/linker guests."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from toolchain import ROOT, COMPONENT, command, digest, run, verified_dist, wasm_tool, wasm_tools


def contract(document, world):
    worlds = document.get('worlds', [])
    selected = [w for w in worlds if w['name'] == world] if world else worlds
    if len(selected) != 1:
        raise ValueError('select exactly one authored WIT world with --world')
    selected = selected[0]
    functions = selected['exports']
    function = functions.get('add', {}).get('function', {})
    if (selected['imports'] or set(functions) != {'add'} or function.get('kind') != 'freestanding' or
            [p['type'] for p in function.get('params', [])] != ['s32', 's32'] or function.get('result') != 's32'):
        raise ValueError('C# MVP requires only export add: func(left: s32, right: s32) -> s32, without imports')
    return selected['name']


def build(source, output, *, wit, world=None, timeout=300):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('timeout must be positive and finite')
    deadline = time.monotonic() + timeout
    def remaining():
        return max(0.01, deadline - time.monotonic())
    dist, manifest = verified_dist()
    source = Path(source).resolve(strict=True)
    wit = Path(wit).resolve(strict=True)
    if not source.is_file() or source.suffix.lower() != '.cs':
        raise ValueError('expected one C# .cs source file')
    contents = source.read_bytes()
    if len(contents) > 1024 * 1024:
        raise ValueError('source exceeds the 1 MiB limit')
    contents.decode('utf-8', errors='strict')
    output = Path(output).absolute()
    targets = [output, Path(str(output) + '.wit'), Path(str(output) + '.build.json')]
    inputs = [source, wit] if wit.is_file() else [source, *wit.rglob('*')]
    if any(p.resolve() in inputs for p in targets) or len({p.resolve() for p in targets}) != len(targets):
        raise ValueError('output paths must be distinct from each other and from inputs')
    if any(p.exists() or p.is_symlink() for p in targets):
        raise ValueError('output already exists; choose a new path')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.csharp-', dir=output.parent) as directory:
        work = Path(directory)
        captured = work / 'wit'
        captured.mkdir()
        if wit.is_file():
            files = [wit]
        elif wit.is_dir():
            files = sorted(wit.glob('*.wit'))
        else:
            raise ValueError('expected authored WIT file or directory')
        if not files:
            raise ValueError('WIT input has no contract files')
        records = []
        for p in files:
            if p.is_symlink() or not p.is_file():
                raise ValueError('WIT input must contain regular files')
            (captured / p.name).write_bytes(p.read_bytes())
            records.append({'path': p.name, 'sha256': digest(captured / p.name)})
        world = contract(json.loads(wasm_tool('component', 'wit', captured, '--json', timeout=remaining())), world)
        (work / 'source.cs').write_bytes(contents)
        with zipfile.ZipFile(dist / 'assemblies.zip') as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or set(names) != set(manifest['assemblies']):
                raise ValueError('invalid managed assembly archive inventory')
            if sum(s['bytes'] for s in manifest['assemblies'].values()) > 128 * 1024 * 1024:
                raise ValueError('managed assembly archive exceeds extraction limit')
            for name in names:
                parts = name.split('/')
                if len(parts) != 2 or parts[0] not in ('managed', 'references') or not parts[1].endswith('.dll') or '..' in parts or '\\' in name:
                    raise ValueError('invalid managed assembly archive path')
                data = archive.read(name)
                spec = manifest['assemblies'][name]
                if len(data) != spec['bytes'] or hashlib.sha256(data).hexdigest() != spec['sha256']:
                    raise ValueError('managed assembly archive checksum mismatch')
                target = work / name
                target.parent.mkdir(exist_ok=True)
                target.write_bytes(data)
        core = work / 'application.wasm'
        run(command(dist / 'compiler/dotnet.wasm', 'Compiler', '/work/source.cs', '/work/application.wasm',
                    '/references', '/runtime/runtime-template.wasm', mounts=[(dist / 'compiler', '/'),
                    (work, '/work'), (work / 'managed', '/managed'),
                    (work / 'references', '/references'), (dist, '/runtime')]), timeout=remaining(),
            cwd=dist / 'compiler')
        if not core.is_file():
            raise ValueError('Wasm compiler did not produce a module')
        adapter = wasm_tools.preview1_adapter()
        def componentize(contract_path, selected_world, name):
            embedded, component = work / (name + '.embedded.wasm'), work / (name + '.component.wasm')
            wasm_tool('component', 'embed', contract_path, core, '--world', selected_world, '-o', embedded, timeout=remaining())
            wasm_tool('component', 'new', embedded, '--adapt', adapter, '-o', component, timeout=remaining())
            return component
        validation_wit = work / 'validation.wit'
        validation_wit.write_text('package wisp:csharp-validation;\nworld validation { export wisp-validate: func() -> s32; }\n')
        validation = componentize(validation_wit, 'validation', 'validation')
        initialized = run([*command(validation)[:-1], '--invoke', 'wisp-validate()', validation], timeout=remaining())
        if initialized.strip() != '1':
            raise ValueError('C# runtime validation did not return 1')
        candidate = componentize(captured, world, 'application')
        wasm_tool('validate', candidate, timeout=remaining())
        if candidate.stat().st_size > 64 * 1024 * 1024 or candidate.read_bytes()[:8] != COMPONENT:
            raise ValueError('output must be a Component within the 64 MiB limit')
        public_wit = wasm_tool('component', 'wit', candidate, timeout=remaining())
        receipt = {'schemaVersion': 1, 'toolchain': 'csharp', 'version': manifest['version'],
                   'source': {'path': str(source), 'sha256': hashlib.sha256(contents).hexdigest()},
                   'wit': {'world': world, 'files': records},
                   'compilerManifestSha256': digest(dist / 'manifest.json'),
                   'sourceCompilationWasmHosted': True, 'fullyWasmHostedBuild': True,
                   'stages': ['Roslyn in Mono WASI emits managed IL', 'Mono WASI guest embeds IL in fixed runtime',
                              'Wasm wasm-tools assembles and validates Component'],
                   'recipe': {p: digest(ROOT / p) for p in ['compile.py', 'toolchain.py']},
                   'output': {'sha256': digest(candidate), 'bytes': candidate.stat().st_size},
                   'limits': {'sourceBytes': 1024 * 1024, 'ilBytes': 1024 * 1024, 'componentBytes': 64 * 1024 * 1024},
                   'entry': 'public static int Calculator.Add(int, int)',
                   'runtimeInitialization': 'once per instance; no application AOT, native compilation or relinking'}
        (work / 'public.wit').write_text(public_wit)
        (work / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
        publications = [(work / 'public.wit', targets[1]), (work / 'receipt.json', targets[2]), (candidate, output)]
        published = []
        try:
            for staged, target in publications:
                os.link(staged, target)
                published.append(target)
        except OSError:
            for p in published:
                p.unlink(missing_ok=True)
            raise
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--wit', type=Path, required=True)
    parser.add_argument('--world')
    parser.add_argument('-o', '--out', '--output', dest='output', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=300)
    args = parser.parse_args()
    report = build(args.source, args.output, wit=args.wit, world=args.world, timeout=args.timeout)
    print(f"{args.output} ({report['output']['bytes']} bytes)")


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(f'csharp compile: {error}') from error
