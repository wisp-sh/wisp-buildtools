"""Generate OCaml callbacks and the synchronous canonical ABI for public signatures.

Layouts follow the Component Model canonical ABI. No OCaml or C compiler runs
on the Python host. The generated files are inputs to the packaged Wasm tools.
"""
from __future__ import annotations

from contracts import wire

SCALARS = {'int': ('int32_t', 'i32', 4), 'int32': ('int32_t', 'i32', 4),
           'int64': ('int64_t', 'i64', 8), 'float': ('double', 'f64', 8),
           'bool': ('uint8_t', 'i32', 1)}
CTYPES = {'i32': 'uint32_t', 'i64': 'uint64_t', 'f32': 'float', 'f64': 'double'}


def base(t):
    return base(t.children[0]) if t.kind == 'alias' else t


def align(n, a):
    return (n + a - 1) // a * a


def layout(t):
    t = base(t)
    if t.kind in SCALARS:
        size = SCALARS[t.kind][2]; return size, size, []
    if t.kind == 'unit': return 0, 1, []
    if t.kind == 'enum':
        size = 1 if len(t.fields) <= 256 else 2
        return size, size, []
    if t.kind in ('string', 'bytes', 'list'): return 8, 4, [0, 4]
    if t.kind == 'record':
        offsets, n, a = [], 0, 1
        for child in t.children:
            size, ca, _ = layout(child)
            n = align(n, ca); offsets.append(n); n += size; a = max(a, ca)
        return align(n, a), a, offsets
    if t.kind in ('option', 'result'):
        a = max(layout(c)[1] for c in t.children)
        offset = align(1, a)
        return align(offset + max(layout(c)[0] for c in t.children), a), a, [offset]
    raise ValueError(f'unsupported ABI type {t.kind}')


def join(a, b):
    if a == b: return a
    if {a, b} <= {'i32', 'f32'}: return 'i32'
    return 'i64'


def flat(t):
    t = base(t)
    if t.kind in SCALARS: return [SCALARS[t.kind][1]]
    if t.kind == 'unit': return []
    if t.kind == 'enum': return ['i32']
    if t.kind in ('string', 'bytes', 'list'): return ['i32', 'i32']
    if t.kind == 'record': return [f for c in t.children for f in flat(c)]
    result = []
    for child in t.children:
        for i, f in enumerate(flat(child)):
            if i == len(result): result.append(f)
            else: result[i] = join(result[i], f)
    return ['i32', *result]


def shape(t):
    t = base(t)
    if t.kind == 'int': return 's32'
    if t.kind in ('int32', 'int64', 'float'): return {'int32': 's32', 'int64': 's64', 'float': 'f64'}[t.kind]
    if t.kind == 'unit': return None
    if t.kind == 'bytes': return {'list': 'u8'}
    if t.kind == 'record':
        return {'record': [(wire(n), shape(c)) for n, c in zip(t.fields, t.children)]}
    if t.kind == 'enum':
        import re
        return {'enum': [wire(re.sub(r'(?<=[a-z0-9])([A-Z])', r'_\1', c).lower()) for c in t.fields]}
    if t.kind == 'result': return {'result': [shape(c) for c in t.children]}
    if t.kind in ('list', 'option'): return {t.kind: shape(t.children[0])}
    return t.kind


