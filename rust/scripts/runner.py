#!/usr/bin/env python3
"""Build offline Cargo projects using Wasm rustc and Wasm build scripts.

Native Cargo is used only to resolve locked metadata. Source compilation and
build scripts always run inside Wasmtime. Procedural macros require an explicitly
enabled compiler profile with the Wasm token bridge and interpreter.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MACRO_TARGET = 'wasm32-unknown-unknown'


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as file:
        for block in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()



class BuildError(RuntimeError):
    pass


def target_matches(expression: str | None, target: str) -> bool:
    """Evaluate the platform predicates supported by these two Wasm targets."""
    if expression is None:
        return True
    if not expression.startswith('cfg('):
        return expression == target
    values = {'target_arch': 'wasm32', 'target_os': 'unknown' if target == MACRO_TARGET else 'wasi',
              'target_env': '' if target == MACRO_TARGET else 'p1', 'target_family': 'wasm',
              'target_pointer_width': '32', 'target_endian': 'little', 'target_vendor': 'unknown',
              'target_abi': '', 'panic': 'abort',
              'target_has_atomic': {'8', '16', '32', '64', 'ptr'},
              'target_has_atomic_primitive_alignment': {'8', '16', '32', '64', 'ptr'},
              'target_feature': {'bulk-memory', 'multivalue', 'mutable-globals',
                                 'nontrapping-fptoint', 'reference-types', 'sign-ext'}
                                | ({'crt-static'} if target != MACRO_TARGET else set())}
    body = expression[4:-1] if expression.endswith(')') else ''
    tokens = re.findall(r'"(?:[^"\\]|\\.)*"|[A-Za-z_][A-Za-z_0-9]*|[(),=]', body)
    if ''.join(tokens) != re.sub(r'\s+', '', body):
        raise BuildError('unsupported dependency target predicate: ' + expression)
    position = 0

    def parse() -> bool:
        nonlocal position
        if position >= len(tokens):
            raise BuildError('invalid dependency target predicate: ' + expression)
        name = tokens[position]
        position += 1
        if position < len(tokens) and tokens[position] == '(':
            position += 1
            children = []
            while position < len(tokens) and tokens[position] != ')':
                children.append(parse())
                if position < len(tokens) and tokens[position] == ',':
                    position += 1
                elif position < len(tokens) and tokens[position] != ')':
                    raise BuildError('invalid dependency target predicate: ' + expression)
            if position >= len(tokens):
                raise BuildError('invalid dependency target predicate: ' + expression)
            position += 1
            if name == 'all':
                return all(children)
            if name == 'any':
                return any(children)
            if name == 'not' and len(children) == 1:
                return not children[0]
            raise BuildError('unsupported dependency target predicate: ' + expression)
        if position < len(tokens) and tokens[position] == '=':
            position += 1
            if position >= len(tokens) or not tokens[position].startswith('"'):
                raise BuildError('unsupported dependency target predicate: ' + expression)
            value = json.loads(tokens[position])
            position += 1
            expected = values.get(name)
            return value in expected if isinstance(expected, set) else expected == value
        # Cargo target dependency predicates may include crate-specific cfgs
        # such as tokio_unstable. They are unset unless explicitly enabled;
        # this runner supplies no custom --cfg flags.
        if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', name):
            return False
        raise BuildError('unsupported dependency target predicate: ' + expression)

    result = parse()
    if position != len(tokens):
        raise BuildError('invalid dependency target predicate: ' + expression)
    return result


def minimal_mounts(paths) -> list[Path]:
    """Keep existing outer capabilities without shadowing them with children.

    WASI resolves a lexical absolute path through its longest preopen. A nested
    preopen would incorrectly reject a workspace member's ../ references even
    when that same file is already covered by an outer package capability.
    """
    resolved = {Path(path).resolve() for path in paths}
    return sorted(path for path in resolved
                  if not any(path != parent and path.is_relative_to(parent)
                             for parent in resolved))


def json_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def verified_install() -> tuple[dict, Path]:
    manifest = json.loads((ROOT / 'toolchain.json').read_text())
    if manifest.get('status') == 'source-build-required':
        raise BuildError('Rust ' + manifest['rustVersion'] + ' requires a source build; run python3 build.py build rust from the repository root')
    compiler_runtime_flags(manifest)
    prefix = ROOT / 'build/bootstrap'
    inventory = prefix / 'SHA256SUMS.json'
    if not inventory.is_file():
        raise BuildError('bootstrap missing; run python3 scripts/bootstrap.py first')
    for name, expected in manifest.get('licenses', {}).items():
        path = ROOT / 'licenses' / name
        if not path.is_file() or sha256(path) != expected:
            raise BuildError(f'Rust license checksum mismatch: {name}')
    files = json.loads(inventory.read_text())
    for relative, expected in files.items():
        path = prefix / relative
        if not path.resolve().is_relative_to(prefix.resolve()) or not path.is_file() or sha256(path) != expected:
            raise BuildError(f'toolchain file checksum mismatch: {relative}')
    compiler = prefix / manifest['compiler']['module']
    if sha256(compiler) != manifest['compiler']['moduleSha256']:
        raise BuildError('compiler checksum does not match pinned upstream module')
    return manifest, prefix


def compiler_runtime_flags(manifest: dict) -> list[str]:
    if manifest.get('compilerHost') != 'wasm32-wasip3':
        raise BuildError('Rust compiler host must be wasm32-wasip3')
    if 'llvmProvider' in manifest:
        raise BuildError('WASIp3 Rust compiler must contain its LLVM backend')
    return ['-Sp3=y', '-Wmax-wasm-stack=16777216', '-Wasync-stack-size=33554432',
            '-Oopt-level=0', '-Oregalloc-algorithm=single-pass', '-Cinlining=n']


def parse_directives(stdout: str, *, cdylib: bool = False) -> tuple[list[str], dict[str, str]]:
    flags: list[str] = []
    environment: dict[str, str] = {}
    for line in stdout.splitlines():
        if not line.startswith('cargo:'):
            continue
        directive = line.removeprefix('cargo:').removeprefix(':')
        key, separator, value = directive.partition('=')
        if not separator:
            raise BuildError(f'invalid build script directive: {line}')
        if key == 'rustc-cfg':
            flags += ['--cfg', value]
        elif key == 'rustc-check-cfg':
            flags += ['--check-cfg', value]
        elif key in ('rustc-cdylib-link-arg', 'rustc-link-arg-cdylib'):
            if not value or '\x00' in value:
                raise BuildError(f'invalid cdylib link argument: {line}')
            if cdylib:
                flags += ['-C', 'link-arg=' + value]
        elif key == 'rustc-env':
            name, equal, content = value.partition('=')
            if not equal or not name or '\x00' in value:
                raise BuildError(f'invalid build script environment: {line}')
            environment[name] = content
        elif key == 'warning':
            print(f'build script warning: {value}', file=sys.stderr)
        elif key == 'error':
            raise BuildError(f'build script error: {value}')
        elif key in ('rerun-if-changed', 'rerun-if-env-changed', 'metadata'):
            # Every invocation is fresh, so there is no incremental cache to invalidate.
            continue
        else:
            raise BuildError(f'unsupported build script directive: {key}')
    return flags, environment


def metadata_query(context_path: Path, argv: list[str]) -> int:
    """Answer Cargo queries without allowing project-selected native programs."""
    context = json.loads(context_path.read_text())
    manifest = context['manifest']
    if argv in (['-vV'], ['--version', '--verbose'], ['--verbose', '--version']):
        # The port prints these values, then panics in current_dll_path for -vV.
        # Cargo needs metadata only; this is not a source compilation fallback.
        print('rustc ' + manifest['rustVersion'])
        print('binary: rustc')
        print('commit-hash: unknown')
        print('commit-date: unknown')
        print('host: ' + manifest['compilerHost'])
        print('release: ' + manifest['rustVersion'])
        print('LLVM version: ' + manifest['llvmVersion'].removesuffix('git'))
        return 0
    if argv == ['--version']:
        print('rustc ' + manifest['rustVersion'])
        return 0
    requested = []
    for index, argument in enumerate(argv):
        if argument.startswith('--print='):
            requested.extend(argument.partition('=')[2].split(','))
        elif argument == '--print' and index + 1 < len(argv):
            requested.extend(argv[index + 1].split(','))
        if argument in ('--emit', '-o', '--out-dir', '--test') or argument.startswith(('--emit=', '--out-dir=')) or (argument.startswith('-o') and argument != '--print'):
            raise BuildError('metadata compiler accepts print/version queries only')
    allowed = {'file-names', 'sysroot', 'split-debuginfo', 'crate-name', 'cfg', 'target-libdir'}
    if not requested or not set(requested).issubset(allowed):
        raise BuildError('unsupported metadata compiler query: ' + repr(argv))
    runtime, compiler, sysroot, temporary = [Path(context[key]) for key in ('runtime', 'compiler', 'sysroot', 'temporary')]
    command = [str(runtime), 'run', '-Ccache=y', *compiler_runtime_flags(context['manifest']), '--dir', str(sysroot),
               '--dir', str(temporary) + '::/', '--env', 'RUST_MIN_STACK=16777216',
               str(compiler), *argv, '--sysroot', str(sysroot)]
    process = subprocess.run(command, cwd=temporary, input=b'', capture_output=True, timeout=120)
    sys.stdout.buffer.write(process.stdout)
    sys.stderr.buffer.write(process.stderr)
    record = {'kind': 'compiler', 'purpose': 'metadata-query', 'program': str(compiler),
              'sha256': sha256(compiler), 'argv': command, 'cwd': str(temporary), 'exitCode': process.returncode}
    json_write(Path(context['records']) / f'{os.getpid()}.json', record)
    return process.returncode


class Builder:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.manifest, self.prefix = verified_install()
        self.macros_enabled = self.manifest.get('capabilities', {}).get('proceduralMacros') is True
        self.compiler = (self.prefix / self.manifest['compiler']['module']).resolve()
        self.sysroot = (self.prefix / 'sysroot').resolve()
        compiler_runtime_flags(self.manifest)
        configured = args.wasmtime or os.environ.get('WISP_RUST_WASMTIME')
        if not configured:
            raise BuildError('Wisp must provide Wasmtime with support for this compiler host; pass --wasmtime or set WISP_RUST_WASMTIME')
        runtime = shutil.which(configured)
        if not runtime:
            raise BuildError(f'Wisp-provided Wasmtime is missing: {configured}')
        self.runtime = Path(runtime).resolve()
        if self.runtime.is_relative_to(ROOT):
            raise BuildError('Wasmtime must be supplied by Wisp outside the Rust compiler package')
        self.source_manifest = args.manifest_path.resolve()
        self.out = args.out_dir.resolve()
        if not (getattr(args, 'resume', False) or getattr(args, 'recover_receipts', False)) and self.out.exists() and any(p.name not in ('runner.stdout.log', 'runner.stderr.log') for p in self.out.iterdir()):
            raise BuildError(f'output directory must be fresh: {self.out}')
        self.out.mkdir(parents=True, exist_ok=True)
        self.libs = self.out / 'lib'
        self.libs.mkdir(exist_ok=True)
        self.temp = self.out / 'tmp'
        self.temp.mkdir(exist_ok=True)
        self.report = {'schemaVersion': 1, 'manifestPath': str(self.source_manifest),
                       'target': args.target,
                       'compiler': {'path': str(self.compiler), 'sha256': sha256(self.compiler), 'host': 'wasm'},
                       'dependencies': [], 'buildScripts': [], 'proceduralMacros': [], 'executions': []}
        self.compiled: dict[object, Path] = {}
        self.script_flags: dict[object, list[str]] = {}
        self.script_env: dict[object, dict[str, str]] = {}
        self.unit_features: dict[tuple[str, str], set[str]] = {}
        self.unit_edges: dict[tuple[str, str], list[dict]] = {}
        self.macro_artifacts: dict[Path, dict] = {}
        self.compiling: set[object] = set()
        self.replay_cursor = None
        if getattr(args, 'resume', False) or getattr(args, 'recover_receipts', False):
            previous = json.loads(args.report.read_text())
            if (previous['manifestPath'] != str(self.source_manifest) or previous['target'] != args.target
                    or previous['compiler'] != self.report['compiler']):
                raise BuildError('resume build identity differs from existing receipt')
            self.report = previous
            self.report.pop('artifact', None)

    def source_snapshot(self, metadata):
        files = {}
        for package in metadata['packages']:
            root = Path(package['manifest_path']).parent
            if package.get('source'):
                self.verify_vendor(root)
                files[str(root / '.cargo-checksum.json')] = sha256(root / '.cargo-checksum.json')
            else:
                for path in root.rglob('*'):
                    if path.is_file() and not any(part in ('target', '.git') for part in path.relative_to(root).parts):
                        files[str(path)] = sha256(path)
        return files

    def metadata(self) -> dict:
        if getattr(self.args, 'resume', False) or getattr(self.args, 'recover_receipts', False):
            metadata = json.loads((self.out / 'metadata.json').read_text())
            snapshot = self.source_snapshot(metadata)
            state = self.out / 'resume-state.json'
            if getattr(self.args, 'recover_receipts', False):
                if state.exists() and json.loads(state.read_text()) != {'sources': snapshot, 'metadataSha256': sha256(self.out / 'metadata.json')}:
                    raise BuildError('recovery sources or metadata changed')
                # Explicit local recovery trusts the already completed execution
                # receipts, then seals their outputs and current verified sources.
                for index, record in enumerate(self.report['executions']):
                    argv = record['argv']
                    if record['exitCode'] == 0 and '-o' in argv:
                        output = Path(argv[argv.index('-o') + 1])
                        if not output.is_file():
                            raise BuildError('recovery output missing: ' + str(output))
                        record.setdefault('outputSha256', sha256(output))
                    if record.get('purpose') != 'metadata-query':
                        for stream in ('stdout', 'stderr'):
                            record.setdefault(stream + 'Sha256', sha256(self.out / 'logs' / f'{index:03}-{record["kind"]}.{stream}'))
                    if record['kind'] == 'build-script':
                        generated = Path(next(a.split('=', 1)[1] for a in argv if a.startswith('OUT_DIR=')))
                        record.setdefault('generatedOutputs', {str(p): sha256(p) for p in generated.rglob('*') if p.is_file()})
                json_write(state, {'sources': snapshot, 'metadataSha256': sha256(self.out / 'metadata.json')})
                json_write(self.args.report, self.report)
            saved = json.loads(state.read_text())
            if saved != {'sources': snapshot, 'metadataSha256': sha256(self.out / 'metadata.json')}:
                raise BuildError('resume sources or metadata changed; use a fresh output directory')
            self.replay_cursor = sum(record.get('purpose') == 'metadata-query' for record in self.report['executions'])
            return metadata
        query_root = self.out / 'metadata-query'
        query_root.mkdir()
        context = query_root / 'context.json'
        records = query_root / 'records'
        records.mkdir()
        json_write(context, {'manifest': self.manifest, 'runtime': str(self.runtime), 'compiler': str(self.compiler),
                             'sysroot': str(self.sysroot), 'temporary': str(self.temp), 'records': str(records)})
        probe = query_root / 'rustc-query'
        python = str(Path(sys.executable).resolve())
        entry = (f'#!{python} -I\nimport os, sys\nos.execv({python!r}, '
                 f'[{python!r}, "-I", {str(Path(__file__).resolve())!r}, "metadata-query", {str(context)!r}, *sys.argv[1:]])\n')
        probe.write_text(entry)
        probe.chmod(0o755)
        command = [self.args.cargo, 'metadata', '--format-version', '1', '--manifest-path', str(self.source_manifest),
                   '--locked', '--offline',
                   '--config', 'build.rustc=' + json.dumps(str(probe)),
                   '--config', 'build.rustc-wrapper=""', '--config', 'build.rustc-workspace-wrapper=""']
        if not self.macros_enabled:
            command += ['--filter-platform', self.args.target]
        if self.args.vendor_dir:
            vendor = self.args.vendor_dir.resolve()
            command += ['--config', 'source.crates-io.replace-with="wisp-vendored"', '--config',
                        'source.wisp-vendored.directory=' + json.dumps(str(vendor))]
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(('RUSTC', 'CARGO_', 'PYTHON', 'LD_', 'DYLD_'))}
        environment.update(RUSTC=str(probe), RUSTC_WRAPPER='', RUSTC_WORKSPACE_WRAPPER='')
        # Cargo discovers config from cwd and CARGO_HOME, not --manifest-path.
        # Keep project [env]/tool hooks out of this native metadata process.
        # Sources and vendoring are selected through explicit arguments above.
        with tempfile.TemporaryDirectory(prefix='wisp-cargo-metadata-', dir=self.temp) as working:
            cargo_home = Path(working) / 'cargo-home'
            cargo_home.mkdir()
            environment['CARGO_HOME'] = str(cargo_home)
            process = subprocess.run(command, cwd=working, env=environment,
                                     stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=600)
        for record in sorted(records.glob('*.json')):
            execution = json.loads(record.read_text())
            self.report['executions'].append(execution)
        json_write(self.args.report.resolve(), self.report)
        if process.returncode:
            raise BuildError('locked offline Cargo metadata failed:\n' + process.stderr)
        metadata = json.loads(process.stdout)
        # A registry source must come from the explicitly supplied vendor directory.
        for package in metadata['packages']:
            if package.get('source'):
                path = Path(package['manifest_path']).resolve().parent
                if not self.args.vendor_dir or not path.is_relative_to(self.args.vendor_dir.resolve()):
                    raise BuildError(f'{package["name"]} is not in --vendor-dir; run cargo vendor first')
                self.verify_vendor(path)
                self.report['dependencies'].append({'name': package['name'], 'version': package['version'], 'source': 'vendored'})
        json_write(self.out / 'metadata.json', metadata)
        json_write(self.out / 'resume-state.json', {'sources': self.source_snapshot(metadata), 'metadataSha256': sha256(self.out / 'metadata.json')})
        return metadata

    @staticmethod
    def verify_vendor(path: Path) -> None:
        checksum = path / '.cargo-checksum.json'
        try:
            checksums = json.loads(checksum.read_text())['files']
        except (OSError, KeyError, ValueError, TypeError) as error:
            raise BuildError(f'missing Cargo vendor checksums: {path}') from error
        if not isinstance(checksums, dict) or any(
            not isinstance(relative, str) or not isinstance(expected, str)
            or len(expected) != 64 or any(c not in '0123456789abcdef' for c in expected)
            for relative, expected in checksums.items()
        ):
            raise BuildError(f'invalid Cargo vendor checksum map: {path}')
        actual_files = set()
        for file in path.rglob('*'):
            if file.is_symlink():
                raise BuildError(f'vendored source must not contain symlinks: {file}')
            if file.is_file() and file != checksum:
                actual_files.add(str(file.relative_to(path)))
        if actual_files != set(checksums):
            raise BuildError(f'vendored source file inventory mismatch: {path}')
        for relative, expected in checksums.items():
            file = path / relative
            if not file.resolve().is_relative_to(path) or not file.is_file() or sha256(file) != expected:
                raise BuildError(f'vendored source checksum mismatch: {file}')

    def target_for(self, context: str) -> str:
        return MACRO_TARGET if context == 'macro' else self.args.target

    def unit_id(self, package: dict, context: str) -> object:
        return (package['id'], context) if self.macros_enabled else package['id']

    def features(self, package: dict, context: str) -> set[str]:
        if self.macros_enabled:
            return self.unit_features[package['id'], context]
        return set(self.nodes[package['id']]['features'])

    def unit_key(self, package: dict, context: str) -> str:
        if not self.macros_enabled:
            return self.key(package)
        identity = json.dumps([package['id'], self.target_for(context), context,
                               sorted(self.features(package, context))])
        return package['name'].replace('-', '_') + '-' + hashlib.sha256(identity.encode()).hexdigest()[:16]

    def library_target(self, package: dict) -> dict:
        targets = [target for target in package['targets'] if set(target['kind']) &
                   {'lib', 'rlib', 'dylib', 'cdylib', 'staticlib', 'proc-macro'}]
        if len(targets) != 1:
            raise BuildError(f'expected one library target in {package["name"]}')
        return targets[0]

    def declared_edges(self, package: dict, context: str) -> list[dict]:
        edges = []
        for declaration in package['dependencies']:
            kind = declaration['kind']
            if kind not in (None, 'build'):
                continue
            platform = self.args.target if kind == 'build' else self.target_for(context)
            if not target_matches(declaration['target'], platform):
                continue
            alias = declaration.get('rename') or declaration['name']
            matches = [edge for edge in self.nodes[package['id']]['deps']
                       if edge['name'] == (alias.replace('-', '_') if declaration.get('rename')
                                           else self.library_target(self.packages[edge['pkg']])['name'].replace('-', '_'))
                       and self.packages[edge['pkg']]['name'] == declaration['name']
                       and any(entry['kind'] == kind and entry.get('target') == declaration['target']
                               for entry in edge['dep_kinds'])]
            if not matches:
                # Inactive optional dependencies may be omitted from Cargo's graph.
                if declaration['optional']:
                    edges.append(dict(declaration, alias=alias, pkg=None, extern=alias.replace('-', '_')))
                    continue
                raise BuildError(f'Cargo metadata omitted target dependency {alias} of {package["name"]}')
            if len(matches) != 1:
                raise BuildError(f'ambiguous Cargo dependency {alias} of {package["name"]}')
            edges.append(dict(declaration, alias=alias, pkg=matches[0]['pkg'], extern=matches[0]['name']))
        return edges

    def configure_units(self) -> None:
        """Resolve features separately for application, macro and build-script code.

        Cargo metadata supplies locked package identities and feature declarations,
        but its node features merge host and target uses. Resolve only the supported
        normal/build edges here, preserving optional and weak dependency features.
        """
        self.unit_features[self.root_id, 'runtime'] = set(self.nodes[self.root_id]['features'])
        changed = True
        while changed:
            changed = False
            for (package_id, context), enabled in list(self.unit_features.items()):
                package = self.packages[package_id]
                declarations = self.declared_edges(package, context)
                aliases = {edge['alias'] for edge in declarations}
                active = {edge['alias'] for edge in declarations if not edge['optional']}
                forwarded: dict[str, set[str]] = {}
                weak: list[tuple[str, str]] = []
                pending = list(enabled)
                expanded = set()
                while pending:
                    feature = pending.pop()
                    if feature in expanded:
                        continue
                    expanded.add(feature)
                    if feature not in package['features']:
                        raise BuildError(f'unsupported Cargo feature {feature} in {package["name"]}')
                    for item in package['features'][feature]:
                        if item.startswith('dep:'):
                            active.add(item[4:])
                        elif '/' in item:
                            alias, child = item.split('/', 1)
                            if alias.endswith('?'):
                                weak.append((alias[:-1], child))
                            else:
                                active.add(alias)
                                forwarded.setdefault(alias, set()).add(child)
                        elif item in package['features']:
                            pending.append(item)
                            enabled.add(item)
                        elif item in aliases:
                            active.add(item)
                        else:
                            raise BuildError(f'unsupported Cargo feature edge {item} in {package["name"]}')
                for alias, feature in weak:
                    if alias in active:
                        forwarded.setdefault(alias, set()).add(feature)
                selected = []
                for edge in declarations:
                    if edge['alias'] not in active:
                        continue
                    if edge['pkg'] is None:
                        raise BuildError(f'Cargo metadata omitted active dependency {edge["alias"]} of {package["name"]}')
                    dependency = self.packages[edge['pkg']]
                    dependency_context = 'build' if edge['kind'] == 'build' else context
                    if 'proc-macro' in self.library_target(dependency)['kind']:
                        dependency_context = 'macro'
                    requested = set(edge['features']) | forwarded.get(edge['alias'], set())
                    if edge['uses_default_features'] and 'default' in dependency['features']:
                        requested.add('default')
                    key = dependency['id'], dependency_context
                    if key not in self.unit_features:
                        self.unit_features[key] = set()
                        changed = True
                    before = len(self.unit_features[key])
                    self.unit_features[key].update(requested)
                    changed |= before != len(self.unit_features[key])
                    selected.append(dict(edge, context=dependency_context))
                self.unit_edges[package_id, context] = selected
        for (package_id, context), features in self.unit_features.items():
            if context == 'macro' and not (self.sysroot / 'lib/rustlib' / MACRO_TARGET / 'lib').is_dir():
                raise BuildError('macro compiler profile is missing the wasm32-unknown-unknown sysroot')
        self.report['compilationUnits'] = [
            {'package': self.packages[package_id]['name'], 'context': context,
             'target': self.target_for(context), 'features': sorted(features)}
            for (package_id, context), features in sorted(self.unit_features.items())]

    def environment(self, package: dict, *, context: str = 'runtime') -> dict[str, str]:
        version = package['version'].split('-', 1)
        numbers = version[0].split('.')
        script_out = self.out / 'build-scripts' / self.unit_key(package, context) / 'out'
        script_out.mkdir(parents=True, exist_ok=True)
        result = {'CARGO_MANIFEST_DIR': str(Path(package['manifest_path']).parent),
                  'CARGO_MANIFEST_PATH': package['manifest_path'],
                  'CARGO_PKG_NAME': package['name'], 'CARGO_PKG_VERSION': package['version'],
                  'CARGO_PKG_VERSION_MAJOR': numbers[0], 'CARGO_PKG_VERSION_MINOR': numbers[1],
                  'CARGO_PKG_VERSION_PATCH': numbers[2], 'CARGO_PKG_VERSION_PRE': version[1] if len(version) > 1 else '',
                  'CARGO_PKG_AUTHORS': ':'.join(package.get('authors', [])),
                  'CARGO_PKG_DESCRIPTION': package.get('description') or '',
                  'OUT_DIR': str(script_out), 'TARGET': self.target_for(context), 'HOST': self.manifest['compilerHost'],
                  'PROFILE': 'release', 'OPT_LEVEL': '1', 'DEBUG': 'false', 'NUM_JOBS': '1',
                  'RUSTC': str(self.compiler), 'RUST_MIN_STACK': '16777216', 'TMPDIR': str(self.temp),
                  'CARGO_CFG_TARGET_ARCH': 'wasm32', 'CARGO_CFG_TARGET_OS': 'unknown' if context == 'macro' else 'wasi',
                  'CARGO_CFG_TARGET_ENV': '' if context == 'macro' else 'p1', 'CARGO_CFG_TARGET_FAMILY': 'wasm',
                  'CARGO_CFG_TARGET_POINTER_WIDTH': '32', 'CARGO_CFG_TARGET_ENDIAN': 'little'}
        for feature in self.features(package, context):
            result['CARGO_FEATURE_' + feature.upper().replace('-', '_')] = '1'
        result.update(self.script_env.get(self.unit_id(package, context), {}))
        return result

    @staticmethod
    def key(package: dict) -> str:
        return package['name'].replace('-', '_') + '-' + hashlib.sha256(package['id'].encode()).hexdigest()[:12]

    def macro_trace(self, path: Path, execution: int) -> None:
        if not path.exists():
            return
        loaded, started, verified = set(), set(), {}
        trace_hash = sha256(path)
        for line in path.read_text().splitlines():
            try:
                event = json.loads(line)
                module = Path(event['module'])
                action, status = event['event'], event['status']
                identity = (module, event['index'], event['name'], event['kind'])
                valid = (event['schemaVersion'] == 1 and event['runtime'] == 'wasmi'
                         and event['macroTarget'] == MACRO_TARGET and module.is_absolute()
                         and type(event['index']) is int and 0 <= event['index'] < 2**32
                         and isinstance(event['name'], str) and event['name']
                         and event['kind'] in ('function', 'attribute', 'derive'))
            except (KeyError, TypeError, ValueError) as error:
                raise BuildError('invalid compiler procedural macro trace') from error
            artifact = self.macro_artifacts.get(module)
            if module not in verified:
                verified[module] = sha256(module) if module.is_file() else None
            if not valid or artifact is None or verified[module] != artifact['sha256']:
                raise BuildError('procedural macro trace names an unverified Wasm artifact')
            if action == 'load' and status == 'succeeded':
                loaded.add(identity)
            elif action == 'invoke' and status == 'started' and identity in loaded and identity not in started:
                started.add(identity)
            elif action == 'invoke' and status == 'succeeded' and identity in started:
                started.remove(identity)
                self.report['proceduralMacros'].append(dict(
                    artifact, loader='compiler-wasm-runtime', runtime='wasmi', execution=execution,
                    macroName=event['name'], macroKind=event['kind'], macroIndex=event['index'],
                    trace=str(path), traceSha256=trace_hash))
            else:
                raise BuildError('procedural macro trace did not record a successful invocation')
        if started:
            raise BuildError('procedural macro invocation did not complete')

    def execute(self, program: Path, argv: list[str], package: dict, kind: str,
                *, context: str = 'runtime') -> int:
        source = Path(package['manifest_path']).parent.resolve()
        # The guest sees only project/dependency sources, sysroot, and the selected output directory.
        mounts = {self.sysroot, self.out}
        for item in self.packages.values():
            mounts.add(Path(item['manifest_path']).parent.resolve())
        command = [str(self.runtime), 'run', '-Ccache=y']
        if kind == 'compiler':
            command += compiler_runtime_flags(self.manifest)
        for mount in minimal_mounts(mounts):
            command += ['--dir', str(mount)]
        # This compiler's patched std uses / for temporary files. Map that guest
        # root to a disposable directory, never the host filesystem root.
        command += ['--dir', str(self.temp) + '::/', '--dir', str(source) + '::.']
        environment = self.environment(package, context=context) if self.macros_enabled else self.environment(package)
        environment['PWD'] = str(source)
        index = self.replay_cursor if self.replay_cursor is not None else len(self.report['executions'])
        trace = self.out / 'macro-traces' / f'{index:03}.jsonl'
        if self.macros_enabled and kind == 'compiler':
            trace.parent.mkdir(exist_ok=True)
            if trace.exists() and self.replay_cursor is None:
                raise BuildError('compiler macro trace must be fresh')
            environment['WISP_WASM_PROC_MACRO_LOG'] = str(trace)
            # The stable release needs the port's experimental macro loader flag.
            environment['RUSTC_BOOTSTRAP'] = '1'
        for name, value in sorted(environment.items()):
            command += ['--env', name + '=' + value]
        command += [str(program), *argv]
        if self.replay_cursor is not None:
            previous = self.report['executions']
            if index < len(previous):
                record = previous[index]
                output = Path(argv[argv.index('-o') + 1]) if '-o' in argv else None
                valid_logs = all(record.get(stream + 'Sha256') == sha256(self.out / 'logs' / f'{index:03}-{kind}.{stream}')
                                 for stream in ('stdout', 'stderr'))
                valid_generated = True
                if kind == 'build-script':
                    generated = Path(environment['OUT_DIR'])
                    valid_generated = record.get('generatedOutputs') == {str(p): sha256(p) for p in generated.rglob('*') if p.is_file()}
                valid_output = output is None or (output.is_file() and record.get('outputSha256') == sha256(output))
                if (record['argv'] == command and record['exitCode'] == 0
                        and record['sha256'] == sha256(program) and valid_output and valid_logs and valid_generated):
                    if kind == 'compiler' and self.macros_enabled:
                        self.macro_trace(trace, index)
                    if kind == 'build-script':
                        stdout = (self.out / 'logs' / f'{index:03}-{kind}.stdout').read_text()
                        self.script_flags[self.unit_id(package, context)], self.script_env[self.unit_id(package, context)] = parse_directives(stdout, cdylib=package['id'] == self.root_id and self.args.crate_type == 'cdylib')
                    self.replay_cursor += 1
                    return index
                del previous[index:]
            self.replay_cursor = None
            trace.unlink(missing_ok=True)
        process = subprocess.run(command, cwd=source, text=True, capture_output=True, timeout=self.args.timeout)
        record = {'kind': kind, 'program': str(program), 'sha256': sha256(program), 'argv': command,
                  'cwd': str(source), 'exitCode': process.returncode}
        if process.returncode == 0 and '-o' in argv:
            record['outputSha256'] = sha256(Path(argv[argv.index('-o') + 1]))
        logs = self.out / 'logs'
        logs.mkdir(exist_ok=True)
        (logs / f'{index:03}-{kind}.stdout').write_text(process.stdout)
        (logs / f'{index:03}-{kind}.stderr').write_text(process.stderr)
        for stream in ('stdout', 'stderr'):
            record[stream + 'Sha256'] = sha256(logs / f'{index:03}-{kind}.{stream}')
        if kind == 'build-script':
            generated = Path(environment['OUT_DIR'])
            record['generatedOutputs'] = {str(p): sha256(p) for p in generated.rglob('*') if p.is_file()}
        self.report['executions'].append(record)
        json_write(self.args.report.resolve(), self.report)
        if process.returncode:
            raise BuildError(f'{kind} failed for {package["name"]} with exit {process.returncode}:\n{process.stderr}')
        if self.macros_enabled and kind == 'compiler':
            self.macro_trace(trace, index)
        if kind == 'build-script':
            self.script_flags[self.unit_id(package, context)], self.script_env[self.unit_id(package, context)] = parse_directives(process.stdout, cdylib=package['id'] == self.root_id and self.args.crate_type == 'cdylib')
        return index

    def dependency_flags(self, package: dict, *, build: bool = False,
                         context: str = 'runtime') -> list[str]:
        flags: list[str] = []
        if self.macros_enabled:
            for dependency in self.unit_edges[package['id'], context]:
                if (dependency['kind'] == 'build') != build:
                    continue
                artifact = self.compile_library(dependency['pkg'], context=dependency['context'])
                flags += ['--extern', dependency['extern'] + '=' + str(artifact)]
            return flags
        for dependency in self.nodes[package['id']]['deps']:
            kinds = {kind['kind'] for kind in dependency['dep_kinds']}
            selected = 'build' in kinds if build else None in kinds
            if not selected:
                continue
            artifact = self.compile_library(dependency['pkg'])
            flags += ['--extern', dependency['name'] + '=' + str(artifact)]
        return flags

    def flags(self, package: dict, target: dict, *, context: str = 'runtime',
              build_script: bool = False) -> list[str]:
        triple = self.args.target if build_script else self.target_for(context)
        libraries = self.libs / ('build' if build_script else context) if self.macros_enabled else self.libs
        libraries.mkdir(exist_ok=True)
        flags = ['--sysroot', str(self.sysroot), '--target', triple,
                 '--edition', target['edition'], '-Cpanic=abort', '-Copt-level=1',
                 '-Ldependency=' + str(libraries)]
        if self.macros_enabled:
            flags += ['-Zwasm-proc-macros']
            macro_libraries = self.libs / 'macro'
            macro_libraries.mkdir(exist_ok=True)
            if macro_libraries != libraries:
                flags += ['-Ldependency=' + str(macro_libraries)]
        # Cargo caps dependency lints so a dependency's deny(warnings) cannot
        # reject an otherwise valid target build with a different compiler.
        if package['id'] != getattr(self, 'root_id', package['id']):
            flags += ['--cap-lints', 'allow']
        for feature in sorted(self.features(package, context)):
            flags += ['--cfg', 'feature=' + json.dumps(feature)]
        return flags

    def build_script(self, package: dict, *, context: str = 'runtime') -> None:
        unit = self.unit_id(package, context)
        if unit in self.script_flags:
            return
        self.script_flags[unit] = []
        scripts = [target for target in package['targets'] if 'custom-build' in target['kind']]
        for script in scripts:
            binary = self.out / 'build-scripts' / self.unit_key(package, context) / 'build.wasm'
            binary.parent.mkdir(parents=True, exist_ok=True)
            argv = [script['src_path'], '--crate-name', 'build_script_build', '--crate-type', 'bin',
                    *self.flags(package, script, context=context, build_script=True),
                    *self.dependency_flags(package, build=True, context=context), '-o', str(binary)]
            self.execute(self.compiler, argv, package, 'compiler', context=context)
            index = self.execute(binary, [], package, 'build-script', context=context)
            self.report['buildScripts'].append({'package': package['name'], 'program': str(binary),
                                               'sha256': sha256(binary), 'execution': index,
                                               'target': self.args.target})

    def compile_library(self, package_id: str, *, context: str = 'runtime') -> Path:
        package = self.packages[package_id]
        target = self.library_target(package)
        macro = 'proc-macro' in target['kind']
        if macro and not self.macros_enabled:
            raise BuildError(f'procedural macro {package["name"]} requires a Wasm macro loader; bootstrap compiler has none')
        if macro:
            context = 'macro'
        unit = self.unit_id(package, context)
        if unit in self.compiled:
            return self.compiled[unit]
        if unit in self.compiling:
            raise BuildError('cyclic Cargo compilation dependency: ' + package['name'])
        self.compiling.add(unit)
        dependencies = self.dependency_flags(package, context=context)
        if macro:
            # Cargo exposes the sysroot proc_macro crate in the extern prelude.
            dependencies += ['--extern', 'proc_macro']
        self.build_script(package, context=context)
        libraries = self.libs / context if self.macros_enabled else self.libs
        libraries.mkdir(exist_ok=True)
        output = libraries / (('' if macro else 'lib') + target['name'] + '-' + self.unit_key(package, context).rsplit('-', 1)[1]
                              + ('.wasm' if macro else '.rlib'))
        argv = [target['src_path'], '--crate-name', target['name'], '--crate-type', 'proc-macro' if macro else 'rlib',
                *self.flags(package, target, context=context), '-Cmetadata=' + self.unit_key(package, context),
                *dependencies, *self.script_flags[unit], '-o', str(output)]
        execution = self.execute(self.compiler, argv, package, 'compiler', context=context)
        self.compiled[unit] = output
        self.compiling.remove(unit)
        if macro:
            if not output.read_bytes().startswith(b'\0asm\x01\0\0\0'):
                raise BuildError('procedural macro compiler output is not core Wasm')
            self.macro_artifacts[output] = {'package': package['name'], 'program': str(output),
                                           'sha256': sha256(output), 'compilation': execution}
            self.report['macroArtifacts'] = list(self.macro_artifacts.values())
        return output

    def build(self) -> None:
        metadata = self.metadata()
        self.report['buildScripts'] = []
        self.report['proceduralMacros'] = []
        self.packages = {package['id']: package for package in metadata['packages']}
        self.nodes = {node['id']: node for node in metadata['resolve']['nodes']}
        root_id = metadata['resolve']['root']
        if root_id is None:
            raise BuildError('a package Cargo.toml is required; virtual workspaces are unsupported')
        self.root_id = root_id
        if self.macros_enabled:
            self.configure_units()
        root = self.packages[root_id]
        library_targets = [target for target in root['targets']
                           if set(target['kind']) & {'lib', 'rlib', 'dylib', 'cdylib', 'staticlib'}]
        if self.args.crate_type == 'rlib':
            artifact = self.compile_library(root_id)
        else:
            if self.args.crate_type == 'cdylib':
                if len(library_targets) != 1:
                    raise BuildError('cdylib needs exactly one ordinary library target')
                target = library_targets[0]
                self.report['libraryName'] = target['name']
                if self.args.entry_path is not None:
                    entry = self.args.entry_path.resolve()
                    if not entry.is_file() or not entry.is_relative_to(self.source_manifest.parent):
                        raise BuildError('--entry-path must be a file inside the source project')
                    target = dict(target, src_path=str(entry), name='wisp_component')
            else:
                targets = [target for target in root['targets'] if 'bin' in target['kind']]
                if len(targets) != 1:
                    raise BuildError('expected exactly one application binary target')
                target = targets[0]
            dependencies = self.dependency_flags(root)
            if library_targets and (self.args.crate_type != 'cdylib' or self.args.entry_path is not None):
                library = self.compile_library(root_id)
                library_target = library_targets[0]
                dependencies += ['--extern', library_target['name'] + '=' + str(library)]
                self.report['libraryName'] = library_target['name']
            self.build_script(root)
            artifact = self.out / 'app.wasm'
            argv = [target['src_path'], '--crate-name', target['name'].replace('-', '_'),
                    '--crate-type', self.args.crate_type,
                    *self.flags(root, target), *dependencies,
                    *self.script_flags[self.unit_id(root, 'runtime')], '-o', str(artifact)]
            self.execute(self.compiler, argv, root, 'compiler')
        self.report['artifact'] = {'path': str(artifact), 'sha256': sha256(artifact)}
        json_write(self.args.report.resolve(), self.report)
        print(artifact)


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == 'metadata-query':
        try:
            return metadata_query(Path(sys.argv[2]), sys.argv[3:])
        except (BuildError, OSError, ValueError, subprocess.TimeoutExpired) as error:
            print(f'rust metadata: {error}', file=sys.stderr)
            return 2
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    build = commands.add_parser('build')
    build.add_argument('--manifest-path', required=True, type=Path)
    build.add_argument('--target', default='wasm32-wasip1', choices=['wasm32-wasip1'])
    build.add_argument('--out-dir', required=True, type=Path)
    build.add_argument('--report', required=True, type=Path)
    build.add_argument('--resume', action='store_true', help='reuse checked receipts with unchanged sources')
    build.add_argument('--recover-receipts', action='store_true', help='trust and seal local receipts created before resume support')
    build.add_argument('--offline', action='store_true', required=True)
    build.add_argument('--vendor-dir', type=Path)
    build.add_argument('--wasmtime')
    build.add_argument('--cargo', default='cargo')
    build.add_argument('--crate-type', default='bin', choices=['bin', 'rlib', 'cdylib'])
    build.add_argument('--entry-path', '--wrapper-source', dest='entry_path', type=Path)
    build.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args()
    try:
        Builder(args).build()
        return 0
    except (BuildError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f'rust: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
