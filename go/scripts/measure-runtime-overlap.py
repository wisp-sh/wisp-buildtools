#!/usr/bin/env python3
"""Count runtime function bodies embedded in the three Go Wasm tools."""

import argparse
from pathlib import Path


def number(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        byte = data[pos]
        pos += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, pos
        shift += 7


def string(data: bytes, pos: int) -> tuple[str, int]:
    length, pos = number(data, pos)
    return data[pos:pos + length].decode(), pos + length


def functions(path: Path) -> dict[str, int]:
    data = path.read_bytes()
    if data[:8] != b"\0asm\x01\0\0\0":
        raise ValueError(f"not a core Wasm module: {path}")
    imports = 0
    bodies = []
    names = {}
    pos = 8
    while pos < len(data):
        section = data[pos]
        length, pos = number(data, pos + 1)
        end = pos + length
        if section == 2:
            count, cursor = number(data, pos)
            for _ in range(count):
                _, cursor = string(data, cursor)
                _, cursor = string(data, cursor)
                kind = data[cursor]
                cursor += 1
                if kind != 0:
                    raise ValueError(f"non-function import in {path}; extend parser")
                _, cursor = number(data, cursor)
                imports += 1
        elif section == 10:
            count, cursor = number(data, pos)
            for _ in range(count):
                size, cursor = number(data, cursor)
                bodies.append(size)
                cursor += size
        elif section == 0:
            label, cursor = string(data, pos)
            if label == "name":
                while cursor < end:
                    subsection = data[cursor]
                    size, cursor = number(data, cursor + 1)
                    stop = cursor + size
                    if subsection == 1:
                        count, cursor = number(data, cursor)
                        for _ in range(count):
                            index, cursor = number(data, cursor)
                            names[index], cursor = string(data, cursor)
                    cursor = stop
        pos = end
    return {names.get(imports + index, ""): size for index, size in enumerate(bodies)
            if names.get(imports + index, "").startswith(("runtime.", "internal/runtime/"))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("toolchain", type=Path)
    args = parser.parse_args()
    sizes = {tool: functions(args.toolchain / f"bin/{tool}.wasm")
             for tool in ("compile", "asm", "link")}
    for tool, functions_by_name in sizes.items():
        print(f"{tool}: {len(functions_by_name)} runtime functions, "
              f"{sum(functions_by_name.values()):,} bytes of function bodies")
    common = set.intersection(*(set(value) for value in sizes.values()))
    total = sum(sum(sizes[tool][name] for name in common) for tool in sizes)
    duplicated = total - sum(max(sizes[tool][name] for tool in sizes) for name in common)
    print(f"shared by all three: {len(common)} functions")
    print(f"duplicate runtime function bodies, approximate: {duplicated:,} bytes")


if __name__ == "__main__":
    main()
