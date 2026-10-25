"""WIT contract identities and bootstrap-only Python binding generation."""
from __future__ import annotations

import ast
import hashlib
import json
import keyword
from pathlib import Path

CONTROL = 'wisp-python-validate'
SUPPORTED = {'record', 'list', 'option', 'tuple', 'result', 'enum', 'flags', 'variant', 'type'}


def selected_world(document, name):
    matches = [world for world in document['worlds'] if world['name'] == name]
    if len(matches) != 1:
        raise ValueError(f'expected one WIT world named {name}')
    return matches[0]


def interface_name(document, index):
    interface = document['interfaces'][index]
    if interface['name'] is None:
        raise ValueError('inline exported interfaces are not supported; use a named interface')
    package = document['packages'][interface['package']]['name']
    identity, separator, version = package.partition('@')
    return identity + '/' + interface['name'] + (separator + version if separator else '')


def contract(document, world_name):
    """Hash selected public types, independent of parser indices and wrapper names."""
    world = selected_world(document, world_name)
    if world['imports']:
        raise ValueError('Python contract templates currently require an import-free WIT world')
    active = set()

    def expand(value):
        if type(value) is int:
            if value in active:
                raise ValueError('recursive Python public types are unsupported')
            active.add(value)
            definition = document['types'][value]
            kind = definition['kind']
            if not isinstance(kind, dict) or not set(kind) <= SUPPORTED:
                raise ValueError('Python resources, handles, futures and streams are unsupported')
            owner = definition.get('owner')
            identity = interface_name(document, owner['interface']) if owner and 'interface' in owner else None
            result = {'name': definition['name'], 'owner': identity, 'kind': expand(kind)}
            active.remove(value)
            return result
        if isinstance(value, dict):
            return {key: expand(item) for key, item in value.items() if key not in ('docs', 'stability')}
        if isinstance(value, list):
            return [expand(item) for item in value]
        return value

    def functions(items):
        result = {}
        for name, function in items.items():
            if function['kind'] != 'freestanding':
                raise ValueError('Python contract templates support synchronous free functions only')
            result[name] = expand(function)
        return result

    exports = {}
    for name, item in world['exports'].items():
        if name == CONTROL:
            raise ValueError(f'{CONTROL} is reserved for build validation')
        if 'function' in item:
            exports[name] = functions({name: item['function']})
        elif 'interface' in item:
            index = item['interface']['id']
            interface = document['interfaces'][index]
            identity = interface_name(document, index) if name.startswith('interface-') else name
            exports[identity] = {'functions': functions(interface['functions']),
                                 'types': {key: expand(value) for key, value in interface['types'].items()}}
        else:
            raise ValueError('world-level exported types are unsupported; put types in an interface')
    if not exports:
        raise ValueError('Python contract requires at least one export')
    encoded = json.dumps(exports, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest(), exports


def python_name(name):
    name = name.replace('-', '_')
    return name + '_' if keyword.iskeyword(name) else name


def preload_modules(modules):
    """Normalize explicit provisioning imports without importing them on the host."""
    for module in modules:
        if not module or any(not part.isidentifier() or keyword.iskeyword(part)
                             for part in module.split('.')):
            raise ValueError(f'invalid Python preload module: {module!r}')
    return sorted(set(modules))


def generate_runtime(bindings: Path, modules=(), export_contract=None):
    """Read trusted generated protocols, never application source."""
    error_types = {}
    def normalize(name):
        return name.replace('-', '').replace('_', '').lower()
    for owner, exported in (export_contract or {}).items():
        if 'functions' in exported and 'types' in exported:
            protocol = owner.rsplit('/', 1)[-1].split('@')[0]
            functions = exported['functions']
        else:
            protocol = 'WitWorld'
            functions = exported
        for name, function in functions.items():
            result = function.get('result')
            while isinstance(result, dict) and 'type' in result.get('kind', {}):
                result = result['kind']['type']
            if isinstance(result, dict) and 'result' in result.get('kind', {}):
                error_types[normalize(protocol), python_name(name)] = result['kind']['result']['err']
    methods = []
    classes = []
    binding_types = []
    for module, base in [('wit_world', 'wit_world'), ('wit_world.exports', 'exports')]:
        path = bindings.joinpath(*module.split('.'), '__init__.py')
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text())
        for declaration in tree.body:
            if not isinstance(declaration, ast.ClassDef):
                continue
            if not any(isinstance(item, ast.Name) and item.id == 'Protocol' for item in declaration.bases):
                raise ValueError('exported resources are unsupported')
            body = []
            for function in declaration.body:
                if isinstance(function, ast.AsyncFunctionDef):
                    raise ValueError('async Python exports are unsupported')
                if not isinstance(function, ast.FunctionDef):
                    continue
                if function.name == python_name(CONTROL):
                    body.append('    def wisp_python_validate(self):\n        return _validate()\n')
                    continue
                names = [arg.arg for arg in function.args.args[1:]]
                methods.append((declaration.name, function.name, len(names)))
                if (normalize(declaration.name), function.name) in error_types:
                    error = error_types[normalize(declaration.name), function.name]
                    binding_types.append(f"ERROR_TYPES[{(declaration.name, function.name)!r}] = {error!r}\n")
                binding_types.append(f"BINDINGS[{(declaration.name, function.name)!r}] = {base}.{declaration.name}.{function.name}\n")
                arguments = ', '.join(['self', *names])
                values = ', '.join(names)
                body.append(f'    def {function.name}({arguments}):\n'
                            f'        return _call({declaration.name!r}, {function.name!r}, {values})\n')
            if body:
                classes.append(f'class {declaration.name}({base}.{declaration.name}):\n' + '\n'.join(body))
    runtime = Path(__file__).with_name('runtime_contract.py').read_text()
    if not (bindings / 'wit_world/exports/__init__.py').is_file():
        runtime = runtime.replace('from wit_world import exports\n', '')
    imports = ''.join(f'import {module}\n' for module in preload_modules(modules))
    return runtime.replace('EXPORTS = []', 'EXPORTS = ' + repr(methods)) + '\n\n' + imports + ''.join(binding_types) + '\n\n'.join(classes)
