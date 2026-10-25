"""Pinned C# guest integrity and bounded, single-worker Wasmtime invocations."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
import wasm_tools

SOURCES = ('bootstrap.py', 'src/Compiler.cs', 'src/Generate.cs', 'src/runtime.c', 'toolchain.lock.json')
CORE = b'\0asm\x01\0\0\0'
COMPONENT = b'\0asm\x0d\0\x01\0'


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verified_dist():
    dist = ROOT / 'dist'
    if dist.is_symlink():
        raise ValueError('C# distribution cannot be a symlink')
    manifest = json.loads((dist / 'manifest.json').read_text())
    if manifest.get('toolchain') != 'csharp' or manifest.get('upstream') != json.loads((ROOT / 'toolchain.lock.json').read_text()):
        raise ValueError('C# distribution does not match its lock; rerun bootstrap')
    inventory = manifest.get('sha256')
    if not isinstance(inventory, dict) or not inventory:
        raise ValueError('C# distribution inventory is missing')
    entries = list(dist.rglob('*'))
    if any(p.is_symlink() for p in entries):
        raise ValueError('C# distribution contains a symlink')
    actual = {p.relative_to(dist).as_posix() for p in entries if p.is_file() and p != dist / 'manifest.json'}
    if set(inventory) != actual:
        raise ValueError('C# distribution file inventory changed')
    for relative, expected in inventory.items():
        path = PurePosixPath(relative)
        if path.is_absolute() or '..' in path.parts or str(path) != relative or '\\' in relative:
            raise ValueError('invalid C# distribution manifest path')
        if digest(dist / relative) != expected:
            raise ValueError(f'C# distribution checksum mismatch: {relative}')
    if manifest.get('bootstrapInputs') != {p: digest(ROOT / p) for p in SOURCES}:
        raise ValueError('C# bootstrap inputs changed; rerun bootstrap')
    return dist, manifest


def command(guest, *arguments, mounts=()):
    result = [str(wasm_tools.runtime()), 'run', '-Ccache=n', '-Cparallel-compilation=n', '-Oopt-level=0']
    if os.environ.get('WISP_CSHARP_CACHE'):
        cache = Path(os.environ['WISP_CSHARP_CACHE']).resolve()
        cache.mkdir(parents=True, exist_ok=True)
        config = cache / 'config.toml'
        config.write_text('[cache]\ndirectory = ' + json.dumps(str(cache / 'modules')) + '\n')
        result += ['-Ccache-config=' + str(config), '-Ccache=y']
    for host, target in mounts:
        result += ['--dir', f'{Path(host).resolve()}::{target}']
    return [*result, str(guest), *(str(a) for a in arguments)]


def run(command, *, timeout=180, cwd=None, env=None):
    result = subprocess.run([str(x) for x in command], capture_output=True, text=True,
                            timeout=timeout, cwd=cwd, env=env)
    if result.returncode:
        raise ValueError((result.stdout + result.stderr).strip() or f'guest exited {result.returncode}')
    return result.stdout


def wasm_tool(*arguments, timeout=180):
    mounts = set()
    for value in arguments:
        path = Path(str(value))
        if path.is_absolute():
            mounts.add(path if path.is_dir() else path.parent)
    return run(command(wasm_tools.module(), *arguments, mounts=[(p, p) for p in sorted(mounts)]), timeout=timeout)
