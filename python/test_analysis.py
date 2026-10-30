"""Input discovery performs host file I/O only, without guest compilation."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.analysis import inputs


class InputTests(unittest.TestCase):
    def test_rebuild_excludes_exact_output_and_cache_but_hashes_wasm_resources(self):
        with tempfile.TemporaryDirectory(prefix='python-rebuild-') as directory:
            project = Path(directory)
            source = project / 'main.py'
            source.write_text('def answer() -> int: return 42\n')
            output, cache, work = project / 'library.wasm', project / 'cache', project / 'scratch'
            resource = project / 'plugin.wasm'
            resource.write_bytes(b'package resource')
            def discover():
                return inputs(source, project, work=work, excluded_paths=[output, cache])
            before = discover()
            output.write_bytes(b'old output' * 1024)
            cache.mkdir()
            (cache / 'native.so').write_bytes(b'bootstrap binary')
            work.mkdir()
            (work / 'candidate.wasm').write_bytes(b'partial output' * 1024)
            with patch('scripts.source_bundle.MAX_SOURCE_BYTES', 256):
                after = discover()
                self.assertEqual(before, after)
                self.assertEqual(set(after[2]), {str(source), str(resource)})
                resource.write_bytes(b'changed resource')
                changed = discover()
                self.assertNotEqual(after[2][str(resource)], changed[2][str(resource)])
                source.write_text('def answer() -> int: return 43\n')
                self.assertNotEqual(changed[2][str(source)], discover()[2][str(source)])
                resource.write_bytes(b'x' * 257)
                with self.assertRaisesRegex(ValueError, 'aggregate source limit'):
                    discover()

    def test_build_outputs_are_excluded_while_nested_package_data_is_retained(self):
        with tempfile.TemporaryDirectory(prefix='python-inputs-') as directory:
            base = Path(directory)
            project = base / 'project'
            project.mkdir()
            source = project / 'main.py'
            source.write_text('raise RuntimeError("discovery must not execute source")\n')
            (project / 'out').mkdir()
            (project / 'out/previous.so').write_bytes(b'old build output')
            resource = project / 'pkg/out/greeting.txt'
            resource.parent.mkdir(parents=True)
            resource.write_text('hello')
            program, _, inventory, _ = inputs(source, project, work=base / 'scratch')
            self.assertEqual(set(inventory), {str(source), str(resource)})
            self.assertEqual(set(program['assets']), {'/__wisp__/pkg/out/greeting.txt'})


if __name__ == '__main__':
    unittest.main()
