"""Small binary packaging helpers. These never parse or compile Python source."""
import struct
import json

CORE_HEADER = b'\0asm\x01\0\0\0'
COMPONENT_HEADER = b'\0asm\x0d\0\x01\0'


def uleb(number):
    if number < 0:
        raise ValueError('negative unsigned LEB')
    result = bytearray()
    while True:
        value = number & 127
        number >>= 7
        result.append(value | (128 if number else 0))
        if not number:
            return bytes(result)


def read_uleb(data, position):
    result = shift = 0
    while position < len(data) and shift <= 35:
        value = data[position]
        position += 1
        result |= (value & 127) << shift
        if not value & 128:
            return result, position
        shift += 7
    raise ValueError('invalid or truncated Wasm LEB')


def section(kind, payload):
    return bytes([kind]) + uleb(len(payload)) + payload


def name(value):
    value = value.encode('utf-8')
    return uleb(len(value)) + value


def sections(data):
    if data[:8] not in (CORE_HEADER, COMPONENT_HEADER):
        raise ValueError('expected a Wasm module or component')
    position = 8
    while position < len(data):
        kind = data[position]
        length, start = read_uleb(data, position + 1)
        end = start + length
        if end > len(data):
            raise ValueError('truncated Wasm section')
        yield kind, data[start:end]
        position = end


SOURCE_MARKER = section(0, name('wisp.python.source') + b'v2')


def source_component(source, entry='add'):
    """A constant UTF-8 string provider using the Component Model canonical ABI."""
    source = json.dumps({'source': source.decode('utf-8', errors='strict'), 'entry': entry},
                        ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return source_payload_component(source)


def source_payload_component(source):
    """Encode already serialized provider data without inspecting Python source."""
    data = struct.pack('<II', 8, len(source)) + source
    pages = max(1, (len(data) + 65535) // 65536)
    core = CORE_HEADER
    core += section(1, bytes.fromhex('016000017f'))  # () -> i32
    core += section(3, bytes.fromhex('0100'))
    core += section(5, b'\x01\x00' + uleb(pages))
    core += section(7, b'\x02' + name('memory') + b'\x02\x00' + name('get') + b'\x00\x00')
    core += section(10, bytes.fromhex('01040041000b'))  # return address zero
    core += section(11, bytes.fromhex('010041000b') + uleb(len(data)) + data)
    result = COMPONENT_HEADER + SOURCE_MARKER + section(1, core)
    result += section(2, bytes.fromhex('01000000'))  # instantiate core module
    result += section(7, bytes.fromhex('0140000073'))  # () -> string
    result += section(6, b'\x02\x00\x00\x01\x00' + name('get') + b'\x00\x02\x01\x00' + name('memory'))
    result += section(8, bytes.fromhex('0100000001030000'))  # canonical lift with memory
    result += section(5, b'\x01\x01\x01\x00' + name('get') + b'\x01\x00')
    result += section(11, b'\x01\x00' + name('wisp:python/source@0.1.0') + b'\x05\x00\x00')
    return result


def public_component(component):
    """Hide componentize-py's bootstrap-only init interface from the public world."""
    expected = 'wisp:python/api@0.1.0'
    found = 0
    result = component[:8]
    for kind, payload in sections(component):
        if kind == 11:
            count, position = read_uleb(payload, 0)
            exports = []
            for _ in range(count):
                start = position
                if payload[position] != 0:
                    raise ValueError('expected an unqualified Component export name')
                length, position = read_uleb(payload, position + 1)
                exported = payload[position:position + length].decode('utf-8')
                position += length
                if payload[position] not in (1, 5):
                    raise ValueError('expected an exported Component function or instance')
                _, position = read_uleb(payload, position + 1)
                if payload[position] != 0:
                    raise ValueError('unexpected explicit export type in composed runtime')
                position += 1
                if exported == expected:
                    exports.append(payload[start:position])
                    found += 1
            if position != len(payload):
                raise ValueError('unexpected bytes after Component exports')
            payload = uleb(len(exports)) + b''.join(exports)
        result += section(kind, payload)
    if found != 1:
        raise ValueError(f'expected one public Python interface, found {found}')
    return result


def replace_source(template, source, entry='add'):
    count = 0
    replacement = source_component(source, entry)

    def rewrite(component):
        nonlocal count
        result = component[:8]
        for kind, payload in sections(component):
            if kind == 4:
                if payload.startswith(COMPONENT_HEADER + SOURCE_MARKER):
                    payload = replacement
                    count += 1
                else:
                    payload = rewrite(payload)
            result += section(kind, payload)
        return result

    result = rewrite(template)
    if count != 1:
        raise ValueError(f'expected exactly one source component, found {count}')
    return result
