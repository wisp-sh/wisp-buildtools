from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from contracts import Signature, Type
from bindings import Generator, exports, flat, layout
from compile import inputs


class PublicContracts(unittest.TestCase):
    def test_explicit_execution_host_precedes_path_worker(self):
        import runner
        hosts={'worker':'/tools/worker', 'ocaml-host':'/tools/ocaml-host',
               'backend-host':'/tools/backend-host', 'wisp-worker':'/path/worker'}
        with patch.dict('os.environ', {'WISP_OCAML_WASMTIME':'ocaml-host',
                                      'WISP_WASMTIME':'backend-host'}, clear=True), \
             patch.object(runner.shutil,'which',side_effect=hosts.get):
            self.assertEqual(runner.runtime(),Path('/tools/ocaml-host'))
            self.assertEqual(runner.runtime(backend=True),Path('/tools/backend-host'))
            with patch.dict('os.environ',{'WISP_WORKER':'worker'}):
                self.assertEqual(runner.runtime(),Path('/tools/worker'))
                self.assertEqual(runner.runtime(backend=True),Path('/tools/worker'))
        with patch.dict('os.environ', {}, clear=True), \
             patch.object(runner.shutil,'which',side_effect=lambda name: '/path/worker' if name in ('wisp-worker','/path/worker') else None):
            self.assertEqual(runner.runtime(),Path('/path/worker'))

    def test_rich_signature_and_private_implementation(self):
        api = Signature('''(* outer (* nested *) "*)" *)
            type mode = Quiet | VeryLoud
            type profile = { name : string; payload : bytes; mode : mode }
            val echo : profile -> (profile list option, string) result
            val next : unit -> int32
            val clear : unit -> unit
        ''')
        wit = api.wit()
        self.assertIn('enum %mode { %quiet, %very-loud }', wit)
        self.assertIn('result<option<list<%profile>>, string>', wit)
        self.assertIn('%clear: func();', wit)
        self.assertEqual(list(api.functions), ['echo', 'next', 'clear'])

    def test_rejects_partial_or_ambiguous_public_interfaces(self):
        invalid = ["val f : 'a -> 'a", 'val x : int', 'module X : sig end',
                   'type t = { next: t } val f : t -> t', 'type t = A of int val f : t -> t',
                   'val f : ?x:int -> int', 'val f : x:int -> int',
                   'val f : int -> int val f : int -> int', '(* unterminated',
                   'type t = { mutable x : int } val f : t -> t',
                   'val f : int -> int external x : int -> int = "x"',
                   'val f : int -> int [@@foo]', 'val f : unit -> int -> int']
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ValueError): Signature(text)

    def test_canonical_layouts_and_variant_join(self):
        api = Signature('''type r = { flag: bool; count: int64; text: string }
                           val f : r -> (float, int32) result''')
        params, result = api.functions['f']
        self.assertEqual(layout(params[0]), (24, 8, [0, 8, 16]))
        self.assertEqual(layout(result), (16, 8, [8]))
        self.assertEqual(flat(result), ['i32', 'i64'])
        self.assertEqual(flat(params[0]), ['i32', 'i64', 'i32', 'i32'])

    def test_callbacks_root_inputs_and_free_owned_results(self):
        api = Signature('val echo : string list -> (string list, string) result')
        c, ml = Generator().generate(api, {'echo': 'test:api/data#echo'}, 'Library')
        self.assertIn('Callback.register "wisp:0" Library.echo', ml)
        self.assertIn('CAMLlocalN(args,1)', c)
        self.assertIn('caml_callbackN_exn', c)
        self.assertIn('export_name("cabi_post_test:api/data#echo")', c)
        self.assertIn('free(data)', c)
        self.assertIn('Store_field(node,1,r)', c)

    def test_indirect_parameters_and_float_records(self):
        text = 'type pair = { x: float; y: float } val f : ' + ' -> '.join(['pair'] * 9 + ['pair'])
        api = Signature(text)
        c, _ = Generator().generate(api, {'f': 'f'}, 'Main')
        self.assertIn('call_0(uint32_t input)', c)
        self.assertIn('Double_array_tag', c)
        self.assertIn('Store_double_field', c)
        self.assertIn('free((void *)(uintptr_t)input)', c)

    def test_wit_identity_and_signature_must_match(self):
        api = Signature('val add : int32 -> int32 -> int32')
        document = {'worlds': [{'name': 'app', 'imports': {}, 'exports': {'interface-0': {'interface': {'id': 0}}}}],
                    'interfaces': [{'name': 'api', 'package': 0, 'functions': {'add': {'kind': 'freestanding',
                        'params': [{'name': 'left', 'type': 's32'}, {'name': 'right', 'type': 's32'}], 'result': 's32'}}}],
                    'packages': [{'name': 'demo:math@1.0.0'}], 'types': []}
        self.assertEqual(exports(api, document, 'app'), {'add': 'demo:math/api@1.0.0#add'})
        document['interfaces'][0]['functions']['add']['result'] = 's64'
        with self.assertRaisesRegex(ValueError, 'differs'): exports(api, document, 'app')
        document['worlds'][0]['imports']['x'] = {}
        with self.assertRaisesRegex(ValueError, 'imports'): exports(api, document, 'app')

    def test_project_inventory_and_interface_order(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'support.ml').write_text('let scale x = 2*x')
            (root / 'main.ml').write_text('let add a b = Support.scale a + b')
            (root / 'main.mli').write_text('val add : int -> int -> int')
            (root / 'output.wasm').write_bytes(b'not source')
            (root / '_build').mkdir(); (root / '_build/stale.ml').write_text('bad')
            _, order, main, inventory = inputs([root/'support.ml', root/'main.ml'], root)
            self.assertEqual([p.name for p in order], ['support.ml', 'main.mli', 'main.ml'])
            self.assertEqual(set(inventory), {'support.ml','main.ml','main.mli'})
            before = inventory['support.ml']
            (root / 'support.ml').write_text('let scale x = 3*x')
            self.assertNotEqual(inputs([root/'main.ml'], root)[3]['support.ml'], before)
            (root/'escape.ml').symlink_to(root/'support.ml')
            with self.assertRaisesRegex(ValueError, 'symlink'): inputs([main], root)

    def test_duplicate_modules_and_reserved_bindings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root/'sub').mkdir()
            for name in ['main.ml', 'sub/main.ml', 'wisp_bindings.ml']: (root/name).touch()
            with self.assertRaisesRegex(ValueError, 'duplicate'): inputs([root/'main.ml',root/'sub/main.ml'],root)
            with self.assertRaisesRegex(ValueError, 'reserved'): inputs([root/'wisp_bindings.ml'],root)


