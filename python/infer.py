#!/usr/bin/env python3
"""Infer a contract using the installed guest CPython, never host ast/imports."""
import argparse
import hashlib
import json
import tempfile
from pathlib import Path

from scripts.common import ROOT, guest_tools, invoke, run, verified_package


def infer_contract(source, *, declarations_only=False):
    if not verified_package(ROOT / 'dist'):
        raise ValueError('Python inference needs the current bootstrapped Python toolchain')
    with source.open('rb') as stream:
        data = stream.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError('Python inference source exceeds 1 MiB')
    data.decode('utf-8')
    with tempfile.TemporaryDirectory(prefix='.wisp-python-infer-', dir=source.resolve().parent) as directory:
        candidate = Path(directory) / 'inference.wasm'
        command = guest_tools.guest_command(ROOT / 'dist/python-packager.wasm',
                    ROOT / 'dist/inference.component.wasm', source.resolve(), '', candidate)
        run([*command[:2], '-O', 'opt-level=0', '-C', 'parallel-compilation=n', *command[2:]])
        response = json.loads(json.loads(invoke(candidate, 'infer()')))
    if 'error' in response:
        raise ValueError(response['error'])
    body = response['declarations']
    if declarations_only:
        return body
    identity = hashlib.sha256(body.encode()).hexdigest()[:24]
    return f'package wisp:python-p{identity}@0.1.0;\ninterface api {{\n{body}}}\nworld library {{ export api; }}\n'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--declarations', action='store_true', help='emit interface declarations for Wisp package inference')
    parser.add_argument('source', type=Path, help='entry or configured API module')
    args = parser.parse_args()
    try:
        print(infer_contract(args.source, declarations_only=args.declarations), end='')
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from error
