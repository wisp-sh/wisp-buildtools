"""Post-link a pinned Swift image into frontend state and an external backend."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def number(data, offset):
    value, shift = 0, 0
    while True:
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
        shift += 7


def text(data, offset):
    size, offset = number(data, offset)
    return data[offset:offset + size].decode("utf-8"), offset + size


def function_names(source):
    data = source.read_bytes()
    names, offset = {}, 8
    while offset < len(data):
        size, start = number(data, offset + 1)
        end = start + size
        if data[offset] == 0:
            name, cursor = text(data, start)
            if name == "name":
                while cursor < end:
                    kind = data[cursor]
                    length, item = number(data, cursor + 1)
                    following = item + length
                    if kind == 1:
                        count, item = number(data, item)
                        for _ in range(count):
                            index, item = number(data, item)
                            value, item = text(data, item)
                            names[index] = value
                    cursor = following
        offset = end
    if not names or set(names) != set(range(len(names))):
        raise ValueError("upstream Swift image must retain its complete function names")
    return names


def strip_debug(source, output):
    data = source.read_bytes()
    with output.open("wb") as target:
        target.write(data[:8])
        offset = 8
        while offset < len(data):
            size, start = number(data, offset + 1)
            end = start + size
            keep = True
            if data[offset] == 0:
                name, _ = text(data, start)
                keep = name != "name" and not name.startswith(".debug")
            if keep:
                target.write(data[offset:end])
            offset = end


def leb(value):
    encoded = bytearray()
    while True:
        byte = value & 127
        value >>= 7
        encoded.append(byte | (128 if value else 0))
        if not value:
            return bytes(encoded)


def export_primary_memory(source, output):
    """WASI resolves memory from the immediate caller, including the backend."""
    data = source.read_bytes()
    alias = b"\x06memory\x02\x00"
    sections, offset, inserted = [], 8, False
    while offset < len(data):
        section = data[offset]
        size, start = number(data, offset + 1)
        end = start + size
        if section == 7:
            count, entries = number(data, start)
            cursor = entries
            for _ in range(count):
                name, cursor = text(data, cursor)
                kind = data[cursor]
                index, cursor = number(data, cursor + 1)
                if name == "memory":
                    if kind != 2 or index != 0:
                        raise ValueError("Swift backend has an unexpected memory export")
                    output.write_bytes(data)
                    return
            payload = leb(count + 1) + data[entries:end] + alias
            sections.append(b"\x07" + leb(len(payload)) + payload)
            inserted = True
        else:
            if not inserted and section > 7:
                payload = b"\x01" + alias
                sections.append(b"\x07" + leb(len(payload)) + payload)
                inserted = True
            sections.append(data[offset:end])
        offset = end
    if not inserted:
        payload = b"\x01" + alias
        sections.append(b"\x07" + leb(len(payload)) + payload)
    output.write_bytes(data[:8] + b"".join(sections))


def tools(root, lock):
    default = root.parent / "clang/.toolchains/binaryen-version_133"
    opt = Path(os.environ.get("WISP_WASM_OPT", default / "bin/wasm-opt")).resolve()
    splitter = opt.with_name("wasm-split")
    for name, path in [("wasm-opt", opt), ("wasm-split", splitter)]:
        if sha(path) != lock["binaryen"][name + "Sha256"]:
            raise ValueError(f"Swift bootstrap requires the pinned Binaryen {lock['binaryen']['version']} {name}")
    environment = {**os.environ, "BINARYEN_CORES": "2"}
    return opt, splitter, environment


def create(root, name, source, work, lock):
    opt, splitter, environment = tools(root, lock)
    symbols = work / (name + "-symbols.txt")
    with symbols.open("w") as stream:
        subprocess.run([str(opt), str(source), "--nm", "-o", os.devnull], stdout=stream,
                       env=environment, check=True)
    identifiers = [line.strip().rsplit(" : ", 1)[0] for line in symbols.read_text().splitlines() if " : " in line]
    originals = function_names(source)
    first_defined = len(originals) - len(identifiers)
    entry = {"_start", "main", "__wasm_call_ctors", "initializeSwiftModules"}
    selected, kept = [], []
    for index, identifier in enumerate(identifiers, first_defined):
        original = originals[index]
        decoded = re.sub(r"\\([0-9a-fA-F]{2})", lambda match: chr(int(match[1], 16)), identifier)
        if not decoded.startswith(original):
            raise ValueError("Binaryen names do not match the pinned Swift function order")
        known_backend = any(pattern in original for pattern in ["llvm::", "clang::", "lld::"]) or original.startswith("LLVM")
        is_swift_frontend = "swift::" in original and not known_backend
        if original not in entry and not is_swift_frontend:
            selected.append(identifier)
        else:
            kept.append(original)
    if len(selected) < 50000:
        raise ValueError("incomplete Swift backend function selection")
    selection = work / (name + "-selection.txt")
    selection.write_text("\n".join(selected) + "\n")
    raw_primary, raw_backend = work / (name + "-primary.debug.wasm"), work / (name + "-backend.debug.wasm")
    argv = [str(splitter), str(source), "--split-funcs", "@" + str(selection), "--no-placeholders",
            "--import-namespace", name + "-image", "--primary-output", str(raw_primary),
            "--secondary-output", str(raw_backend), "--debuginfo", "--all-features", "--disable-compact-imports", "--disable-custom-descriptors"]
    result = subprocess.run(argv, env=environment, capture_output=True, text=True, check=True)
    if "does not exist" in result.stderr:
        raise ValueError("Binaryen rejected a Swift backend function name")
    primary, backend = work / (name + "-primary.wasm"), work / (name + "-llvm.so.wasm")
    strip_debug(raw_primary, primary)
    exported_backend = work / (name + "-backend-memory.wasm")
    export_primary_memory(raw_backend, exported_backend)
    strip_debug(exported_backend, backend)
    return {"sourceSha256": sha(source), "sourceFunctions": len(originals), "definedFunctions": len(identifiers),
            "selectedFunctions": len(selected), "retainedFunctions": len(kept),
            "selectionSha256": sha(selection), "primaryNamespace": name + "-image",
            "backendMemoryExport": "alias of primary memory 0 for WASI caller access",
            "primary": {"sha256": sha(primary), "bytes": primary.stat().st_size},
            "backend": {"sha256": sha(backend), "bytes": backend.stat().st_size},
            "policy": "Retain positively identified Swift frontend functions and process entry points. Move LLVM, Clang, LLD, C/C++ runtime and unattributed helpers to the exact-image backend. No source archive ownership inventory is claimed."}
