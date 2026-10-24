#!/usr/bin/env python3
"""Extract upstream's JavaScript compiler without running its Node executable.

Grain publishes its js_of_ocaml compiler as a Brotli-compressed asset inside
the release's pkg executable. The executable is only a verified data source.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import io
import json
from pathlib import Path
import tarfile
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.7.2"
COMMIT = "49829d7966b38b177291f7e91f5eb81c65ec07aa"
RELEASE = {
    "url": "https://github.com/grain-lang/grain/releases/download/grain-v0.7.2/grain-linux-x64",
    "sha256": "82658891d33f5431e7bd260f0c00b8e86c43eb9182c5327f41db25d60b54dadd",
    "size": 79185888,
}
COMPILER = {
    "sha256": "0a85cf0120aafd873e41eb901a18090453010f8f819074b77c14e1c359cafd1e",
    "size": 20872240,
    "payloadOffset": 70326696,
    "offset": 3640,
    "compressedSize": 2116299,
    "preludeOffset": 79099665,
    "payloadSize": 8772969,
    "vfsKey": "0/1/2/3/4/8",
}
SOURCE = {
    "url": f"https://codeload.github.com/grain-lang/grain/tar.gz/{COMMIT}",
    "sha256": "85ae3fcae60560f677632ac319d29ce92b99cea3315c733cd18137f42e2ffb1c",
    "size": 1484148,
}
STDLIB = {
    "url": "https://registry.npmjs.org/@grain/stdlib/-/stdlib-0.7.2.tgz",
    "sha256": "f91cdf68db4b607b354b4158d89d3681fa2e619d6118a9b435a84b38106c6c46",
    "size": 406987,
}


def check(data: bytes, pin: dict) -> None:
    if len(data) != pin["size"] or hashlib.sha256(data).hexdigest() != pin["sha256"]:
        raise ValueError(f"artifact does not match pinned SHA-256 {pin['sha256']}")


def download(name: str, pin: dict) -> bytes:
    cache = ROOT / ".cache" / name
    if cache.is_file():
        data = cache.read_bytes()
        check(data, pin)
        return data
    request = urllib.request.Request(pin["url"], headers={"User-Agent": "wisp-buildtools"})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read(pin["size"] + 1)
    check(data, pin)
    cache.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache.with_suffix(cache.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(cache)
    return data


def decompress(data: bytes) -> bytes:
    try:
        import brotli
    except ImportError:
        library = ctypes.util.find_library("brotlidec")
        if library is None:
            raise RuntimeError("bootstrap requires Python brotli or the system libbrotlidec library")
        decoder = ctypes.CDLL(library).BrotliDecoderDecompress
        decoder.argtypes = [ctypes.c_size_t, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p]
        decoder.restype = ctypes.c_int
        output = ctypes.create_string_buffer(COMPILER["size"])
        size = ctypes.c_size_t(len(output))
        if decoder(len(data), data, ctypes.byref(size), output) != 1:
            raise ValueError("upstream compiler asset is not valid Brotli")
        result = output.raw[:size.value]
    else:
        result = brotli.decompress(data)
    check(result, COMPILER)
    return result


def compiler_from_release(release: bytes) -> bytes:
    check(release, RELEASE)
    marker = b"(function(process, require, console, EXECPATH_FD, PAYLOAD_POSITION, PAYLOAD_SIZE)"
    prelude = release.index(marker)
    if prelude != COMPILER["preludeOffset"]:
        raise ValueError("upstream pkg prelude offset changed")
    marker = b"//# sourceMappingURL=common.js.map\n},\n"
    start = release.index(marker, prelude) + len(marker)
    vfs, _ = json.JSONDecoder().raw_decode(release[start:].decode())
    payload_size = max(offset + size for entry in vfs.values() for offset, size in entry.values())
    if payload_size != COMPILER["payloadSize"]:
        raise ValueError("upstream pkg payload size changed")
    offset, size = vfs[COMPILER["vfsKey"]]["1"]
    if [offset, size] != [COMPILER["offset"], COMPILER["compressedSize"]]:
        raise ValueError("upstream compiler asset moved")
    absolute = prelude - payload_size + offset
    if absolute != COMPILER["payloadOffset"] + COMPILER["offset"]:
        raise ValueError("upstream pkg payload offset changed")
    return decompress(release[absolute:absolute + size])


def extract(archive: bytes, prefix: str, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar:
            if member.name == prefix.rstrip("/"):
                continue
            if not member.name.startswith(prefix):
                raise ValueError(f"unexpected archive member {member.name}")
            relative = Path(member.name[len(prefix):])
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise ValueError(f"invalid archive member {member.name}")
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                assert source is not None
                target.write_bytes(source.read())
                target.chmod(0o644)
            elif member.issym():
                if Path(member.linkname).is_absolute():
                    raise ValueError(f"absolute archive symlink {member.name}")
                resolved = (target.parent / member.linkname).resolve()
                if not resolved.is_relative_to(destination.resolve()):
                    raise ValueError(f"archive symlink escapes destination {member.name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_symlink():
                    target.unlink()
                target.symlink_to(member.linkname)
            else:
                raise ValueError(f"unsupported archive member {member.name}")


def main(distribution: Path | None = None) -> None:
    distribution = distribution or ROOT / "dist"
    distribution.mkdir(parents=True, exist_ok=True)
    compiler = compiler_from_release(download(f"grain-linux-x64-{VERSION}", RELEASE))
    (distribution / "grainc.js").write_bytes(compiler)
    source = ROOT / ".sources" / f"grain-{COMMIT}"
    extract(download(f"grain-{COMMIT}.tar.gz", SOURCE), f"grain-{COMMIT}/", source)
    extract(download(f"stdlib-{VERSION}.tgz", STDLIB), "package/", distribution / "stdlib")
    licenses = distribution / "licenses"
    licenses.mkdir(parents=True, exist_ok=True)
    (licenses / "Grain-LGPL-3.0.txt").write_bytes((source / "LICENSE").read_bytes())
    (licenses / "Grain-stdlib.txt").write_bytes((distribution / "stdlib" / "LICENSE").read_bytes())
    receipt = {
        "schemaVersion": 1,
        "upstreamVersion": VERSION,
        "upstreamCommit": COMMIT,
        "compilerExecution": "js_of_ocaml frontend and wasm2js Binaryen backend inside QuickJS Wasm",
        "nativeReleaseExecuted": False,
        "release": RELEASE,
        "compiler": COMPILER,
        "source": SOURCE,
        "stdlib": STDLIB,
    }
    (distribution / "upstream.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(f"Grain {VERSION}: extracted verified {len(compiler):,}-byte compiler and standard library")


if __name__ == "__main__":
    main()
