"""Focused checks for template identity, generated dispatch and validation.

These tests execute the runtime under host Python solely as a unit-test double.
Application builds always compile user source in guest CPython.
"""
import copy
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import compile_contract as compiler
import bootstrap_contract as bootstrapper
from scripts.contracts import CONTROL, contract, generate_runtime, preload_modules

ROOT = Path(__file__).resolve().parent


def document():
    return {'worlds': [{'name': 'library', 'imports': {}, 'exports': {
        'hello': {'function': {'name': 'hello', 'kind': 'freestanding',
                              'params': [{'name': 'name', 'type': 'string'}], 'result': 'string'}}},
        'package': 0}], 'interfaces': [], 'types': [],
        'packages': [{'name': 'demo:example@0.1.0'}]}


class ContractTests(unittest.TestCase):
    def test_identity_ignores_wrapper_and_docs_but_includes_public_abi(self):
        original = document()
        key, _ = contract(original, 'library')
        changed = copy.deepcopy(original)
        changed['worlds'][0]['name'] = 'wrapper'
        changed['packages'][0]['name'] = 'demo:release@1.0.0'
        changed['worlds'][0]['exports']['hello']['function']['docs'] = {'contents': 'hello'}
        self.assertEqual(key, contract(changed, 'wrapper')[0])
        changed['worlds'][0]['exports']['hello']['function']['result'] = 'u32'
        self.assertNotEqual(key, contract(changed, 'wrapper')[0])

    def test_rejects_imports_async_and_reserved_exports(self):
        value = document()
        value['worlds'][0]['imports'] = {'host': {}}
        with self.assertRaisesRegex(ValueError, 'import-free'):
            contract(value, 'library')
        value = document()
        value['worlds'][0]['exports']['hello']['function']['kind'] = 'async-freestanding'
        with self.assertRaisesRegex(ValueError, 'synchronous'):
            contract(value, 'library')
        value = document()
        value['worlds'][0]['exports'][CONTROL] = value['worlds'][0]['exports'].pop('hello')
        with self.assertRaisesRegex(ValueError, 'reserved'):
            contract(value, 'library')

    def test_type_indices_do_not_affect_contract_identity(self):
        value = document()
        value['types'] = [{'name': None, 'kind': {'list': 'u8'}, 'owner': None}]
        value['worlds'][0]['exports']['hello']['function']['result'] = 0
        key = contract(value, 'library')[0]
        value['types'].insert(0, {'name': None, 'kind': {'list': 's32'}, 'owner': None})
        value['worlds'][0]['exports']['hello']['function']['result'] = 1
        self.assertEqual(key, contract(value, 'library')[0])
        value['types'][1]['kind'] = {'handle': {'own': 0}}
        with self.assertRaisesRegex(ValueError, 'resources'):
            contract(value, 'library')

    def runtime(self, source_text, inferred=False):
        namespace = {}
        world = types.ModuleType('wit_world')
        exports = types.ModuleType('wit_world.exports')
        imports = types.ModuleType('wit_world.imports')
        source = types.SimpleNamespace(get=lambda: json.dumps({'source': source_text, 'entry': '__wisp_inferred__' if inferred else ''}))
        imports.source = source
        world.exports = exports
        errors = types.ModuleType('componentize_py_types')
        class Err(Exception):
            pass
        errors.Err = Err
        with patch.dict(sys.modules, {'wit_world': world, 'wit_world.exports': exports,
                                     'wit_world.imports': imports, 'componentize_py_types': errors}):
            exec((ROOT / 'scripts/runtime_contract.py').read_text(), namespace)
        namespace['EXPORTS'] = [('Api', 'greet', 1), ('Api', 'total', 1)]
        return namespace, Err

    def test_multiple_functions_unicode_lists_and_shared_state(self):
        runtime, _ = self.runtime('''calls = 0
def greet(name):
    global calls
    calls += 1
    return f"Hello, {name}! {calls}"
def total(values):
    return sum(values)
''')
        self.assertIsNone(runtime['_validate']())
        self.assertEqual('Hello, 世界! 1', runtime['_call']('Api', 'greet', '世界'))
        self.assertEqual('Hello, x! 2', runtime['_call']('Api', 'greet', 'x'))
        self.assertEqual(42, runtime['_call']('Api', 'total', [20, 22]))

    def test_normalized_inferred_names_keep_the_original_python_function(self):
        runtime, _ = self.runtime('def greetPerson(name): return name + "!"\ndef answer_(): return 42', inferred=True)
        runtime['EXPORTS'] = [('Api', 'greet_person', 1), ('Api', 'answer', 0)]
        self.assertEqual(runtime['_call']('Api', 'greet_person', 'x'), 'x!')
        self.assertEqual(runtime['_call']('Api', 'answer'), 42)

    def test_interface_classes_preserve_instance_state(self):
        runtime, _ = self.runtime('''class Api:
    def __init__(self): self.calls = 0
    def greet(self, name):
        self.calls += 1
        return str(self.calls)
    def total(self, values): return sum(values)
''')
        self.assertEqual('1', runtime['_call']('Api', 'greet', 'x'))
        self.assertEqual('2', runtime['_call']('Api', 'greet', 'x'))

    def test_authored_classes_keep_precedence_and_inference_selects_top_level_functions(self):
        source = '''def greet(name): return "global " + name
def total(values): return sum(values)
class Api:
    def greet(self, name): return "class " + name
'''
        authored, _ = self.runtime(source)
        inferred, _ = self.runtime(source, inferred=True)
        self.assertEqual(authored['_call']('Api', 'greet', 'x'), 'class x')
        self.assertEqual(inferred['_call']('Api', 'greet', 'x'), 'global x')

    def test_invalid_sources_fail_before_any_export_is_cached(self):
        sources = ['def broken(: pass', 'raise ValueError("broken")',
                   'def greet(name): return name',
                   'def greet(): pass\ndef total(values): pass',
                   'async def greet(name): pass\ndef total(values): pass',
                   'def greet(name, *args): pass\ndef total(values): pass']
        for source in sources:
            with self.subTest(source=source):
                runtime, error = self.runtime(source)
                with self.assertRaises(error):
                    runtime['_validate']()
                self.assertIsNone(runtime['_functions'])

    def test_generated_protocol_methods_and_control_export(self):
        with tempfile.TemporaryDirectory(prefix='python-contract-test-') as directory:
            bindings = Path(directory)
            (bindings / 'wit_world/exports').mkdir(parents=True)
            (bindings / 'wit_world/__init__.py').write_text(
                'class WitWorld(Protocol):\n def wisp_python_validate(self): ...\n def ready(self) -> bool: ...\n')
            (bindings / 'wit_world/exports/__init__.py').write_text(
                'class Api(Protocol):\n def greet(self, name: str) -> str: ...\n')
            generated = generate_runtime(bindings)
            compile(generated, '<generated-runtime>', 'exec')
            self.assertIn("('Api', 'greet', 1)", generated)
            self.assertIn('return _validate()', generated)
            self.assertIn("_call('WitWorld', 'ready', )", generated)


    def test_root_only_bindings_do_not_require_an_exports_module(self):
        with tempfile.TemporaryDirectory(prefix='python-contract-test-') as directory:
            bindings = Path(directory)
            (bindings / 'wit_world').mkdir()
            (bindings / 'wit_world/__init__.py').write_text(
                'class WitWorld(Protocol):\n def wisp_python_validate(self): ...\n def is_ready(self) -> bool: ...\n')
            generated = generate_runtime(bindings)
            compile(generated, '<root-runtime>', 'exec')
            self.assertNotIn('from wit_world import exports', generated)
            self.assertIn("('WitWorld', 'is_ready', 0)", generated)

    def test_preloads_generate_explicit_guest_imports_without_host_importing(self):
        with tempfile.TemporaryDirectory(prefix='python-preloads-test-') as directory:
            bindings = Path(directory)
            modules = ['xml.etree.ElementTree', 'html.parser', 'logging', 'html.parser']
            # Generation accepts dotted names without executing any imports on the host.
            with patch('builtins.__import__', side_effect=AssertionError('host import')):
                generated = generate_runtime(bindings, modules)
            compile(generated, '<preloaded-runtime>', 'exec')
            self.assertEqual(preload_modules(modules), ['html.parser', 'logging', 'xml.etree.ElementTree'])
            self.assertEqual(generated.count('import html.parser\n'), 1)
            self.assertIn('import xml.etree.ElementTree\n', generated)
            for invalid in ['html; print(1)', 'html..parser', 'from', '.html', 'html.', '']:
                with self.subTest(module=invalid), self.assertRaisesRegex(ValueError, 'invalid Python preload'):
                    generate_runtime(bindings, [invalid])

    def test_bootstrap_reuses_only_the_requested_preload_set(self):
        with tempfile.TemporaryDirectory(prefix='python-bootstrap-test-') as directory:
            root = Path(directory)
            wit = root / 'library.wit'
            wit.write_text('fixture')
            key, exports = contract(document(), 'library')
            destination = root / 'dist/contracts' / key
            destination.mkdir(parents=True)
            required = ['template.component.wasm', 'python-packager.wasm', 'public-exports.wasm']
            for name in required:
                (destination / name).write_bytes(b'fixture')
            manifest = {'schema': 1, 'contractSha256': key, 'exports': exports,
                        'componentizePy': bootstrapper.LOCK['componentize_py'], 'sourceModules': 1,
                        'preloadModules': ['html.parser', 'logging'],
                        'sha256': {name: bootstrapper.digest(destination / name) for name in required}}
            (destination / 'manifest.json').write_text(json.dumps(manifest))
            args = ['bootstrap_contract.py', '--wit', str(wit), '--world', 'library']
            tools = {name: root / name for name in ['wasm-tools', 'wasm-opt', 'wac']}
            for path in tools.values():
                path.write_bytes(b'tool fixture')
            lock = dict(bootstrapper.LOCK, tools={name: {'sha256': bootstrapper.digest(path)}
                                                 for name, path in tools.items()})
            with patch.object(bootstrapper, 'ROOT', root), patch.object(bootstrapper, 'LOCK', lock), \
                    patch.object(bootstrapper, 'verified_package', return_value=True), \
                    patch.object(bootstrapper, 'tool', side_effect=tools.__getitem__), \
                    patch.object(bootstrapper, 'run', return_value=types.SimpleNamespace(stdout=json.dumps(document()))), \
                    patch.object(bootstrapper.bootstrap, 'install_componentize_py') as install:
                with patch.object(sys, 'argv', args + ['--preload-module', 'logging', '--preload-module', 'html.parser']):
                    bootstrapper.main()
                with patch.object(sys, 'argv', args), self.assertRaisesRegex(ValueError, 'use --rebuild'):
                    bootstrapper.main()
                manifest['bootstrapInputs'] = {'removed.lock.json': 'old pin'}
                (destination / 'manifest.json').write_text(json.dumps(manifest))
                with patch.object(sys, 'argv', args + ['--preload-module', 'html.parser', '--preload-module', 'logging']), \
                        self.assertRaisesRegex(ValueError, 'use --rebuild'):
                    bootstrapper.main()
                install.assert_not_called()


class TemplateInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='python-inventory-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'main.py'
        self.source.write_text('def hello(name): return name\n')
        self.wit = self.root / 'library.wit'
        self.wit.write_text('placeholder WIT, parsed by the mocked guest tool\n')
        self.output = self.root / 'output.wasm'
        self.output.write_bytes(b'existing output')
        key, exports = contract(document(), 'library')
        self.package = self.root / 'dist/contracts' / key
        self.package.mkdir(parents=True)
        inventory = {}
        for name in ('template.component.wasm', 'python-packager.wasm', 'public-exports.wasm'):
            path = self.package / name
            path.write_bytes(b'fixture')
            inventory[name] = compiler.digest(path)
        self.manifest = {'schema': 1, 'contractSha256': key, 'exports': exports,
                         'componentizePy': compiler.LOCK['componentize_py'],
                         'sourceModules': 1, 'sha256': inventory}
        self.args = types.SimpleNamespace(entry='add', world='library', wit=self.wit,
                                          package_root=None, python_path=[], timeout=1)

    def reject(self, diagnostic):
        (self.package / 'manifest.json').write_text(json.dumps(self.manifest))
        with patch.object(compiler, 'ROOT', self.root), patch.object(compiler, 'guest_tools') as tools, \
                patch.object(compiler, 'run') as run:
            tools.command.return_value = ['runtime', 'run', 'mocked-wit']
            run.return_value = types.SimpleNamespace(stdout=json.dumps(document()))
            with self.assertRaisesRegex(ValueError, diagnostic):
                compiler.compile_contract(self.args, self.source, self.output)
            # No packager, validation invocation or output publication is allowed.
            tools.guest_command.assert_not_called()
            self.assertEqual(run.call_count, 1)
        self.assertEqual(self.output.read_bytes(), b'existing output')

    def test_changed_template_is_rejected_before_packaging(self):
        (self.package / 'template.component.wasm').write_bytes(b'tampered')
        self.reject('checksum mismatch: template.component.wasm')

    def test_incomplete_inventory_is_rejected_before_packaging(self):
        del self.manifest['sha256']['public-exports.wasm']
        self.reject('inventory is incomplete')

    def test_inventory_cannot_escape_template_directory(self):
        outside = self.package.parent / 'outside'
        outside.write_bytes(b'fixture')
        self.manifest['sha256']['../outside'] = compiler.digest(outside)
        self.reject('checksum mismatch: ../outside')

    def test_unknown_loader_version_is_rejected(self):
        self.manifest['sourceModules'] = 2
        self.reject('unsupported Python template source-loader version')

    def test_legacy_templates_require_rebuild_for_package_roots(self):
        del self.manifest['sourceModules']
        self.args.package_root = self.root
        self.reject('template lacks package imports')


