"""Offline dependency tests use tiny synthetic wheels, never package execution."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from scripts.common import digest, LOCK
from scripts.dependencies import prepare, satisfies


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.packages = []

    def wheel(self, name, dependencies=(), *, native=False, extra=None):
        path = self.project / f'{name}-1.2-py3-none-any.whl'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr(f'{name}/__init__.py', 'raise RuntimeError("host must not import me")\n')
            archive.writestr(f'{name}-1.2.dist-info/WHEEL', f'Wheel-Version: 1.0\nRoot-Is-Purelib: {str(not native).lower()}\nTag: py3-none-any\n')
            archive.writestr(f'{name}-1.2.dist-info/METADATA', f'Name: {name}\nVersion: 1.2\n' + ''.join('Requires-Dist: ' + value + '\n' for value in dependencies))
            if extra:
                archive.writestr(extra, b'payload')
        self.packages.append({'name': name, 'version': '1.2', 'path': path.name, 'sha256': digest(path)})
        return path

    def run_prepare(self, requirement='one==1.2'):
        (self.project / 'requirements.txt').write_text(requirement + '\n')
        (self.project / 'python-deps.lock.json').write_text(json.dumps({'schema': 1, 'packages': self.packages}))
        return prepare(self.project, self.root / 'extracted')

    def test_transitive_wheels_are_selected_without_importing_them(self):
        self.wheel('one', ['two>=1.0,<2.0'])
        self.wheel('two')
        roots, details = self.run_prepare()
        self.assertEqual([p['name'] for p in details['packages']], ['one', 'two'])
        self.assertEqual(len(roots), 2)
        self.assertTrue((roots[0] / 'one/__init__.py').is_file())

    def test_static_typing_marker_does_not_require_runtime_package_data(self):
        self.wheel('one', extra='one/py.typed')
        roots, _ = self.run_prepare()
        self.assertEqual([p.name for p in (roots[0] / 'one').iterdir()], ['__init__.py'])

    def test_tampered_wheel_is_rejected(self):
        path = self.wheel('one')
        path.write_bytes(path.read_bytes() + b'tampered')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self.run_prepare()

    def test_native_wheel_and_traversal_and_package_data_fail_closed(self):
        for options, message in [({'native': True}, 'native Python extensions'),
                                 ({'extra': '../escape.py'}, 'unsafe Python wheel'),
                                 ({'extra': 'one/native.so'}, 'native Python extensions')]:
            with self.subTest(options=options):
                self.packages.clear()
                self.wheel('one', **options)
                with self.assertRaisesRegex(ValueError, message):
                    self.run_prepare()
                import shutil
                shutil.rmtree(self.root / 'extracted')

    def test_unlocked_transitive_dependency_and_constraints_are_rejected(self):
        self.wheel('one', ['two>=2.0'])
        self.wheel('two')
        with self.assertRaisesRegex(ValueError, 'does not satisfy'):
            self.run_prepare()

    def test_pyproject_ranges_still_require_an_artifact_lock(self):
        (self.project / 'pyproject.toml').write_text('[project]\ndependencies=["one>=1"]\n')
        with self.assertRaisesRegex(ValueError, 'python-deps.lock.json'):
            prepare(self.project, self.root / 'extracted')

    def test_vendor_inventory_is_checked_and_bundled(self):
        vendor = self.project / 'vendor'
        vendor.mkdir()
        (vendor / 'one.py').write_text('value = 42\n')
        files = {'one.py': digest(vendor / 'one.py')}
        sha = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.packages.append({'name': 'one', 'version': '1.2', 'path': 'vendor', 'files': files, 'sha256': sha})
        roots, _ = self.run_prepare()
        self.assertEqual((roots[0] / 'one.py').read_text(), 'value = 42\n')

    def test_verified_scratch_under_project_output_is_bundled_once(self):
        from scripts.source_bundle import bundle
        self.wheel('one')
        source = self.project / 'main.py'
        source.write_text('import one\ndef api() -> int: return 42\n')
        (self.project / 'requirements.txt').write_text('one==1.2\n')
        (self.project / 'python-deps.lock.json').write_text(json.dumps({'schema': 1, 'packages': self.packages}))
        roots, _ = prepare(self.project, self.project / 'build/deps')
        _, details = bundle(source, self.project, roots, excluded_paths=roots)
        self.assertEqual(details['moduleCount'], 2)

    def test_version_constraints(self):
        self.assertTrue(satisfies('1.2.3', '>=1.0,<2.0,!=1.3'))
        self.assertTrue(satisfies('1.2.3', '~=1.2'))
        self.assertFalse(satisfies('2.0', '~=1.2'))
        self.assertTrue(satisfies('1.2.3', '==1.2.*'))
        self.assertFalse(satisfies('1.2.3', '!=1.2.*'))
        self.assertTrue(satisfies('1.0.0.0.0.0.0.0.0', '==1'))
        self.assertTrue(satisfies('1', '==1.0.*'))
        self.assertFalse(satisfies('1', '!=1.0.*'))
        with self.assertRaises(ValueError):
            satisfies('2.0', '~=1')


class MetadataTests(unittest.TestCase):
    setUp = DependencyTests.setUp
    wheel = DependencyTests.wheel
    run_prepare = DependencyTests.run_prepare

    def test_vendored_metadata_parser_matches_the_pinned_inventory(self):
        vendor = Path(__file__).parent / 'scripts/_vendor'
        manifest = json.loads((vendor / 'manifest.json').read_text())
        self.assertEqual(manifest['packaging'], LOCK['packaging'])
        for path, sha in manifest['sha256'].items():
            self.assertEqual(digest(vendor / path), sha, path)

    def test_requirement_includes_constraints_and_inactive_hashes(self):
        self.wheel('one')
        sha = self.packages[0]['sha256']
        (self.project / 'shared.txt').write_text('one>=1,<2\n')
        (self.project / 'requirements.txt').write_text(
            '-c shared.txt\n-r shared.txt\n'
            + 'one; sys_platform == "win32" --hash=sha256:' + '0' * 64 + '\n'
            + 'one; sys_platform == "wasi" \\\n  --hash=sha256:' + sha + '\n')
        (self.project / 'python-deps.lock.json').write_text(json.dumps({'schema': 1, 'packages': self.packages}))
        roots, receipt = prepare(self.project, self.root / 'extracted')
        self.assertEqual(len(roots), 1)
        self.assertIn('shared.txt', receipt['inputs'])

    def test_direct_url_is_only_an_offline_identity_and_checks_its_hash(self):
        self.wheel('one')
        url = 'https://invalid.example/one.whl#sha256=' + self.packages[0]['sha256']
        self.packages[0]['url'] = url
        roots, _ = self.run_prepare('one @ ' + url)
        self.assertEqual(len(roots), 1)
        import shutil
        shutil.rmtree(self.root / 'extracted')
        bad = url.split('#')[0] + '#sha256=' + '0' * 64
        self.packages[0]['url'] = bad
        with self.assertRaisesRegex(ValueError, 'URL hash differs'):
            self.run_prepare('one>=1\none @ ' + bad)
    def test_markers_are_evaluated_for_wasi_and_optional_extras_are_not_installed(self):
        self.wheel('one', ['missing-win; sys_platform == "win32"', 'missing-opt; extra == "dev"', 'two>=1; python_version >= "3.14"'])
        self.wheel('two')
        _, receipt = self.run_prepare()
        self.assertEqual([p['name'] for p in receipt['packages']], ['one', 'two'])

    def test_late_extras_expand_a_previously_selected_dependency(self):
        self.wheel('one', ['two', 'two[feature]'])
        wheel = self.wheel('two', ['three; extra == "feature"'])
        with zipfile.ZipFile(wheel, 'a') as archive:
            archive.writestr('two-1.2.dist-info/EXTRA', 'ignored data')
        # Rebuild this tiny synthetic wheel to add a Provides-Extra header.
        with zipfile.ZipFile(wheel) as archive:
            contents = {p.filename: archive.read(p) for p in archive.infolist()}
        contents['two-1.2.dist-info/METADATA'] += b'Provides-Extra: feature\n'
        with zipfile.ZipFile(wheel, 'w') as archive:
            for name, data in contents.items(): archive.writestr(name, data)
        self.packages[-1]['sha256'] = digest(wheel)
        self.wheel('three')
        _, receipt = self.run_prepare()
        self.assertEqual([p['name'] for p in receipt['packages']], ['one', 'three', 'two'])
        self.assertEqual(receipt['packages'][-1]['extras'], ['feature'])

    def test_project_version_ranges_select_exact_locked_artifacts(self):
        self.wheel('one')
        (self.project/'pyproject.toml').write_text('[project]\ndependencies=["one>=1,<2"]\n')
        (self.project/'python-deps.lock.json').write_text(json.dumps({'schema':1,'packages':self.packages}))
        roots, _ = prepare(self.project, self.root/'extracted')
        self.assertEqual(len(roots), 1)

    def test_data_files_are_extracted_and_hashed_with_the_wheel(self):
        self.wheel('one', extra='one/data.json')
        roots, receipt = self.run_prepare()
        self.assertEqual((roots[0]/'one/data.json').read_bytes(), b'payload')
        self.assertIn(self.packages[0]['path'], receipt['inputs'])

    def test_version_semantics_include_prereleases_epochs_and_local_versions(self):
        self.assertTrue(satisfies('2.0rc1', '==2.0rc1'))
        self.assertTrue(satisfies('1!2.0+vendor.1', '>=1!2.0'))
        self.assertFalse(satisfies('1.9', '>=1!1.0'))

if __name__ == '__main__':
    unittest.main()
