"""Host unit doubles for the guest parser, not application build execution."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('guest_inference', Path(__file__).parent / 'runtime/inference.py')
guest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guest)


class InferenceTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
