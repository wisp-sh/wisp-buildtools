"""Type-hint inference, snapshotted into CPython and called only inside Wasm."""
import ast
import json
import re


def infer(source):
    tree = ast.parse(source, filename='<python entry module>')
    aliases = {}
    classes = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module in ('typing', 'dataclasses', 'enum', 'componentize_py_types'):
            for item in node.names:
                aliases[item.asname or item.name] = item.name
        elif isinstance(node, ast.Import):
            for item in node.names:
                if item.name in ('typing', 'dataclasses', 'enum'):
                    aliases[item.asname or item.name] = item.name
        elif isinstance(node, ast.ClassDef):
            if node.name in classes:
                raise ValueError('duplicate public type ' + node.name)
            classes[node.name] = node
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

    def identifier(node):
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return identifier(node.value) + '.' + node.attr
        return ''

    def fail(node, where):
        label = ast.unparse(node) if node is not None else '<missing annotation>'
        raise ValueError(f'{where}: unsupported Python type {label}')

    def ty(node, where, depth=0, result=False):
        if depth > 32:
            raise ValueError(where + ': public type expansion exceeds 32 levels')
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return ty(ast.parse(node.value, mode='eval').body, where, depth + 1, result)
        if node is None:
            fail(node, where)
        if isinstance(node, ast.Constant) and node.value is None:
            if result:
                return None
            fail(node, where)
        name = identifier(node)
        scalar = {'int': 's64', 'float': 'f64', 'bool': 'bool', 'str': 'string', 'bytes': 'list<u8>'}
        if name in scalar:
            return scalar[name]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            if isinstance(node.right, ast.Constant) and node.right.value is None:
                return optional(node.left, where, depth)
            if isinstance(node.left, ast.Constant) and node.left.value is None:
                return optional(node.right, where, depth)
        if isinstance(node, ast.Subscript):
            constructor = identifier(node.value)
            items = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            if constructor in ('list', 'List', 'typing.List', 'Optional', 'typing.Optional') and len(items) == 1:
                kind = 'option' if constructor.endswith('Optional') else 'list'
                return optional(items[0], where, depth) if kind == 'option' else 'list<' + ty(items[0], where, depth + 1) + '>'
            if constructor in ('Result', 'componentize_py_types.Result') and result and len(items) == 2:
                ok, err = [ty(item, where, depth + 1, True) for item in items]
                return 'result' + (f'<{ok or "_"}, {err}>' if err else f'<{ok}>' if ok else '')
        if isinstance(node, ast.Name) and name in classes:
            if name in active:
                raise ValueError(where + ': recursive Python type ' + name)
            normalized = wire(name)
            if normalized in names and names[normalized] != name:
                raise ValueError(where + ': public type names collide: ' + name)
            names[normalized] = name
            if normalized in declarations:
                return normalized
            active.add(name)
            cls = classes[name]
            is_record = any(identifier(d.func if isinstance(d, ast.Call) else d) in ('dataclass', 'dataclasses.dataclass') for d in cls.decorator_list)
            is_enum = any(identifier(base) in ('Enum', 'enum.Enum') for base in cls.bases)
            fields = []
            if is_record and not cls.bases:
                for field in cls.body:
                    if isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name):
                        fields.append(wire(field.target.id) + ': ' + ty(field.annotation, where + '.' + field.target.id, depth + 1))
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
                raise ValueError(where + ': invalid or colliding fields/cases in ' + name)
            declarations[normalized] = f'  {kind} {normalized} {{ ' + ', '.join(fields) + ' }\n'
            active.remove(name)
            return normalized
        fail(node, where)

    def optional(node, where, depth):
        element = ty(node, where, depth + 1)
        if element.startswith('option<'):
            raise ValueError(where + ': nested Optional types collapse in Python; use authored WIT with Some values')
        return 'option<' + element + '>'

    functions = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name.startswith('_'):
            continue
        where = 'function ' + node.name
        if isinstance(node, ast.AsyncFunctionDef) or node.decorator_list or getattr(node, 'type_params', []):
            raise ValueError(where + ': async, decorated and generic functions require authored WIT')
        args = node.args
        if args.vararg or args.kwarg or args.kwonlyargs:
            raise ValueError(where + ': variadic and keyword-only parameters require authored WIT')
        parameters = args.posonlyargs + args.args
        if len(parameters) > 16:
            raise ValueError(where + ': parameter limit is 16')
        params = [wire(p.arg) + ': ' + ty(p.annotation, where + ' parameter ' + p.arg) for p in parameters]
        returns = ty(node.returns, where + ' return', result=True)
        name = wire(node.name)
        if name in functions:
            raise ValueError(where + ': duplicate or colliding public function name')
        functions[name] = f'  {name}: func(' + ', '.join(params) + ')' + (' -> ' + returns if returns else '') + ';\n'
    if not 1 <= len(functions) <= 64:
        raise ValueError('entry module must define 1 to 64 public annotated top-level functions')
    if functions.keys() & declarations.keys():
        raise ValueError('public function and type names collide after WIT normalization')
    return ''.join(declarations[k] for k in sorted(declarations)) + ''.join(functions[k] for k in sorted(functions))


def infer_json(source):
    try:
        return json.dumps({'declarations': infer(source)}, ensure_ascii=True)
    except (ValueError, SyntaxError, RecursionError) as error:
        return json.dumps({'error': str(error)}, ensure_ascii=True)
