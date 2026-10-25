"""Input discovery performs host file I/O only, without guest compilation."""
from pathlib import Path
import tempfile
import unittest

from scripts.analysis import inputs


class InputTests(unittest.TestCase):
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
