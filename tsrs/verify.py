#!/usr/bin/env python3
"""Verify real compiler diagnostics, repeated request isolation and native loading."""
import json
from pathlib import Path
import sys
from bootstrap import check, HERE
from wasmtime_host import compileEngine, runRequest


def project(text):
    return {'cwd': '/p', 'args': ['--noEmit', '--strict', '--pretty', 'false', '/p/a.ts'],
            'files': {'/p/a.ts': text}, 'diagnostics': 'json', 'env': {}}


def main():
    check()
    with compileEngine(HERE / 'dist/tsrs.wasm') as compiled:
        good = project('export const answer: number = 42;\n')
        bad = project("export const answer: number = 'text';\n")
        for label, request, expected in [('pass', good, 0), ('fail', bad, 1), ('pass again', good, 0)]:
            result = runRequest(compiled, request)
            assert (result['exitCode'] != 0) == bool(expected), result
            if expected:
                assert any(d['code'] == 2322 for d in result['diagnostics']), result
            else:
                assert result['diagnostics'] == [], result
            assert result['files'] == request['files'], result
            print('PASS actual compiler: ' + label)
    print('PASS tsrs Wasmtime Python 50.0.1')


if __name__ == '__main__':
    main()
