"""Generic string entry adapter. Application modules compile only inside Wasm."""
import json
import zlib
import binascii
import sys
import importlib.util
from componentize_py_types import Err
from wit_world import exports
from wit_world.imports import source
_module = None
_entry = None

def load_program():
    global _module, _entry
    if _module is None:
        program = json.loads(source.get())
        namespace = {'__name__': '__wisp_user__', '__file__': '<user.py>'}
        exec(compile(program['source'], '<user.py>', 'exec', dont_inherit=True), namespace)
        function = namespace.get(program['entry'])
        if not callable(function):
            raise TypeError('string entry must be callable')
        _entry = function
        _module = namespace
    return _entry

class Api(exports.Api):
    def validate(self):
        try:
            load_program()
        except Exception as error:
            raise Err(f'{type(error).__name__}: {error}')
    def transform(self, text: str) -> str:
        try:
            value = load_program()(text)
            if not isinstance(value, str):
                raise TypeError('string entry must return str')
            return value
        except Exception as error:
            raise Err(f'{type(error).__name__}: {error}')
    def runtime_info(self) -> str:
        return f'CPython {sys.version}; platform={sys.platform}; source-compiler=guest compile/exec; package-loader=source-map'
