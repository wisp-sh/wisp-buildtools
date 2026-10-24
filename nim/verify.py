#!/usr/bin/env python3
"""Verify real Nim compilation, diagnostics, imports, macros and reactor startup."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
from compile import ROOT, compile_nim, runtime

def main() -> None:
    work = ROOT / 'work/verify'
    work.mkdir(parents=True, exist_ok=True)
    wasmtime = runtime()
    result = subprocess.run([str(wasmtime), 'run', str(ROOT / 'dist/bin/nim.wasm'), '--version'], capture_output=True, text=True, check=True)
    pin = json.loads((ROOT / 'toolchain.lock.json').read_text())['nim']
    assert f'Nim Compiler Version {pin["version"]}' in result.stdout, result.stdout
    assert f'git hash: {pin["git_commit"]}' in result.stdout, result.stdout
    add = work / 'add.wasm'
    compile_nim(ROOT / 'examples/add.nim', add, exports=['add'])
    result = subprocess.run([str(wasmtime), 'run', '--invoke', 'add', str(add), '20', '22'], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == '42', result.stdout
    invalid = work / 'invalid.nim'
    invalid.write_text('proc broken*(a: int32): int32 =\n  a + missingIdentifier\n')
    invalid_output = work / 'invalid.wasm'
    invalid_output.write_bytes(b'previous-output')
    result = subprocess.run([sys.executable, str(ROOT / 'compile.py'), str(invalid), '-o', str(invalid_output)], capture_output=True, text=True)
    assert result.returncode != 0, result.stdout
    assert 'undeclared identifier' in result.stdout + result.stderr, result.stdout + result.stderr
    assert invalid_output.read_bytes() == b'previous-output', 'failed compile overwrote output'
    (work / 'invalid.log').write_text(result.stdout + result.stderr)
    failed_link = work / 'failed-link.wasm'
    failed_link.write_bytes(b'previous-output')
    result = subprocess.run([sys.executable, str(ROOT / 'compile.py'), str(ROOT / 'examples/add.nim'), '-o', str(failed_link), '--export', 'missing_export'], capture_output=True, text=True)
    assert result.returncode != 0 and 'missing_export' in result.stderr, result.stdout + result.stderr
    assert failed_link.read_bytes() == b'previous-output', 'failed link overwrote output'
    (work / 'failed-link.log').write_text(result.stdout + result.stderr)
    # An empty PATH makes any accidental native compiler/linker invocation fail.
    no_tools = work / 'empty-path'
    no_tools.mkdir(exist_ok=True)
    spaced_output = work / 'path with spaces/add.wasm'
    env = {**os.environ, 'PATH': str(no_tools), 'WISP_WASMTIME': str(wasmtime.resolve())}
    subprocess.run([sys.executable, str(ROOT / 'compile.py'), str(ROOT / 'examples/add.nim'), '-o', str(spaced_output), '--export', 'add'], env=env, check=True)
    result = subprocess.run([str(wasmtime), 'run', '--invoke', 'add', str(spaced_output), '20', '22'], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == '42', result.stdout
    features = work / 'features.wasm'
    compile_nim(ROOT / 'examples/features.nim', features, exports=['initialized', 'doubled'])
    # Wasmtime initializes WASI reactors before invoking the requested export.
    # Both calls therefore exercise the generated _initialize -> NimMain path.
    for exported, expected in [('initialized', '42'), ('doubled', '84')]:
        result = subprocess.run([str(wasmtime), 'run', '--invoke', exported, str(features)],
                                check=True, capture_output=True, text=True)
        assert result.stdout.strip() == expected, result.stdout + result.stderr
    report = {'compiler_version': pin['version'], 'frontend_host': 'wasm32-wasip1', 'backend_host': 'wasm32-wasip3', 'add_20_22': 42, 'invalid_source_rejected': True, 'failed_compile_preserves_output': True, 'failed_link_preserves_output': True, 'native_toolchain_removed_from_path': True, 'output_path_with_spaces': True, 'nim_macros_generics_sequences_and_imports': True, 'initialized_global_sum': 42, 'mapped_global_sum': 84, 'artifact_bytes': {'add': add.stat().st_size, 'features': features.stat().st_size}}
    report['fullyWasmHostedBuild'] = True
    (work / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))

if __name__ == '__main__':
    main()
