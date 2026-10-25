"""Type-hint inference, snapshotted into CPython and called only inside Wasm."""
import ast
import json
import keyword
import re

# CPython 3.14 loads its unparser lazily. Snapshot that compiler helper now;
# application analysis has no stdlib filesystem available at runtime.
ast.unparse(ast.Name(id='int', ctx=ast.Load()))


class Graph:
    def __init__(self, modules):
        self.modules, self.trees, self.scopes = modules, {}, {}

    def tree(self, module):
        if module not in self.trees:
            self.trees[module] = ast.parse(self.modules[module][0] or '', filename=self.modules[module][1])
        return self.trees[module]

    def absolute(self, module, node):
        if not node.level:
            return node.module or ''
        package = module if self.modules[module][2] else module.rpartition('.')[0]
        parts = package.split('.') if package else []
        if node.level > len(parts):
            raise ValueError('relative Python import escapes the package: ' + module)
        return '.'.join(parts[:len(parts) - node.level + 1] + ([node.module] if node.module else []))

    def scope(self, module):
        if module in self.scopes:
            return self.scopes[module]
        scope = self.scopes[module] = {}
        for node in self.tree(module).body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in scope:
                    raise ValueError('duplicate public declaration ' + module + '.' + node.name)
                scope[node.name] = ('class' if isinstance(node, ast.ClassDef) else 'function', module, node)
            elif isinstance(node, ast.Import):
                for item in node.names:
                    target = item.name if item.asname else item.name.split('.')[0]
                    scope[item.asname or target] = ('module', target)
            elif isinstance(node, ast.ImportFrom):
                owner = self.absolute(module, node)
                for item in node.names:
                    if item.name == '*':
                        if owner not in self.modules:
                            continue
                        for name in self.exports(owner):
                            scope[name] = ('reference', owner, name)
                    else:
                        scope[item.asname or item.name] = ('reference', owner, item.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        scope[target.id] = ('expression', module, node.value)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                scope[node.target.id] = ('expression', module, node.value)
        return scope

    def exports(self, module):
        scope = self.scope(module)
        explicit = scope.get('__all__')
        if explicit:
            value = explicit[2]
            if not isinstance(value, (ast.List, ast.Tuple)) or any(not isinstance(item, ast.Constant) or not isinstance(item.value, str) for item in value.elts):
                raise ValueError(module + ': inferred __all__ must be a literal list or tuple of names')
            return [item.value for item in value.elts]
        return [name for name in scope if not name.startswith('_')]

    def reference(self, module, name, seen=()):
        if len(seen) > 64 or (module, name) in seen:
            raise ValueError('cyclic Python import/type alias: ' + module + '.' + name)
        seen = (*seen, (module, name))
        value = self.scope(module).get(name) if module in self.modules else None
        if value is None:
            child = module + '.' + name if module else name
            return ('module', child) if child in self.modules else ('external', child)
        if value[0] == 'reference':
            return self.reference(value[1], value[2], seen)
        if value[0] == 'expression' and isinstance(value[2], (ast.Name, ast.Attribute)):
            return self.resolve(value[1], value[2], seen)
        return value

    def resolve(self, module, node, seen=()):
        if isinstance(node, ast.Name):
            if node.id in self.scope(module):
                value = self.reference(module, node.id, seen)
                if value[0] == 'module' and value[1] not in self.modules:
                    return ('external', value[1])
                return value
            return ('external', node.id)
        if isinstance(node, ast.Attribute):
            owner = self.resolve(module, node.value, seen)
            if owner[0] == 'module':
                return self.reference(owner[1], node.attr, seen)
            if owner[0] == 'external':
                return ('external', owner[1] + '.' + node.attr)
        return ('expression', module, node)

    def imports(self, entry):
        import sys
        stdlib, visited = set(), set()
        def follow(name):
            if name in self.modules:
                visit(name)
            elif name.split('.')[0] in sys.stdlib_module_names:
                stdlib.add(name)
        def visit(module):
            if module in visited:
                return
            visited.add(module)
            parent = module.rpartition('.')[0]
            if parent in self.modules:
                visit(parent)
            for node in ast.walk(self.tree(module)):
                if isinstance(node, ast.Import):
                    for item in node.names: follow(item.name)
                elif isinstance(node, ast.ImportFrom):
                    owner = self.absolute(module, node)
                    follow(owner)
                    for item in node.names:
                        candidate = owner + '.' + item.name
                        if candidate in self.modules: follow(candidate)
                        elif owner.split('.')[0] in sys.stdlib_module_names:
                            # A from-import may be a submodule or an attribute.
                            # Provisioning attempts it without making it mandatory.
                            if item.name != '*': stdlib.add(candidate)
                elif isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute)):
                    name = ast.unparse(node.func)
                    if name in ('__import__', 'importlib.import_module') and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                        follow(node.args[0].value)
        visit(entry)
        return sorted(stdlib)


