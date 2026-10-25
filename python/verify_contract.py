#!/usr/bin/env python3
"""Execute the authored-WIT Python example and failure-atomicity regressions.

Requires a provisioned template for examples/library/library.wit.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from compile_contract import limited
from scripts.common import ROOT, digest, guest_tools, run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--example', choices=['library', 'root'], default='library')
    parser.add_argument('--verify-preloads', action='store_true',
                        help='check dynamic stdlib imports in a separately preloaded template')
    args = parser.parse_args()
    example = ROOT / 'examples' / args.example
    runtime = str(guest_tools.runtime())
    environment = dict(os.environ, PATH='/nonexistent', WISP_WASMTIME=runtime)
    with tempfile.TemporaryDirectory(prefix='.contract-verify-', dir=ROOT) as directory:
        work = Path(directory)
        output = work / 'library.wasm'
        def build(source, target=output, succeeds=True):
            command = [sys.executable, ROOT / 'compile.py', source, '--wit',
                       example / 'library.wit', '--world', 'library', '-o', target]
            result = subprocess.run([str(item) for item in command], env=environment,
                                    text=True, capture_output=True, timeout=180)
            if (result.returncode == 0) != succeeds:
                raise ValueError(f'unexpected build result: {result.stdout}\n{result.stderr}')
            return result
        source = example / 'main.py'
        if args.verify_preloads:
            source = work / 'preloaded.py'
            source.write_text('''import importlib
importlib.import_module('html.parser').HTMLParser().feed('<p>hello</p>')
assert importlib.import_module('xml.etree.ElementTree').fromstring('<hello/>').tag == 'hello'
assert importlib.import_module('logging').getLogger('example').name == 'example'
assert importlib.import_module('typing').get_origin(list[int]) is list
''' + (example / 'main.py').read_text())
        build(source)
        cases = {
            'greet("世界")': '"Hello, 世界!"',
            'total([20,22])': '42',
            'total([])': '0',
            'birthday({name: "Ada", age: 41})': '{name: "Ada", age: 42}',
            'checked(21)': 'ok(42)',
            'checked(-1)': 'err("value must be nonnegative")',
        }
        if args.example == 'root':
            cases = {'is-ready()': 'true', 'echo-bytes([0,1,255])': '[255, 1, 0]',
                     'maybe(none)': 'none', 'maybe(some("世界"))': 'some("世界")'}
        for expression, expected in cases.items():
            actual = run(limited([runtime, 'run', '--invoke', expression, output]), capture=True).stdout.strip()
            if actual != expected:
                raise ValueError(f'{expression}: expected {expected!r}, got {actual!r}')
            print('PASS', expression, flush=True)
        original = digest(output)
        if args.example == 'root':
            print(json.dumps({'passed': True, 'componentSha256': original, 'rootExports': True}))
            return
        build(source)
        if digest(output) != original:
            raise ValueError('repeated compilation is not deterministic')
        print('PASS deterministic application builds', flush=True)
        for label, text in {
            'syntax': 'def invalid(: pass\n',
            'missing export': 'def greet(name): return name\n',
            'wrong signature': (example / 'main.py').read_text().replace('def total(values: list[int])', 'def total()'),
            'initialization error': 'raise RuntimeError("initialization failed")\n',
        }.items():
            source = work / 'invalid.py'
            source.write_text(text)
            build(source, succeeds=False)
            if digest(output) != original:
                raise ValueError('failed build damaged existing output')
            absent = work / 'absent.wasm'
            build(source, target=absent, succeeds=False)
            if absent.exists():
                raise ValueError('failed build left a partial output')
            print('PASS atomic rejection:', label, flush=True)
        # The validation function is intentionally absent from the public ABI.
        result = subprocess.run([runtime, 'run', '--invoke', 'wisp-python-validate()', str(output)],
                                text=True, capture_output=True, timeout=60)
        if result.returncode == 0:
            raise ValueError('build-only export leaked into the public ABI')
        print('PASS build-only validation export removed', flush=True)
        print(json.dumps({'passed': True, 'componentSha256': original,
                          'nativeToolsRemovedFromPath': True, 'componentBytes': output.stat().st_size}))


if __name__ == '__main__':
    main()
