#!/usr/bin/env python3
"""Construct commands for Java bytecode execution in the shared WASI JVM."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
import importlib.util

ROOT = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("_wisp_shared_jvm_integrity", ROOT / "runtime.py")
_integrity = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_integrity)
integrity = _integrity.integrity
runtime_host = _integrity.host


def command(main_class: str, classpath: Sequence[Path], *,
            mounts: Mapping[Path, str] | None = None,
            arguments: Sequence[str] = (),
            properties: Mapping[str, str] | None = None) -> list[str]:
    """Mount compiler data and run main; argument paths use the supplied guest mounts."""
    integrity()
    dist = ROOT / 'dist'
    entries = [Path(entry).resolve(strict=True) for entry in classpath]
    if not main_class or not entries:
        raise ValueError('JVM needs a main class and at least one classpath entry')
    mapped: list[tuple[Path, str]] = [(dist, '/runtime')]
    for directory, guest in (mounts or {}).items():
        directory = Path(directory).resolve(strict=True)
        guest_path = PurePosixPath(guest)
        if (not directory.is_dir() or not guest_path.is_absolute()
                or '..' in guest_path.parts or guest_path == PurePosixPath('/')
                or guest_path == PurePosixPath('/runtime')):
            raise ValueError(f'invalid JVM guest mount: {directory}::{guest}')
        mapped.append((directory, str(guest_path)))
    guest_entries = []
    for entry in entries:
        owner = next(((host, guest) for host, guest in sorted(mapped, key=lambda item: -len(item[0].parts))
                      if entry.is_relative_to(host)), None)
        if owner is None:
            parent = entry if entry.is_dir() else entry.parent
            owner = (parent, f'/classpath/{len(mapped)}')
            mapped.append(owner)
        host, guest = owner
        relative = entry.relative_to(host).as_posix()
        guest_entries.append(guest if relative == '.' else guest + '/' + relative)
    if len({guest for _, guest in mapped}) != len(mapped):
        raise ValueError('JVM guest mount paths must be unique')
    invocation = [str(runtime_host()), 'run', '-C', 'cache=y', '-O', 'opt-level=0', '-W', 'all-proposals=y', '-W', 'exceptions=y',
                  '--env', 'JAVA_HOME=/runtime/jdk']
    for directory, guest in mapped:
        invocation += ['--dir', f'{directory}::{guest}']
    invocation.append(str(dist / 'jvm.wasm'))
    for name, value in sorted((properties or {}).items()):
        if not name or '=' in name or '\0' in name or '\0' in value:
            raise ValueError('invalid JVM system property')
        if name == 'java.home':
            raise ValueError('java.home belongs to the shared JVM provider')
        invocation.append(f'-D{name}={value}')
    return [*invocation, '/runtime/boot.jar', ':'.join(guest_entries),
            main_class.replace('.', '/'), *map(str, arguments)]