def analyze(source, modules=None, entry='main', *, metadata_only=False):
    modules = modules or {entry: [source, '<python entry module>', False]}
    graph = Graph(modules)
    tree = graph.tree(entry)
    stdlib = graph.imports(entry)
    if metadata_only:
        return {'stdlib': stdlib, 'exports': {}}
    declarations = {}
    active = set()
    names = {}

    def wire(name):
        name = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1-\2', name)
        name = re.sub(r'([a-z0-9])([A-Z])', r'\1-\2', name)
        name = re.sub(r'_+', '-', name).strip('-').lower()
        if not re.fullmatch(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*', name):
            raise ValueError('public name cannot map to WIT: ' + name)
        return '%' + name

    def identifier(node, module):
        value = graph.resolve(module, node)
        return value[1] if value[0] == 'external' else ''

    def fail(node, where):
        label = ast.unparse(node) if node is not None else '<missing annotation>'
        raise ValueError(f'{where}: unsupported Python type {label}')

    def ty(node, where, depth=0, result=False, module=entry):
        if depth > 32:
            raise ValueError(where + ': public type expansion exceeds 32 levels')
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return ty(ast.parse(node.value, mode='eval').body, where, depth + 1, result, module)
        if node is None:
            fail(node, where)
        if isinstance(node, ast.Constant) and node.value is None:
            if result:
                return None
            fail(node, where)
        resolved = graph.resolve(module, node)
        if resolved[0] == 'expression' and resolved[2] is not node:
            return ty(resolved[2], where, depth + 1, result, resolved[1])
        name = identifier(node, module)
        scalar = {'int': 's64', 'float': 'f64', 'bool': 'bool', 'str': 'string', 'bytes': 'list<u8>'}
        if name.startswith('builtins.'):
            name = name.partition('.')[2]
        if name in scalar:
            return scalar[name]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            if isinstance(node.right, ast.Constant) and node.right.value is None:
                return optional(node.left, where, depth, module)
            if isinstance(node.left, ast.Constant) and node.left.value is None:
                return optional(node.right, where, depth, module)
        if isinstance(node, ast.Subscript):
            constructor = identifier(node.value, module)
            items = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            if constructor in ('list', 'List', 'typing.List', 'Optional', 'typing.Optional') and len(items) == 1:
                kind = 'option' if constructor.endswith('Optional') else 'list'
                return optional(items[0], where, depth, module) if kind == 'option' else 'list<' + ty(items[0], where, depth + 1, module=module) + '>'
            if constructor in ('Result', 'componentize_py_types.Result') and result and len(items) == 2:
                ok, err = [ty(item, where, depth + 1, True, module) for item in items]
                return 'result' + (f'<{ok or "_"}, {err}>' if err else f'<{ok}>' if ok else '')
        if resolved[0] == 'class':
            owner, cls = resolved[1:]
            identity = owner + '.' + cls.name
            if identity in active:
                raise ValueError(where + ': recursive Python type ' + identity)
            normalized = wire(cls.name if owner == entry else owner.replace('.', '_') + '_' + cls.name)
            if normalized in names and names[normalized] != identity:
                raise ValueError(where + ': public type names collide: ' + identity)
            names[normalized] = identity
            if normalized in declarations:
                return normalized
            active.add(identity)
            is_record = any(identifier(d.func if isinstance(d, ast.Call) else d, owner) in ('dataclass', 'dataclasses.dataclass') for d in cls.decorator_list)
            is_enum = any(identifier(base, owner) in ('Enum', 'enum.Enum') for base in cls.bases)
            fields = []
            if is_record and not cls.bases:
                for field in cls.body:
                    if isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name):
                        fields.append(wire(field.target.id) + ': ' + ty(field.annotation, where + '.' + field.target.id, depth + 1, module=owner))
                kind = 'record'
                limit = 64
            elif is_enum:
                for field in cls.body:
                    if isinstance(field, ast.Assign) and len(field.targets) == 1 and isinstance(field.targets[0], ast.Name) and not field.targets[0].id.startswith('_'):
                        fields.append(wire(field.targets[0].id))
                    elif isinstance(field, ast.AnnAssign):
                        fail(node, where)
                kind = 'enum'
                limit = 256
            else:
                fail(node, where)
            if not fields or len(fields) > limit or len(set(f.split(':')[0] for f in fields)) != len(fields):
                raise ValueError(where + ': invalid or colliding fields/cases in ' + identity)
            declarations[normalized] = f'  {kind} {normalized} {{ ' + ', '.join(fields) + ' }\n'
            active.remove(identity)
            return normalized
        fail(node, where)

    def optional(node, where, depth, module):
        element = ty(node, where, depth + 1, module=module)
        if element.startswith('option<'):
            raise ValueError(where + ': nested Optional types collapse in Python; use authored WIT with Some values')
        return 'option<' + element + '>'

    functions, export_names = {}, {}
    for exported in graph.exports(entry):
        resolved = graph.reference(entry, exported)
        if resolved[0] != 'function':
            continue
        owner, node = resolved[1:]
        where = 'function ' + node.name
        if isinstance(node, ast.AsyncFunctionDef) or node.decorator_list or getattr(node, 'type_params', []):
            raise ValueError(where + ': async, decorated and generic functions require authored WIT')
        args = node.args
        if args.vararg or args.kwarg or args.kwonlyargs:
            raise ValueError(where + ': variadic and keyword-only parameters require authored WIT')
        parameters = args.posonlyargs + args.args
        if len(parameters) > 16:
            raise ValueError(where + ': parameter limit is 16')
        params = [wire(p.arg) + ': ' + ty(p.annotation, where + ' parameter ' + p.arg, module=owner) for p in parameters]
        returns = ty(node.returns, where + ' return', result=True, module=owner)
        name = wire(exported)
        if name in functions:
            raise ValueError(where + ': duplicate or colliding public function name')
        python_name = name[1:].replace('-', '_')
        if keyword.iskeyword(python_name): python_name += '_'
        export_names[python_name] = exported
        functions[name] = f'  {name}: func(' + ', '.join(params) + ')' + (' -> ' + returns if returns else '') + ';\n'
    if not 1 <= len(functions) <= 64:
        raise ValueError('entry module must define 1 to 64 public annotated top-level functions')
    if functions.keys() & declarations.keys():
        raise ValueError('public function and type names collide after WIT normalization')
    body = ''.join(declarations[k] for k in sorted(declarations)) + ''.join(functions[k] for k in sorted(functions))
    return {'declarations': body, 'exports': export_names, 'stdlib': stdlib}


def infer_json(source):
    try:
        try:
            request = json.loads(source)
        except ValueError:
            request = None
        if isinstance(request, dict) and request.get('schema') == 2:
            result = analyze('', request['modules'], request['entry'], metadata_only=request.get('metadataOnly', False))
        else:
            result = analyze(source)
        return json.dumps(result, ensure_ascii=True)
    except Exception as error:
        return json.dumps({'error': str(error)}, ensure_ascii=True)


def infer(source):
    return analyze(source)['declarations']
