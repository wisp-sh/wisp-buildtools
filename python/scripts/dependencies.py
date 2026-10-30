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
from urllib.parse import urlsplit, parse_qs

from scripts.common import digest
from scripts.source_bundle import MAX_SOURCE_BYTES, NATIVE_SUFFIXES

MAX_ARCHIVE_BYTES = 16 * 1024 * 1024


from scripts._vendor.packaging.requirements import Requirement, InvalidRequirement
from scripts._vendor.packaging.specifiers import SpecifierSet
from scripts._vendor.packaging.version import Version
from scripts._vendor.packaging.tags import parse_tag, compatible_tags
from scripts._vendor.packaging.utils import parse_wheel_filename
from scripts.limits import GUEST_ENVIRONMENT


def normalized(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def requirement(value, *, pinned=False):
    """Parse metadata only. Artifact identity and hashes come from the local lock."""
    try:
        return Requirement(value)
    except InvalidRequirement as error:
        raise ValueError('invalid Python requirement: ' + value) from error


def satisfies(version, constraints):
    return SpecifierSet(constraints).contains(Version(version), prereleases=True)


def enabled(req, extras=(), environment=None):
    env = dict(GUEST_ENVIRONMENT, **(environment or {}))
    return req.marker is None or any(req.marker.evaluate(dict(env, extra=extra))
                                     for extra in {'', *extras})


def project_requirements(root, extras=()):
    """Read PEP 621 / requirements files without invoking any build backend."""
    requested, hashes, inputs, constraints = [], {}, {}, []
    settings = {}
    pyproject = _path(root, 'pyproject.toml')
    if pyproject.is_file():
        inputs['pyproject.toml'] = digest(pyproject)
        document = tomllib.loads(pyproject.read_text())
        project = document.get('project', {})
        if {'dependencies', 'optional-dependencies'} & set(project.get('dynamic', [])):
            raise ValueError('dynamic Python dependencies require prebuilt, locked wheel metadata')
        if project.get('requires-python') and not satisfies(GUEST_ENVIRONMENT['python_full_version'], project['requires-python']):
            raise ValueError('Python project excludes guest CPython 3.14')
        settings = document.get('tool', {}).get('wisp', {}).get('python', {})
        extras = set(extras) | set(settings.get('extras', []))
        requested += [requirement(value) for value in project.get('dependencies', [])]
        optional = project.get('optional-dependencies', {})
        for extra in sorted(extras):
            if extra not in optional:
                raise ValueError('unknown Python project extra: ' + extra)
            requested += [requirement(value) for value in optional[extra]]
    seen = set()
    def read(path, constraint=False):
        path = _path(root, path)
        relative = path.relative_to(root).as_posix()
        identity = (relative, constraint)
        if identity in seen:
            return
        if len(seen) >= 64:
            raise ValueError('Python requirements include limit is 64 files')
        seen.add(identity)
        if path.stat().st_size > 1024 * 1024:
            raise ValueError('Python requirements file exceeds 1 MiB: ' + relative)
        inputs[relative] = digest(path)
        text = path.read_text().replace('\\\n', ' ')
        for line in text.splitlines():
            line = re.split(r'\s+#', line.strip(), maxsplit=1)[0].strip()
            if not line or line.startswith('#'):
                continue
            include = re.fullmatch(r'(?:-r|--requirement(?:=|\s+))\s*(.+)', line)
            limits = re.fullmatch(r'(?:-c|--constraint(?:=|\s+))\s*(.+)', line)
            if include or limits:
                target = (path.parent / (include or limits)[1]).relative_to(root).as_posix()
                read(target, constraint or limits is not None)
                continue
            allowed = set(re.findall(r'\s+--hash=sha256:([0-9a-f]{64})(?=\s|$)', line))
            value = re.sub(r'\s+--hash=sha256:[0-9a-f]{64}(?=\s|$)', '', line)
            req = requirement(value)
            (constraints if constraint else requested).append(req)
            if allowed:
                name = normalized(req.name)
                hashes.setdefault(name, []).append((req, allowed))
    if (root / 'requirements.txt').is_file():
        read('requirements.txt')
    return requested, hashes, inputs, constraints, settings


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


def prepare(root: Path, destination: Path, *, extras=(), environment=None):
    """Return verified roots and a receipt; all resolution uses local lock entries."""
    root = root.resolve(strict=True)
    requested, hashes, inputs, constraints, settings = project_requirements(root, extras)
    requested = [req for req in requested if enabled(req, environment=environment)]
    hashes = {name: [allowed for req, allowed in records if enabled(req, environment=environment)]
              for name, records in hashes.items()}
    if not requested:
        return [], {'inputs': inputs, 'packages': [], 'excludedPaths': []}
    lock = _path(root, 'python-deps.lock.json')
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
    expanded = {}
    package_metadata = {}
    destination.mkdir(parents=True, exist_ok=True)
    roots = []
    excluded = []
    total = 0

    def resolve(req, parent_extras=()):
        nonlocal total
        if not enabled(req, parent_extras, environment):
            return
        name = normalized(req.name)
        if name not in packages:
            raise ValueError('Python dependency missing from offline lock: ' + name)
        package = packages[name]
        version = Version(package['version'])
        if not req.specifier.contains(version, prereleases=True):
            raise ValueError(f'Python dependency {name}=={version} does not satisfy {req.specifier}')
        for constraint in constraints:
            if normalized(constraint.name) == name and enabled(constraint, environment=environment):
                if (not constraint.specifier.contains(version, prereleases=True)
                        or constraint.url and constraint.url != package.get('url')):
                    raise ValueError(f'locked Python dependency {name} violates constraint {constraint}')
        if req.url and package.get('url') != req.url:
            raise ValueError('Python requirement URL must match the offline lock identity: ' + name)
        expected = package.get('sha256', '')
        if not re.fullmatch('[0-9a-f]{64}', expected) or any(expected not in allowed for allowed in hashes.get(name, [])):
            raise ValueError('missing or conflicting SHA-256 for Python dependency ' + name)
        if req.url:
            url_hashes = parse_qs(urlsplit(req.url).fragment).get('sha256', [])
            if url_hashes and any(value != expected for value in url_hashes):
                raise ValueError('Python requirement URL hash differs from offline lock: ' + name)
        requested_extras = set(req.extras)
        if name in selected:
            expand(name, requested_extras)
            return
        path = _path(root, package['path'])
        target = destination / name
        target.mkdir()
        sources = {}
        dependencies = []
        if path.is_file() and path.suffix == '.whl':
            wheel_name, wheel_version, _, filename_tags = parse_wheel_filename(path.name)
            if normalized(wheel_name) != name or wheel_version != version:
                raise ValueError('Python wheel filename name/version differs from lock: ' + name)
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
                tags = {tag for line in info.get_all('Tag', []) for tag in parse_tag(line)}
                if tags != filename_tags or wheel_info[0].filename.rsplit('/', 1)[0] != metadata[0].filename.rsplit('/', 1)[0]:
                    raise ValueError('Python wheel metadata differs from its filename or dist-info directory: ' + name)
                compatible = set(compatible_tags(python_version=(3, 14), interpreter='cp314', platforms=['any']))
                if info.get('Root-Is-Purelib', '').lower() != 'true' or not tags or any(tag.abi != 'none' or tag.platform != 'any' for tag in tags) or not tags & compatible:
                    raise ValueError('native Python extensions are unsupported; require a pure Python *-none-any wheel: ' + name)
                meta = parser.parsestr(wheel.read(metadata[0]).decode())
                if normalized(meta.get('Name', '')) != name or Version(meta.get('Version', '')) != version:
                    raise ValueError('Python wheel name/version differs from lock: ' + name)
                dependencies = meta.get_all('Requires-Dist', [])
                available_extras = {normalized(value) for value in meta.get_all('Provides-Extra', [])}
                if meta.get('Requires-Python') and not satisfies('3.14.0', meta['Requires-Python'].replace(' ', '')):
                    raise ValueError('Python wheel excludes guest CPython 3.14: ' + name)
                for entry in entries:
                    relative = PurePosixPath(entry.filename)
                    if relative.is_absolute() or '..' in relative.parts or '\\' in entry.filename or stat.S_ISLNK(entry.external_attr >> 16):
                        raise ValueError('unsafe Python wheel entry: ' + entry.filename)
                    if relative.suffix in NATIVE_SUFFIXES:
                        raise ValueError('native Python extensions are unsupported: ' + entry.filename)
                    if entry.is_dir() or relative.suffix == '.pyi' or relative.name == 'py.typed':
                        continue
                    parts = relative.parts
                    if parts[0].endswith('.data'):
                        if len(parts) < 3 or parts[1] != 'purelib':
                            raise ValueError('unsupported Python wheel installation scheme: ' + entry.filename)
                        relative = PurePosixPath(*parts[2:])
                    if relative.suffix in ('.pyc', '.pyo'):
                        raise ValueError('Python dependencies must provide source, not bytecode: ' + entry.filename)
                    if relative.as_posix() in sources:
                        raise ValueError('Python wheel relocation collision: ' + str(relative))
                    sources[relative.as_posix()] = wheel.read(entry)
        elif path.is_dir():
            if package.get('requires-python') and not satisfies(GUEST_ENVIRONMENT['python_full_version'], package['requires-python']):
                raise ValueError('vendored Python dependency excludes guest CPython 3.14: ' + name)
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
                if child.suffix in NATIVE_SUFFIXES:
                    raise ValueError('native Python extensions are unsupported: ' + relative)
                if child.suffix in ('.pyc', '.pyo'):
                    raise ValueError('Python dependencies must provide source, not bytecode: ' + relative)
                vendor_bytes += child.stat().st_size
                if vendor_bytes > MAX_SOURCE_BYTES - total:
                    raise ValueError('Python dependencies exceed the 16 MiB source/data budget')
                if digest(child) != sha:
                    raise ValueError('vendored Python checksum mismatch: ' + relative)
                sources[relative] = child.read_bytes()
                inputs[package['path'].rstrip('/') + '/' + relative] = sha
            dependencies = package.get('requires', [])
            available_extras = {normalized(value) for value in package.get('extras', [])}
            info = normalized(name).replace('-', '_') + '-' + str(version) + '.dist-info/METADATA'
            sources.setdefault(info, ('Metadata-Version: 2.3\nName: ' + name + '\nVersion: ' + str(version) + '\n'
                + ''.join('Requires-Dist: ' + value + '\n' for value in dependencies)).encode('utf-8'))
        else:
            raise ValueError('Python dependency must be a local .whl or vendor directory: ' + name)
        if not any(relative.endswith('.py') for relative in sources):
            raise ValueError('Python dependency contains no Python modules: ' + name)
        for relative, data in sources.items():
            total += len(data)
            if total > MAX_SOURCE_BYTES:
                raise ValueError('Python dependencies exceed the 16 MiB source/data budget')
            file = _path(target, relative)
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(data)
        selected[name] = {'name': name, 'version': package['version'], 'sha256': expected}
        roots.append(target)
        package_metadata[name] = (dependencies, available_extras)
        expand(name, requested_extras)

    def expand(name, extras):
        dependencies, available = package_metadata[name]
        extras = {normalized(extra) for extra in extras}
        if extras - available:
            raise ValueError('unknown Python dependency extras for ' + name + ': ' + ', '.join(sorted(extras - available)))
        previous = expanded.get(name)
        combined = (previous or set()) | extras
        if previous is not None and combined == previous:
            return
        expanded[name] = combined
        selected[name]['extras'] = sorted(combined)
        for value in dependencies:
            resolve(requirement(value), combined)

    for req in requested:
        resolve(req)
    return roots, {'inputs': dict(sorted(inputs.items())), 'packages': [selected[k] for k in sorted(selected)],
                   'excludedPaths': sorted(excluded)}
