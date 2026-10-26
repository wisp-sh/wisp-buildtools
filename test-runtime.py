#!/usr/bin/env python3
"""Bounded release assembly and archive safety checks, without compiler builds."""
import copy
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent / 'tools'))
from assemble_runtime import REQUIRED, compiler_bundles, compiler_environment, npm_environment
from package_runtime import package
from runtime_format import extract_runtime


class RuntimeTests(unittest.TestCase):
    def prepared(self, root):
        prepared = root / 'prepared'
        (prepared / 'bin').mkdir(parents=True)
        for name in ('wisp', 'wisp-worker'):
            path = prepared / 'bin' / name
            path.write_bytes(b'#!/bin/sh\nexit 0\n')
            path.chmod(0o755)
        return prepared

    def test_packaging_requires_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = self.prepared(root)
            (prepared / 'bin/wisp-worker').unlink()
            with self.assertRaisesRegex(ValueError, 'wisp-worker'):
                package(prepared, root / 'runtime.tar.gz', {}, 'https://example.com/runtime.tar.gz')

    def test_archive_round_trip_preserves_file_links_modes_and_empty_caches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = self.prepared(root)
            (prepared / 'bin/alias').symlink_to('wisp')
            (prepared / 'cache').mkdir()
            archive = root / 'runtime.tar.gz'
            catalog = package(prepared, archive, {'CARGO_HOME': 'cache'}, 'https://example.com/runtime.tar.gz')
            self.assertEqual(catalog['runtime']['size'], archive.stat().st_size)
            installed = root / 'different-prefix'
            installed.mkdir()
            extract_runtime(archive, installed)
            self.assertTrue((installed / 'cache').is_dir())
            self.assertEqual((installed / 'bin/alias').read_bytes(), (prepared / 'bin/wisp').read_bytes())
            self.assertTrue(os.access(installed / 'bin/wisp-worker', os.X_OK))

    def test_packaging_rejects_external_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = self.prepared(root)
            (root / 'private').write_text('private')
            (prepared / 'escape').symlink_to(root / 'private')
            with self.assertRaisesRegex(ValueError, 'inside'):
                package(prepared, root / 'runtime.tar.gz', {}, 'https://example.com/runtime.tar.gz')

    def test_authenticated_npm_cache_survives_runtime_packaging_and_relocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = self.prepared(root)
            def install(argv, **kwargs):
                self.assertIn('--ignore-scripts', argv)
                cache = Path(argv[argv.index('--cache') + 1]) / '_cacache/content-v2/sha512/archive'
                cache.parent.mkdir(parents=True)
                cache.write_bytes(b'authenticated npm package archive')
            with patch('assemble_runtime.subprocess.run', side_effect=install):
                exports = npm_environment(prepared, {})
            archive = root / 'runtime.tar.gz'
            catalog = package(prepared, archive, exports, 'https://example.com/runtime.tar.gz')
            installed = root / 'relocated'
            installed.mkdir()
            extract_runtime(archive, installed)
            cache = installed / catalog['env']['WISP_NPM_CACHE']
            self.assertEqual((cache / '_cacache/content-v2/sha512/archive').read_bytes(),
                             b'authenticated npm package archive')

    def test_native_kit_with_its_own_npm_cache_needs_no_extra_install(self):
        with patch('assemble_runtime.subprocess.run') as install:
            exports = {'WISP_NPM_CACHE': 'npm-cache'}
            self.assertEqual(npm_environment(Path('/kit'), exports), exports)
            install.assert_not_called()

    def manifest(self):
        return {'schema': 2, 'platform': 'linux-x86_64', 'bundles': [
            {'tool': tool, 'platform': 'wasm32-wasi', 'version': '0.1.0', 'size': 10, 'requires': []}
            for tool in sorted(REQUIRED)]}

    def test_assembly_requires_five_compatible_compilers_and_dependency_closure(self):
        value = self.manifest()
        self.assertEqual({entry['tool'] for entry in compiler_bundles(value)}, REQUIRED)
        value['bundles'][0]['requires'] = ['wisp:missing']
        with self.assertRaisesRegex(ValueError, 'missing'):
            compiler_bundles(value)
        for mutation in ('missing', 'other-platform', 'duplicate'):
            value = self.manifest()
            if mutation == 'missing':
                value['bundles'].pop()
            elif mutation == 'other-platform':
                value['bundles'][0]['platform'] = 'darwin-arm64'
            else:
                value['bundles'].append(copy.deepcopy(value['bundles'][0]))
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                compiler_bundles(value)

    def test_linux_releases_can_include_native_precompiled_companions(self):
        value = self.manifest()
        for entry in value['bundles']:
            entry['platform'] = 'linux-x86_64'
        self.assertEqual({entry['tool'] for entry in compiler_bundles(value)}, REQUIRED)

    def test_environment_paths_are_relocated_without_evaluating_shell(self):
        prefix = Path('/release/runtime')
        self.assertEqual(compiler_environment("export WISP_CLANG='/release/runtime/toolchains/clang'\n", prefix),
                         {'WISP_CLANG': 'toolchains/clang'})
        for text in ("export WISP_CLANG=/tmp/clang", "echo secret", "export WISP_CLANG=/release/runtime/../escape"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                compiler_environment(text, prefix)


if __name__ == '__main__':
    unittest.main()
