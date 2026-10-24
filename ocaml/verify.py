#!/usr/bin/env python3
"""Compile and run an OCaml regression program while blocking native compilers."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from runner import ROOT, digest, manifest, runtime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    record = manifest()
    destination = args.out.absolute() if args.out else ROOT / 'verification/latest.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    report = {'schema': 1, 'passed': False, 'compilerVersion': record['version']}
    destination.write_text(json.dumps(report, indent=2) + '\n')
    with tempfile.TemporaryDirectory(prefix='ocaml-verify-') as temporary:
        work = Path(temporary)
        guard = work / 'guard'
        guard.mkdir()
        attempts = work / 'native-tool-attempts.txt'
        attempts.touch()
        script = guard / 'deny'
        script.write_text(f'#!{sys.executable}\nimport os,sys\n'
                          'with open(os.environ["WISP_NATIVE_TOOL_ATTEMPTS"], "a") as log: log.write(sys.argv[0]+"\\n")\n'
                          'raise SystemExit("native compiler blocked")\n')
        script.chmod(0o755)
        for name in ('ocaml', 'ocamlc', 'ocamlopt', 'ocamlrun', 'opam', 'dune',
                     'cc', 'gcc', 'clang', 'wasm-ld', 'node', 'npm', 'npx'):
            (guard / name).symlink_to(script)
        environment = dict(os.environ, PATH=str(guard) + os.pathsep + os.environ.get('PATH', ''),
                           WISP_NATIVE_TOOL_ATTEMPTS=str(attempts))
        shutil.copy2(ROOT / 'examples/check.ml', work / 'check.ml')
        output = work / 'check.wasm'
        subprocess.run([sys.executable, ROOT / 'compile.py', work / 'check.ml', '-o', output],
                       env=environment, check=True)
        (work / 'input.bin').write_bytes(bytes([0, 255, 128, 1]))
        result = subprocess.run([runtime(), 'run', '-W', 'exceptions=y', '--dir', f'{work}::/', output],
                                env=environment, check=True, capture_output=True, text=True)
        if result.stdout.strip() != '42' or (work / 'output.bin').read_bytes() != bytes([0,255,128,1]):
            raise ValueError('OCaml execution or binary file round trip failed.')
        if attempts.read_text():
            raise ValueError('Source compilation invoked a native compiler.')
        report.update(passed=True, result=42, outputSha256=digest(output),
                      fullyWasmHostedBuild=True, nativeToolAttempts=[],
                      checks=['32 bit values', 'GC and compaction', 'Int64', 'floats', 'exceptions',
                              'Hashtbl', 'binary file I/O', 'standalone WASI command'])
    destination.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
