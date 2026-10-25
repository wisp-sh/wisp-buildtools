"""Offline, fail-closed selection of hash-locked pure Python wheels/vendors.

Only metadata and bytes are read on the host. No installer or package code runs.
"""
import email.parser
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import tomllib
import zipfile

from scripts.common import digest
from scripts.source_bundle import MAX_SOURCE_BYTES, NATIVE_SUFFIXES

MAX_ARCHIVE_BYTES = 16 * 1024 * 1024


def normalized(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def requirement(value, *, pinned=False):
    # Markers, extras, URLs and editable installs must be resolved before locking.
    match = re.fullmatch(r'\s*([A-Za-z0-9][A-Za-z0-9_.-]*)\s*(?:\(?\s*((?:(?:===|==|!=|~=|<=|>=|<|>)\s*[0-9][0-9.*]*(?:\s*,\s*)?)+)\s*\)?)?\s*', value)
    if not match:
        raise ValueError('unsupported Python requirement; use plain pinned local dependencies: ' + value)
    constraints = (match[2] or '').replace(' ', '')
    if pinned and not re.fullmatch(r'==[0-9]+(?:\.[0-9]+)*', constraints):
        raise ValueError('Python project dependencies must pin an exact stable version: ' + value)
    return normalized(match[1]), constraints


def satisfies(version, constraints):
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)*', version):
        raise ValueError('Python dependency versions must be stable numeric releases: ' + version)
    def parts(value):
        result = tuple(int(p) for p in value.split('.'))
        while result and result[-1] == 0:
            result = result[:-1]
        return result
    actual = parts(version)
    for constraint in filter(None, constraints.split(',')):
        match = re.fullmatch(r'(==|!=|~=|<=|>=|<|>)([0-9]+(?:\.[0-9]+)*(?:\.\*)?)', constraint)
        if not match:
            raise ValueError('unsupported Python version constraint: ' + constraint)
        operator, expected = match.groups()
        if expected.endswith('.*'):
            if operator not in ('==', '!='):
                raise ValueError('wildcard only supported with == or !=')
            prefix = tuple(int(p) for p in expected.split('.')[:-1])
            equal = tuple(int(p) for p in version.split('.')[:len(prefix)]) == prefix
            valid = equal if operator == '==' else not equal
        else:
            target = parts(expected)
            if operator == '~=':
                prefix = tuple(int(p) for p in expected.split('.')[:-1])
                valid = actual >= target and tuple(int(p) for p in version.split('.')[:len(prefix)]) == prefix
            else:
                valid = {'==': actual == target, '!=': actual != target, '<': actual < target,
                         '<=': actual <= target, '>': actual > target, '>=': actual >= target}[operator]
        if not valid:
            return False
    return True


