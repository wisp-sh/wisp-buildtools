"""Runtime template. Application source is loaded only by guest CPython."""
import collections
import functools
import itertools
import json
import math
import re
import sys
import wit_world
from wit_world import exports
from wit_world.imports import source
from componentize_py_types import Err

EXPORTS = []
_namespace = None
_functions = None


def _load():
    global _namespace, _functions
    if _functions is not None:
        return _functions
    program = json.loads(source.get())
    namespace = {'__name__': '__wisp_user__', '__file__': '<user.py>'}
    exec(compile(program['source'], '<user.py>', 'exec', dont_inherit=True), namespace)
    functions = {}
    instances = {}
    for owner, name, count in EXPORTS:
        implementation = namespace.get(owner)
        if implementation is not None:
            if owner not in instances:
                instances[owner] = implementation()
            function = getattr(instances[owner], name, None)
        else:
            function = namespace.get(name)
        code = getattr(function, '__code__', None)
        bound = 1 if getattr(function, '__self__', None) is not None else 0
        if (not callable(function) or code is None or code.co_argcount != count + bound
                or code.co_kwonlyargcount or code.co_flags & (4 | 8 | 128 | 512)):
            raise TypeError(f'{owner}.{name} must be a synchronous function with {count} positional parameters')
        functions[owner, name] = function
    _namespace = namespace
    _functions = functions
    return functions


def _validate():
    try:
        _load()
    except Exception as error:
        raise Err(f'{type(error).__name__}: {error}')


def _call(owner, name, *args):
    return _load()[owner, name](*args)
