"""Host unit doubles for the guest parser, not application build execution."""
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('guest_inference', Path(__file__).parent / 'runtime/inference.py')
guest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guest)


class InferenceTests(unittest.TestCase):
    def test_dependency_syntax_diagnostic_preserves_exception_type_and_location(self):
        request = {'schema': 2, 'entry': 'api', 'metadataOnly': True, 'modules': {
            'api': ['import arithmetic\ndef total() -> int: return 42\n', '/__wisp__/api.py', False],
            'arithmetic': ['def broken(: pass\n', '/__wisp__/arithmetic.py', False],
        }}
        response = json.loads(guest.infer_json(json.dumps(request)))
        self.assertIn('SyntaxError:', response['error'])
        self.assertIn('arithmetic.py, line 1', response['error'])

    def test_rich_types_aliases_and_results_without_executing_source(self):
        body = guest.infer('''from dataclasses import dataclass as record
from enum import Enum
from typing import Optional
from componentize_py_types import Result
raise RuntimeError("must not execute application source")
@record
class Person:
    name: str
    age: int
class Color(Enum):
    DARK_BLUE = "blue"
    RED = "red"
def greet(who: str, enabled: bool, data: bytes, scale: float) -> str: ...
def people(values: list[Person], color: Color) -> Optional[Person]: ...
def checked(x: int) -> Result[int, str]: ...
def nothing() -> None: ...
''')
        for expected in ('record %person', '%age: s64', 'enum %color', '%dark-blue', 'list<%person>',
                         'option<%person>', 'result<s64, string>', '%data: list<u8>', '%scale: f64', '%nothing: func();'):
            self.assertIn(expected, body)

    def test_private_functions_ignored_and_forward_annotations_supported(self):
        body = guest.infer('def _helper(x): ...\ndef api(x: "list[int]") -> str | None: ...')
        self.assertNotIn('helper', body)
        self.assertIn('list<s64>', body)
        self.assertIn('option<string>', body)

    def test_signature_errors_name_the_function_and_type(self):
        for source, expected in [
            ('def bad(x) -> int: ...', 'function bad parameter x: unsupported Python type <missing annotation>'),
            ('def bad(x: dict[str, int]) -> int: ...', 'function bad parameter x: unsupported Python type dict[str, int]'),
            ('def bad(x: int): ...', 'function bad return: unsupported Python type <missing annotation>'),
            ('async def bad() -> int: ...', 'function bad: async'),
            ('def bad(*x: int) -> int: ...', 'function bad: variadic'),
            ('def bad(x: Result[int, str]) -> int: ...', 'function bad parameter x: unsupported Python type'),
        ]:
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, expected.replace('[', r'\[').replace(']', r'\]')):
                guest.infer(source)

    def test_recursion_and_collisions_fail(self):
        for source in [
            'from dataclasses import dataclass\n@dataclass\nclass Node:\n child: "Node"\ndef api(x: Node) -> int: ...',
            'def some_name() -> int: ...\ndef someName() -> int: ...',
            'def api(x: int) -> int: ...\ndef api(x: int) -> int: ...',
        ]:
            with self.assertRaises(ValueError):
                guest.infer(source)


class ModuleInferenceTests(unittest.TestCase):
    def analyze(self, entry, **sources):
        modules = {name: [text, '/__wisp__/' + name.replace('.', '/') + '.py', False]
                   for name, text in sources.items()}
        for name in list(modules):
            parent = name.rpartition('.')[0]
            while parent:
                modules.setdefault(parent, [None, '/__wisp__/' + parent.replace('.', '/') + '/__init__.py', True])
                parent = parent.rpartition('.')[0]
        return guest.analyze('', modules, entry)

    def test_imported_dataclasses_and_enums_have_distinct_qualified_names(self):
        result = self.analyze('api', api='import models as m\nfrom other import Person\ndef api(a: m.Person, b: Person) -> m.Color: ...',
                             models='from dataclasses import dataclass\nfrom enum import Enum\n@dataclass\nclass Person:\n name: str\nclass Color(Enum):\n RED=1\n',
                             other='from dataclasses import dataclass\n@dataclass\nclass Person:\n value: int\n')
        self.assertIn('record %models-person', result['declarations'])
        self.assertIn('record %other-person', result['declarations'])
        self.assertIn('enum %models-color', result['declarations'])

    def test_reexports_aliases_and_relative_imports_do_not_execute_source(self):
        result = self.analyze('pkg.api', **{'pkg.api': 'from .impl import greetPerson as helloWorld\n__all__=["helloWorld"]\n',
            'pkg.impl': 'raise RuntimeError("must never execute")\nfrom .models import Person\ndef greetPerson(p: Person) -> str: ...',
            'pkg.models': 'from dataclasses import dataclass\n@dataclass\nclass Person:\n name: str\n'})
        self.assertIn('%hello-world: func(', result['declarations'])
        self.assertIn('record %pkg-models-person', result['declarations'])
        self.assertEqual(result['exports'], {'hello_world': 'helloWorld'})

    def test_stdlib_import_discovery_follows_helpers_and_literal_dynamic_imports(self):
        result = self.analyze('api', api='from helper import api\n', helper='import datetime\nimport importlib\ndef api() -> str:\n return importlib.import_module("decimal").__name__')
        self.assertTrue({'datetime', 'decimal', 'importlib'} <= set(result['stdlib']))

    def test_aliases_and_builtin_shadowing_are_resolved_statically(self):
        self.assertIn('list<s64>', self.analyze('api', api='Number=int\nValues=list[Number]\ndef api(x: Values) -> Number: ...')['declarations'])
        with self.assertRaisesRegex(ValueError, 'function api parameter x'):
            self.analyze('api', api='class int: pass\ndef api(x: int) -> str: ...')

    def test_cross_module_recursion_and_dynamic_export_lists_fail_clearly(self):
        with self.assertRaisesRegex(ValueError, 'recursive Python type'):
            self.analyze('api', api='from models import Node\ndef api(x: Node) -> str: ...', models='from dataclasses import dataclass\n@dataclass\nclass Node:\n child: "Node"')
        with self.assertRaisesRegex(ValueError, '__all__'):
            self.analyze('api', api='__all__ = make_exports()\ndef api() -> str: ...')

if __name__ == '__main__':
    unittest.main()
