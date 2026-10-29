#!/usr/bin/env python3
"""Same-instance checks using Wasmtime's Python Component API (50.0.1)."""
import argparse
import sys
from pathlib import Path


def verify(path, interface="example:csharp-library/api@1.0.0", inferred=False):
    import wasmtime
    from wasmtime.component import Component, Linker, Record
    config = wasmtime.Config(); config.parallel_compilation = False; config.cranelift_opt_level = 'none'
    engine = wasmtime.Engine(config); store = wasmtime.Store(engine)
    wasi = wasmtime.WasiConfig(); wasi.inherit_stderr(); store.set_wasi(wasi)
    component = Component.from_file(engine, str(path)); linker = Linker(engine); linker.add_wasip2()
    instance = linker.instantiate(store, component)
    api = instance.get_export_index(store, interface)
    count = 0
    def call(name, *args):
        nonlocal count
        index = name if name == 'add' and not inferred else instance.get_export_index(store, name, api)
        function = instance.get_func(store, index)
        value = function(store, *args); function.post_return(store); count += 1
        return value
    def person(name, age):
        value = Record(); value.name = name; value.age = age; return value
    assert call('add', 9007199254740993, 1) == 9007199254740994
    assert call('greet', '世界') == 'Hello, 世界!'
    assert call('advance', person('Zoë', 41)) == person('Zoë', 42)
    assert call('advance', person('Zoë', 0)) == 'age must be positive'
    assert call('choose', 'high') == b'*'
    assert call('choose', None) == b'\0'
    assert call('permissions', {'read'}) == {'read', 'write'}
    assert call('echo-char', '🦀') == '🦀'
    assert call('pair', 'hello', -42) == ('hello', -42)
    assert call('people', [person('雪', 1), person('Ana', 2)]) == [person('雪', 2), person('Ana', 3)]
    assert call('maybe', None) is None
    assert call('maybe', 'yes') == 'yes'
    assert call('scale', 1.25) == 2.5
    assert call('scale', None) is None
    assert call('number', 1.25) == 1.25
    assert call('number', -9007199254740993) == -9007199254740993
    assert call('flip', True) is False
    assert call('nothing') is None
    for name, value in [('u8',255),('s8',-128),('u16',65535),('s16',-32768),('u32',4294967295),('s32',-2147483648),('u64',18446744073709551615),('f32',1.25),('f64',-1.25)]:
        assert call('echo-' + name, value) == value
    assert call('many', *range(1, 18)) == 153
    # Each call allocates strings and nested lists. Post-return must reclaim output
    # buffers without freeing the adapter's persistent state or leaking input data.
    for i in range(100):
        value = '雪' * 4096
        assert call('greet', value) == 'Hello, ' + value + '!'
        assert call('people', [person(value, i)]) == [person(value, i + 1)]
        assert call('add', i, 1) == i + 1
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('component', type=Path)
    parser.add_argument('--interface', default='example:csharp-library/api@1.0.0')
    parser.add_argument('--inferred', action='store_true')
    parser.add_argument('--bindings', type=Path, help='directory containing the wasmtime Python package')
    args = parser.parse_args()
    if args.bindings: sys.path.insert(0, str(args.bindings))
    print(f'{verify(args.component, args.interface, args.inferred)} Component calls passed in one instance, with post-return')

if __name__ == '__main__': main()
