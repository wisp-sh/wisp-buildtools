#!/usr/bin/env python3
"""Exercise Roslyn/Mono source builds with native compilers absent from PATH."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from toolchain import ROOT, command, digest, run, verified_dist, wasm_tool, wasm_tools


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, default=ROOT / 'verification.json')
    args = parser.parse_args()
    _, manifest = verified_dist()
    host = wasm_tools.runtime()
    environment = {**os.environ, 'PATH': '/nonexistent', 'WISP_WASMTIME': str(host)}
    checks = []
    started = time.monotonic()
    scratch = ROOT / '.toolchains/verification'
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='scalar-', dir=scratch) as directory:
        work = Path(directory)
        def compile_source(source, name, wit=ROOT / 'examples/add.wit', success=True):
            output = work / (name + '.wasm')
            result = subprocess.run([sys.executable, ROOT / 'compile.py', source, '--wit', wit,
                                     '-o', output], env=environment, capture_output=True, text=True, timeout=300)
            if success and result.returncode:
                raise ValueError(result.stdout + result.stderr)
            if not success:
                if not result.returncode or any(p.exists() for p in [output, Path(str(output)+'.wit'), Path(str(output)+'.build.json')]):
                    raise ValueError(f'{name}: invalid build succeeded or published output')
                checks.append({'name': name, 'rejected': True, 'diagnostic': result.stderr.strip()})
                return None
            receipt = json.loads(Path(str(output) + '.build.json').read_text())
            if not receipt['fullyWasmHostedBuild'] or not receipt['sourceCompilationWasmHosted']:
                raise ValueError('missing Wasm source-build provenance')
            checks.append({'name': name, 'bytes': output.stat().st_size, 'sha256': digest(output)})
            return output
        def invoke(output, expression, expected=None, failure=False):
            result = subprocess.run([*command(output)[:-1], '--invoke', expression, output], env=environment,
                                    capture_output=True, text=True, timeout=120)
            if failure:
                if result.returncode == 0 or 'managed exception' not in result.stderr:
                    raise ValueError('managed exception did not produce the expected trap:\n' + result.stdout + result.stderr)
            elif result.returncode or result.stdout.strip() != expected:
                raise ValueError(f'{expression}: expected {expected}:\n' + result.stdout + result.stderr)
            checks.append({'name': expression, 'expected': 'managed exception trap' if failure else expected})
        output = compile_source(ROOT / 'examples/add.cs', 'ordinary-add')
        invoke(output, 'add(20,22)', '42')
        invoke(output, 'add(-20,22)', '2')
        invoke(output, 'add(2147483647,1)', '-2147483648')
        public = json.loads(wasm_tool('component', 'wit', output, '--json'))
        if set(public['worlds'][0]['exports']) != {'add'}:
            raise ValueError('validation-only export leaked into public Component')
        checks.append({'name': 'only authored add exported'})
        richer = work / 'collections.cs'
        richer.write_text('using System.Linq; using System.Collections.Generic;\n'
            'public static class Calculator { public static int Add(int a, int b) {\n'
            'var values = new List<int> { a, b }; return values.Select(x => x * 2).Sum() / 2; } }\n')
        invoke(compile_source(richer, 'collections-and-linq'), 'add(20,22)', '42')
        throwing = work / 'throwing.cs'
        throwing.write_text('public static class Calculator { public static int Add(int a, int b) { '
                            'throw new System.InvalidOperationException("example failure"); } }')
        invoke(compile_source(throwing, 'throwing-add'), 'add(20,22)', failure=True)
        invalid = work / 'invalid.cs'
        invalid.write_text('public static class Calculator { public static int Add(int a, int b) { return a + ; } }')
        compile_source(invalid, 'syntax-error', success=False)
        invalid.write_text('public static class Calculator { public static long Add(int a, int b) { return a + b; } }')
        compile_source(invalid, 'wrong-signature', success=False)
        invalid.write_text('public static class Calculator { public static int Add<T>(int a, int b) { return a + b; } }')
        compile_source(invalid, 'generic-entry', success=False)
        wrong_wit = work / 'wrong.wit'
        wrong_wit.write_text('package example:wrong; world wrong { export add: func(a: s64, b: s64) -> s64; }')
        compile_source(ROOT / 'examples/add.cs', 'wrong-wit', wit=wrong_wit, success=False)
        existing = work / 'existing.wasm'
        existing.write_bytes(b'preserve me')
        result = subprocess.run([sys.executable, ROOT / 'compile.py', ROOT / 'examples/add.cs', '--wit',
            ROOT / 'examples/add.wit', '-o', existing], env=environment, capture_output=True, text=True, timeout=120)
        if not result.returncode or existing.read_bytes() != b'preserve me':
            raise ValueError('existing output was replaced')
        checks.append({'name': 'existing output preserved'})
    verified_dist()
    report = {'schemaVersion': 1, 'toolchain': 'csharp', 'version': manifest['version'],
              'passed': len(checks), 'nativeCompilerOnPath': False, 'fullyWasmHostedBuild': True,
              'seconds': round(time.monotonic() - started, 3), 'checks': checks}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(f"C#: {len(checks)} checks passed; native compilers absent from PATH")


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(f'csharp verify: {error}') from error
