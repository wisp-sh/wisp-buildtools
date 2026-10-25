"""Package compiler provisioning must carry each dependency's environment forward."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('package_ci', ROOT / 'tools/ci-package.py')
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


class PackageCompilerCI(unittest.TestCase):
    def test_go_application_host_is_provisioned_after_bootstrap_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'buildtools.json').write_text('{}')
            host = root / '.ci/hosts/wisp-wasmtime-50.0.0/wasmtime'
            events, exports = [], []
            def install(tool):
                events.append(tool)
                return {'environment': {'WISP_WASMTIME': '/host/wasmtime-50.0.0'}, 'paths': []}
            def execution_host(recipe, prefix, cache):
                events.append('wisp-host')
                self.assertEqual(recipe, 'build.py')
                self.assertEqual(prefix, host.parent)
                self.assertEqual(cache, root / '.ci/host-build')
                prefix.mkdir(parents=True)
                host.touch()
            prerequisites = {
                'check_lock': lambda: None, 'install_for': install,
                'execution_host': execution_host,
                'LOCK': {'downloads': {'wasmtime': {'executable': 'bin/wasmtime'}}},
                'github_environment': lambda env, paths: exports.append(env),
            }
            with patch.object(ci, 'ROOT', root), patch.object(ci, 'load_driver', return_value={
                    'dependency_order': lambda manifest, names: ['go', 'wasm-tools']}), \
                 patch.object(ci.runpy, 'run_path', return_value=prerequisites), \
                 patch.object(ci.subprocess, 'run'), patch.dict(os.environ):
                ci.provision('go', root / 'recipes')
            self.assertEqual(events, ['go', 'wasm-tools', 'wisp-host'])
            self.assertEqual(exports[-1]['WISP_WASMTIME'], str(root / 'cwasm.py'))
            self.assertEqual(exports[-1]['WISP_CWASM_RUNTIME'], str(host))
            self.assertEqual(exports[-1]['WISP_RUST_WASMTIME'], str(root / 'cwasm.py'))
            self.assertEqual(exports[-1]['WISP_BOOTSTRAP_WASMTIME'], str(host))

    def test_driver_import_from_an_unrelated_checkout(self):
        script = (
            'import runpy; '
            f'ci = runpy.run_path({str(ROOT / "tools/ci-package.py")!r}); '
            'driver = ci["load_driver"](); '
            'manifest = driver["load_manifest"](); '
            'assert driver["dependency_order"](manifest, ["go"]) == ["go"]'
        )
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run([sys.executable, '-I', '-c', script], cwd=directory, check=True)

    def test_dependency_plans_cover_runtime_backends(self):
        import runpy
        driver = runpy.run_path(str(ROOT / 'build.py'))
        manifest = driver['load_manifest']()
        order = driver['dependency_order'](manifest, ci.targets('rust'))
        self.assertLess(order.index('clang'), order.index('llvm'))
        self.assertLess(order.index('llvm'), order.index('rust'))
        self.assertIn('clang', driver['dependency_order'](manifest, ci.targets('python')))
        self.assertIn('llvm-swift', driver['dependency_order'](manifest, ci.targets('swift')))
        with self.assertRaises(ValueError): ci.targets('../unknown')

    def test_prerequisite_environment_reaches_the_build_and_custom_host_survives(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'buildtools.json').write_text('{}')
            host = root / '.ci/hosts/wisp-wasmtime-50.0.0/wasmtime'
            host.parent.mkdir(parents=True)
            host.touch()
            builds, exports = [], []
            def install(tool):
                return {'environment': {'WISP_TEST_TOOL': tool, 'WISP_WASMTIME': '/stock/wasmtime'},
                        'paths': ['/bootstrap/' + tool]}
            def execute(argv, **kwargs):
                builds.append((argv, os.environ['WISP_TEST_TOOL'], os.environ['PATH'].split(os.pathsep)[0]))
            prerequisites = {'check_lock': lambda: None, 'install_for': install,
                             'LOCK': {'downloads': {'wasmtime': {'executable': 'bin/wasmtime'}}},
                             'github_environment': lambda env, paths: exports.append(env)}
            def recipe(path):
                return prerequisites if Path(path).name == 'ci-prerequisites.py' else {
                    'dependency_order': lambda manifest, names: ['clang', 'wasm-tools']}
            with patch.object(ci, 'ROOT', root), patch.object(ci.runpy, 'run_path', side_effect=recipe), \
                 patch.object(ci.subprocess, 'run', side_effect=execute), patch.dict(os.environ):
                ci.provision('clang', root / 'recipes')
            self.assertEqual([value[1] for value in builds], ['clang', 'wasm-tools'])
            self.assertEqual([value[2] for value in builds], ['/bootstrap/clang', '/bootstrap/wasm-tools'])
            self.assertEqual(exports[-1]['WISP_WASMTIME'], str(root / 'cwasm.py'))
            self.assertEqual(exports[-1]['WISP_CWASM_RUNTIME'], str(host))
            self.assertEqual(exports[-1]['WISP_PREVIEW1_ADAPTER'], str(root / 'wasm-tools/dist/wasi_snapshot_preview1.reactor.wasm'))
            self.assertEqual(exports[-1]['WISP_BOOTSTRAP_WASMTIME'], str(host))


if __name__ == '__main__':
    unittest.main()
