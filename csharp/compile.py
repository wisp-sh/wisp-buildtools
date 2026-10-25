#!/usr/bin/env python3
"""Build C# libraries and authored WIT Components using Wasm-hosted Roslyn."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time
import zipfile
from project import Inputs, packages as extract_packages, package_closure
from toolchain import ROOT, COMPONENT, command, digest, run, verified_dist, wasm_tool, wasm_tools


def contract(document, world):
    selected = [w for w in document.get('worlds', []) if not world or w['name'] == world]
    if len(selected) != 1:
        raise ValueError('select exactly one authored WIT world with --world')
    if selected[0]['imports']:
        raise ValueError('C# WIT imports are not supported yet')
    return selected[0]['name']


def build(source, output, *, wit=None, world=None, timeout=300, entry=None, exports=(),
          references=(), packages=(), package_dirs=(), project_root=None, library=False, assembly=None):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('timeout must be positive and finite')
    deadline = time.monotonic() + timeout
    def remaining(): return max(0.01, deadline - time.monotonic())
    dist, manifest = verified_dist()
    if manifest.get('payloadFormat') != 2: raise ValueError('compiler bundle format is outdated; rerun bootstrap')
    sources = [source] if isinstance(source, (str, Path)) else list(source)
    inputs = Inputs(sources, project_root)
    mappings = {}
    for mapping in exports:
        key, separator, value = mapping.partition('=')
        if not separator or not key or not value or key in mappings:
            raise ValueError('--export requires a unique wit-name=Namespace.Type.Method mapping')
        mappings[key] = value
    if library and (wit or world or entry or mappings): raise ValueError('--library cannot be combined with WIT or export selection')
    if not library and wit is None: raise ValueError('Component builds require --wit')
    output = Path(output).absolute()
    if assembly is not None: inputs.main.name = assembly
    elif library and inputs.main.path is None: inputs.main.name = output.stem
    targets = [output, Path(str(output) + '.build.json')]
    if not library: targets.append(Path(str(output) + '.wit'))
    if len({p.resolve() for p in targets}) != len(targets): raise ValueError('output paths must be distinct')
    if any(p.exists() or p.is_symlink() for p in targets): raise ValueError('output already exists; choose a new path')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.csharp-', dir=output.parent) as directory:
        work = Path(directory)
        # Snapshot every source before starting the first compiler guest.
        staged = {}
        for path in sorted({p for project in inputs.order for p in project.sources}):
            target = work / 'sources' / path.relative_to(inputs.root)
            with path.open('rb') as stream: data = stream.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024: raise ValueError('source changed and exceeds 8 MiB')
            target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
            if digest(target) != inputs.records[str(path.relative_to(inputs.root))]['sha256']:
                raise ValueError('source changed while snapshotting inputs')
            staged[path] = '/work/' + target.relative_to(work).as_posix()
        reference_map = {}
        reference_records = []
        reference_bytes = 0
        for number, path in enumerate(sorted({Path(p).absolute() for p in references} | {r for p in inputs.order for r in p.references})):
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
                raise ValueError('reference must be a regular managed DLL no larger than 16 MiB')
            with path.open('rb') as stream: data = stream.read(16 * 1024 * 1024 + 1)
            reference_bytes += len(data)
            if len(data) > 16 * 1024 * 1024 or reference_bytes > 32 * 1024 * 1024 or number >= 127: raise ValueError('too many or oversized managed references')
            target = work / 'references-local' / str(number) / path.name
            target.parent.mkdir(parents=True); target.write_bytes(data); reference_map[path] = target
            reference_records.append({'path': path.name, 'bytes': len(data), 'sha256': digest(target)})
        if sum(p.stat().st_size for p in reference_map.values()) > 32 * 1024 * 1024: raise ValueError('references exceed 32 MiB')
        package_paths = list(packages)
        for path in package_dirs:
            path = Path(path)
            if not path.is_dir() or path.is_symlink(): raise ValueError('--package-dir requires a regular directory')
            package_paths.extend(sorted(path.glob('*.nupkg')))
        if len(package_paths) > 64: raise ValueError('at most 64 offline NuGet packages are supported')
        available, package_records = extract_packages(package_paths, work / 'packages')
        if not library:
            wit = Path(wit).absolute()
            captured = work / 'wit'; captured.mkdir()
            if wit.is_symlink(): raise ValueError('WIT input cannot be a symlink')
            files = [wit] if wit.is_file() else sorted(wit.rglob('*.wit')) if wit.is_dir() else []
            if not files or len(files) > 256: raise ValueError('expected 1..256 authored WIT files')
            wit_records = []
            total = 0
            for path in files:
                relative = Path(path.name) if wit.is_file() else path.relative_to(wit)
                if any((wit / Path(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts) + 1)) or path.is_symlink():
                    raise ValueError('WIT input must contain regular files')
                with path.open('rb') as stream: data = stream.read(4 * 1024 * 1024 + 1)
                total += len(data)
                if total > 4 * 1024 * 1024: raise ValueError('WIT exceeds 4 MiB')
                target = captured / relative; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
                wit_records.append({'path': relative.as_posix(), 'sha256': digest(target)})
            document = json.loads(wasm_tool('component', 'wit', captured, '--json', timeout=remaining()))
            world = contract(document, world)
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
        compiled = {}
        reports = []
        def dependency_projects(project):
            result = []
            def visit(current):
                for child in current.projects:
                    if child not in result: visit(child); result.append(child)
            visit(project)
            return result
        for number, project in enumerate(inputs.order):
            is_main = project is inputs.main
            mode = 'library' if library or not is_main else 'component'
            candidate = work / ('application.wasm' if mode == 'component' else f'project-{number}.dll')
            dependency_files = [compiled[id(p)] for p in dependency_projects(project)]
            dependency_files += [reference_map[p] for p in project.references]
            dependency_files += [reference_map[Path(p).absolute()] for p in references]
            # Referenced projects' runtime dependencies must travel with the final assembly.
            for child in dependency_projects(project):
                dependency_files += [reference_map[p] for p in child.references]
            requested = dict(project.packages)
            for child in dependency_projects(project):
                for identity, version in child.packages.items():
                    if identity in requested and requested[identity] != version: raise ValueError('conflicting PackageReference versions')
                    requested[identity] = version
            dependency_files += package_closure(requested, available)
            # Explicit --package files are also usable without a csproj PackageReference.
            if not requested:
                dependency_files += package_closure({p['id']: p['version'] for p in package_records}, available)
            if len(set(dependency_files)) >= 128: raise ValueError('managed bundle supports at most 128 assemblies')
            request = {'sources': [{'path': staged[p], 'name': p.relative_to(inputs.root).as_posix()} for p in project.sources],
                       'references': ['/work/' + p.relative_to(work).as_posix() for p in sorted(set(dependency_files))],
                       'framework': '/references', 'assembly': project.name, 'options': project.options,
                       'runtimeAssemblies': manifest['runtimeAssemblies'], 'mode': mode, 'entry': entry, 'exports': mappings,
                       'output': '/work/' + candidate.name, 'template': '/runtime/runtime-template.wasm',
                       'report': '/work/guest-report.json'}
            if mode == 'component': request.update(world=world, wit=document)
            (work / 'request.json').write_text(json.dumps(request))
            run(command(dist / 'compiler/dotnet.wasm', 'Compiler', '/work/request.json', mounts=[(dist / 'compiler', '/'),
                (work, '/work'), (work / 'managed', '/managed'), (work / 'references', '/references'), (dist, '/runtime')]),
                timeout=remaining(), cwd=dist / 'compiler', diagnostics=True)
            if not candidate.is_file(): raise ValueError('Wasm compiler did not produce output')
            report = json.loads((work / 'guest-report.json').read_text()); reports.append(report)
            compiled[id(project)] = candidate
        if not library:
            core = candidate; adapter = wasm_tools.preview1_adapter()
            def componentize(contract_path, selected_world, name):
                embedded, component = work / (name + '.embedded.wasm'), work / (name + '.component.wasm')
                wasm_tool('component', 'embed', contract_path, core, '--world', selected_world, '-o', embedded, timeout=remaining())
                wasm_tool('component', 'new', embedded, '--adapt', adapter, '-o', component, timeout=remaining())
                return component
            validation_wit = work / 'validation.wit'
            validation_wit.write_text('package wisp:csharp-validation; world validation { export wisp-validate: func() -> s32; }\n')
            validation = componentize(validation_wit, 'validation', 'validation')
            initialized = run([*command(validation)[:-1], '--invoke', 'wisp-validate()', validation], timeout=remaining())
            if initialized.strip() != '1': raise ValueError('C# runtime validation did not return 1')
            candidate = componentize(captured, world, 'application')
            wasm_tool('validate', candidate, timeout=remaining())
            if candidate.stat().st_size > 64 * 1024 * 1024 or candidate.read_bytes()[:8] != COMPONENT:
                raise ValueError('output must be a Component within the 64 MiB limit')
            (work / 'public.wit').write_text(wasm_tool('component', 'wit', candidate, timeout=remaining()))
        receipt = {'schemaVersion': 2, 'toolchain': 'csharp', 'version': manifest['version'], 'mode': 'library' if library else 'component',
                   'sources': [{'path': p, **record} for p, record in sorted(inputs.records.items())],
                   'references': reference_records, 'packages': package_records, 'projects': reports,
                   'compilerManifestSha256': digest(dist / 'manifest.json'),
                   'sourceCompilationWasmHosted': True, 'fullyWasmHostedBuild': True,
                   'stages': ['Roslyn in Mono WASI emits managed IL and typed WIT bindings',
                              'Wasm guest packages IL and canonical ABI exports into fixed Mono runtime',
                              'Wasm wasm-tools assembles and validates Component'] if not library else ['Roslyn in Mono WASI emits managed IL'],
                   'recipe': {p: digest(ROOT / p) for p in ['compile.py', 'project.py', 'toolchain.py']},
                   'output': {'sha256': digest(candidate), 'bytes': candidate.stat().st_size},
                   'limits': {'sourceBytes': 8 * 1024 * 1024, 'bundleBytes': 16 * 1024 * 1024, 'componentBytes': 64 * 1024 * 1024},
                   'runtimeInitialization': 'once per instance; no application AOT, native compilation or relinking'}
        if not library: receipt['wit'] = {'world': world, 'files': wit_records}
        (work / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
        publications = [(work / 'receipt.json', targets[1])]
        if not library: publications.append((work / 'public.wit', targets[2]))
        publications.append((candidate, output))
        published = []
        try:
            for staged_file, target in publications:
                os.link(staged_file, target); published.append(target)
        except OSError:
            for path in published: path.unlink(missing_ok=True)
            raise
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, nargs='+')
    parser.add_argument('--wit', type=Path)
    parser.add_argument('--world')
    parser.add_argument('--assembly-name', dest='assembly', help='override the main assembly name; loose --library builds default to the output stem')
    parser.add_argument('--entry', help='public static entry type, including its namespace')
    parser.add_argument('--export', action='append', default=[], dest='exports', metavar='WIT=TYPE.METHOD')
    parser.add_argument('--reference', type=Path, action='append', default=[], dest='references')
    parser.add_argument('--package', type=Path, action='append', default=[], dest='packages', help='offline .nupkg with managed lib assemblies')
    parser.add_argument('--package-dir', type=Path, action='append', default=[], dest='package_dirs')
    parser.add_argument('--project-root', type=Path, help='boundary for source files and ProjectReference paths')
    parser.add_argument('--library', action='store_true', help='emit a managed IL library instead of a Component')
    parser.add_argument('-o', '--out', '--output', dest='output', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=300)
    args = parser.parse_args()
    report = build(**vars(args))
    print(f"{args.output} ({report['output']['bytes']} bytes)")


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
        raise SystemExit(f'csharp compile: {error}') from error