if __name__ == '__main__': unittest.main()

class SnapshotPublication(unittest.TestCase):
    def test_receipt_hashes_compiled_snapshot_after_original_changes(self):
        import compile as adapter
        import json
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); project=root/'project'; project.mkdir()
            source=project/'main.ml'; source.write_text('let () = print_int 42')
            expected=adapter.digest(source)
            tool=root/'tool'; (tool/'dist').mkdir(parents=True)
            (tool/'dist/manifest.json').write_text('{}')
            def compiler(work,*args):
                self.assertEqual(adapter.digest(work/'source/main.ml'),expected)
                source.write_text('let () = print_int 99')
            def backend(work,*args,**kwargs): (work/'program.wasm').write_bytes(b'\0asm\x01\0\0\0')
            with patch.object(adapter,'ROOT',tool), patch.object(adapter,'manifest',return_value={'version':'4.14.2'}), \
                 patch.object(adapter,'ocamlc',compiler), patch.object(adapter.backend,'build',backend):
                adapter.build([source],root/'out.wasm')
            receipt=json.loads((root/'out.wasm.build.json').read_text())
            self.assertEqual(receipt['sourceSha256'],expected)
            self.assertNotEqual(expected,adapter.digest(source))
            self.assertEqual([p.name for p in project.iterdir()],['main.ml'])

    def test_failure_and_existing_sidecars_do_not_publish(self):
        import compile as adapter
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); source=root/'main.ml'; source.write_text('bad')
            with patch.object(adapter,'manifest',return_value={'version':'4.14.2'}), \
                 patch.object(adapter,'ocamlc',side_effect=ValueError('syntax error')):
                with self.assertRaisesRegex(ValueError,'syntax error'): adapter.build([source],root/'out.wasm')
            self.assertFalse((root/'out.wasm').exists())
            self.assertFalse((root/'out.wasm.build.json').exists())
            receipt=root/'out.wasm.build.json'; receipt.write_text('preserve')
            with self.assertRaisesRegex(ValueError,'already exists'): adapter.build([source],root/'out.wasm')
            self.assertEqual(receipt.read_text(),'preserve')
