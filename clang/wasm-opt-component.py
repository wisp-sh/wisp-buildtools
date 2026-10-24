#!/usr/bin/env python3
"""Run wasm-opt on the main Core module inside a stripped WASIp3 component."""
import os
import subprocess
import sys
from pathlib import Path


def read_leb(data, offset):
    value = shift = 0
    while True:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7f) << shift
        if byte < 0x80:
            return value, offset
        shift += 7


def write_leb(value):
    output = bytearray()
    while True:
        byte = value & 0x7f
        value >>= 7
        output.append(byte | 0x80 if value else byte)
        if not value:
            return output


def main():
    if len(sys.argv) != 4 or sys.argv[2] not in ("-O1", "-Os", "-Oz"):
        raise SystemExit("usage: wasm-opt-component.py INPUT -O1|-Os|-Oz OUTPUT")
    source, opt, destination = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    data = source.read_bytes()
    if data[:8] != b"\0asm\r\0\1\0":
        raise SystemExit("input is not a WebAssembly component")
    sections = []
    offset = 8
    while offset < len(data):
        section_start = offset
        kind = data[offset]
        size, payload_start = read_leb(data, offset + 1)
        sections.append((kind, section_start, payload_start, payload_start + size))
        offset = payload_start + size
    modules = [section for section in sections if section[0] == 1]
    largest = max(modules, key=lambda section: section[3] - section[2])
    if largest[3] - largest[2] < 1_000_000:
        raise SystemExit("large compiler Core module not found")
    temp = destination.parent / (destination.name + ".core-input.wasm")
    optimized = destination.parent / (destination.name + ".core-output.wasm")
    temp.write_bytes(data[largest[2]:largest[3]])
    root = Path(__file__).resolve().parent
    binaryen = Path(os.environ.get("BINARYEN_ROOT",
                                 root / ".toolchains/binaryen-version_133"))
    env = os.environ.copy()
    subprocess.run([str(binaryen / "bin/wasm-opt"), "--all-features", "--disable-compact-imports", "--disable-custom-descriptors", opt,
                    str(temp), "-o", str(optimized)], check=True, env=env)
    optimized_bytes = optimized.read_bytes()
    rebuilt = bytearray(data[:largest[1]])
    rebuilt.append(1)
    rebuilt.extend(write_leb(len(optimized_bytes)))
    rebuilt.extend(optimized_bytes)
    rebuilt.extend(data[largest[3]:])
    destination.write_bytes(rebuilt)
    tools = root / ".toolchains/wasm-tools-1.262.0-x86_64-linux/wasm-tools"
    subprocess.run([str(tools), "validate", str(destination)], check=True)
    temp.unlink()
    optimized.unlink()
    print(f"{opt}: {len(data)} -> {len(rebuilt)} bytes")


if __name__ == "__main__":
    main()
