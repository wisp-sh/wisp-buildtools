#!/usr/bin/env python3
"""Guest acceptance: inference, offline wheels, provisioning, reuse and calls.

Run only after checking host capacity. This provisions three new CPython templates.
"""
import json
from pathlib import Path
import sys
import tempfile
import zipfile

from scripts.common import ROOT, digest, invoke, run, tool, verified_package

PROGRAM = '''from models import Person, Color
from typing import Optional
from componentize_py_types import Result, Err
from exports import say_hello as greet
import helper

def birthday(person: Person) -> Person:
    return Person(person.name, person.age + 1)
def total(values: list[int]) -> int:
    return sum(values)
def checked(value: int) -> Result[int, str]:
    if value < 0:
        raise Err("negative")
    return value * 2
def echo(data: bytes) -> bytes:
    return data
def maybe(value: Optional[str]) -> Optional[str]:
    return value
def echo_color(value: Color) -> Color:
    return value
def package_info() -> str:
    return helper.info()
def payload_size() -> int:
    from pathlib import Path
    return len(Path(__file__).with_name("payload.bin").read_bytes())
'''
MODELS = '''from dataclasses import dataclass
from enum import Enum
@dataclass
class Person:
    name: str
    age: int
class Color(Enum):
    BLUE = "blue"
    RED = "red"
'''
HELPER = '''from importlib.resources import files, as_file
from importlib.metadata import version
from pkgutil import get_data
from datetime import date
import prefix

def greet(name):
    with as_file(files(__package__).joinpath("greeting.txt")) as path:
        template = path.read_text()
    assert get_data(__package__, "greeting.txt").decode() == template
    return prefix.value + template.format(name=name)
def info():
    return version("helper") + ":" + str(date(2026, 1, 2))
'''



def check_diagnostics(work):
    import subprocess
    invalid = work / 'invalid-input'
    invalid.mkdir()
    bad = invalid / 'bad.py'
    for declaration, diagnostic in [
        ('def missing(value) -> int: ...', 'function missing parameter value: unsupported Python type <missing annotation>'),
        ('def unsupported(value: dict[str, int]) -> int: ...', 'function unsupported parameter value: unsupported Python type dict[str, int]'),
    ]:
        bad.write_text('raise RuntimeError("inference must not execute source")\n' + declaration + '\n')
        try:
            run([sys.executable, ROOT / 'infer.py', bad], capture=True)
        except subprocess.CalledProcessError as error:
            if diagnostic not in error.stderr:
                raise ValueError('guest inference diagnostic differs: ' + error.stderr) from error
        else:
            raise ValueError('guest inference accepted an invalid annotation')
    print('PASS guest diagnostics without executing source', flush=True)


