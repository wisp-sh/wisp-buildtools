"""Read and hash application bytes. Application parsing happens in guest CPython."""
from __future__ import annotations

import base64
import hashlib
import json
import keyword
from pathlib import Path

from scripts.limits import MAX_SOURCE_BYTES, MAX_PROGRAM_BYTES, MAX_MODULES, MAX_FILES

EXCLUDED = {'__pycache__', 'node_modules', 'target', 'dist', 'build', 'work'}
NATIVE_SUFFIXES = {'.so', '.pyd', '.dll', '.dylib'}
RESERVED = {'wit_world', 'componentize_py_types', 'componentize_py_runtime'}
LOADER_VERSION = 2
VIRTUAL_ROOT = '/__wisp__/'


def default_root(source):
    root = source.parent
    while (root / '__init__.py').is_file():
        root = root.parent
    return root


def collect(source: Path, package_root: Path | None = None, python_paths=(), *, excluded_paths=()):
    source = source.resolve(strict=True)
    root = (package_root or default_root(source)).resolve(strict=True)
    if not root.is_dir() or not source.is_relative_to(root):
        raise ValueError('Python entrypoint must be inside --package-root')
    roots = [root, *(path.resolve(strict=True) for path in python_paths)]
    if any(not path.is_dir() for path in roots):
        raise ValueError('Python search paths must be directories')
    excluded_paths = {path.resolve() for path in excluded_paths}
    for index, path in enumerate(roots):
        overlap = any(path.is_relative_to(other) or other.is_relative_to(path) for other in roots[:index])
        isolated_scratch = path in excluded_paths and path != root and path.is_relative_to(root)
        if overlap and not isolated_scratch:
            raise ValueError('Python source roots must not overlap')
    modules, assets, origins, inputs, paths = {}, {}, {}, {}, {}
    total, entry = 0, None

    def add(name, record, origin):
        previous = modules.get(name)
        if previous is not None:
            if previous[0] is None and record[0] is None:
                origins[name].add(origin)
                return
            if previous[0] is not None and record[0] is None and previous[2] and origins[name] == {origin}:
                return
            if previous[0] is None and record[0] is not None and record[2] and origins[name] == {origin}:
                modules[name] = record
                return
            raise ValueError(f'ambiguous Python module: {name}')
        if len(modules) >= MAX_MODULES:
            raise ValueError('Python source bundle exceeds 4096 modules')
        modules[name], origins[name] = record, {origin}

    def visit(directory, owner, label):
        nonlocal total, entry
        for path in sorted(directory.iterdir()):
            if path.absolute() in excluded_paths:
                continue
            if path.name.startswith('.') or path.name in EXCLUDED or path.name.endswith('.egg-info'):
                continue
            if path.is_symlink():
                raise ValueError(f'symlink in Python source input: {path.relative_to(owner)}')
            if path.is_dir():
                visit(path, owner, label)
                continue
            if path.suffix in NATIVE_SUFFIXES:
                raise ValueError(f'native Python extensions cannot be bundled: {path.relative_to(owner)}')
            if path.suffix in ('.pyc', '.pyo', '.pyi', '.whl') or path.name == 'py.typed':
                continue
            relative = path.relative_to(owner)
            metadata = any(part.endswith('.dist-info') for part in relative.parts)
            if metadata and path.suffix == '.py':
                continue
            if not path.is_file():
                raise ValueError('Python inputs must be regular files: ' + str(relative))
            if len(inputs) >= MAX_FILES:
                raise ValueError('Python source/data file limit is 16384')
            with path.open('rb') as stream:
                data = stream.read(MAX_SOURCE_BYTES - total + 1)
            total += len(data)
            if total > MAX_SOURCE_BYTES:
                raise ValueError('Python sources and package data exceed the 16 MiB aggregate source limit')
            digest = hashlib.sha256(data).hexdigest()
            inputs[label + '/' + relative.as_posix()] = digest
            paths[str(path)] = digest
            virtual = VIRTUAL_ROOT + relative.as_posix()
            if virtual in assets:
                raise ValueError('ambiguous Python package data: ' + relative.as_posix())
            assets[virtual] = base64.b64encode(data).decode('ascii')
            if path.suffix != '.py' or metadata:
                continue
            parts = list(relative.with_suffix('').parts)
            package = parts[-1] == '__init__'
            if package:
                parts.pop()
            if not parts:
                if path == source:
                    raise ValueError('select the parent of the entry package with --package-root')
                continue
            if any(not part.isidentifier() or keyword.iskeyword(part) for part in parts):
                raise ValueError(f'invalid Python module path: {relative}')
            if parts[0] in RESERVED or parts[0].startswith('_wisp_'):
                raise ValueError(f'Python module name is reserved by the runtime: {parts[0]}')
            add('.'.join(parts), [data.decode('utf-8-sig'), virtual, package], label)
            # Source is already represented as text; encode only data assets.
            del assets[virtual]
            for count in range(1, len(parts)):
                parent = '.'.join(parts[:count])
                add(parent, [None, VIRTUAL_ROOT + '/'.join(parts[:count]) + '/__init__.py', True], label)
            if path == source:
                entry = '.'.join(parts)

    for index, owner in enumerate(roots):
        visit(owner, owner, 'project' if index == 0 else f'python-path-{index}')
    if entry is None:
        raise ValueError('Python entrypoint was excluded from the source bundle')
    for name in modules:
        parent = name.rpartition('.')[0]
        if parent and not modules[parent][2]:
            raise ValueError(f'Python module {parent} cannot also contain submodules')
    return {'entry': entry, 'modules': modules, 'assets': assets,
            'inputs': dict(sorted(inputs.items())), 'paths': dict(sorted(paths.items())),
            'sourceBytes': total}


def encode(program, *, exports=None):
    loader = Path(__file__).with_name('source_loader.py').read_text()
    serialized = json.dumps({'modules': program['modules'], 'assets': program['assets']},
                            sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    payload = (loader + '\nimport json as __wisp_json\n'
               + 'globals().update(__wisp_load_bundle(__wisp_json.loads(' + repr(serialized)
               + '), ' + repr(program['entry']) + '))\n'
               + '__wisp_export_names__ = ' + repr(exports or {}) + '\n').encode('utf-8')
    if len(payload) > MAX_PROGRAM_BYTES:
        raise ValueError('Python source bundle and loader exceed the 32 MiB packaged-source limit')
    details = {'entryModule': program['entry'], 'inputs': program['inputs'],
               'bundledSourceSha256': hashlib.sha256(payload).hexdigest(),
               'moduleCount': len(program['modules']), 'dataFileCount': len(program['assets']),
               'sourceBytes': program['sourceBytes'], 'bundledBytes': len(payload)}
    return payload, details


def bundle(source: Path, package_root: Path | None = None, python_paths=(), *, excluded_paths=(), exports=None):
    return encode(collect(source, package_root, python_paths, excluded_paths=excluded_paths), exports=exports)
