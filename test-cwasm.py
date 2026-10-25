#!/usr/bin/env python3
"""Check AOT cache invalidation and CLI argument handling without large builds."""
import json
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import cwasm

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('compiler_builds', ROOT / 'build.py')
builds = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builds)


class CwasmTests(unittest.TestCase):
    def setUp(self):
        (ROOT / 'build').mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / 'build', prefix='cwasm-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'guest.wasm'
        self.source.write_bytes(b'\0asm\x01\0\0\0')
        self.runtime = self.root / 'wasmtime'
        self.runtime.write_bytes(b'runtime')
        self.cache = self.root / 'cache'
        self.log = mock.patch('sys.stderr', new_callable=io.StringIO)
        self.log.start()
        self.addCleanup(self.log.stop)

    def compile(self, source=None, flags=()):
        return cwasm.precompile(source or self.source, runtime=self.runtime, flags=flags, cache=self.cache)

    @staticmethod
    def fake_compile(command, **kwargs):
        Path(command[command.index('-o') + 1]).write_bytes(b'native-code')

    def test_cache_reuse_and_source_runtime_configuration_invalidation(self):
        with mock.patch.object(cwasm.subprocess, 'run', side_effect=self.fake_compile) as run:
            first = self.compile()
            self.assertEqual(first, self.compile())
            self.assertEqual(run.call_count, 1)
            self.source.write_bytes(self.source.read_bytes() + b'changed')
            self.assertNotEqual(first, self.compile())
            self.runtime.write_bytes(b'new-runtime')
            self.assertNotEqual(first, self.compile())
            self.assertNotEqual(first, self.compile(flags=['-O', 'opt-level=2']))
            self.assertEqual(run.call_count, 4)
            self.assertIn('parallel-compilation=n', run.call_args.args[0])
            self.assertEqual(run.call_args.kwargs['env']['RAYON_NUM_THREADS'], '1')

    def test_corrupt_native_code_is_recompiled(self):
        with mock.patch.object(cwasm.subprocess, 'run', side_effect=self.fake_compile) as run:
            output = self.compile()
            output.write_bytes(b'corrupted')
            self.compile()
            self.assertEqual(run.call_count, 2)
            self.assertEqual(output.read_bytes(), b'native-code')

    def test_corrupt_receipt_is_recompiled(self):
        with mock.patch.object(cwasm.subprocess, 'run', side_effect=self.fake_compile) as run:
            output = self.compile()
            output.with_name('receipt.json').write_text('incomplete')
            self.compile()
            self.assertEqual(run.call_count, 2)

    def test_worker_uses_its_own_engine_and_command(self):
        with mock.patch.object(cwasm.subprocess, 'run', side_effect=self.fake_compile) as run:
            output = cwasm.precompile(self.source, runtime=self.runtime, cache=self.cache, worker=True)
            self.assertEqual(run.call_args.args[0][:3], [str(self.runtime), 'precompile', str(self.source)])
        self.assertEqual(json.loads(output.with_name('receipt.json').read_text())['identity']['backend'], 'worker')
        with self.assertRaisesRegex(ValueError, 'fixed engine'):
            cwasm.precompile(self.source, runtime=self.runtime, cache=self.cache, worker=True, flags=['-Oopt-level=2'])

    def test_publication_contains_native_artifacts_and_runtime_identity(self):
        source = self.root / 'compiler/dist/compiler.wasm'
        source.parent.mkdir(parents=True)
        source.write_bytes(self.source.read_bytes())
        output = self.root / 'published'
        document = {'tools': {'compiler': {'artifacts': ['compiler/dist/compiler.wasm'],
                     'runtimeDependencies': [], 'package': {'include': ['compiler/dist']}}}}
        with mock.patch.object(cwasm.subprocess, 'run', side_effect=self.fake_compile):
            builds.precompile_tools(document, ['compiler'], root=self.root, runtime=self.runtime,
                                    cache=self.cache, output_dir=output)
        manifest = json.loads((output / 'cwasm-manifest.json').read_text())
        artifact = manifest['artifacts'][0]
        self.assertEqual((output / artifact['path']).read_bytes(), b'native-code')
        self.assertEqual(artifact['identity']['runtimeSha256'], cwasm.digest(self.runtime))
        self.assertEqual(artifact['format'], 'cwasm')
        self.assertNotIn('cachedPath', artifact)
        self.assertFalse(list(output.rglob('*.wasm')))
        with mock.patch.object(cwasm.subprocess, 'run', side_effect=self.fake_compile):
            with self.assertRaisesRegex(ValueError, 'must be absent'):
                builds.precompile_tools(document, ['compiler'], root=self.root, runtime=self.runtime,
                                        cache=self.cache, output_dir=output)

    def test_failed_compile_is_not_published(self):
        with mock.patch.object(cwasm.subprocess, 'run', side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError):
                self.compile()
        self.assertEqual(list(self.cache.rglob('module.cwasm')), [])
        self.assertEqual(list(self.cache.rglob('.compile-*')), [])

    def test_host_changed_during_compilation_is_not_published(self):
        def changed(command, **kwargs):
            self.fake_compile(command, **kwargs)
            self.runtime.write_bytes(b'updated-runtime')
        with mock.patch.object(cwasm.subprocess, 'run', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'host changed'):
                self.compile()
        self.assertEqual(list(self.cache.rglob('module.cwasm')), [])

    def test_python_embedding_reuses_serialized_code_and_invalidates_configuration(self):
        self.cache.mkdir(mode=0o700)
        module = SimpleNamespace(serialize=mock.Mock(return_value=b'\x7fELFnative'))
        api = SimpleNamespace(from_file=mock.Mock(return_value=module),
                              deserialize_file=mock.Mock(return_value='loaded-native'))
        binding = SimpleNamespace(_ffi=SimpleNamespace(dll=SimpleNamespace(_name=str(self.runtime))), Module=api)
        with mock.patch.object(cwasm, 'cache_directory', return_value=self.cache):
            for _ in range(2):
                result = cwasm.load_module(binding, 'engine', self.source, configuration={'stack': 4})
                self.assertEqual(result, 'loaded-native')
            self.assertEqual(api.from_file.call_count, 1)
            self.assertEqual(api.deserialize_file.call_count, 2)
            cwasm.load_module(binding, 'new-engine', self.source, configuration={'stack': 8})
            self.assertEqual(api.from_file.call_count, 2)

    def test_swift_provider_uses_its_package_namespace(self):
        source = self.root / 'llvm/dist/swift-shared/backend.wasm'
        source.parent.mkdir(parents=True)
        source.write_bytes(self.source.read_bytes())
        native = self.root / 'precompiled/llvm-swift/backend.cwasm'
        native.parent.mkdir(parents=True)
        native.write_bytes(b'native-code')
        identity = {'sourceSha256': cwasm.digest(source)}
        native.with_name('manifest.json').write_text(json.dumps({'artifacts': [{
            'source': 'llvm/dist/swift-shared/backend.wasm',
            'path': 'precompiled/llvm-swift/backend.cwasm', 'identity': identity,
            'sha256': cwasm.digest(native)}]}))
        with mock.patch.object(cwasm, 'ROOT', self.root):
            self.assertEqual(cwasm.packaged(source, identity), native)
            native.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                cwasm.packaged(source, identity)

    def test_registered_package_includes_native_companions_by_default(self):
        source = self.root / 'clang/dist/compiler.wasm'
        source.parent.mkdir(parents=True)
        source.write_bytes(self.source.read_bytes())
        (self.root / 'cwasm.py').write_text('# native launcher\n')
        native = self.root / 'precompiled/clang/compiler.cwasm'
        native.parent.mkdir(parents=True)
        native.write_bytes(b'\x7fELFnative')
        manifest = native.with_name('manifest.json')
        manifest.write_text(json.dumps({'artifacts': [{'source': 'clang/dist/compiler.wasm',
                            'path': 'precompiled/clang/compiler.cwasm'}]}))
        document = {'tools': {'clang': {'version': '1.0.0', 'runtimeDependencies': [],
                    'bootstrapRequirements': [], 'artifacts': ['clang/dist/compiler.wasm'],
                    'package': {'include': ['clang/dist']}}}}
        companions = {p.relative_to(self.root).as_posix(): p for p in (native, manifest)}
        with mock.patch.object(builds, 'prepare_tool', return_value=companions) as prepare:
            record = builds.package_tool(document, 'clang', root=self.root, output_dir=self.root / 'packages')
        prepare.assert_called_once()
        release = builds.packages.read_manifest(Path(record['archive']))
        self.assertEqual(release['platform'], 'linux-x86_64')
        self.assertEqual(release['archive']['inventory']['precompiled/clang/compiler.cwasm']['format'], 'native')
        self.assertIn('cwasm.py', release['archive']['inventory'])

    def test_native_code_and_shared_cache_are_rejected(self):
        self.source.write_bytes(b'\x7fELFuntrusted')
        with self.assertRaisesRegex(ValueError, 'binary Wasm'):
            self.compile()
        self.cache.mkdir(mode=0o755)
        with self.assertRaisesRegex(ValueError, 'private directory'):
            cwasm.private_directory(self.cache)

    def test_flags_preloads_and_guest_arguments_remain_distinct(self):
        args = ['-Oopt-level=0', '-C', 'cache=y', '--dir', '/host::/guest',
                '--preload=lib=' + str(self.source), '--preload', 'other=' + str(self.source),
                str(self.source), '--target', 'guest-argument', '--preload=guest-argument']
        with mock.patch.object(cwasm.subprocess, 'run', side_effect=self.fake_compile) as run:
            result = cwasm.prepare(args, runtime=self.runtime, cache=self.cache)
        self.assertEqual(result[0], '--allow-precompiled')
        self.assertEqual(result[1:3], ['--argv0', str(self.source)])
        result = [result[0], *result[3:]]
        self.assertEqual(result[1:6], args[:5])
        self.assertTrue(result[6].startswith('--preload=lib='))
        self.assertEqual(result[7], '--preload')
        self.assertTrue(result[8].startswith('other='))
        self.assertTrue(result[9].endswith('.cwasm'))
        self.assertEqual(result[10:], args[9:])
        self.assertEqual(run.call_count, 1)

    def test_unknown_options_and_wat_are_forwarded(self):
        for args in [['--config', 'engine.toml', str(self.source)],
                     ['--unknown', str(self.source)], ['--help'], ['missing.wasm']]:
            self.assertEqual(cwasm.prepare(args, runtime=self.runtime), args)
        self.source.write_text('(module)')
        self.assertEqual(cwasm.prepare([str(self.source)], runtime=self.runtime), [str(self.source)])

    def test_precompile_rejects_execution_options(self):
        self.assertIsNone(cwasm.engine_options(['--dir', '/tmp', 'guest.wasm'], engine_only=True))


if __name__ == '__main__':
    unittest.main()