def exports(signature, document, world_name):
    worlds = [w for w in document['worlds'] if w['name'] == world_name]
    if len(worlds) != 1: raise ValueError('select exactly one WIT world with --world')
    world = worlds[0]
    if world['imports']: raise ValueError('OCaml library WIT imports are not supported')

    def expand(t, active=()):
        if type(t) is not int: return t
        if t in active: raise ValueError('recursive WIT types are unsupported')
        kind = document['types'][t]['kind']; active = (*active, t)
        if 'type' in kind: return expand(kind['type'], active)
        if 'record' in kind:
            return {'record': [(f['name'], expand(f['type'], active)) for f in kind['record']['fields']]}
        if 'enum' in kind: return {'enum': [c['name'] for c in kind['enum']['cases']]}
        if 'result' in kind: return {'result': [expand(kind['result'].get(k), active) for k in ('ok', 'err')]}
        for k in ('option', 'list'):
            if k in kind: return {k: expand(kind[k], active)}
        raise ValueError('unsupported OCaml public WIT type')

    functions = {}
    for name, item in world['exports'].items():
        if 'function' in item:
            functions[name] = (name, item['function'])
        elif 'interface' in item:
            interface = document['interfaces'][item['interface']['id']]
            identity = name
            if name.startswith('interface-'):
                package = document['packages'][interface['package']]['name']
                pkg, sep, version = package.partition('@')
                identity = pkg + '/' + interface['name'] + (sep + version if sep else '')
            for fname, function in interface['functions'].items():
                if fname in functions: raise ValueError('duplicate OCaml WIT function name')
                functions[fname] = (identity + '#' + fname, function)
        else:
            raise ValueError('OCaml exports must be functions or interfaces')
    if set(functions) != {wire(n) for n in signature.functions}:
        raise ValueError('WIT functions differ from the public OCaml .mli')
    names = {}
    for name, (params, result) in signature.functions.items():
        abi, function = functions[wire(name)]
        if function['kind'] != 'freestanding': raise ValueError('only synchronous free functions are supported')
        actual = [expand(p['type']) for p in function['params']]
        if actual != [shape(p) for p in params] or expand(function.get('result')) != shape(result):
            raise ValueError(f'WIT signature differs from OCaml .mli: {name}')
        names[name] = abi
    return names


PREAMBLE = r'''
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <caml/mlvalues.h>
#include <caml/memory.h>
#include <caml/alloc.h>
#include <caml/callback.h>
#define LIMIT (16u * 1024u * 1024u)
static size_t output_bytes;
static void fail(void) { __builtin_trap(); }
static void *allocate(size_t n) {
  if (n > LIMIT || output_bytes > LIMIT - n) fail();
  output_bytes += n;
  void *p = calloc(n ? n : 1, 1); if (!p) fail(); return p;
}
__attribute__((export_name("cabi_realloc")))
void *wisp_realloc(void *p, size_t old, size_t alignment, size_t n) {
  (void)old;
  if (n > LIMIT || alignment > 8) fail();
  if (!n) { free(p); return NULL; }
  p = realloc(p, n); if (!p) fail(); return p;
}
static void *pointer(const unsigned char *p) { uint32_t n; memcpy(&n,p,4); return (void *)(uintptr_t)n; }
static uint32_t length(const unsigned char *p) { uint32_t n; memcpy(&n,p+4,4); if(n>LIMIT)fail(); return n; }
static void buffer(unsigned char *p, void *data, uint32_t n) {
  uint32_t address = (uint32_t)(uintptr_t)data; memcpy(p,&address,4); memcpy(p+4,&n,4);
}
'''


