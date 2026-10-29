#!/usr/bin/env python3
"""Measure cold module loading separately from requests on one tiny TS project."""
import argparse
import json
from pathlib import Path
import statistics
import time
from bootstrap import HERE, check
from wasmtime_host import compileEngine, runRequest
from verify import project


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 5:
        parser.error('use 1 to 5 repetitions')
    manifest = check()
    module = HERE / 'dist/tsrs.wasm'
    # Prepare once outside the samples. Both modes use identical engine settings.
    with compileEngine(module):
        pass
    results = {}
    for native in (False, True):
        samples = []
        for _ in range(args.repetitions):
            start = time.perf_counter()
            with compileEngine(module, precompiled=native) as compiled:
                loaded = time.perf_counter()
                version = runRequest(compiled, {'args': ['--version']})
                started = time.perf_counter()
                result = runRequest(compiled, project('export const answer: number = 42;\n'))
                finished = time.perf_counter()
                assert version['exitCode'] == result['exitCode'] == 0, result
                samples.append({'moduleLoadMs': (loaded-start)*1000,
                                'startupRequestMs': (started-loaded)*1000,
                                'typecheckRequestMs': (finished-started)*1000,
                                'loadAndTypecheckMs': (loaded-start+finished-started)*1000})
        results['precompiled' if native else 'wasm'] = {
            'samples': samples, 'median': {key: statistics.median(s[key] for s in samples) for key in samples[0]}}
    report = {'moduleSha256': manifest['files']['tsrs.wasm']['sha256'],
              'moduleBytes': module.stat().st_size, 'host': 'Wasmtime Python 50.0.1',
              'project': 'one strict TS file, noEmit, exported number constant',
              'optLevel': 'none', 'parallelCompilation': False, 'results': results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
