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
import zipfile
from toolchain import ROOT, command, digest, run, verified_dist, wasm_tool, wasm_tools


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, default=ROOT / 'verification.json')
    args = parser.parse_args()
    _, manifest = verified_dist()
    run([sys.executable, '-m', 'unittest', 'test_project'], cwd=ROOT)
    host = wasm_tools.runtime()
    environment = {**os.environ, 'PATH': '/nonexistent', 'WISP_WASMTIME': str(host)}
    checks = []
    started = time.monotonic()
    scratch = ROOT / '.toolchains/verification'
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='scalar-', dir=scratch) as directory:
        work = Path(directory)
        def compile_source(source, name, wit=ROOT / 'examples/add.wit', success=True, extra=()):
            output = work / (name + '.wasm')
            result = subprocess.run([sys.executable, ROOT / 'compile.py', *([source] if isinstance(source, Path) else source), *(['--wit', wit] if wit is not None else []),
                                     '-o', output, *extra], env=environment, capture_output=True, text=True, timeout=300)
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
        rich = compile_source(ROOT / 'examples/library/Api/Api.csproj', 'project-library',
            wit=ROOT / 'examples/library/library.wit', extra=['--project-root', ROOT / 'examples/library', '--entry', 'Example.Api'])
        for expression, expected in [
            ('add(9007199254740993,1)', '9007199254740994'),
            ('greet("世界")', '"Hello, 世界!"'),
            ('choose(some(high))', '[42]'), ('choose(none)', '[0]'),
            ('advance({name: "Zoë", age: 41})', 'ok({name: "Zoë", age: 42})'),
            ('advance({name: "Zoë", age: 0})', 'err("age must be positive")'),
            ('people([{name: "雪", age: 1}])', '[{name: "雪", age: 2}]'),
            ('pair("hello", -42)', '("hello", -42)'),
            ('maybe(none)', 'none'), ('maybe(some("yes"))', 'some("yes")'),
            ('scale(some(1.25))', 'some(2.5)'),
            ('number(ok(1.25))', 'ok(1.25)'), ('number(err(-9007199254740993))', 'err(-9007199254740993)'),
            ('flip(true)', 'false'), ('nothing()', '()'),
            ('many(1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17)', '153'),
        ]: invoke(rich, expression, expected)
        for name, value in [('u8','255'),('s8','-128'),('u16','65535'),('s16','-32768'),
                            ('u32','4294967295'),('s32','-2147483648'),('u64','18446744073709551615'),('f32','1.25'),('f64','-1.25')]:
            invoke(rich, f'echo-{name}({value})', value)
        bindings = os.environ.get('WISP_WASMTIME_PYTHON')
        if bindings:
            message = run([sys.executable, ROOT / 'verify_component.py', rich, '--bindings', bindings], timeout=120, env=environment)
            checks.append({'name': 'same-instance rich calls and post-return', 'output': message.strip()})
        else:
            raise ValueError('same-instance acceptance requires pinned WISP_WASMTIME_PYTHON bindings')
        inferred = compile_source(ROOT / 'examples/library/Api/Api.csproj', 'inferred-project', wit=None,
            extra=['--project-root', ROOT / 'examples/library', '--entry', 'Example.Api'])
        message = run([sys.executable, ROOT / 'verify_component.py', inferred, '--bindings', bindings,
            '--interface', 'wisp:csharp-inferred/api', '--inferred'], timeout=120, env=environment)
        checks.append({'name': 'inferred project same-instance rich calls', 'output': message.strip()})
        discovery = work / 'discovered.wit'
        run([sys.executable, ROOT / 'compile.py', ROOT / 'examples/library/Api/Api.csproj', '--project-root',
            ROOT / 'examples/library', '--entry', 'Example.Api', '--discover', '-o', discovery], timeout=300, env=environment)
        api = json.loads(Path(str(discovery) + '.build.json').read_text())['api']
        if 'record %person' not in api or '%echo-u8: func(%value: u8)' not in api or 'option<string>' not in api:
            raise ValueError('semantic discovery omitted or mistyped public exports')
        checks.append({'name': 'discover project API without emitting an application'})
        partial = work / 'Partial.cs'
        for number, method in enumerate([
            'public static T Generic<T>(T value) => value;',
            'public static System.Threading.Tasks.Task<int> Async() => System.Threading.Tasks.Task.FromResult(1);',
            'public static int Add(long a, long b) => 1;',
            'public static int ByRef(ref int value) => value;',
            'public static char Utf16(char value) => value;',
        ]):
            partial.write_text('public static class Api { public static int Add(int a,int b)=>a+b; ' + method + ' }')
            compile_source(partial, 'unsupported-discovery-' + str(number), wit=None, success=False)
        first, second = work / 'First.cs', work / 'Second.cs'
        first.write_text('public static partial class Api { public static int Add(int a,int b)=>Sum(a,b); }')
        second.write_text('public static partial class Api { private static int Sum(int a,int b)=>a+b; }')
        invoke(compile_source([first, second], 'inferred-multiple-files', wit=None), 'add(20,22)', '42')
        second.write_text('public static partial class Api { private static int Sum(int a,int b)=>a+b+1; }')
        invoke(compile_source([first, second], 'changed-helper', wit=None), 'add(20,22)', '43')
        # Build a portable managed dependency in Wasm, then consume its DLL and a
        # locally packed offline NuGet equivalent. Neither path may run dotnet.
        dependency = work / 'Dependency.cs'
        dependency.write_text('namespace Portable; public static class Math { public static int Sum(int a,int b)=>a+b; }')
        dll = work / 'Portable.dll'
        run([sys.executable, ROOT / 'compile.py', dependency, '--library', '-o', dll], timeout=300, env=environment)
        consumer = work / 'Consumer.cs'
        consumer.write_text('public static class Export { public static int Add(int a,int b)=>Portable.Math.Sum(a,b); }')
        invoke(compile_source(consumer, 'local-managed-reference', extra=['--reference', dll]), 'add(20,22)', '42')
        package = work / 'Portable.1.0.0.nupkg'
        with zipfile.ZipFile(package, 'w') as archive:
            archive.writestr('Portable.nuspec', '<package><metadata><id>Portable</id><version>1.0.0</version></metadata></package>')
            archive.write(dll, 'lib/net8.0/Portable.dll')
        consumer_project = work / 'Consumer.csproj'
        consumer_project.write_text('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net8.0</TargetFramework><EnableDefaultCompileItems>false</EnableDefaultCompileItems></PropertyGroup><ItemGroup><Compile Include="Consumer.cs"/><PackageReference Include="Portable" Version="1.0.0"/></ItemGroup></Project>')
        invoke(compile_source(consumer_project, 'offline-nuget', extra=['--package', package]), 'add(20,22)', '42')
        ambiguous = work / 'Ambiguous.cs'
        ambiguous.write_text('public static class First { public static int Add(int a,int b)=>a+b; } public static class Second { public static int Add(int a,int b)=>a-b; }')
        compile_source(ambiguous, 'ambiguous-entry', success=False)
        inline_wit = work / 'inline.wit'
        inline_wit.write_text('package example:inline; world calculator { export numbers: interface { add: func(a:s32,b:s32)->s32; } }')
        invoke(compile_source(ambiguous, 'explicit-export', wit=inline_wit, extra=['--export','numbers.add=First.Add']), 'add(20,22)', '42')
        compile_source(consumer, 'missing-reference', success=False)
        existing = work / 'existing.wasm'
        existing.write_bytes(b'preserve me')
        result = subprocess.run([sys.executable, ROOT / 'compile.py', ROOT / 'examples/add.cs', '--wit',
            ROOT / 'examples/add.wit', '-o', existing], env=environment, capture_output=True, text=True, timeout=120)
        if not result.returncode or existing.read_bytes() != b'preserve me':
            raise ValueError('existing output was replaced')
        checks.append({'name': 'existing output preserved'})
    verified_dist()
    report = {'schemaVersion': 1, 'toolchain': 'csharp', 'version': manifest['version'],
              'passed': sum('skipped' not in c for c in checks), 'skipped': sum('skipped' in c for c in checks), 'nativeCompilerOnPath': False, 'fullyWasmHostedBuild': True,
              'seconds': round(time.monotonic() - started, 3), 'checks': checks}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(f"C#: {len(checks)} checks passed; native compilers absent from PATH")


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(f'csharp verify: {error}') from error
