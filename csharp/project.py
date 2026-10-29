"""Bounded, offline C# project inputs. This reads metadata; it does not run MSBuild."""
from __future__ import annotations
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import xml.etree.ElementTree as ET
import zipfile

EXCLUDED = {'.git', '.tools', '.toolchains', 'bin', 'obj', 'build', 'dist', 'target', 'node_modules', 'out'}
MAX_SOURCE_BYTES = 8 * 1024 * 1024

@dataclass
class Project:
    name: str
    sources: list[Path]
    references: list[Path] = field(default_factory=list)
    projects: list['Project'] = field(default_factory=list)
    packages: dict[str, str] = field(default_factory=dict)
    options: dict = field(default_factory=dict)
    path: Path | None = None

class Inputs:
    def __init__(self, sources, root=None):
        sources = [Path(p).absolute() for p in sources]
        if not sources:
            raise ValueError('provide C# files, a directory, or one .csproj')
        first = sources[0] if sources[0].is_dir() else sources[0].parent
        if root is None:
            root = first
            for parent in [first, *first.parents]:
                if (parent / '.git').exists() or list(parent.glob('*.sln')) or list(parent.glob('*.slnx')):
                    root = parent
                    break
        self.root = Path(root).resolve(strict=True)
        self.records = {}
        self.projects = {}
        self.active = set()
        if len(sources) == 1 and sources[0].is_dir():
            candidates = sorted(sources[0].glob('*.csproj'))
            if len(candidates) > 1:
                raise ValueError('directory has multiple .csproj files; select one explicitly')
            if candidates:
                sources = candidates
        if len(sources) == 1 and sources[0].suffix.lower() == '.csproj':
            self.main = self.project(sources[0])
        else:
            files = []
            for source in sources:
                if source.is_dir(): files.extend(self.glob_sources(source))
                elif source.suffix.lower() == '.cs': files.append(self.checked(source))
                else: raise ValueError('expected .cs files, directories, or one .csproj')
            self.main = Project('Application', sorted(set(files)))
        self.order = []
        visited = set()
        def visit(project):
            if id(project) in visited: return
            visited.add(id(project))
            for child in project.projects: visit(child)
            self.order.append(project)
        visit(self.main)
        total = 0
        for project in self.order:
            if not project.sources or len(project.sources) > 256:
                raise ValueError('each project requires between 1 and 256 C# source files')
            for path in project.sources:
                with path.open('rb') as stream: data = stream.read(MAX_SOURCE_BYTES + 1)
                total += len(data)
                if total > MAX_SOURCE_BYTES: raise ValueError('project graph exceeds 8 MiB of C# source')
                data.decode('utf-8-sig', errors='strict')
                self.record(path, data)
        if len(self.order) > 32 or total > MAX_SOURCE_BYTES:
            raise ValueError('project graph exceeds 32 projects or 8 MiB of C# source')
        names = [p.name.casefold() for p in self.order]
        if len(names) != len(set(names)):
            raise ValueError('project assembly names must be unique')

    def checked(self, path):
        path = Path(os.path.abspath(path))
        try: relative = path.relative_to(self.root)
        except ValueError: raise ValueError(f'input is outside --project-root: {path}') from None
        current = self.root
        for part in relative.parts:
            current /= part
            if current.is_symlink(): raise ValueError(f'symlink inputs are unsupported: {path}')
        if not path.is_file(): raise ValueError(f'expected regular input file: {path}')
        return path

    def record(self, path, data=None):
        if data is None: data = path.read_bytes()
        self.records[str(path.relative_to(self.root))] = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}

    def glob_sources(self, directory):
        return [self.checked(p) for p in sorted(directory.rglob('*.cs')) if not any(x in EXCLUDED for x in p.relative_to(directory).parts)]

    def project(self, path):
        path = self.checked(path)
        if path in self.active: raise ValueError('cyclic ProjectReference graph')
        if path in self.projects: return self.projects[path]
        if len(self.projects) >= 32: raise ValueError('project graph exceeds 32 projects')
        if path.stat().st_size > 1024 * 1024: raise ValueError('project metadata exceeds 1 MiB')
        self.active.add(path)
        data = path.read_bytes(); self.record(path, data)
        try: tree = ET.fromstring(data)
        except ET.ParseError as error: raise ValueError(f'invalid project XML: {error}') from error
        if tree.tag != 'Project' or tree.get('Sdk', 'Microsoft.NET.Sdk') != 'Microsoft.NET.Sdk':
            raise ValueError('only Microsoft.NET.Sdk projects are supported')
        if set(tree.attrib) - {'Sdk'}: raise ValueError('unsupported Project attributes')
        properties = {}
        items = []
        for group in tree:
            if group.tag not in {'PropertyGroup', 'ItemGroup'}: raise ValueError(f'unsupported project build action: {group.tag}')
            if group.attrib: raise ValueError('conditional project groups are unsupported')
            if group.tag == 'PropertyGroup':
                for item in group:
                    if item.attrib or list(item): raise ValueError('conditional or nested properties are unsupported')
                    properties[item.tag] = (item.text or '').strip()
            elif group.tag == 'ItemGroup': items.extend(group)
            else: raise ValueError(f'unsupported project build action: {group.tag}')
        allowed = {'TargetFramework', 'AssemblyName', 'RootNamespace', 'OutputType', 'Nullable', 'ImplicitUsings',
                   'DefineConstants', 'LangVersion', 'AllowUnsafeBlocks', 'EnableDefaultCompileItems', 'Deterministic',
                   'TreatWarningsAsErrors', 'Description', 'Authors', 'Version', 'GenerateDocumentationFile',
                   'IsPackable', 'PackageId', 'RepositoryUrl', 'Copyright', 'Optimize', 'CheckForOverflowUnderflow'}
        if set(properties) - allowed: raise ValueError('unsupported project properties: ' + ', '.join(sorted(set(properties) - allowed)))
        if properties.get('TargetFramework') != 'net8.0': raise ValueError('projects must target net8.0')
        if properties.get('OutputType', 'Library').lower() != 'library': raise ValueError('only library projects are supported')
        if any('$(' in v or '@(' in v for v in properties.values()): raise ValueError('MSBuild expressions are unsupported')
        name = properties.get('AssemblyName', path.stem)
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,240}', name) or name in {'.', '..'}: raise ValueError('invalid assembly name')
        for key in ('AllowUnsafeBlocks', 'EnableDefaultCompileItems', 'Deterministic', 'TreatWarningsAsErrors', 'Optimize', 'CheckForOverflowUnderflow'):
            if key in properties and properties[key].lower() not in {'true', 'false'}: raise ValueError(f'{key} requires true or false')
        if properties.get('Deterministic', 'true').lower() != 'true': raise ValueError('C# builds require Deterministic=true')
        if properties.get('ImplicitUsings', 'disable').lower() not in {'enable', 'disable', 'true', 'false'}: raise ValueError('invalid ImplicitUsings value')
        if properties.get('Nullable', 'enable') not in {'enable', 'disable', 'annotations', 'warnings'}: raise ValueError('invalid Nullable value')
        project = Project(name, [], path=path, options={
            'nullable': properties.get('Nullable', 'enable'), 'implicitUsings': properties.get('ImplicitUsings', 'disable').lower() in {'enable', 'true'},
            'defines': [x.strip() for x in properties.get('DefineConstants', '').split(';') if x.strip()],
            'language': properties.get('LangVersion', '12.0'), 'unsafe': properties.get('AllowUnsafeBlocks', 'false').lower() == 'true',
            'warningsAsErrors': properties.get('TreatWarningsAsErrors', 'false').lower() == 'true',
            'optimize': properties.get('Optimize', 'true').lower() == 'true',
            'checked': properties.get('CheckForOverflowUnderflow', 'false').lower() == 'true'})
        self.projects[path] = project
        files = set(self.glob_sources(path.parent)) if properties.get('EnableDefaultCompileItems', 'true').lower() != 'false' else set()
        for item in items:
            if item.get('Condition') or any(c.get('Condition') for c in item): raise ValueError('conditional project items are unsupported')
            if item.tag == 'Compile':
                if set(item.attrib) - {'Include', 'Remove', 'Exclude'} or list(item): raise ValueError('unsupported Compile metadata')
                for operation in ('Include', 'Remove', 'Exclude'):
                    for pattern in item.get(operation, '').split(';'):
                        if not pattern: continue
                        pattern = pattern.replace('\\', '/')
                        if '$(' in pattern or '@(' in pattern or Path(pattern).is_absolute(): raise ValueError('unsupported Compile pattern')
                        matches = [self.checked(p) for p in path.parent.glob(pattern) if p.is_file()]
                        if operation == 'Include': files.update(matches)
                        else: files.difference_update(matches)
            elif item.tag == 'ProjectReference':
                if set(item.attrib) != {'Include'} or list(item): raise ValueError('unsupported ProjectReference metadata')
                project.projects.append(self.project(path.parent / item.attrib['Include'].replace('\\', '/')))
            elif item.tag == 'Reference':
                if set(item.attrib) != {'Include'} or any(c.tag not in {'HintPath', 'Private'} for c in item): raise ValueError('unsupported Reference metadata')
                hint = item.findtext('HintPath')
                if not hint: raise ValueError('assembly Reference requires a local HintPath')
                project.references.append(self.checked(path.parent / hint.replace('\\', '/')))
            elif item.tag == 'PackageReference':
                if set(item.attrib) - {'Include', 'Version'} or list(item): raise ValueError('unsupported PackageReference metadata')
                identity, version = item.get('Include', '').lower(), item.get('Version', '')
                if not identity or not re.fullmatch(r'\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?', version): raise ValueError('PackageReference requires an exact version')
                if identity in project.packages: raise ValueError('duplicate PackageReference')
                project.packages[identity] = version
            elif item.tag in {'None', 'Content'}:
                if list(item) or set(item.attrib) - {'Include', 'Remove', 'Update'}: raise ValueError('content copying and custom item metadata are unsupported')
            else: raise ValueError(f'unsupported project item: {item.tag}')
        project.sources = sorted(files)
        self.active.remove(path)
        return project


