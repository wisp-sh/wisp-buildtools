"""Integrity checks and host selection for Goose's Wasm tools."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def runtime(*, frontend: bool = False) -> Path:
    selected = (os.environ.get('WISP_GOOSE_FRONTEND_HOST') if frontend else None) or os.environ.get('WISP_WASMTIME', 'wasmtime')
    path = shutil.which(selected)
    if not path:
        raise ValueError("Set WISP_WASMTIME to Wisp's worker or Wasmtime 49 with exception support.")
    return Path(path).resolve()


def manifest() -> dict:
    dist = ROOT / 'dist'
    record = json.loads((dist / 'manifest.json').read_text())
    lock = json.loads((ROOT / 'toolchain.lock.json').read_text())
    if (record.get('schema') != 1 or record.get('language') != 'goose'
            or record.get('bootstrap') != lock or record.get('profile') != 'scalar-v1'):
        raise ValueError('Invalid Goose toolchain manifest; rebuild the toolchain.')
    required = {'bin/goose.wasm', 'bin/c-backend.wasm', 'runtime/runtime.h',
                'runtime/runtime_ext.h', 'runtime/support.h', 'sysroot/lib/crt1-reactor.o'}
    if not required.issubset(record['sha256']):
        raise ValueError('Incomplete Goose toolchain manifest.')
    for name, expected in record['sha256'].items():
        path = dist / name
        if path.is_symlink() or not path.resolve().is_relative_to(dist.resolve()) or digest(path) != expected:
            raise ValueError(f'Goose toolchain checksum mismatch: {name}')
    return record
