#!/usr/bin/env python3
"""Execute package imports in guest Python and check complete source receipts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from compile_contract import limited
from scripts.common import ROOT, digest, guest_tools, run


def main():
    runtime = str(guest_tools.runtime())
    environment = dict(os.environ, PATH='/nonexistent', WISP_WASMTIME=runtime)
    with tempfile.TemporaryDirectory(prefix='.package-verify-', dir=ROOT) as directory:
        work = Path(directory)
        project = work/'project'
        shutil.copytree(ROOT/'examples/packages', project)
        entry = project/'src/calculator/api.py'
        output = work/'library.wasm'

        def build(expected=True):
            command = [sys.executable, ROOT/'compile.py', entry, '--package-root', project/'src',
                       '--python-path', project/'deps', '--wit', project/'library.wit',
                       '--world', 'library', '-o', output]
            result = subprocess.run([str(item) for item in command], env=environment, text=True,
                                    capture_output=True, timeout=180)
            if (result.returncode == 0) != expected:
                raise ValueError(f'unexpected build result: {result.stdout}\n{result.stderr}')
            return json.loads(result.stdout) if expected else result.stderr

        def call(expression, expected):
            actual = run(limited([runtime, 'run', '--invoke', expression, output]), capture=True).stdout.strip()
            if actual != expected:
                raise ValueError(f'{expression}: {actual!r}, expected {expected!r}')
            print('PASS', expression, flush=True)

        receipt = build()
        assert receipt['sourceBundle']['entryModule'] == 'calculator.api'
        for name in ['project/calculator/formatting.py', 'project/calculator/state.py',
                     'python-path-1/vendor_math/arithmetic.py']:
            assert name in receipt['sourceBundle']['inputs'], name
        for expression, expected in [('greet("世界")', '"Hello, 世界!"'), ('total([20,22])', '42'),
                                     ('birthday({name: "Ada", age: 41})', '{name: "Ada", age: 42}'),
                                     ('checked(-1)', 'err("value must be nonnegative")')]:
            call(expression, expected)
        first_hash = digest(output)
        second_receipt = build()
        assert digest(output) == first_hash
        assert receipt == second_receipt
        print('PASS deterministic builds with every bundled source hashed', flush=True)
        helper = project/'deps/vendor_math/arithmetic.py'
        helper.write_text('def sum_values(values): return sum(values) + 1\n')
        changed = build()
        call('total([20,22])', '43')
        assert changed['sourceBundle']['inputs'] != receipt['sourceBundle']['inputs']
        assert digest(output) != first_hash
        print('PASS changing a dependency changes both behavior and receipt', flush=True)
        intact = digest(output)
        for text, diagnostic in [('def broken(: pass\n', 'SyntaxError'),
                                 ('raise RuntimeError("dependency initialization failed")\n', 'RuntimeError')]:
            helper.write_text(text)
            error = build(expected=False)
            assert diagnostic in error, error
            assert digest(output) == intact
            print('PASS atomic dependency failure:', diagnostic, flush=True)
        print(json.dumps({'passed': True, 'nativeToolsRemovedFromPath': True,
                          'componentBytes': output.stat().st_size,
                          'sourceInputs': len(receipt['sourceBundle']['inputs'])}))


if __name__ == '__main__':
    main()
