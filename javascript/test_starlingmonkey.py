"""Small contract checks; compiler execution belongs to hosted acceptance."""
import unittest

from starlingmonkey import initialization_environment


class InitializationMetadataTests(unittest.TestCase):
    def test_guest_canonical_abi_controls_indirect_parameters_and_returns(self):
        result = initialization_environment({"schemaVersion": 1, "exports": [{
            "name": "export_data_echo", "params": ["i32", "i64"], "ret": "i32",
            "paramptr": True, "retptr": True, "retsize": 24,
        }], "imports": [["demo:host/math", "import_add", 2]]})
        self.assertEqual(result["EXPORT0_ARGS"], "*i32,i64")
        self.assertEqual(result["EXPORT0_RET"], "*i32")
        self.assertEqual(result["EXPORT0_RETSIZE"], "24")
        self.assertEqual(result["IMPORT0_NAME"], "import_add")
        self.assertEqual(result["IMPORT0_ARGCNT"], "2")
        self.assertEqual(result["FEATURE_CLOCKS"], "")

    def test_void_return_is_empty_and_clocks_are_explicit(self):
        result = initialization_environment({"schemaVersion": 1, "exports": [{
            "name": "export_reset", "params": [], "ret": None,
            "paramptr": False, "retptr": False, "retsize": 0,
        }], "imports": []}, clocks=True)
        self.assertEqual(result["EXPORT0_RET"], "")
        self.assertEqual(result["FEATURE_CLOCKS"], "1")


if __name__ == "__main__":
    unittest.main()
