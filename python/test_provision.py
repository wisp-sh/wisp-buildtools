"""Check contract-only provisioning and reuse without launching native tools."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import provision


class ProvisionTests(unittest.TestCase):
    def test_provisioning_receives_only_wit_and_reuses_the_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / 'toolchain'
            package.mkdir()
            for name in provision.INPUTS:
                path = package / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('bootstrap input')
            wit = root / 'library.wit'
            wit.write_text('contract only')
            def create(command, **kwargs):
                cache = Path(command[command.index('--cache-dir') + 1])
                (cache / 'contract-hash').mkdir()
            with patch.object(provision, 'ROOT', package), patch.object(provision, 'run', side_effect=create) as run:
                first = provision.template('contract-hash', {}, wit, 'library', root / 'cache')
                second = provision.template('contract-hash', {}, wit, 'library', root / 'cache')
                self.assertEqual(first, second)
                self.assertEqual(run.call_count, 1)
                self.assertIn('--offline', run.call_args.args[0])
                self.assertIn(wit, run.call_args.args[0])
                (package / 'scripts/runtime_contract.py').write_text('updated runtime')
                third = provision.template('contract-hash', {}, wit, 'library', root / 'cache')
                self.assertNotEqual(first, third)
                self.assertEqual(run.call_count, 2)

                with patch.object(provision.platform, 'system', return_value='Darwin'):
                    self.assertEqual(third, provision.template('contract-hash', {}, wit, 'library', root / 'cache'))
                    (package / 'scripts/runtime_contract.py').write_text('another runtime')
                    with self.assertRaisesRegex(ValueError, 'requires Linux x86_64'):
                        provision.template('contract-hash', {}, wit, 'library', root / 'cache')
                self.assertEqual(run.call_count, 2)
