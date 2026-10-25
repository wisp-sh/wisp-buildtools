"""Run the packaged OCaml bytecode compiler inside WASI."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def runtime(*, backend: bool = False) -> Path:
    if selected := os.environ.get('WISP_WORKER') or shutil.which('wisp-worker'):
        resolved = shutil.which(selected)
        if not resolved:
            raise ValueError('Configured WISP_WORKER is unavailable.')
        return Path(resolved)
    name = os.environ.get('WISP_WASMTIME', 'wasmtime') if backend else (
        os.environ.get('WISP_OCAML_WASMTIME') or os.environ.get('WISP_BOOTSTRAP_WASMTIME')
        or os.environ.get('WISP_WASMTIME', 'wasmtime'))
    selected = shutil.which(name)
    if not selected:
        raise ValueError('Set WISP_WASMTIME to the Wasmtime 50 compiler launcher.')
    return Path(selected)


def manifest() -> dict:
    dist = ROOT / 'dist'
    record = json.loads((dist / 'manifest.json').read_text())
    if record.get('schema') != 1 or record.get('language') != 'ocaml':
        raise ValueError('Invalid OCaml toolchain manifest.')
    for name, expected in record['sha256'].items():
        path = dist / name
        if path.is_symlink() or not path.resolve().is_relative_to(dist.resolve()) or digest(path) != expected:
            raise ValueError(f'OCaml toolchain checksum mismatch: {name}')
    return record


def command(work: Path, *arguments: str | Path) -> list[str]:
    arguments = list(map(str, arguments))
    if '-depend' in arguments:
        arguments.remove('-depend')
        prefix = ['-depend', '-I', '/ocaml/lib']
    else:
        prefix = ['-nostdlib', '-I', '/ocaml/lib']
    return [str(runtime()), 'run', '-C', 'cache=y', '-O', 'opt-level=0',
            '-W', 'exceptions=y', '-W', 'max-wasm-stack=33554432',
            '--dir', f'{ROOT / "dist"}::/ocaml', '--dir', f'{work.resolve()}::/work',
            '--dir', f'{work.resolve()}::/',
            '--env', 'OCAMLLIB=/ocaml/lib', str(ROOT / 'dist/bin/ocamlrun.wasm'),
            '/ocaml/bin/ocamlc.byte', *prefix, *arguments]


def ocamlc(work: Path, *arguments: str | Path, capture: bool = False):
    return subprocess.run(command(work, *arguments), check=True, text=True,
                          stdout=subprocess.PIPE if capture else None)
