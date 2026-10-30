"""Source-bundle checks using host Python only as a guest-runtime test double."""
from contextlib import contextmanager
import json
import builtins
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.source_bundle import bundle

ROOT = Path(__file__).resolve().parent


class SourceBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='python-bundle-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / 'project'
        self.project.mkdir()

    def write(self, name, text, root=None):
        path = (root or self.project) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    @contextmanager
    def execute(self, payload):
        with patch.dict(sys.modules), patch.object(sys, 'meta_path', list(sys.meta_path)), \
                patch.object(builtins, 'open', builtins.open), patch.object(io, 'open', io.open), \
                patch.object(os, 'stat', os.stat), patch.object(os, 'lstat', os.lstat), \
                patch.object(os, 'listdir', os.listdir), patch.object(os, 'scandir', os.scandir), \
                patch.object(os, 'access', os.access):
            namespace = {}
            exec(compile(payload, '<bundle-test>', 'exec'), namespace)
            yield namespace

    def test_regular_packages_relative_imports_and_entry_identity(self):
        self.write('samplepkg/__init__.py', 'initialized = 1\n')
        self.write('samplepkg/tools.py', 'def greet(name): return "Hello, " + name\n')
        entry = self.write('samplepkg/api.py', 'from .tools import greet\nfrom . import initialized\n'
                           'import samplepkg.api as myself\ndef answer(): return myself is __import__(__name__, fromlist=["answer"])\n')
        payload, receipt = bundle(entry, self.project)
        with self.execute(payload) as module:
            self.assertEqual(module['greet']('世界'), 'Hello, 世界')
            self.assertTrue(module['answer']())
            self.assertEqual(module['__name__'], 'samplepkg.api')
            self.assertEqual(module['__package__'], 'samplepkg')
            self.assertEqual(module['initialized'], 1)
        self.assertEqual(receipt['entryModule'], 'samplepkg.api')
        self.assertEqual(len(receipt['inputs']), 3)
        self.assertNotIn(str(self.root).encode(), payload)

    def test_lazy_imports_circular_imports_and_cached_state(self):
        entry = self.write('app.py', 'def answer():\n from helper import increment\n return increment()\n')
        self.write('helper.py', 'import app\ncalls = 0\ndef increment():\n global calls\n calls += 1\n return calls\n')
        payload, _ = bundle(entry)
        with self.execute(payload) as module:
            self.assertEqual(module['answer'](), 1)
            self.assertEqual(module['answer'](), 2)
            self.assertIs(sys.modules['helper'].app, sys.modules['app'])

    def test_namespace_packages_across_vendored_roots(self):
        vendor = self.root / 'vendor'
        vendor.mkdir()
        entry = self.write('main.py', 'from team.one import left\nfrom team.two import right\nanswer = left + right\n')
        self.write('team/one.py', 'left = 20\n')
        self.write('team/two.py', 'right = 22\n', root=vendor)
        payload, receipt = bundle(entry, self.project, [vendor])
        with self.execute(payload) as module:
            self.assertEqual(module['answer'], 42)
            self.assertEqual(sys.modules['team'].__package__, 'team')
            self.assertTrue(sys.modules['team'].__spec__.submodule_search_locations)
        self.assertIn('python-path-1/team/two.py', receipt['inputs'])

    def test_entry_dataclasses_and_dynamic_imports(self):
        entry = self.write('api.py', 'from dataclasses import dataclass\nimport importlib\n'
                           '@dataclass\nclass Item:\n value: int\n'
                           'def answer(): return importlib.import_module("helper").make(Item).value\n')
        self.write('helper.py', 'def make(kind): return kind(42)\n')
        payload, _ = bundle(entry)
        with self.execute(payload) as module:
            self.assertEqual(module['answer'](), 42)
            self.assertEqual(sys.modules['api'].Item.__module__, 'api')

    def test_bundle_does_not_execute_source_on_host(self):
        entry = self.write('main.py', 'raise RuntimeError("user code executed")\n')
        payload, _ = bundle(entry)
        self.assertIn(b'user code executed', payload)

    def test_dependency_changes_affect_receipt_and_output_deterministically(self):
        entry = self.write('main.py', 'from helper import value\n')
        self.write('helper.py', 'value = 41\n')
        original, receipt = bundle(entry)
        self.assertEqual((original, receipt), bundle(entry))
        self.write('helper.py', 'value = 42\n')
        changed, changed_receipt = bundle(entry)
        self.assertNotEqual(original, changed)
        self.assertNotEqual(receipt['inputs']['project/helper.py'], changed_receipt['inputs']['project/helper.py'])

    def test_ignores_caches_build_outputs_and_metadata(self):
        entry = self.write('main.py', 'value = 42\n')
        self.write('__pycache__/bad.py', 'not source')
        self.write('build/bad.py', 'not source')
        self.write('vendor-1.dist-info/test.py', 'not source')
        self.write('.hidden/bad.py', 'not source')
        _, receipt = bundle(entry)
        self.assertEqual(set(receipt['inputs']), {'project/main.py'})

    def test_rejects_native_extensions_and_symlinks(self):
        entry = self.write('main.py', 'value = 42\n')
        native = self.write('extension.cpython-314.so', 'native')
        with self.assertRaisesRegex(ValueError, 'native Python extensions'):
            bundle(entry)
        native.unlink()
        (self.project / 'link.py').symlink_to(entry)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            bundle(entry)

    def test_rejects_module_collisions_and_overlapping_roots(self):
        entry = self.write('main.py', 'value = 42\n')
        with self.assertRaisesRegex(ValueError, 'overlap'):
            bundle(entry, self.project, [self.project])
        self.write('helpers.py', '')
        self.write('helpers/__init__.py', '')
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            bundle(entry)

    def test_rejects_reserved_and_preinitialized_names(self):
        entry = self.write('main.py', '')
        reserved = self.write('wit_world.py', '')
        with self.assertRaisesRegex(ValueError, 'reserved'):
            bundle(entry)
        reserved.unlink()
        self.write('json.py', '')
        payload, _ = bundle(entry)
        with self.assertRaisesRegex(ImportError, 'preinitialized runtime'):
            with self.execute(payload):
                pass

    def test_failed_import_rolls_back_finder_and_loaded_modules(self):
        entry = self.write('main.py', 'import helper\nraise RuntimeError("broken")\n')
        self.write('helper.py', 'value = 42\n')
        payload, _ = bundle(entry)
        with patch.dict(sys.modules), patch.object(sys, 'meta_path', list(sys.meta_path)):
            before = list(sys.meta_path)
            with self.assertRaisesRegex(RuntimeError, 'broken'):
                exec(compile(payload, '<bundle-test>', 'exec'), {})
            self.assertEqual(sys.meta_path, before)
            self.assertNotIn('helper', sys.modules)
            self.assertNotIn('main', sys.modules)

    def test_source_and_emitted_bundle_limits(self):
        # Exercise both boundaries without allocating MiB-sized fixtures.
        with patch('scripts.source_bundle.MAX_SOURCE_BYTES', 4096), patch('scripts.source_bundle.MAX_PROGRAM_BYTES', 4096):
            entry = self.write('main.py', 'x' * 4097)
            with self.assertRaisesRegex(ValueError, 'aggregate source limit'):
                bundle(entry)
            entry.write_text('#' + 'x' * 4095)
            with self.assertRaisesRegex(ValueError, 'packaged-source limit'):
                bundle(entry)

    def test_regular_packages_cannot_merge_across_search_roots(self):
        entry = self.write('main.py', '')
        self.write('shared/one.py', '')
        vendor = self.root / 'vendor'
        vendor.mkdir()
        self.write('shared/__init__.py', '', root=vendor)
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            bundle(entry, self.project, [vendor])

    def test_entry_must_be_inside_selected_root(self):
        entry = self.write('main.py', '')
        other = self.root / 'other'
        other.mkdir()
        with self.assertRaisesRegex(ValueError, 'inside --package-root'):
            bundle(entry, other)


