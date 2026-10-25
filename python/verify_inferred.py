#!/usr/bin/env python3
"""Guest acceptance: inference, offline wheels, provisioning, reuse and calls.

Run only after checking host capacity. This provisions two new CPython templates.
"""
import json
from pathlib import Path
import sys
import tempfile
import zipfile

from scripts.common import ROOT, digest, invoke, run, verified_package

PROGRAM = '''from dataclasses import dataclass
from enum import Enum
from typing import Optional
from componentize_py_types import Result, Err
import helper
@dataclass
class Person:
    name: str
    age: int
class Color(Enum):
    BLUE = "blue"
    RED = "red"
def greet(name: str) -> str:
    return helper.greet(name)
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
'''


def main():
    if not verified_package(ROOT / 'dist'):
        raise ValueError('bootstrap the current Python package first')
    build = ROOT / 'build'
    build.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='inferred-acceptance-', dir=build) as directory:
        work = Path(directory)
        project = work / 'project'
        project.mkdir()
        source = project / 'main.py'
        source.write_text(PROGRAM)
        wheel = project / 'helper-1.0-py3-none-any.whl'
        with zipfile.ZipFile(wheel, 'w') as archive:
            archive.writestr('helper.py', 'def greet(name): return "Hello, " + name + "!"\n')
            archive.writestr('helper-1.0.dist-info/WHEEL', 'Root-Is-Purelib: true\nTag: py3-none-any\n')
            archive.writestr('helper-1.0.dist-info/METADATA', 'Name: helper\nVersion: 1.0\n')
        (project / 'pyproject.toml').write_text('[project]\ndependencies=["helper==1.0"]\n')
        (project / 'python-deps.lock.json').write_text(json.dumps({'schema': 1, 'packages': [
            {'name': 'helper', 'version': '1.0', 'path': wheel.name, 'sha256': digest(wheel)}]}))
        wit = work / 'inferred.wit'
        inferred = run([sys.executable, ROOT / 'infer.py', source], capture=True).stdout
        wit.write_text(inferred)
        output = work / 'library.wasm'
        cache = work / 'cache'
        command = [sys.executable, ROOT / 'compile.py', source, '--wit', wit, '--world', 'library',
                   '--package-root', project, '--cache-dir', cache, '-o', output]
        receipt = json.loads(run(command, capture=True).stdout)
        for expression, expected in [
            ('greet("世界")', '"Hello, 世界!"'), ('total([20, 22])', '42'),
            ('checked(-1)', 'err("negative")'), ('echo-color(blue)', 'blue'),
            ('birthday({name: "A", age: 41})', '{name: "A", age: 42}'),
            ('maybe(none)', 'none'), ('echo([1,2])', '[1, 2]'),
        ]:
            actual = invoke(output, expression)
            if actual != expected:
                raise ValueError(f'{expression}: expected {expected}, got {actual}')
        manifests = list(cache.rglob('manifest.json'))
        if len(manifests) != 1:
            raise ValueError('expected one automatically provisioned contract')
        manifest_hash = digest(manifests[0])
        run(command, capture=True)
        if digest(manifests[0]) != manifest_hash:
            raise ValueError('runtime template changed on reuse')
        source.write_text(source.read_text().replace('return sum(values)', 'return sum(values) + 1'))
        if run([sys.executable, ROOT / 'infer.py', source], capture=True).stdout != inferred:
            raise ValueError('function-body edit changed the inferred contract')
        run(command, capture=True)
        if invoke(output, 'total([20,22])') != '43' or digest(manifests[0]) != manifest_hash:
            raise ValueError('function-body edit did not reuse the template')
        source.write_text(source.read_text() + '\ndef ready() -> bool:\n    return True\n')
        changed = run([sys.executable, ROOT / 'infer.py', source], capture=True).stdout
        if changed == inferred:
            raise ValueError('API edit did not change the inferred contract')
        wit.write_text(changed)
        run(command, capture=True)
        if len(list(cache.rglob('manifest.json'))) != 2 or invoke(output, 'ready()') != 'true':
            raise ValueError('API edit did not provision a matching template')
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
    print('PASS guest inference, rich values, offline wheel, automatic provisioning, reuse, API/body changes and failure atomicity')


if __name__ == '__main__':
    main()
