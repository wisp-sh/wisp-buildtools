#!/usr/bin/env python3
"""Compile an OCaml source program to a standalone WASI command."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import tempfile

import backend
from runner import digest, manifest, ocamlc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('-o', '--output', required=True, type=Path)
    parser.add_argument('--project-root', type=Path)
    parser.add_argument('--source', dest='additional_sources', action='append', default=[], type=Path, help='additional source, in dependency order')
    parser.add_argument('--library', action='append', default=[], help='standard library archive, e.g. str.cma')
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    project = (args.project_root or source.parent).resolve(strict=True)
    output = args.output.absolute()
    receipt = Path(str(output) + '.build.json')
    if output.exists() or receipt.exists():
        parser.error('Output or receipt already exists.')
    record = manifest()
    sources = [p.resolve(strict=True) for p in args.additional_sources] + [source]
    for path in sources:
        path.relative_to(project)
    project_files = [path for path in project.rglob('*')
                     if path.suffix in ('.ml', '.mli') and path.is_file()]
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.ocaml-', dir=output.parent) as directory:
        work = Path(directory)
        for path in project_files:
            target = work / path.relative_to(project)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        guests = ['/work/' + path.relative_to(project).as_posix() for path in sources]
        includes = sorted({'/work/' + path.parent.relative_to(project).as_posix() for path in sources})
        include_args = [value for directory in includes for value in ('-I', directory)]
        ocamlc(work, *include_args, '-output-obj', '-o', '/work/program.c', *args.library, *guests)
        backend.build(work, ['/work/program.c', '/ocaml/src/command.c'], '/work/program.wasm')
        shutil.copy2(work / 'program.wasm', output)
    receipt.write_text(json.dumps({'schema': 1, 'language': 'ocaml', 'compilerVersion': record['version'],
                                  'fullyWasmHostedBuild': True, 'sourceSha256': digest(source),
                                  'sources': {str(path.relative_to(project)): digest(path) for path in project_files},
                                  'outputSha256': digest(output)}, indent=2) + '\n')


if __name__ == '__main__':
    main()
