"""Run tsrs against a frozen module-root file map, with no filesystem preopens."""
from __future__ import annotations
import base64
import hashlib
from pathlib import Path
import time

from bootstrap import check, HERE
from wasmtime_host import compileEngine, runRequest


def check_project(module_root, source, *, module=None):
    manifest = check()
    root, source = Path(module_root).resolve(strict=True), Path(source).resolve(strict=True)
    if not source.is_relative_to(root):
        raise ValueError('typecheck source must be inside module root')
    files, inputs = {}, []
    # Include package metadata and declarations for normal TS module resolution.
    # Never follow a symlink outside the explicitly selected root.
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.suffix.lower() not in {'.ts', '.tsx', '.mts', '.cts', '.js', '.jsx', '.mjs', '.cjs', '.json'}:
            continue
        if any(part in {'.git', '.toolchains', '__pycache__'} for part in path.relative_to(root).parts):
            continue
        if not path.resolve().is_relative_to(root):
            raise ValueError('typecheck input escapes module root: ' + str(path))
        data = path.read_bytes()
        relative = path.relative_to(root).as_posix()
        files['/project/' + relative] = {'base64': base64.b64encode(data).decode('ascii')}
        inputs.append({'path': str(path), 'sha256': hashlib.sha256(data).hexdigest()})
    if (root / 'tsconfig.json').is_file():
        arguments = ['-p', '/project/tsconfig.json', '--noEmit', '--pretty', 'false']
    else:
        # Checking the whole module root catches errors outside the entry graph.
        arguments = ['--noEmit', '--strict', '--target', 'es2022', '--module', 'esnext',
                     '--moduleResolution', 'bundler', '--allowImportingTsExtensions', '--pretty', 'false']
        arguments += [name for name in files if name.endswith(('.ts', '.tsx', '.mts', '.cts'))
                      and 'node_modules' not in Path(name).parts]
        if not any(name.endswith(('.ts', '.tsx', '.mts', '.cts')) for name in files):
            raise ValueError('--typecheck requires TypeScript files in the module root')
    arguments.append("--listFiles")
    start = time.perf_counter()
    with compileEngine(module or HERE / 'dist/tsrs.wasm') as compiled:
        result = runRequest(compiled, {'cwd': '/project', 'args': arguments, 'files': files,
                                      'diagnostics': 'json', 'env': {}, 'caseSensitive': True})
    if result['exitCode']:
        import json
        raise ValueError('tsrs typecheck failed (exit ' + str(result['exitCode']) + '):\n' +
                         result['stdout'] + result['stderr'] + json.dumps(result['diagnostics'], ensure_ascii=False))
    checked_files = result['stdout'].splitlines()
    entry = '/project/' + source.relative_to(root).as_posix()
    if entry not in checked_files:
        raise ValueError('typecheck entrypoint is excluded by tsconfig: ' + str(source))
    return {'enabled': True, 'passed': True, 'compiler': 'tsrs@' + manifest['version'],
            'moduleSha256': manifest['files']['tsrs.wasm']['sha256'],
            'typescriptVersion': manifest['typescriptVersion'],
            'host': 'Wasmtime Python 50.0.1', 'inputs': inputs, 'arguments': arguments,
            'checkedFiles': checked_files,
            'recipe': {name: hashlib.sha256((HERE / name).read_bytes()).hexdigest()
                       for name in ['typecheck.py', 'wasmtime_host.py', 'bootstrap.py']},
            'milliseconds': round((time.perf_counter() - start) * 1000, 3),
            'requestIsolation': 'fresh Store, instance, file map and buffers; no preopens or inherited environment'}


def main():
    import argparse
    import json
    import sys
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--module-root', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = check_project(args.module_root, args.source)
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 1
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
