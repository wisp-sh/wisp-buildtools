"""Read and hash Python sources without importing or parsing application code."""
from __future__ import annotations

import hashlib
import json
import keyword
from pathlib import Path

MAX_SOURCE_BYTES = 1024 * 1024
MAX_MODULES = 4096
EXCLUDED = {'__pycache__', 'node_modules', 'target', 'dist', 'build', 'work'}
NATIVE_SUFFIXES = {'.so', '.pyd', '.dll', '.dylib'}
RESERVED = {'wit_world', 'componentize_py_types', 'componentize_py_runtime'}
LOADER_VERSION = 1


def default_root(source):
    root = source.parent
    while (root / '__init__.py').is_file():
        root = root.parent
    return root


def bundle(source: Path, package_root: Path | None = None, python_paths=(), *, excluded_paths=()):
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
    modules = {}
    origins = {}
    inputs = {}
    total = 0
    entry = None

    def add(name, record, origin):
        previous = modules.get(name)
        if previous is not None:
            if previous[0] is None and record[0] is None:
                origins[name].add(origin)
                return  # Merge namespace packages contributed by several roots.
            if previous[0] is not None and record[0] is None and previous[2] and origins[name] == {origin}:
                return
            if previous[0] is None and record[0] is not None and record[2] and origins[name] == {origin}:
                modules[name] = record
                return
            raise ValueError(f'ambiguous Python module: {name}')
        if len(modules) >= MAX_MODULES:
            raise ValueError('Python source bundle exceeds 4096 modules')
        modules[name] = record
        origins[name] = {origin}

    def visit(directory, owner, label):
        nonlocal total, entry
        for path in sorted(directory.iterdir()):
            if path.absolute() in excluded_paths:
                continue
            if path.name.startswith('.') or path.name in EXCLUDED or path.name.endswith(('.dist-info', '.egg-info')):
                continue
            if path.is_symlink():
                raise ValueError(f'symlink in Python source input: {path.relative_to(owner)}')
            if path.is_dir():
                visit(path, owner, label)
                continue
            if path.suffix in NATIVE_SUFFIXES:
                raise ValueError(f'native Python extensions cannot be bundled: {path.relative_to(owner)}')
            if path.suffix != '.py':
                continue
            relative = path.relative_to(owner)
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
            remaining = MAX_SOURCE_BYTES - total
            with path.open('rb') as stream:
                data = stream.read(remaining + 1)
            total += len(data)
            if total > MAX_SOURCE_BYTES:
                raise ValueError('Python sources exceed the 1 MiB aggregate source limit')
            text = data.decode('utf-8-sig')
            module = '.'.join(parts)
            virtual = '<wisp>/' + relative.as_posix()
            add(module, [text, virtual, package], label)
            for count in range(1, len(parts)):
                parent = '.'.join(parts[:count])
                add(parent, [None, '<wisp>/' + '/'.join(parts[:count]) + '/__init__.py', True], label)
            inputs[label + '/' + relative.as_posix()] = hashlib.sha256(data).hexdigest()
            if path == source:
                entry = module

    for index, owner in enumerate(roots):
        visit(owner, owner, 'project' if index == 0 else f'python-path-{index}')
    if entry is None:
        raise ValueError('Python entrypoint was excluded from the source bundle')
    for name, record in modules.items():
        parent, _, _ = name.rpartition('.')
        if parent and not modules[parent][2]:
            raise ValueError(f'Python module {parent} cannot also contain submodules')
    serialized = json.dumps(modules, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    loader = Path(__file__).with_name('source_loader.py').read_text()
    payload = (loader + '\nimport json as __wisp_json\n'
               + 'globals().update(__wisp_load_bundle(__wisp_json.loads('
               + repr(serialized) + '), ' + repr(entry) + '))\n').encode('utf-8')
    if len(payload) > MAX_SOURCE_BYTES:
        raise ValueError('Python source bundle and loader exceed the 1 MiB packaged-source limit')
    return payload, {'entryModule': entry, 'inputs': dict(sorted(inputs.items())),
                     'bundledSourceSha256': hashlib.sha256(payload).hexdigest(),
                     'moduleCount': len(modules), 'sourceBytes': total, 'bundledBytes': len(payload)}