def main():
    if not verified_package(ROOT / 'dist'):
        raise ValueError('bootstrap the current Python package first')
    build = ROOT / 'build'
    build.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='inferred-acceptance-', dir=build) as directory:
        work = Path(directory)
        check_diagnostics(work)
        project = work / 'project'
        project.mkdir()
        package_root = project / 'src'
        package_root.mkdir()
        source = package_root / 'main.py'
        source.write_text(PROGRAM)
        (package_root / 'models.py').write_text(MODELS)
        (package_root / 'exports.py').write_text('import helper\ndef say_hello(name: str) -> str: return helper.greet(name)\n')
        (package_root / 'payload.bin').write_bytes(b'x' * (1024 * 1024 + 1))
        wheel = project / 'helper-1.0-py3-none-any.whl'
        with zipfile.ZipFile(wheel, 'w') as archive:
            archive.writestr('helper/__init__.py', HELPER)
            archive.writestr('helper/greeting.txt', '{name}!')
            archive.writestr('helper-1.0.dist-info/WHEEL', 'Root-Is-Purelib: true\nTag: py3-none-any\n')
            archive.writestr('helper-1.0.dist-info/METADATA', 'Name: helper\nVersion: 1.0\nProvides-Extra: resources\nRequires-Dist: prefix>=1; extra == "resources"\nRequires-Dist: windows-only; sys_platform == "win32"\n')
        prefix = project / 'prefix-1.0-py3-none-any.whl'
        with zipfile.ZipFile(prefix, 'w') as archive:
            archive.writestr('prefix.py', 'value = "Hello, "\n')
            archive.writestr('prefix-1.0.dist-info/WHEEL', 'Root-Is-Purelib: true\nTag: py3-none-any\n')
            archive.writestr('prefix-1.0.dist-info/METADATA', 'Name: prefix\nVersion: 1.0\n')
        (project / 'pyproject.toml').write_text('[project]\ndependencies=["helper[resources]>=1,<2"]\n')
        (project / 'python-deps.lock.json').write_text(json.dumps({'schema': 1, 'packages': [
            {'name': 'helper', 'version': '1.0', 'path': wheel.name, 'sha256': digest(wheel)},
            {'name': 'prefix', 'version': '1.0', 'path': prefix.name, 'sha256': digest(prefix)}]}))
        wit = work / 'inferred.wit'
        inferred = run([sys.executable, ROOT / 'infer.py', source, '--package-root', package_root, '--dependency-root', project], capture=True).stdout
        wit.write_text(inferred)
        print("PASS guest multi-module inference and src-layout metadata", flush=True)
        output = work / 'library.wasm'
        cache = work / 'cache'
        command = [sys.executable, ROOT / 'compile.py', source, '--wit', wit, '--world', 'library', '--inferred',
                   '--package-root', package_root, '--dependency-root', project, '--cache-dir', cache, '-o', output]
        receipt = json.loads(run(command, capture=True).stdout)
        for expression, expected in [
            ('greet("世界")', '"Hello, 世界!"'), ('total([20, 22])', '42'),
            ('checked(-1)', 'err("negative")'), ('echo-color(blue)', 'blue'),
            ('birthday({name: "A", age: 41})', '{name: "A", age: 42}'),
            ('maybe(none)', 'none'), ('echo([1,2])', '[1, 2]'),
            ('package-info()', '"1.0:2026-01-02"'), ('payload-size()', '1048577'),
        ]:
            try:
                actual = invoke(output, expression)
            except ValueError:
                # Keep denied guest stderr in production; expose it in this
                # test subprocess so a Python traceback explains a failed call.
                import subprocess
                diagnostic = subprocess.run([str(tool('wasmtime')), 'run', '-C', 'cache=y',
                    '-O', 'opt-level=0', '-C', 'parallel-compilation=n',
                    '-S', 'inherit-stderr=y', '--invoke', expression, str(output)],
                    capture_output=True, text=True, timeout=30)
                print(diagnostic.stderr, file=sys.stderr, flush=True)
                raise
            if actual != expected:
                raise ValueError(f'{expression}: expected {expected}, got {actual}')
        print('PASS typed calls, offline extras/markers, resources/metadata and >1 MiB data', flush=True)
        manifests = list(cache.rglob('manifest.json'))
        if len(manifests) != 1:
            raise ValueError('expected one automatically provisioned contract')
        manifest_hash = digest(manifests[0])
        run(command, capture=True)
        if digest(manifests[0]) != manifest_hash:
            raise ValueError('runtime template changed on reuse')
        source.write_text(source.read_text().replace('return sum(values)', 'return sum(values) + 1'))
        if run([sys.executable, ROOT / 'infer.py', source, '--package-root', package_root, '--dependency-root', project], capture=True).stdout != inferred:
            raise ValueError('function-body edit changed the inferred contract')
        run(command, capture=True)
        if invoke(output, 'total([20,22])') != '43' or digest(manifests[0]) != manifest_hash:
            raise ValueError('function-body edit did not reuse the template')
        print('PASS deterministic template reuse and body edit', flush=True)
        source.write_text(source.read_text() + '\ndef ready() -> bool:\n    return True\n')
        changed = run([sys.executable, ROOT / 'infer.py', source, '--package-root', package_root, '--dependency-root', project], capture=True).stdout
        if changed == inferred:
            raise ValueError('API edit did not change the inferred contract')
        wit.write_text(changed)
        run(command, capture=True)
        if len(list(cache.rglob('manifest.json'))) != 2 or invoke(output, 'ready()') != 'true':
            raise ValueError('API edit did not provision a matching template')
        source.write_text(source.read_text().replace('return sum(values) + 1', 'import decimal\n    return int(decimal.Decimal(sum(values))) + 1'))
        run(command, capture=True)
        if len(list(cache.rglob('manifest.json'))) != 3 or invoke(output, 'total([20,22])') != '43':
            raise ValueError('stdlib import edit did not provision the updated runtime')
        previous = digest(output)
        source.write_text(source.read_text() + '\nraise RuntimeError("initialization failed")\n')
        import subprocess
        try:
            run(command, capture=True)
        except subprocess.CalledProcessError:
            pass
        else:
            raise ValueError('invalid initializer unexpectedly succeeded')
        if digest(output) != previous:
            raise ValueError('failed build replaced the previous output')
        if not receipt['sourceBundle']['dependencies']['packages']:
            raise ValueError('dependency pins missing from receipt')
    print('PASS guest multi-module inference, aliases, rich values, src layout, offline wheels/extras/markers, resources/metadata, >1 MiB data, stdlib discovery, provisioning/reuse, failure atomicity and diagnostics')


if __name__ == '__main__':
    import subprocess
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--diagnostics-only', action='store_true', help='check guest errors without reprovisioning runtime templates')
    args = parser.parse_args()
    try:
        if args.diagnostics_only:
            with tempfile.TemporaryDirectory(prefix='python-diagnostics-') as directory:
                check_diagnostics(Path(directory))
        else:
            main()
    except subprocess.CalledProcessError as error:
        print(error.stdout or '', file=sys.stderr)
        print(error.stderr or '', file=sys.stderr)
        raise