class ResourceTests(unittest.TestCase):
    setUp = SourceBundleTests.setUp
    write = SourceBundleTests.write
    execute = SourceBundleTests.execute
    def test_resources_pkgutil_pathlib_and_read_only_file_access(self):
        self.write('resource_pkg/__init__.py', '')
        self.write('resource_pkg/assets/schema.json', '{"answer":42}')
        entry = self.write('main.py', '''from importlib.resources import files, as_file
from pathlib import Path
import pkgutil
import resource_pkg
def answer():
    resource = files(resource_pkg) / 'assets' / 'schema.json'
    assert resource.is_file()
    assert [p.name for p in resource.parent.iterdir()] == ['schema.json']
    assert pkgutil.get_data('resource_pkg', 'assets/schema.json') == b'{"answer":42}'
    with as_file(resource) as path:
        assert path.resolve(strict=True).read_text() == '{"answer":42}'
    return Path(resource_pkg.__file__).parent.joinpath('assets/schema.json').read_bytes()
''')
        payload, details = bundle(entry)
        with self.execute(payload) as module:
            self.assertEqual(module['answer'](), b'{"answer":42}')
            with self.assertRaises(PermissionError):
                open('/__wisp__/resource_pkg/assets/schema.json', 'w')
        self.assertEqual(details['dataFileCount'], 1)

    def test_wheel_distribution_metadata_is_available_without_host_installation(self):
        self.write('demo_pkg/__init__.py', '')
        self.write('demo_pkg-1.2.dist-info/METADATA', 'Name: demo-pkg\nVersion: 1.2\n')
        entry = self.write('main.py', 'from importlib.metadata import version\ndef api(): return version("demo-pkg")')
        payload, _ = bundle(entry)
        with self.execute(payload) as module:
            self.assertEqual(module['api'](), '1.2')

    def test_namespace_package_resources_are_available(self):
        entry = self.write('main.py', 'from importlib.resources import files\nimport team\ndef api(): return (files(team)/"data.txt").read_text()')
        self.write('team/one.py', '')
        self.write('team/data.txt', 'namespace data')
        payload, _ = bundle(entry)
        with self.execute(payload) as module:
            self.assertEqual(module['api'](), 'namespace data')

    def test_bundle_larger_than_old_limit_is_supported(self):
        entry = self.write('main.py', 'def api(): return 42\n')
        self.write('data.bin', 'x' * (1024 * 1024 + 17))
        payload, details = bundle(entry)
        self.assertGreater(details['sourceBytes'], 1024 * 1024)
        self.assertGreater(len(payload), 1024 * 1024)

if __name__ == '__main__':
    unittest.main()