if __name__ == '__main__':
    unittest.main()


class RuntimeConversionTests(unittest.TestCase):
    def test_nested_dataclasses_lists_options_and_enum_values(self):
        import dataclasses
        import enum
        import typing
        @dataclasses.dataclass
        class User:
            firstName: str
        @dataclasses.dataclass
        class Wire:
            first_name: str
        class UserColor(enum.Enum):
            DARK_BLUE = 'blue'
        class WireColor(enum.Enum):
            DARK_BLUE = 0
        runtime, _ = ContractTests().runtime('def greet(name): return name\ndef total(values): return sum(values)')
        convert = runtime['_convert']
        converted = convert([Wire('世界')], list[User])
        self.assertEqual(converted, [User('世界')])
        self.assertEqual(convert(User('x'), Wire), Wire('x'))
        self.assertIs(convert(WireColor.DARK_BLUE, UserColor), UserColor.DARK_BLUE)
        self.assertIs(convert(UserColor.DARK_BLUE, WireColor), WireColor.DARK_BLUE)
        self.assertIsNone(convert(None, typing.Optional[User]))
        self.assertEqual(convert([1, 2], bytes), b'\x01\x02')

    def test_dispatch_converts_plain_records_and_typed_enum_errors(self):
        import dataclasses
        import enum
        @dataclasses.dataclass
        class User:
            value: int
        @dataclasses.dataclass
        class Wire:
            value: int
        def implementation(value: User) -> User:
            self.assertIsInstance(value, User)
            return User(value.value + 1)
        def binding(value: Wire) -> Wire:
            pass
        runtime, _ = ContractTests().runtime('')
        runtime['_functions'] = {('Api', 'call'): implementation}
        runtime['BINDINGS'] = {('Api', 'call'): binding}
        self.assertEqual(runtime['_call']('Api', 'call', Wire(41)), Wire(42))
        UserStatus = enum.Enum('Status', {'FAILED': 'failed'})
        WireStatus = enum.Enum('Status', {'FAILED': 0})
        generated = types.ModuleType('wit_world.exports.api')
        generated.Status = WireStatus
        class Err(Exception):
            def __init__(self, value):
                self.value = value
        def failure(value: int) -> int:
            raise Err(UserStatus.FAILED)
        runtime['Err'] = Err
        runtime['_functions'][('Api', 'call')] = failure
        with patch.dict(binding.__globals__, {'_generated_test_types': generated}):
            with self.assertRaises(Err) as caught:
                runtime['_call']('Api', 'call', 1)
        self.assertIs(caught.exception.value, WireStatus.FAILED)

        runtime['ERROR_TYPES'][('Api', 'call')] = {'kind': {'list': {
            'name': 'status', 'kind': {'enum': {'cases': []}}}}}
        def failures(value: int) -> int:
            raise Err([UserStatus.FAILED])
        runtime['_functions'][('Api', 'call')] = failures
        with patch.dict(binding.__globals__, {'_generated_test_types': generated}):
            with self.assertRaises(Err) as caught:
                runtime['_call']('Api', 'call', 1)
        self.assertEqual(caught.exception.value, [WireStatus.FAILED])
