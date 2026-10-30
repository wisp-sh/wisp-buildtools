"""Runtime template. Application source is loaded only by guest CPython."""
import pathlib
import base64
import io
import os
import importlib.resources._adapters
import importlib.resources.readers
import importlib.metadata
import importlib.metadata._adapters
import email.parser
import email.message
import pkgutil
import dataclasses
import enum
import typing
import types
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
BINDINGS = {}
ERROR_TYPES = {}
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
        if program.get('entry') == '__wisp_inferred__':
            # Infer only top-level functions, preserving their original spelling.
            mapping = namespace.get('__wisp_export_names__', {})
            if name in mapping:
                function = namespace.get(mapping[name])
            else:
                local = [value for key, value in namespace.items()
                         if key.replace('_', '').lower() == name.replace('_', '').lower()
                         and getattr(value, '__module__', None) == namespace['__name__']
                         and callable(value)]
                if len(local) > 1:
                    raise TypeError('ambiguous Python export: ' + name)
                function = local[0] if local else namespace.get(name)
        elif implementation is not None and hasattr(implementation, name):
            # Authored contracts retain interface-class precedence over helpers.
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


def _convert(value, target):
    if target is None or value is None:
        return value
    if isinstance(target, type) and isinstance(value, target):
        return value
    origin = typing.get_origin(target)
    parameters = typing.get_args(target)
    if origin in (typing.Union, types.UnionType):
        choices = [t for t in parameters if t is not type(None)]
        if len(choices) == 1:
            return _convert(value, choices[0])
    if target is bytes:
        return bytes(value)
    if origin is list:
        return [_convert(item, parameters[0]) for item in value]
    if isinstance(target, type) and issubclass(target, enum.Enum):
        normalized = value.name.replace('_', '').lower()
        matches = [member for member in target if member.name.replace('_', '').lower() == normalized]
        if len(matches) != 1:
            raise TypeError('enum case cannot map to ' + target.__name__)
        return matches[0]
    if isinstance(target, type) and dataclasses.is_dataclass(target):
        hints = typing.get_type_hints(target)
        source_fields = {field.name.replace('_', '').lower(): field.name
                         for field in dataclasses.fields(value)}
        return target(**{field.name: _convert(getattr(value, source_fields[field.name.replace('_', '').lower()]), hints[field.name])
                         for field in dataclasses.fields(target)})
    return value


def _error_value(value, wire_type, binding):
    if value is None or not isinstance(wire_type, dict):
        return value
    kind = wire_type['kind']
    if 'type' in kind:
        return _error_value(value, kind['type'], binding)
    if 'list' in kind:
        return [_error_value(item, kind['list'], binding) for item in value]
    if 'option' in kind:
        return _error_value(value, kind['option'], binding)
    if 'record' in kind or 'enum' in kind:
        normalized = wire_type['name'].replace('-', '').lower()
        for module in binding.__globals__.values():
            if isinstance(module, types.ModuleType) and module.__name__.startswith('wit_world.'):
                target = next((item for key, item in vars(module).items()
                               if key.replace('_', '').lower() == normalized), None)
                if target is not None:
                    return _convert(value, target)
        raise TypeError('missing generated error binding: ' + wire_type['name'])
    return value


def _call(owner, name, *args):
    function = _load()[owner, name]
    binding = BINDINGS.get((owner, name))
    if binding is None:
        return function(*args)
    user_hints = typing.get_type_hints(function)
    bound = 1 if getattr(function, '__self__', None) is not None else 0
    parameters = function.__code__.co_varnames[bound:bound + len(args)]
    converted = [_convert(value, user_hints.get(parameter)) for parameter, value in zip(parameters, args)]
    try:
        value = function(*converted)
    except Err as error:
        if (owner, name) in ERROR_TYPES:
            raise Err(_error_value(error.value, ERROR_TYPES[owner, name], binding)) from None
        # Result[T, E] uses a normal success return and Err(E) for failure.
        # Convert user enums/records to the corresponding generated binding type.
        error_type = type(error.value)
        target = None
        for module in binding.__globals__.values():
            if isinstance(module, types.ModuleType) and module.__name__.startswith('wit_world.'):
                target = next((item for key, item in vars(module).items()
                               if key.replace('_', '').lower() == error_type.__name__.replace('_', '').lower()), None)
                if target is not None:
                    break
        raise Err(_convert(error.value, target)) from None
    return _convert(value, typing.get_type_hints(binding).get('return'))
