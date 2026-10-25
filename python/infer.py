#!/usr/bin/env python3
"""Infer contracts and discover imports using installed guest CPython."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile

from scripts.analysis import analyze, inputs


def infer_contract(source, *, declarations_only=False, package_root=None, dependency_root=None,
                   python_paths=(), json_output=False, inputs_only=False):
    source = source.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='.wisp-python-infer-', dir=source.parent) as directory:
        work = Path(directory)
        program, dependencies, inventory, settings = inputs(source, package_root, python_paths, dependency_root, work=work)
        response = {} if inputs_only else analyze(program, work)
    response['inputs'] = inventory
    if inputs_only or json_output:
        return json.dumps(response, sort_keys=True)
    body = response['declarations']
    if declarations_only:
        return body
    identity = hashlib.sha256(body.encode()).hexdigest()[:24]
    return f'package wisp:python-p{identity}@0.1.0;\ninterface api {{\n{body}}}\nworld library {{ export api; }}\n'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--declarations', action='store_true')
    parser.add_argument('--analysis', action='store_true', help='emit guest declarations and complete original input hashes as JSON')
    parser.add_argument('--inputs', action='store_true', help='list staging inputs with file I/O only; never parse application source')
    parser.add_argument('--package-root', type=Path)
    parser.add_argument('--dependency-root', type=Path)
    parser.add_argument('--python-path', type=Path, action='append', default=[])
    parser.add_argument('source', type=Path)
    args = parser.parse_args()
    try:
        print(infer_contract(args.source, declarations_only=args.declarations,
                            package_root=args.package_root, dependency_root=args.dependency_root,
                            python_paths=args.python_path, json_output=args.analysis, inputs_only=args.inputs), end='')
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from error
