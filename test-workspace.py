#!/usr/bin/env python3
"""Real Component assembly and project-publication regression checks."""
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import wasm_tools


class SharedAdapterIntegrity(unittest.TestCase):
    def test_corrupt_adapter_is_rejected_without_repairing_during_check(self):
        with tempfile.TemporaryDirectory(prefix='wisp-adapter-test-') as directory:
            package = Path(directory)
            dist = package / 'dist'
            dist.mkdir()
            for filename in ('bootstrap.py', 'adapter.lock.json'):
                shutil.copyfile(ROOT / 'wasm-tools' / filename, package / filename)
            core = dist / 'wasm-tools.wasm'
            core.write_bytes(b'\0asm\x01\0\0\0')
            lock = json.loads((ROOT / 'wasm-tools/toolchain.lock.json').read_text())
            lock['module_sha256'] = wasm_tools.digest(core)
            (package / 'toolchain.lock.json').write_text(json.dumps(lock))
            adapter_lock = json.loads((package / 'adapter.lock.json').read_text())
            for spec in (adapter_lock['module'], adapter_lock['license']):
                destination = dist / spec['file']
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / 'wasm-tools/dist' / spec['file'], destination)
            manifest = {'bootstrap': lock, 'sha256': lock['module_sha256'],
                        'preview1Adapter': adapter_lock}
            (dist / 'manifest.json').write_text(json.dumps(manifest))
            adapter = dist / adapter_lock['module']['file']
            with mock.patch.object(wasm_tools, 'PACKAGE', package), mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(wasm_tools.preview1_adapter(), adapter)
                data = bytearray(adapter.read_bytes())
                data[-1] ^= 1
                adapter.write_bytes(data)
                with self.assertRaisesRegex(ValueError, 'adapter changed'):
                    wasm_tools.preview1_adapter()
            before = {str(path.relative_to(package)): wasm_tools.digest(path)
                      for path in package.rglob('*') if path.is_file()}
            result = subprocess.run([sys.executable, str(package / 'bootstrap.py'), '--check'],
                                    text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('adapter is missing or changed', result.stderr)
            after = {str(path.relative_to(package)): wasm_tools.digest(path)
                     for path in package.rglob('*') if path.is_file()}
            self.assertEqual(before, after)
            self.assertFalse((package / '.downloads').exists())


class WorkspaceIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wisp-workspace-test-')
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.tool = wasm_tools.module()
        self.runtime = wasm_tools.runtime()
        self.wit = self.work / 'math.wit'
        self.wit.write_text('package example:math@0.1.0;\ninterface api { add: func(a: s32, b: s32) -> s32; }\nworld library { export api; }\n')
        wat = self.work / 'add.wat'
        wat.write_text('(module (func (export "example:math/api@0.1.0#add") (param i32 i32) (result i32) local.get 0 local.get 1 i32.add))')
        self.module = self.work / 'add.wasm'
        subprocess.run(wasm_tools.command('parse', wat, '-o', self.module), check=True)
        self.component = self.work / 'math.component.wasm'
        self.command('componentize', self.module, '--wit', self.wit, '--world', 'library', '-o', self.component)

    def command(self, *arguments, success=True):
        result = subprocess.run([sys.executable, str(ROOT / 'toolchains.py'), *map(str, arguments)], text=True, capture_output=True)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result

    def test_project_keeps_exported_interface_identity_and_component_bytes(self):
        project = self.work / 'release project'
        self.command('wisp-project', self.component, '--name', 'demo:release', '-o', project)
        contract = (project / 'wit/world.wit').read_text()
        self.assertIn('package demo:release@0.1.0;', contract)
        self.assertIn('example:math/api@0.1.0', contract)
        self.assertEqual((project / 'component.wasm').read_bytes(), self.component.read_bytes())
        if self.runtime.is_file():
            result = subprocess.check_output([str(self.runtime), 'run', '--invoke', 'add(20,22)', str(project / 'component.wasm')], text=True)
            self.assertEqual(result.strip(), '42')

    def test_invalid_contracts_never_publish_a_project(self):
        for index, arguments in enumerate([
            ['--name', 'demo:bad-'], ['--name', 'demo:bad--name'],
            ['--name', 'demo:release', '--version', '01.0.0'],
            ['--name', 'demo:release', '--version', '1.0.0-a..b'],
            ['--name', 'demo:wrong', '--wit', str(self.wit), '--world', 'library'],
            ['--name', 'example:math', '--wit', str(self.wit), '--world', 'absent'],
        ]):
            output = self.work / f'invalid-{index}'
            self.command('wisp-project', self.component, *arguments, '-o', output, success=False)
            self.assertFalse(output.exists())

    def test_native_component_tool_override_is_rejected(self):
        output = self.work / 'native-tool.component.wasm'
        result = self.command('componentize', self.module, '--wit', self.wit,
                              '--world', 'library', '--wasm-tools', self.runtime,
                              '-o', output, success=False)
        self.assertIn('native binaries are not supported', result.stderr)
        self.assertFalse(output.exists())

    def test_bootstrap_check_does_not_write_missing_installation(self):
        package = self.work / 'uninstalled-tool'
        package.mkdir()
        for filename in ('bootstrap.py', 'toolchain.lock.json'):
            shutil.copyfile(ROOT / 'wasm-tools' / filename, package / filename)
        before = set(package.iterdir())
        result = subprocess.run([sys.executable, str(package / 'bootstrap.py'), '--check'],
                                text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(set(package.iterdir()), before)

    def test_interrupted_verification_report_cannot_claim_success(self):
        output = self.work / 'interrupted'
        process = subprocess.Popen([sys.executable, str(ROOT / 'verify-workspace.py'), '--languages', 'zig', 'zig', '--out', str(output)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while not (output / 'verification.json').exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            report = json.loads((output / 'verification.json').read_text())
            self.assertEqual(report['requested_languages'], ['zig'])
            self.assertFalse(report['complete'])
            self.assertFalse(report['passed'])
            self.assertNotIn('finished', report)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10)


if __name__ == '__main__':
    unittest.main()