def _path(root, name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or '..' in path.parts or '\\' in name:
        raise ValueError('unsafe Python dependency path: ' + name)
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('symlink in Python dependency: ' + name)
    if not current.resolve().is_relative_to(root.resolve()):
        raise ValueError('Python dependency escapes project: ' + name)
    return current


def prepare(root: Path, destination: Path):
    """Return roots to bundle and a receipt; destination is task-owned scratch."""
    requested = []
    hashes = {}
    inputs = {}
    requirements = root / 'requirements.txt'
    pyproject = root / 'pyproject.toml'
    if requirements.is_file():
        inputs['requirements.txt'] = digest(requirements)
        for line in requirements.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            value, _, suffix = line.partition(' --hash=sha256:')
            name, constraint = requirement(value, pinned=True)
            if suffix and not re.fullmatch('[0-9a-f]{64}', suffix):
                raise ValueError('invalid requirements.txt SHA-256 for ' + name)
            if suffix:
                hashes[name] = suffix
            requested.append((name, constraint))
    if pyproject.is_file():
        inputs['pyproject.toml'] = digest(pyproject)
        project = tomllib.loads(pyproject.read_text()).get('project', {})
        if 'dependencies' in project.get('dynamic', []):
            raise ValueError('dynamic Python dependencies must be pinned before building')
        requested += [requirement(value, pinned=True) for value in project.get('dependencies', [])]
    if not requested:
        return [], {'inputs': inputs, 'packages': [], 'excludedPaths': []}
    lock = root / 'python-deps.lock.json'
    if not lock.is_file():
        raise ValueError('Python dependencies require python-deps.lock.json with local paths, versions and SHA-256 hashes')
    inputs[lock.name] = digest(lock)
    document = json.loads(lock.read_text())
    if document.get('schema') != 1 or not isinstance(document.get('packages'), list) or len(document['packages']) > 256:
        raise ValueError('unsupported Python dependency lock schema')
    packages = {}
    for package in document['packages']:
        name = normalized(package['name'])
        if name in packages:
            raise ValueError('duplicate Python dependency lock entry: ' + name)
        packages[name] = package
    selected = {}
    visiting = set()
    destination.mkdir(parents=True, exist_ok=True)
    roots = []
    excluded = []
    total = 0

    def resolve(name, constraints):
        nonlocal total
        if name not in packages:
            raise ValueError('Python dependency missing from offline lock: ' + name)
        package = packages[name]
        if not satisfies(package['version'], constraints):
            raise ValueError(f'Python dependency {name}=={package["version"]} does not satisfy {constraints}')
        if name in selected or name in visiting:
            return
        visiting.add(name)
        expected = package.get('sha256', '')
        if not re.fullmatch('[0-9a-f]{64}', expected) or hashes.get(name, expected) != expected:
            raise ValueError('missing or conflicting SHA-256 for Python dependency ' + name)
        path = _path(root, package['path'])
        target = destination / name
        target.mkdir()
        sources = {}
        dependencies = []
        if path.is_file() and path.suffix == '.whl':
            if path.stat().st_size > MAX_ARCHIVE_BYTES or digest(path) != expected:
                raise ValueError('Python wheel checksum mismatch or wheel exceeds 16 MiB: ' + name)
            inputs[package['path']] = expected
            with zipfile.ZipFile(path) as wheel:
                entries = wheel.infolist()
                if len(entries) > 8192 or sum(e.file_size for e in entries) > MAX_ARCHIVE_BYTES:
                    raise ValueError('Python wheel exceeds extraction budget: ' + name)
                inventory = {e.filename: e for e in entries}
                if len(inventory) != len(entries):
                    raise ValueError('duplicate Python wheel entry: ' + name)
                wheel_info = [e for e in entries if e.filename.endswith('.dist-info/WHEEL')]
                metadata = [e for e in entries if e.filename.endswith('.dist-info/METADATA')]
                if len(wheel_info) != 1 or len(metadata) != 1:
                    raise ValueError('Python wheel metadata missing or ambiguous: ' + name)
                parser = email.parser.Parser()
                info = parser.parsestr(wheel.read(wheel_info[0]).decode())
                tags = info.get_all('Tag', [])
                if info.get('Root-Is-Purelib', '').lower() != 'true' or not tags or any(not re.fullmatch(r'py[0-9]+(?:\.py[0-9]+)*-none-any', tag) for tag in tags):
                    raise ValueError('native Python extensions are unsupported; require a pure Python *-none-any wheel: ' + name)
                meta = parser.parsestr(wheel.read(metadata[0]).decode())
                if normalized(meta.get('Name', '')) != name or meta.get('Version') != package['version']:
                    raise ValueError('Python wheel name/version differs from lock: ' + name)
                dependencies = meta.get_all('Requires-Dist', [])
                if meta.get('Requires-Python') and not satisfies('3.14.0', meta['Requires-Python'].replace(' ', '')):
                    raise ValueError('Python wheel excludes guest CPython 3.14: ' + name)
                for entry in entries:
                    relative = PurePosixPath(entry.filename)
                    if relative.is_absolute() or '..' in relative.parts or '\\' in entry.filename or stat.S_ISLNK(entry.external_attr >> 16):
                        raise ValueError('unsafe Python wheel entry: ' + entry.filename)
                    if relative.suffix in NATIVE_SUFFIXES:
                        raise ValueError('native Python extensions are unsupported: ' + entry.filename)
                    if entry.is_dir() or relative.suffix == '.pyi' or relative.name == 'py.typed' or any(p.endswith('.dist-info') for p in relative.parts):
                        continue
                    if '.data' in ''.join(relative.parts) or relative.suffix != '.py':
                        raise ValueError('Python wheel requires unsupported package data or relocation: ' + entry.filename)
                    sources[entry.filename] = wheel.read(entry)
        elif path.is_dir():
            excluded.append(package['path'])
            files = package.get('files')
            if not isinstance(files, dict) or not files or len(files) > 8192:
                raise ValueError('vendored Python dependencies require a per-file SHA-256 inventory: ' + name)
            identity = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            if identity != expected:
                raise ValueError('vendored Python inventory checksum mismatch: ' + name)
            actual = set()
            for child in path.rglob('*'):
                if child.is_symlink():
                    raise ValueError('symlink in vendored Python dependency: ' + name)
                if child.is_file():
                    actual.add(child.relative_to(path).as_posix())
            if actual != set(files):
                raise ValueError('vendored Python files differ from locked inventory: ' + name)
            vendor_bytes = 0
            for relative, sha in files.items():
                child = _path(path, relative)
                if child.suffix != '.py':
                    raise ValueError('vendored Python package data/native extensions are unsupported: ' + relative)
                vendor_bytes += child.stat().st_size
                if vendor_bytes > MAX_SOURCE_BYTES - total:
                    raise ValueError('Python dependencies exceed the 1 MiB source budget')
                if digest(child) != sha:
                    raise ValueError('vendored Python checksum mismatch: ' + relative)
                sources[relative] = child.read_bytes()
                inputs[package['path'].rstrip('/') + '/' + relative] = sha
            dependencies = package.get('requires', [])
        else:
            raise ValueError('Python dependency must be a local .whl or vendor directory: ' + name)
        if not sources:
            raise ValueError('Python dependency contains no Python modules: ' + name)
        for relative, data in sources.items():
            total += len(data)
            if total > MAX_SOURCE_BYTES:
                raise ValueError('Python dependencies exceed the 1 MiB source budget')
            file = _path(target, relative)
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(data)
        selected[name] = {'name': name, 'version': package['version'], 'sha256': expected}
        roots.append(target)
        for dependency in dependencies:
            resolve(*requirement(dependency))
        visiting.remove(name)

    for name, constraint in requested:
        resolve(name, constraint)
    return roots, {'inputs': dict(sorted(inputs.items())), 'packages': [selected[k] for k in sorted(selected)],
                   'excludedPaths': sorted(excluded)}
