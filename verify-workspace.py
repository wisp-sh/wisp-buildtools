#!/usr/bin/env python3
"""Build language examples, assemble Components, and verify their Wisp import."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
ADAPTER = ROOT / 'toolchains.py'
LANGUAGES = {
    'zig': ('zig/examples/add.zig', []),
    'assemblyscript': ('assemblyscript/examples/add.ts', []),
    'nim': ('nim/examples/add.nim', ['--export', 'add']),
    'javascript': ('javascript/examples/add.js', ['--stub-wasi']),
    'typescript': ('javascript/examples/add.ts', ['--stub-wasi']),
    'python': ('python/examples/add/main.py', []),
    'grain': ('grain/examples/add.gr', ['--component', '--wit', str(ROOT / 'examples/scalar-library/world.wit'), '--world', 'scalar']),
    'swift': ('swift/examples/add.swift', ['--component', '--wit', str(ROOT / 'examples/scalar-library/world.wit'), '--world', 'scalar']),
    'java': ('examples/scalar-library/JavaScalar.java', ['--main', 'JavaScalar', '--component', '--wit', str(ROOT / 'examples/scalar-library/world.wit'), '--world', 'scalar']),
    'kotlin': ('examples/scalar-library/KotlinScalar.kt', ['--main', 'KotlinScalarKt', '--component', '--wit', str(ROOT / 'examples/scalar-library/world.wit'), '--world', 'scalar']),
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--languages', nargs='+', choices=LANGUAGES, default=list(LANGUAGES))
    parser.add_argument('--out', type=Path, required=True, help='new directory for reports and generated projects')
    parser.add_argument('--wisp', default=os.environ.get('WISP_RUST_CLI', str(ROOT.parent / 'wisp/target/debug/wisp')))
    parser.add_argument('--wasmtime', default=os.environ.get('WISP_WASMTIME', 'wasmtime'))
    parser.add_argument('--preview1-adapter', default=os.environ.get('WISP_PREVIEW1_ADAPTER', str(ROOT / 'wasm-tools/dist/wasi_snapshot_preview1.reactor.wasm')))
    parser.add_argument('--wasm-only', action='store_true',
                        help='block Node and native compiler/Component tools during every source build')
    args = parser.parse_args()
    args.languages = list(dict.fromkeys(args.languages))
    if args.out.exists():
        parser.error('output directory already exists')
    output = args.out.resolve()
    output.mkdir(parents=True)
    report = {'schema': 1, 'started': datetime.now(timezone.utc).isoformat(), 'requested_languages': args.languages, 'checks': [], 'complete': False, 'passed': False}
    environment = os.environ.copy()
    if args.wasm_only:
        blocked = output / 'blocked-native-tools'
        blocked.mkdir()
        attempts = output / 'native-tool-attempts.log'
        attempts.write_text('')
        guard = blocked / 'deny.py'
        guard.write_text(
            '#!' + sys.executable + '\n'
            'import os, sys\n'
            'from pathlib import Path\n'
            'name = Path(sys.argv[0]).name\n'
            'with open(os.environ["WISP_NATIVE_TOOL_ATTEMPTS"], "a") as output:\n'
            '    output.write(name + "\\n")\n'
            'raise SystemExit("native compiler/tool blocked during Wasm-only verification: " + name)\n')
        guard.chmod(0o755)
        names = ('node', 'nodejs', 'npm', 'npx', 'bun', 'deno', 'cargo', 'rustc', 'go',
                 'cc', 'c++', 'gcc', 'g++', 'clang', 'clang++', 'ld', 'ld.lld', 'wasm-ld',
                 'wasm-tools', 'wac', 'wasm-opt', 'wat2wasm', 'esbuild', 'componentize-qjs', 'componentize-py',
                 'grain', 'grainc', 'qjs', 'qjsc', 'ocaml', 'ocamlc', 'ocamlopt',
                 'swift', 'swiftc', 'swift-frontend', 'java', 'javac', 'kotlin', 'kotlinc', 'kotlinc-jvm')
        for name in names:
            (blocked / name).symlink_to(guard.name)
        environment['PATH'] = str(blocked) + os.pathsep + environment.get('PATH', '')
        environment['WISP_NATIVE_TOOL_ATTEMPTS'] = str(attempts)
        environment['WISP_WASM_TOOLS'] = str(ROOT / 'wasm-tools/dist/wasm-tools.wasm')
        environment['WISP_WASMTIME'] = str(Path(shutil.which(args.wasmtime) or args.wasmtime).resolve())
        environment['WISP_NODE'] = str(blocked / 'node')
        report['execution_policy'] = {'wasm_only': True, 'blocked_tools': list(names),
                                      'native_tool_attempts': str(attempts)}
    def save_report():
        pending = output / '.verification.json.tmp'
        pending.write_text(json.dumps(report, indent=2) + '\n')
        pending.replace(output / 'verification.json')
    save_report()
    failed = False
    for language in args.languages:
        started = time.monotonic()
        directory = output / language
        directory.mkdir()
        result = {'language': language, 'passed': False}
        with (directory / 'commands.log').open('w') as log:
            def run(command, *, expect=None):
                log.write('$ ' + repr([str(item) for item in command]) + '\n')
                log.flush()
                done = subprocess.run([str(item) for item in command], text=True, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, env=environment)
                log.write(done.stdout)
                log.flush()
                if done.returncode:
                    raise RuntimeError(f'command exited {done.returncode}; see {directory / "commands.log"}')
                if expect is not None and done.stdout.strip() != expect:
                    raise RuntimeError(f'expected {expect!r}, received {done.stdout!r}')
                return done.stdout
            try:
                source, extra = LANGUAGES[language]
                module = directory / 'library.wasm'
                run([sys.executable, ADAPTER, 'build', language, ROOT / source, '-o', module, *extra])
                if module.read_bytes()[:8] == b'\0asm\x01\0\0\0':
                    component = directory / 'component.wasm'
                    command = [sys.executable, ADAPTER, 'componentize', module, '--wit', ROOT / 'examples/scalar-library/world.wit', '--world', 'scalar', '-o', component]
                    if language == 'nim':
                        command += ['--preview1-adapter', Path(args.preview1_adapter).resolve()]
                    run(command)
                else:
                    component = module
                run([args.wasmtime, 'run', '--invoke', 'add(20,22)', component], expect='42')
                project = directory / 'project'
                release = directory / 'release'
                run([sys.executable, ADAPTER, 'wisp-project', component, '--name', f'demo:{language}', '-o', project])
                run([args.wisp, 'build', project, '--component-only', '--out', release])
                published = release / 'component.wasm'
                if digest(published) != digest(component):
                    raise RuntimeError('Wisp did not preserve the imported Component bytes')
                run([args.wasmtime, 'run', '--invoke', 'add(20,22)', published], expect='42')
                result.update(passed=True, result=42, bytes=published.stat().st_size,
                              sha256=digest(published), wisp_preserves_component_bytes=True)
                # A mismatched receipt must fail admission before compiler work.
                config_path = project / 'wisp.toml'
                original = config_path.read_text()
                config_path.write_text(original.replace(digest(component), '0' * 64))
                rejected = subprocess.run([args.wisp, 'build', str(project), '--component-only', '--out', str(directory / 'rejected')], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=environment)
                config_path.write_text(original)
                log.write(rejected.stdout)
                if rejected.returncode == 0 or 'sha256 mismatch' not in rejected.stdout:
                    raise RuntimeError('Wisp failed to reject the altered Component hash')
                result['tampered_component_hash_rejected'] = True
            except Exception as error:
                failed = True
                result.update(passed=False, error=str(error))
            result['seconds'] = round(time.monotonic() - started, 3)
            report['checks'].append(result)
            print(json.dumps(result), flush=True)
        report['complete'] = len(report['checks']) == len(args.languages)
        report['passed'] = report['complete'] and not failed
        if args.wasm_only:
            attempted = attempts.read_text().splitlines()
            report['execution_policy']['attempted_native_tools'] = attempted
            if attempted:
                failed = True
                report['passed'] = False
        if report['complete']:
            report['finished'] = datetime.now(timezone.utc).isoformat()
        save_report()
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
