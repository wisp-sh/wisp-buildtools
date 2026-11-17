#!/usr/bin/env python3
"""Compile OCaml commands or public .mli libraries using Wasm-hosted compilers."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

import backend
from contracts import Signature
from bindings import Generator, exports
from runner import ROOT, digest, manifest, ocamlc

MAX_SOURCE = 32 * 1024 * 1024


def inputs(sources, project):
    sources = [Path(p).absolute() for p in sources]
    project = Path(project).resolve(strict=True)
    selected, modules = [], {}
    for path in sources:
        if path.is_symlink() or not path.is_file() or path.suffix not in ('.ml', '.mli'):
            raise ValueError(f'expected a regular .ml or .mli source: {path}')
        path.resolve(strict=True).relative_to(project)
        if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_]*', path.stem):
            raise ValueError('invalid OCaml module filename')
        module = path.stem[0].upper() + path.stem[1:]
        if module == 'Wisp_bindings': raise ValueError('Wisp_bindings is reserved')
        if module in modules and modules[module] != path.with_suffix(''):
            raise ValueError('duplicate OCaml module name')
        modules[module] = path.with_suffix('')
        interface = path.with_suffix('.mli')
        if path.suffix == '.ml' and interface.exists() and interface not in selected:
            selected.append(interface)
        if path in selected: raise ValueError('duplicate source or interface after implementation')
        selected.append(path)
    inventory, total = {}, 0
    # Only source inputs enter the snapshot. Compiler output, caches and package
    # artifacts cannot become inputs when an output directory is below project.
    def walk(directory):
        nonlocal total
        for path in sorted(directory.iterdir()):
            if path.name.startswith('.') or path.name in ('_build', 'dist', 'target', 'node_modules'):
                continue
            if path.is_symlink(): raise ValueError(f'symlink in OCaml project: {path}')
            if path.is_dir(): walk(path)
            elif path.suffix in ('.ml', '.mli'):
                if not path.is_file(): raise ValueError('source must be a regular file')
                total += path.stat().st_size
                if total > MAX_SOURCE: raise ValueError('OCaml source snapshot exceeds 32 MiB')
                if len(inventory) >= 4096: raise ValueError('OCaml source file limit is 4096')
                inventory[path.relative_to(project).as_posix()] = digest(path)
    walk(project)
    for path in selected:
        if path.relative_to(project).as_posix() not in inventory:
            raise ValueError('selected source is outside the project inventory')
    implementations = [p for p in selected if p.suffix == '.ml']
    if not implementations: raise ValueError('at least one implementation is required')
    return project, selected, implementations[-1], inventory


def public_api(main, entry):
    interface = main.with_suffix('.mli')
    if not interface.is_file():
        raise ValueError('OCaml libraries require a public .mli beside the last implementation')
    module = entry or main.stem[0].upper() + main.stem[1:]
    if not re.fullmatch(r'[A-Z][a-zA-Z0-9_]*', module):
        raise ValueError('--entry-module must name a top-level OCaml module')
    return Signature(interface.read_text()), module


def wasm_tools():
    sys.path.insert(0, str(ROOT.parent))
    if os.environ.get('WISP_WASM_TOOLS_TOOLCHAIN'):
        sys.path.insert(0, str(Path(os.environ['WISP_WASM_TOOLS_TOOLCHAIN']).resolve().parent))
    import wasm_tools as tools
    return tools


def run_tools(tools, *args, capture=False):
    return subprocess.run(tools.command(*args), check=True, text=True,
                          stdout=subprocess.PIPE if capture else None)


def build(sources, output, *, project_root=None, libraries=(), component=False,
          wit=None, world=None, entry=None):
    project, selected, main, inventory = inputs(sources, project_root or Path(sources[-1]).parent)
    if not component and (wit or world or entry):
        raise ValueError('WIT and entry options require --component')
    output = Path(output).absolute()
    targets = [output, Path(str(output) + '.build.json')]
    if component: targets.append(Path(str(output) + '.wit'))
    if any(p.exists() or p.is_symlink() for p in targets):
        raise ValueError('output or sidecar already exists; select a new output path')
    for library in libraries:
        if not re.fullmatch(r'[A-Za-z0-9_]+\.cma', library):
            raise ValueError('--library selects a packaged standard library archive')
    record = manifest()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.ocaml-', dir=output.parent) as directory:
        work = Path(directory)
        source_root = work / 'source'
        for name, expected in inventory.items():
            src, target = project / name, source_root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
            if digest(target) != expected: raise ValueError('OCaml source changed while staging')
        guests = ['/work/source/' + p.relative_to(project).as_posix() for p in selected]
        includes = sorted({'/work/source/' + p.parent.relative_to(project).as_posix() for p in selected})
        include_args = [v for directory in includes for v in ('-I', directory)]
        c_sources = ['/work/program.c', '/ocaml/src/reactor.c' if component else '/ocaml/src/command.c']
        contract_record = None
        if component:
            signature, module = public_api(source_root / main.relative_to(project), entry)
            tools = wasm_tools()
            contract = work / 'public.wit'
            if wit:
                # Wisp sends a self-contained selected world. Standalone callers
                # use a single WIT file; directory dependency resolution is explicit.
                wit = Path(wit)
                if wit.is_symlink() or not wit.is_file() or wit.stat().st_size > MAX_SOURCE:
                    raise ValueError('--wit must be a bounded regular WIT file')
                contract.write_bytes(wit.read_bytes())
            else:
                contract.write_text(signature.wit())
            document = json.loads(run_tools(tools, 'component', 'wit', '--json', contract, capture=True).stdout)
            world = world or (document['worlds'][0]['name'] if len(document['worlds']) == 1 else None)
            names = exports(signature, document, world)
            c, ml = Generator().generate(signature, names, module)
            (work / 'bindings.c').write_text(c)
            (work / 'wisp_bindings.ml').write_text(ml)
            guests.append('/work/wisp_bindings.ml')
            c_sources.append('/work/bindings.c')
            contract_record = {'sha256': digest(contract), 'world': world,
                               'exports': names, 'publicInterface': main.with_suffix('.mli').relative_to(project).as_posix()}
        ocamlc(work, *include_args, '-output-obj', '-o', '/work/program.c', *libraries, *guests)
        backend.build(work, c_sources, '/work/program.wasm', reactor=component)
        candidate = work / 'program.wasm'
        if component:
            run_tools(tools, 'component', 'embed', contract, candidate, '--world', world, '-o', work / 'embedded.wasm')
            adapter = tools.preview1_adapter()
            run_tools(tools, 'component', 'new', work / 'embedded.wasm', '--adapt',
                      f'wasi_snapshot_preview1={adapter}', '-o', work / 'component.wasm')
            candidate = work / 'component.wasm'
            run_tools(tools, 'validate', candidate)
            (work / 'output.wit').write_text(run_tools(tools, 'component', 'wit', candidate, capture=True).stdout)
            contract_record['preview1AdapterSha256'] = digest(adapter)
        receipt = {'schema': 1, 'language': 'ocaml', 'compilerVersion': record['version'],
                   'fullyWasmHostedBuild': True, 'compilerManifestSha256': digest(ROOT / 'dist/manifest.json'),
                   'sourceSha256': inventory[main.relative_to(project).as_posix()], 'sources': inventory,
                   'sourceOrder': [p.relative_to(project).as_posix() for p in selected],
                   'libraries': list(libraries), 'component': component, 'contract': contract_record,
                   'initialization': 'WASI reactor constructor calls caml_startup' if component else 'WASI command',
                   'outputSha256': digest(candidate)}
        (work / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
        publications = [(work / 'receipt.json', targets[1])]
        if component: publications.append((work / 'output.wit', targets[2]))
        publications.append((candidate, output))
        published = []
        try:
            for staged, target in publications:
                os.link(staged, target); published.append(target)
        except OSError:
            for path in published: path.unlink(missing_ok=True)
            raise
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sources', nargs='+', type=Path)
    parser.add_argument('-o', '--output', type=Path)
    parser.add_argument('--project-root', type=Path)
    parser.add_argument('--source', dest='additional_sources', action='append', default=[], type=Path,
                        help='command compatibility: additional source before the main source')
    parser.add_argument('--library', action='append', default=[])
    parser.add_argument('--component', action='store_true')
    parser.add_argument('--wit', type=Path)
    parser.add_argument('--world')
    parser.add_argument('--entry-module')
    parser.add_argument('--infer', action='store_true', help='print public contract and source inventory JSON')
    args = parser.parse_args()
    sources = args.additional_sources + args.sources
    if args.infer:
        project, _, main_source, inventory = inputs(sources, args.project_root or sources[-1].parent)
        signature, _ = public_api(main_source, args.entry_module)
        print(json.dumps({'body': signature.declarations(),
                          'project': str(project), 'sources': inventory}))
    else:
        if not args.output: parser.error('--output is required')
        build(sources, args.output, project_root=args.project_root, libraries=args.library,
              component=args.component, wit=args.wit, world=args.world, entry=args.entry_module)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(f'ocaml: {error}')