class Generator:
    def __init__(self):
        self.types = []

    def index(self, t):
        t = base(t)
        for i, other in enumerate(self.types):
            if t == other: return i
        i = len(self.types); self.types.append(t)
        for child in t.children: self.index(child)
        return i

    def convert(self, t):
        i = self.index(t); t = base(t); size, _, offsets = layout(t)
        lift, lower, drop = [], [], []
        if t.kind in SCALARS:
            c, _, size = SCALARS[t.kind]
            lift.append(f'{c} n; memcpy(&n,p,{size});')
            if t.kind == 'int':
                lift += ['if(n < -1073741824 || n > 1073741823) fail();', 'r=Val_long(n);']
                lower += ['int32_t n=Long_val(v);']
            elif t.kind == 'int32': lift += ['r=caml_copy_int32(n);']; lower += ['int32_t n=Int32_val(v);']
            elif t.kind == 'int64': lift += ['r=caml_copy_int64(n);']; lower += ['int64_t n=Int64_val(v);']
            elif t.kind == 'float': lift += ['r=caml_copy_double(n);']; lower += ['double n=Double_val(v);']
            elif t.kind == 'bool': lift += ['r=Val_bool(n != 0);']; lower += ['uint8_t n=Bool_val(v);']
            lower.append(f'memcpy(p,&n,{size});')
        elif t.kind == 'unit': lift += ['r=Val_unit;']
        elif t.kind == 'enum':
            lift += [f'uint{size*8}_t n; memcpy(&n,p,{size}); if(n>={len(t.fields)})fail(); r=Val_int(n);']
            lower += [f'uint{size*8}_t n=Int_val(v); if(n>={len(t.fields)})fail(); memcpy(p,&n,{size});']
        elif t.kind in ('string', 'bytes'):
            lift += ['r=caml_alloc_initialized_string(length(p),pointer(p));']
            lower += ['size_t n=caml_string_length(v); void *data=allocate(n); memcpy(data,String_val(v),n); buffer(p,data,n);']
            drop += ['free(pointer(p));']
        elif t.kind == 'list':
            child = t.children[0]; ci = self.index(child); cs = layout(child)[0]
            lift += [f'uint32_t n=length(p); if(n>LIMIT/{max(1,cs)})fail(); unsigned char *data=pointer(p); r=Val_emptylist;',
                     f'for(uint32_t j=n;j>0;j--) {{ t=lift_{ci}(data+(j-1)*{cs}); node=caml_alloc(2,0); Store_field(node,0,t); Store_field(node,1,r); r=node; }}']
            lower += ['size_t n=0; for(value x=v;x!=Val_emptylist;x=Field(x,1)) { if(++n>LIMIT)fail(); }',
                      f'if(n>LIMIT/{max(1,cs)})fail(); unsigned char *data=allocate(n*{cs}); buffer(p,data,n);',
                      f'for(size_t j=0;j<n;j++,v=Field(v,1)) lower_{ci}(Field(v,0),data+j*{cs});']
            drop += [f'uint32_t n=length(p); unsigned char *data=pointer(p); for(uint32_t j=0;j<n;j++) drop_{ci}(data+j*{cs}); free(data);']
        elif t.kind == 'record':
            floats = all(base(c).kind == 'float' for c in t.children)
            if floats:
                lift += [f'r=caml_alloc({len(t.children)}*Double_wosize,Double_array_tag);']
                for j, offset in enumerate(offsets):
                    lift += [f'{{ double n; memcpy(&n,p+{offset},8); Store_double_field(r,{j},n); }}']
                    lower += [f'{{ double n=Double_field(v,{j}); memcpy(p+{offset},&n,8); }}']
            else:
                lift += [f'r=caml_alloc({len(t.children)},0);', f'for(int j=0;j<{len(t.children)};j++) Store_field(r,j,Val_unit);']
                for j, (child, offset) in enumerate(zip(t.children, offsets)):
                    ci = self.index(child)
                    lift += [f't=lift_{ci}(p+{offset}); Store_field(r,{j},t);']
                    lower += [f'lower_{ci}(Field(v,{j}),p+{offset});']
            for child, offset in zip(t.children, offsets):
                drop += [f'drop_{self.index(child)}(p+{offset});']
        elif t.kind in ('option', 'result'):
            offset = offsets[0]
            lift += ['if(*p>1)fail();']
            if t.kind == 'option':
                ci = self.index(t.children[0])
                lift += [f'if(!*p) r=Val_none; else {{ t=lift_{ci}(p+{offset}); r=caml_alloc(1,0); Store_field(r,0,t); }}']
                lower += [f'*p=Is_block(v); if(*p) lower_{ci}(Field(v,0),p+{offset});']
                drop += [f'if(*p)drop_{ci}(p+{offset});']
            else:
                for tag, child in enumerate(t.children):
                    ci = self.index(child)
                    lift += [f'if(*p=={tag}) {{ t=lift_{ci}(p+{offset}); r=caml_alloc(1,{tag}); Store_field(r,0,t); }}']
                    lower += [f'if(Tag_val(v)=={tag}) {{ *p={tag}; lower_{ci}(Field(v,0),p+{offset}); }}']
                    drop += [f'if(*p=={tag})drop_{ci}(p+{offset});']
        return (f'static value lift_{i}(const unsigned char *p) {{ CAMLparam0(); CAMLlocal3(r,t,node); ' + ' '.join(lift) + ' CAMLreturn(r); }\n'
                + f'static void lower_{i}(value v, unsigned char *p) {{ ' + ' '.join(lower) + ' }\n'
                + f'static void drop_{i}(const unsigned char *p) {{ ' + ' '.join(drop) + ' }\n')

    def pack(self, t, expressions, p):
        """Store flattened arguments in canonical memory, including variant joins."""
        t = base(t); _, _, offsets = layout(t)
        if t.kind == 'unit': return []
        if t.kind in SCALARS or t.kind == 'enum':
            c, target, size = SCALARS.get(t.kind, (f'uint{layout(t)[0]*8}_t', 'i32', layout(t)[0]))
            expression, source = expressions[0]
            # Variant joins reinterpret float bits and zero-extend narrow slots.
            if target == 'f64' and source != 'f64':
                expr = f'bits_double((uint64_t){expression})'
            else: expr = expression
            return [f'{{ {c} n=({c})({expr}); memcpy({p},&n,{size}); }}']
        if t.kind in ('string', 'bytes', 'list'):
            return [f'{{ uint32_t n={expressions[j][0]}; memcpy({p}+{j*4},&n,4); }}' for j in range(2)]
        if t.kind == 'record':
            lines, cursor = [], 0
            for child, offset in zip(t.children, offsets):
                count = len(flat(child)); lines += self.pack(child, expressions[cursor:cursor+count], f'({p}+{offset})'); cursor += count
            return lines
        lines = [f'*{p}=(uint8_t){expressions[0][0]};']
        for tag, child in enumerate(t.children):
            discriminant = tag if t.kind == 'result' else 1
            lines += [f'if({expressions[0][0]}=={discriminant}) {{', *self.pack(child, expressions[1:1+len(flat(child))], f'({p}+{offsets[0]})'), '}']
        return lines

    def generate(self, signature, names, module):
        for params, result in signature.functions.values():
            for t in [*params, result]: self.index(t)
        code = [PREAMBLE, 'static double bits_double(uint64_t n) { double f; memcpy(&f,&n,8); return f; }']
        for i in range(len(self.types)):
            code += [f'static value lift_{i}(const unsigned char *); static void lower_{i}(value,unsigned char *); static void drop_{i}(const unsigned char *);']
        code += [self.convert(t) for t in self.types]
        ml = []
        for number, (name, (params, result)) in enumerate(signature.functions.items()):
            abi = names[name]
            ml += [f'let () = Callback.register "wisp:{number}" {module}.{name}']
            flattened = [f for t in params for f in flat(t)]
            arguments = ', '.join(f'{CTYPES[f]} a{i}' for i, f in enumerate(flattened)) or 'void'
            indirect = len(flattened) > 16
            if indirect: arguments = 'uint32_t input'
            rf = flat(result); ret = 'void' if not rf else CTYPES[rf[0]] if len(rf) == 1 else 'uint32_t'
            code += [f'__attribute__((export_name("{abi}"))) {ret} call_{number}({arguments}) {{',
                     'CAMLparam0(); CAMLlocal1(result);', f'CAMLlocalN(args,{max(1,len(params))});', 'output_bytes=0;']
            cursor, mem = 0, 0
            for i, t in enumerate(params):
                size, alignment, _ = layout(t)
                if indirect:
                    mem = align(mem, alignment); p = f'((unsigned char *)(uintptr_t)input+{mem})'; mem += size
                else:
                    p = f'p{i}'; code += [f'_Alignas(8) unsigned char {p}[{max(1,size)}]={{0}};']
                    expressions = [(f'a{j}', flattened[j]) for j in range(cursor,cursor+len(flat(t)))]
                    code += self.pack(t, expressions, p); cursor += len(flat(t))
                code += [f'args[{i}]=lift_{self.index(t)}({p}); drop_{self.index(t)}({p});']
            if indirect: code += ['free((void *)(uintptr_t)input);']
            if not params: code += ['args[0]=Val_unit;']
            code += [f'const value *closure=caml_named_value("wisp:{number}"); if(!closure)fail();',
                     f'result=caml_callbackN_exn(*closure,{max(1,len(params))},args);',
                     'if(Is_exception_result(result)) { fputs("uncaught OCaml exception\\n",stderr); fail(); }']
            ri = self.index(result); size = max(1,layout(result)[0])
            if len(rf)>1:
                code += [f'unsigned char *p=allocate({size}); lower_{ri}(result,p); CAMLreturnT(uint32_t,(uint32_t)(uintptr_t)p);', '}',
                         f'__attribute__((export_name("cabi_post_{abi}"))) void post_{number}(uint32_t p) {{ drop_{ri}((unsigned char *)(uintptr_t)p); free((void *)(uintptr_t)p); }}']
            elif rf:
                code += [f'_Alignas(8) unsigned char p[{size}]={{0}}; lower_{ri}(result,p); {ret} n=0; memcpy(&n,p,{min(size,8)}); CAMLreturnT({ret},n);', '}']
            else: code += ['CAMLreturn0;', '}']
        return '\n'.join(code) + '\n', '\n'.join(ml) + '\n'
