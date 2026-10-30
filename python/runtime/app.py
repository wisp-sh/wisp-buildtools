"""The user program is parsed, compiled and executed only by guest CPython."""
from inference import infer_json
import collections
import functools
import itertools
import json
import math
import re
import sys
from componentize_py_types import Err
import wit_world
from wit_world import exports
from wit_world.imports import source

_module = None
_entry = None


def load_program():
    global _module, _entry
    if _module is None:
        program = json.loads(source.get())
        entry = program['entry']
        code = compile(program['source'], '<user.py>', 'exec', dont_inherit=True)
        namespace = {'__name__': '__wisp_user__', '__file__': '<user.py>'}
        exec(code, namespace)
        function = namespace.get(entry)
        if not callable(function):
            raise TypeError(f'source must define {entry}(a: int, b: int) -> int')
        code = getattr(function, '__code__', None)
        if code is None or code.co_argcount != 2 or code.co_kwonlyargcount or code.co_flags & 12:
            raise TypeError(f'{entry} must take exactly two positional parameters')
        annotations = function.__annotations__
        if any(annotations.get(p) not in (int, 'int') for p in code.co_varnames[:2]) or annotations.get('return') not in (int, 'int'):
            raise TypeError(f'{entry} parameters and return value must be annotated int')
        _module = namespace
        _entry = entry
    return _module


class WitWorld(wit_world.WitWorld):
    def infer(self) -> str:
        return infer_json(json.loads(source.get())['source'])


class Api(exports.Api):
    def validate(self):
        try:
            load_program()
            return None
        except Exception as error:
            raise Err(f'{type(error).__name__}: {error}')

    def add(self, a: int, b: int) -> int:
        namespace = load_program()
        result = namespace[_entry](a, b)
        if type(result) is not int:
            raise TypeError('add must return int')
        if not -(2**31) <= result < 2**31:
            raise OverflowError('add result does not fit WIT s32')
        return result

    def runtime_info(self) -> str:
        return f'CPython {sys.version}; platform={sys.platform}; source-compiler=guest compile/exec'
