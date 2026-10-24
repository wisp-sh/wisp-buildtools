"""Scheduler and trace policy tests, not evidence of a rebuilt macro compiler."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('macro_scheduler_runner', Path(__file__).resolve().parents[1] / 'scripts/runner.py')
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class TargetPredicateTests(unittest.TestCase):
    def test_wasm_platforms_and_nested_predicates(self):
        for expression, wasi, macro in [
            (None, True, True), ('cfg(target_has_atomic="64")', True, True),
            ('cfg(feature="x")', False, False), ('cfg(tokio_unstable)', False, False),
            ('cfg(getrandom_backend="custom")', False, False), ('cfg(any())', False, False), ('cfg(all())', True, True),
            ('cfg(target_os = "wasi")', True, False), ('cfg(target_env="")', False, True),
            ('cfg(all(target_arch="wasm32",not(target_os="unknown")))', True, False),
            ('cfg(any(unix,windows))', False, False), ('wasm32-unknown-unknown', False, True),
        ]:
            with self.subTest(expression=expression):
                self.assertEqual(runner.target_matches(expression, 'wasm32-wasip1'), wasi)
                self.assertEqual(runner.target_matches(expression, runner.MACRO_TARGET), macro)

    def test_rejects_unsupported_or_malformed_predicates(self):
        for expression in ('cfg(not())',
                           'cfg(target_os="wasi",)', 'cfg(all(target_arch="wasm32")'):
            with self.subTest(expression=expression), self.assertRaises(runner.BuildError):
                runner.target_matches(expression, 'wasm32-wasip1')


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.prefix = self.root / 'prefix'
        (self.prefix / 'sysroot/lib/rustlib/wasm32-unknown-unknown/lib').mkdir(parents=True)
        (self.prefix / 'rustc.wasm').write_bytes(b'\0asm\x01\0\0\0')
        self.manifest = {'compiler': {'module': 'rustc.wasm'}, 'compilerHost': 'wasm32-wasip3',
                         'runtime': {'version': '0'}, 'capabilities': {'proceduralMacros': True}}
        self.args = SimpleNamespace(manifest_path=self.root / 'app/Cargo.toml', out_dir=self.root / 'out',
                                    wasmtime=sys.executable, target='wasm32-wasip1',
                                    report=self.root / 'out/report.json', cargo='metadata-only-cargo',
                                    vendor_dir=None, timeout=10, crate_type='bin')
        with patch.object(runner, 'verified_install', return_value=(self.manifest, self.prefix)):
            self.builder = runner.Builder(self.args)
        self.builder.packages = {}
        self.builder.nodes = {}
        self.package('app', features={'default': ['derive'], 'derive': ['dep:derive']})
        self.package('derive', macro=True)
        self.package('shared', features={'runtime': [], 'macro': [], 'build': [], 'extra': []})
        self.edge('app', 'derive', optional=True)
        self.edge('app', 'shared', features=['runtime'])
        self.edge('app', 'shared', kind='build', features=['build'])
        self.edge('derive', 'shared', features=['macro'])
        self.builder.root_id = 'app'
        self.builder.nodes['app']['features'] = ['default', 'derive']
        # Cargo metadata merges these; the scheduler must not reuse that union.
        self.builder.nodes['shared']['features'] = ['runtime', 'macro', 'build']

    def package(self, name, *, features=None, macro=False, build_script=False):
        directory = self.root / name
        (directory / 'src').mkdir(parents=True)
        (directory / 'Cargo.toml').write_text('[package]\nname=' + json.dumps(name) + '\nversion="0.1.0"\n')
        (directory / 'src/lib.rs').write_text('// Source remains unchanged by scheduling.\n')
        targets = [{'name': name, 'kind': ['proc-macro' if macro else 'lib'], 'edition': '2021',
                    'src_path': str(directory / 'src/lib.rs')}]
        if build_script:
            (directory / 'build.rs').write_text('fn main() {}\n')
            targets.append({'name': 'build-script-build', 'kind': ['custom-build'], 'edition': '2021',
                            'src_path': str(directory / 'build.rs')})
        package = {'id': name, 'name': name, 'version': '0.1.0', 'manifest_path': str(directory / 'Cargo.toml'),
                   'targets': targets, 'features': features or {}, 'dependencies': []}
        self.builder.packages[name] = package
        self.builder.nodes[name] = {'id': name, 'features': [], 'deps': []}
        return package

    def edge(self, parent, child, *, alias=None, features=(), optional=False, kind=None,
             default=False, target=None):
        declaration = {'name': child, 'rename': alias, 'kind': kind, 'target': target,
                       'features': list(features), 'optional': optional, 'uses_default_features': default}
        self.builder.packages[parent]['dependencies'].append(declaration)
        alias = (alias or child).replace('-', '_')
        existing = next((edge for edge in self.builder.nodes[parent]['deps']
                         if edge['name'] == alias and edge['pkg'] == child), None)
        if existing is None:
            existing = {'name': alias, 'pkg': child, 'dep_kinds': []}
            self.builder.nodes[parent]['deps'].append(existing)
        existing['dep_kinds'].append({'kind': kind, 'target': target})

    def configure(self):
        self.builder.configure_units()
        return self.builder

    def fake_execute(self, program, argv, package, kind, **kwargs):
        self.calls.append((program, argv, package['id'], kind, kwargs.get('context', 'runtime')))
        if '-o' in argv:
            Path(argv[argv.index('-o') + 1]).write_bytes(b'\0asm\x01\0\0\0')
        return len(self.calls) - 1

    def test_shared_crate_features_are_separate_for_runtime_macro_and_build(self):
        builder = self.configure()
        self.assertEqual(builder.unit_features['shared', 'runtime'], {'runtime'})
        self.assertEqual(builder.unit_features['shared', 'macro'], {'macro'})
        self.assertEqual(builder.unit_features['shared', 'build'], {'build'})
        package = builder.packages['shared']
        self.assertEqual(len({builder.unit_key(package, context) for context in ('runtime', 'macro', 'build')}), 3)

    def test_macro_dependencies_and_module_use_unknown_target_without_source_rewrites(self):
        builder = self.configure()
        source = Path(builder.packages['derive']['targets'][0]['src_path'])
        original = source.read_bytes()
        self.calls = []
        with patch.object(builder, 'execute', side_effect=self.fake_execute):
            macro = builder.compile_library('derive')
            runtime = builder.compile_library('shared')
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(macro.suffix, '.wasm')
        self.assertEqual(macro.parent, builder.libs / 'macro')
        self.assertEqual(runtime.parent, builder.libs / 'runtime')
        compile_calls = {package: argv for _, argv, package, kind, _ in self.calls if kind == 'compiler'}
        macro_flags = compile_calls['derive']
        self.assertEqual(macro_flags[macro_flags.index('--crate-type') + 1], 'proc-macro')
        self.assertEqual(macro_flags[macro_flags.index('--target') + 1], runner.MACRO_TARGET)
        self.assertIn('-Zwasm-proc-macros', macro_flags)
        self.assertTrue(any(arg.startswith('shared=') and '/macro/' in arg for arg in macro_flags))
        self.assertEqual(builder.report['proceduralMacros'], [], 'compilation alone must not claim execution')
        self.assertEqual(builder.report['macroArtifacts'][0]['program'], str(macro))

    def test_build_script_binary_and_build_dependencies_stay_wasi_for_macro_crate(self):
        derive = self.builder.packages['derive']
        path = Path(derive['manifest_path']).parent / 'build.rs'
        path.write_text('fn main() {}\n')
        derive['targets'].append({'name': 'build-script-build', 'kind': ['custom-build'],
                                  'edition': '2021', 'src_path': str(path)})
        self.edge('derive', 'shared', kind='build', features=['build'])
        builder = self.configure()
        self.calls = []
        with patch.object(builder, 'execute', side_effect=self.fake_execute):
            builder.compile_library('derive')
        script_compile = next(argv for _, argv, _, kind, _ in self.calls if kind == 'compiler' and str(path) in argv)
        self.assertEqual(script_compile[script_compile.index('--target') + 1], 'wasm32-wasip1')
        self.assertEqual(builder.report['buildScripts'][0]['target'], 'wasm32-wasip1')
        self.assertTrue(any(arg.startswith('shared=') and '/build/' in arg for arg in script_compile))
        self.assertEqual(builder.environment(derive, context='macro')['TARGET'], runner.MACRO_TARGET)
        self.assertEqual(builder.environment(derive, context='macro')['CARGO_CFG_TARGET_OS'], 'unknown')
        self.assertTrue(any(kind == 'build-script' for _, _, _, kind, _ in self.calls))

    def test_proc_macro_import_is_exposed_only_to_the_macro_crate(self):
        derive = self.builder.packages['derive']
        source = Path(derive['targets'][0]['src_path'])
        source.write_text('use proc_macro::TokenStream;\n'
                          '#[proc_macro]\npub fn identity(input: TokenStream) -> TokenStream { input }\n')
        script = source.parents[1] / 'build.rs'
        script.write_text('fn main() {}\n')
        derive['targets'].append({'name': 'build-script-build', 'kind': ['custom-build'],
                                  'edition': '2021', 'src_path': str(script)})
        builder = self.configure()
        self.calls = []
        with patch.object(builder, 'execute', side_effect=self.fake_execute):
            builder.compile_library('app')
        macro_calls = 0
        for _, argv, _, kind, _ in self.calls:
            if kind != 'compiler':
                continue
            externs = [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == '--extern']
            if str(source) in argv:
                macro_calls += 1
                self.assertEqual(argv[argv.index('--crate-type') + 1], 'proc-macro')
                self.assertIn('proc_macro', externs)
            else:
                self.assertNotIn('proc_macro', externs)
        self.assertEqual(macro_calls, 1)

    def test_transitive_reexported_macro_matches_wasm_crate_locator_filename(self):
        app = self.builder.packages['app']
        app['dependencies'] = [edge for edge in app['dependencies'] if edge['name'] != 'derive']
        self.builder.nodes['app']['deps'] = [edge for edge in self.builder.nodes['app']['deps'] if edge['pkg'] != 'derive']
        app['features'] = {'default': []}
        self.builder.nodes['app']['features'] = ['default']
        self.package('reexport')
        self.edge('app', 'reexport')
        self.edge('reexport', 'derive')
        builder = self.configure()
        self.calls = []
        with patch.object(builder, 'execute', side_effect=self.fake_execute):
            root_library = builder.compile_library('app')
        app_arguments = next(argv for _, argv, package, kind, _ in self.calls
                             if package == 'app' and kind == 'compiler')
        self.assertFalse(any(argument.startswith('derive=') for argument in app_arguments))
        self.assertTrue(any(argument.startswith('reexport=') for argument in app_arguments))
        self.assertIn('-Ldependency=' + str(builder.libs / 'macro'), app_arguments)
        macro = builder.compiled['derive', 'macro']
        # CrateLocator uses the target's empty dll_prefix and .wasm suffix
        # for hash-based transitive lookup, which cannot use a direct --extern.
        self.assertEqual(list((builder.libs / 'macro').glob('derive*.wasm')), [macro])
        self.assertFalse(macro.name.startswith('lib'))
        self.assertTrue(root_library.name.startswith('libapp-'))
        self.assertEqual(root_library.suffix, '.rlib')
        self.assertEqual(builder.macro_artifacts[macro]['program'], str(macro))

    def test_out_dir_and_rustc_environment_do_not_leak_between_contexts(self):
        builder = self.configure()
        package = builder.packages['shared']
        builder.script_env[builder.unit_id(package, 'macro')] = {'ONLY_MACRO': 'yes'}
        macro = builder.environment(package, context='macro')
        normal = builder.environment(package)
        self.assertNotEqual(macro['OUT_DIR'], normal['OUT_DIR'])
        self.assertIn('CARGO_FEATURE_MACRO', macro)
        self.assertNotIn('CARGO_FEATURE_MACRO', normal)
        self.assertNotIn('ONLY_MACRO', normal)

    def test_weak_features_do_not_activate_an_optional_dependency(self):
        self.package('optional', features={'extra': []})
        derive = self.builder.packages['derive']
        derive['features'] = {'weak': ['renamed?/extra'], 'with-helper': ['dep:renamed']}
        self.builder.packages['app']['dependencies'][0]['features'] = ['weak']
        self.edge('derive', 'optional', alias='renamed', optional=True)
        builder = self.configure()
        self.assertNotIn(('optional', 'macro'), builder.unit_features)
        self.builder.packages['app']['dependencies'][0]['features'].append('with-helper')
        builder.unit_features.clear()
        builder.unit_edges.clear()
        builder.configure_units()
        self.assertEqual(builder.unit_features['optional', 'macro'], {'extra'})

    def test_default_and_strong_dependency_feature_forwarding(self):
        self.builder.packages['derive']['features'] = {'default': ['shared/extra']}
        self.builder.packages['app']['dependencies'][0]['uses_default_features'] = True
        builder = self.configure()
        self.assertEqual(builder.unit_features['shared', 'macro'], {'macro', 'extra'})

    def test_target_specific_edges_are_selected_for_each_compilation_target(self):
        self.package('only-unknown')
        self.edge('derive', 'only-unknown', target='cfg(target_os="unknown")')
        self.edge('app', 'only-unknown', target='cfg(any())')
        builder = self.configure()
        self.assertIn(('only-unknown', 'macro'), builder.unit_features)
        self.assertNotIn(('only-unknown', 'runtime'), builder.unit_features)

    def test_missing_selected_optional_metadata_edge_is_rejected(self):
        self.builder.nodes['app']['deps'] = [edge for edge in self.builder.nodes['app']['deps'] if edge['pkg'] != 'derive']
        with self.assertRaisesRegex(runner.BuildError, 'omitted active dependency'):
            self.configure()

    def test_missing_macro_sysroot_is_rejected(self):
        (self.prefix / 'sysroot/lib/rustlib/wasm32-unknown-unknown/lib').rmdir()
        with self.assertRaisesRegex(runner.BuildError, 'missing.*sysroot'):
            self.configure()

    def test_capability_is_explicit_and_bootstrap_rejects_macro_before_execution(self):
        for index, capability in enumerate((False, None, 'true', 1)):
            manifest = dict(self.manifest, capabilities={'proceduralMacros': capability})
            args = SimpleNamespace(**vars(self.args))
            args.out_dir = self.root / f'disabled-profile-{index}'
            with patch.object(runner, 'verified_install', return_value=(manifest, self.prefix)):
                builder = runner.Builder(args)
            builder.packages = {'derive': self.builder.packages['derive']}
            self.assertFalse(builder.macros_enabled)
            with self.subTest(capability=capability), patch.object(builder, 'execute') as execute:
                with self.assertRaisesRegex(runner.BuildError, 'Wasm macro loader'):
                    builder.compile_library('derive')
                execute.assert_not_called()

    def test_macro_metadata_keeps_all_platform_edges_without_native_build(self):
        response = subprocess.CompletedProcess([], 0, stdout=json.dumps({'packages': []}), stderr='')
        with patch.object(runner.subprocess, 'run', return_value=response) as run:
            self.builder.metadata()
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ['metadata-only-cargo', 'metadata'])
        self.assertNotIn('--filter-platform', command)
        self.assertIn('--offline', command)
        self.assertIn('--locked', command)

    def setup_trace(self):
        module = self.builder.libs / 'macro.wasm'
        module.write_bytes(b'\0asm\x01\0\0\0')
        artifact = {'package': 'derive', 'program': str(module), 'sha256': runner.sha256(module), 'compilation': 0}
        self.builder.macro_artifacts[module] = artifact
        trace = self.builder.out / 'trace.jsonl'
        base = {'schemaVersion': 1, 'runtime': 'wasmi', 'module': str(module), 'index': 0,
                'name': 'Answer', 'kind': 'derive', 'macroTarget': runner.MACRO_TARGET}
        return module, trace, base

    def test_successful_loader_trace_binds_macro_hash_to_compiler_execution(self):
        module, trace, base = self.setup_trace()
        events = [dict(base, event='load', status='succeeded'), dict(base, event='invoke', status='started'),
                  dict(base, event='invoke', status='succeeded')]
        trace.write_text(''.join(json.dumps(event) + '\n' for event in events))
        self.builder.macro_trace(trace, 7)
        record = self.builder.report['proceduralMacros'][0]
        self.assertEqual(record['execution'], 7)
        self.assertEqual(record['program'], str(module))
        self.assertEqual(record['sha256'], runner.sha256(module))
        self.assertEqual(record['traceSha256'], runner.sha256(trace))
        self.assertEqual(record['loader'], 'compiler-wasm-runtime')

    def test_loading_a_module_does_not_claim_macro_execution(self):
        _, trace, base = self.setup_trace()
        trace.write_text(json.dumps(dict(base, event='load', status='succeeded')) + '\n')
        self.builder.macro_trace(trace, 0)
        self.assertEqual(self.builder.report['proceduralMacros'], [])

    def test_rejects_failed_incomplete_or_out_of_order_macro_invocations(self):
        _, trace, base = self.setup_trace()
        for events in (
            [('invoke', 'succeeded')], [('invoke', 'started')],
            [('load', 'succeeded'), ('invoke', 'started')],
            [('load', 'succeeded'), ('invoke', 'started'), ('invoke', 'failed')],
        ):
            trace.write_text(''.join(json.dumps(dict(base, event=event, status=status)) + '\n' for event, status in events))
            with self.subTest(events=events), self.assertRaises(runner.BuildError):
                self.builder.macro_trace(trace, 0)

    def test_rejects_changed_macro_bytes_and_unregistered_modules(self):
        module, trace, base = self.setup_trace()
        trace.write_text(json.dumps(dict(base, event='load', status='succeeded')) + '\n')
        module.write_bytes(b'\0asm\x01\0\0\0changed')
        with self.assertRaisesRegex(runner.BuildError, 'unverified'):
            self.builder.macro_trace(trace, 0)
        self.builder.macro_artifacts.clear()
        with self.assertRaisesRegex(runner.BuildError, 'unverified'):
            self.builder.macro_trace(trace, 0)

    def test_rejects_malformed_trace_without_traceback(self):
        _, trace, _ = self.setup_trace()
        for content in ('{}\n', '[]\n', 'not json\n'):
            trace.write_text(content)
            with self.subTest(content=content), self.assertRaises(runner.BuildError):
                self.builder.macro_trace(trace, 0)


if __name__ == '__main__':
    unittest.main()
