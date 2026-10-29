"""Read the supported public OCaml signature grammar without evaluating source.

The real OCaml compiler checks the .mli against the implementation. This parser
fails closed on declarations that cannot cross the Component boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re


@dataclass
class Type:
    kind: str
    name: str = ''
    children: list = field(default_factory=list)
    fields: list = field(default_factory=list)


PRIMITIVES = {'int', 'int32', 'int64', 'float', 'bool', 'string', 'bytes', 'unit'}
WIRE = {'int': 's32', 'int32': 's32', 'int64': 's64', 'float': 'f64',
        'bool': 'bool', 'string': 'string', 'bytes': 'list<u8>', 'unit': '_'}


def wire(name):
    if not re.fullmatch(r'[a-z][a-z0-9]*(?:_[a-z0-9]+)*', name):
        raise ValueError(f'public name cannot map to WIT: {name}')
    return name.replace('_', '-')


def tokens(text):
    # OCaml comments nest. Quotes inside comments shield comment delimiters.
    out, i, depth = [], 0, 0
    while i < len(text):
        if text.startswith('(*', i):
            depth += 1; i += 2
        elif depth and text.startswith('*)', i):
            depth -= 1; i += 2
        elif depth:
            if text[i] == '"':
                i += 1
                while i < len(text) and text[i] != '"':
                    i += 2 if text[i] == '\\' else 1
                i += 1
            else:
                i += 1
        elif text[i].isspace():
            i += 1
        else:
            match = re.match(r"[a-zA-Z_][a-zA-Z_0-9']*|->|[(){}:;,|=]", text[i:])
            if not match:
                raise ValueError(f'unsupported public signature near {text[i:i+40]!r}')
            out.append(match[0]); i += len(match[0])
    if depth:
        raise ValueError('unterminated OCaml comment')
    return out


class Signature:
    def __init__(self, text):
        if len(text.encode()) > 65536: raise ValueError('public signature exceeds 64 KiB')
        self.tokens, self.pos = tokens(text), 0
        self.types, self.functions = {}, {}
        while self.peek():
            if self.take() == 'type':
                name = self.take(); wire(name)
                if name in self.types or name in PRIMITIVES:
                    raise ValueError(f'duplicate or reserved public type {name}')
                self.expect('=')
                if self.peek() == '{':
                    self.take(); names, children = [], []
                    while self.peek() != '}':
                        label = self.take(); wire(label); self.expect(':')
                        if label in names:
                            raise ValueError('duplicate record field')
                        names.append(label); children.append(self.ty())
                        if self.peek() != '}': self.expect(';')
                    self.take()
                    if not names: raise ValueError('empty public record')
                    if len(names) > 64 or any(c.kind == 'unit' for c in children):
                        raise ValueError('public records support 1 to 64 non-unit fields')
                    ty = Type('record', name, children, names)
                elif self.peek() == '|' or (self.peek() and self.peek()[0].isupper()):
                    cases = []
                    if self.peek() == '|': self.take()
                    while True:
                        case = self.take()
                        if not re.fullmatch(r'[A-Z][A-Za-z0-9_]*', case):
                            raise ValueError('public variants must have constant constructors')
                        # Preserve OCaml constructor spelling; normalize WIT separately.
                        case_wire = re.sub(r'(?<=[a-z0-9])([A-Z])', r'_\1', case).lower()
                        wire(case_wire)
                        if case in cases: raise ValueError('duplicate enum case')
                        cases.append(case)
                        if self.peek() != '|': break
                        self.take()
                    if len(cases) > 256: raise ValueError('public enum case limit is 256')
                    ty = Type('enum', name, fields=cases)
                else:
                    target = self.ty()
                    if target.kind == 'unit': raise ValueError('public unit aliases are unsupported')
                    ty = Type('alias', name, [target])
                self.types[name] = ty
            else:
                if self.tokens[self.pos - 1] != 'val':
                    raise ValueError('public signatures support type and val declarations only')
                name = self.take(); wire(name); self.expect(':')
                parts = [self.ty()]
                while self.peek() == '->':
                    self.take(); parts.append(self.ty())
                if len(parts) < 2:
                    raise ValueError(f'{name}: exported values must be functions')
                if name in self.functions:
                    raise ValueError(f'duplicate public function {name}')
                params = parts[:-1]
                if len(params) == 1 and params[0].kind == 'unit': params = []
                if any(p.kind == 'unit' for p in params):
                    raise ValueError('unit parameters must be the sole argument')
                if len(params) > 16: raise ValueError('public function parameter limit is 16')
                self.functions[name] = (params, parts[-1])
            if len(self.types) > 64 or len(self.functions) > 64:
                raise ValueError('public declaration limit is 64')
        budget = [4096]
        def visit(ty, depth=0):
            budget[0] -= 1
            if budget[0] < 0 or depth > 32: raise ValueError('public type expansion limit')
            for child in ty.children: visit(child, depth + 1)
        for params, result in self.functions.values():
            for ty in [*params, result]: visit(ty)
        if not self.functions:
            raise ValueError('public .mli must declare at least one function')

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else ''

    def take(self):
        if not self.peek(): raise ValueError('incomplete public signature')
        value = self.peek(); self.pos += 1; return value

    def expect(self, value):
        if self.take() != value: raise ValueError(f'expected {value} in public signature')

    def ty(self, depth=0):
        if depth > 16: raise ValueError('public type nesting limit is 16')
        token = self.take()
        if token == '(':
            result = self.ty(depth + 1)
            if self.peek() == ',':
                self.take(); error = self.ty(depth + 1)
                self.expect(')'); self.expect('result')
                result = Type('result', children=[result, error])
            else:
                self.expect(')')
        elif token in PRIMITIVES:
            result = Type(token)
        elif token in self.types:
            result = self.types[token]
        else:
            raise ValueError(f'unsupported, recursive or forward public type {token}')
        while self.peek() in ('list', 'option'):
            kind = self.take()
            if result.kind == 'unit': raise ValueError('unit collections are unsupported')
            result = Type(kind, children=[result])
            depth += 1
            if depth > 16: raise ValueError('public type nesting limit is 16')
        return result

    def wit_type(self, ty):
        if ty.name: return '%' + wire(ty.name)
        if ty.kind in WIRE: return WIRE[ty.kind]
        return ty.kind + '<' + ', '.join(self.wit_type(t) for t in ty.children) + '>'

    def declarations(self):
        lines = []
        for name, ty in self.types.items():
            name = '%' + wire(name)
            if ty.kind == 'record':
                lines.append('record ' + name + ' { ' + ', '.join('%' + wire(n) + ': ' + self.wit_type(t)
                             for n, t in zip(ty.fields, ty.children)) + ' }')
            elif ty.kind == 'enum':
                cases = [re.sub(r'(?<=[a-z0-9])([A-Z])', r'_\1', c).lower() for c in ty.fields]
                lines.append('enum ' + name + ' { ' + ', '.join('%' + wire(c) for c in cases) + ' }')
            else:
                lines.append('type ' + name + ' = ' + self.wit_type(ty.children[0]) + ';')
        for name, (params, result) in self.functions.items():
            args = ', '.join(f'a{i}: {self.wit_type(t)}' for i, t in enumerate(params))
            ret = '' if result.kind == 'unit' else ' -> ' + self.wit_type(result)
            lines.append(f'%{wire(name)}: func({args}){ret};')
        return '\n'.join(lines)

    def wit(self):
        return 'package ocaml:library;\ninterface api {\n' + self.declarations() + '\n}\nworld library { export api; }\n'