def packages(paths, destination):
    """Extract only portable implementation DLLs. Never restore or execute package build assets."""
    found = {}
    records = []
    extracted = 0
    for number, path in enumerate(sorted({Path(p).absolute() for p in paths})):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError('package must be a regular .nupkg file no larger than 32 MiB')
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or len(names) > 8192 or sum(i.file_size for i in archive.infolist()) > 64 * 1024 * 1024:
                raise ValueError('invalid or oversized NuGet archive')
            for name in names:
                p = PurePosixPath(name)
                if p.is_absolute() or '..' in p.parts or '\\' in name: raise ValueError('unsafe NuGet archive path')
            if any(n.startswith(('runtimes/', 'native/', 'build/', 'buildTransitive/', 'analyzers/', 'tools/')) and not n.endswith('/') for n in names):
                raise ValueError('NuGet packages with native assets, build tasks, tools or analyzers are unsupported')
            nuspecs = [n for n in names if n.endswith('.nuspec') and '/' not in n]
            if len(nuspecs) != 1: raise ValueError('package requires one root .nuspec')
            if archive.getinfo(nuspecs[0]).file_size > 1024 * 1024: raise ValueError('NuGet metadata exceeds 1 MiB')
            try: root = ET.fromstring(archive.read(nuspecs[0]))
            except ET.ParseError as error: raise ValueError(f'invalid NuGet XML: {error}') from error
            for element in root.iter(): element.tag = element.tag.rsplit('}', 1)[-1]
            metadata = root.find('metadata')
            if metadata is None: raise ValueError('package metadata is missing')
            identity = (metadata.findtext('id') or '').lower(); version = metadata.findtext('version') or ''
            if not re.fullmatch(r'[a-z0-9_.-]+', identity) or not re.fullmatch(r'\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?', version): raise ValueError('invalid package identity/version')
            if identity in found: raise ValueError('supply exactly one version per NuGet package')
            frameworks = ('net8.0', 'net7.0', 'net6.0', 'netstandard2.1', 'netstandard2.0')
            framework = next((f for f in frameworks if any(n.startswith('lib/' + f + '/') and n.endswith('.dll') for n in names)), None)
            if framework is None: raise ValueError(f'{identity}: no compatible managed lib implementation')
            target = destination / str(number); target.mkdir(parents=True)
            dlls = []
            for name in sorted(names):
                if name.startswith('lib/' + framework + '/') and name.endswith('.dll'):
                    if name.count('/') != 2: raise ValueError('localized/nested managed assemblies are unsupported')
                    data = archive.read(name)
                    extracted += len(data)
                    if len(data) > 16 * 1024 * 1024 or extracted > 32 * 1024 * 1024: raise ValueError('package assemblies exceed 16 MiB individually or 32 MiB combined')
                    out = target / PurePosixPath(name).name; out.write_bytes(data); dlls.append(out)
            dependencies = []
            node = metadata.find('dependencies')
            if node is not None:
                groups = list(node.findall('group'))
                if groups:
                    aliases = {'net8.0': 'net8.0', 'net7.0': 'net7.0', 'net6.0': 'net6.0', 'netstandard2.1': '.NETStandard2.1', 'netstandard2.0': '.NETStandard2.0'}
                    matching = [g for g in groups if g.get('targetFramework', '').lower() in {framework.lower(), aliases[framework].lower(), ''}]
                    if len(matching) > 1: raise ValueError('ambiguous NuGet dependency groups')
                    if not matching: raise ValueError('unsupported NuGet dependency target framework')
                    deps = list(matching[0])
                else: deps = list(node)
                for dependency in deps:
                    dependency_id = dependency.get('id', '').lower(); constraint = dependency.get('version', '')
                    dependencies.append((dependency_id, constraint))
            found[identity] = {'version': version, 'dlls': dlls, 'dependencies': dependencies}
            records.append({'path': path.name, 'id': identity, 'version': version, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    return found, records


def package_closure(requested, available):
    selected = set()
    def version_key(value):
        if '-' in value: raise ValueError('transitive prerelease ranges require an exact [version] constraint')
        parts = tuple(int(x) for x in value.split('.'))
        return parts + (0,) * (4 - len(parts))
    def accepts(value, constraint):
        if constraint == '[' + value + ']': return True
        if not constraint: return True
        if constraint[0:1] not in {'[', '('}: return version_key(value) >= version_key(constraint)
        if ',' not in constraint: return False
        left, right = constraint[1:-1].split(',', 1)
        current = version_key(value)
        lower = not left.strip() or (current >= version_key(left.strip()) if constraint[0] == '[' else current > version_key(left.strip()))
        upper = not right.strip() or (current <= version_key(right.strip()) if constraint[-1] == ']' else current < version_key(right.strip()))
        return lower and upper

    def visit(identity, constraint):
        package = available.get(identity.lower())
        if package is None: raise ValueError(f'offline package missing: {identity} {constraint}; supply --package or --package-dir')
        if not accepts(package['version'], constraint): raise ValueError(f'offline package version mismatch: {identity} {constraint}')
        if identity in selected: return
        selected.add(identity)
        for child, version in package['dependencies']: visit(child, version)
    for identity, version in requested.items(): visit(identity, '[' + version + ']')
    return [p for identity in sorted(selected) for p in available[identity]['dlls']]
