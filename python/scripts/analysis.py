"""Host I/O for guest AST analysis. Never imports/parses application modules."""
import json
from pathlib import Path
import tempfile

from scripts.common import ROOT, guest_tools, invoke, run, verified_package
from scripts.dependencies import prepare, project_requirements
from scripts.limits import MAX_PROGRAM_BYTES
from scripts.source_bundle import collect, default_root


def inputs(source, package_root=None, python_paths=(), dependency_root=None, *, work):
    root = (package_root or default_root(source.resolve())).resolve()
    dependency_root = (dependency_root or root).resolve()
    dependencies, receipt = prepare(dependency_root, work / 'dependencies')
    excluded = [dependency_root / 'out', *dependencies,
                *(dependency_root / p for p in receipt['excludedPaths'])]
    program = collect(source, root, [*python_paths, *dependencies], excluded_paths=excluded)
    # Staging hashes refer to original files/wheels, never temporary extraction.
    inventory = {path: sha for path, sha in program['paths'].items()
                 if not Path(path).is_relative_to(work)}
    inventory.update({str((dependency_root / p).resolve()): sha for p, sha in receipt['inputs'].items()})
    _, _, _, _, settings = project_requirements(dependency_root)
    return program, receipt, inventory, settings


def analyze(program, work, *, metadata_only=False):
    if not verified_package(ROOT / 'dist'):
        raise ValueError('Python analysis needs the current bootstrapped Python toolchain')
    request = {'schema': 2, 'entry': program['entry'], 'modules': program['modules'],
               'metadataOnly': metadata_only}
    encoded = json.dumps(request, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(encoded) > MAX_PROGRAM_BYTES:
        raise ValueError('Python analysis request exceeds the 32 MiB guest program limit')
    source = work / 'analysis.json'
    source.write_bytes(encoded)
    candidate = work / 'analysis.wasm'
    command = guest_tools.guest_command(ROOT / 'dist/python-packager.wasm',
                 ROOT / 'dist/inference.component.wasm', source, '', candidate)
    run([*command[:2], '-O', 'opt-level=0', '-C', 'parallel-compilation=n', *command[2:]])
    response = json.loads(json.loads(invoke(candidate, 'infer()')))
    if 'error' in response:
        raise ValueError(response['error'])
    return response
