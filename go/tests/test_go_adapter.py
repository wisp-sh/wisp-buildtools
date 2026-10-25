import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from wasmbuild.go_adapter import construct, discover, inspect  # noqa: E402


class GoAdapterTests(unittest.TestCase):
    def test_multifile_imports_and_target_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "main.go").write_text('package main\nimport ("fmt"; "project/foo")\n')
            (root / "extra.go").write_text('package main\nfunc extra() {}\n')
            (root / "main_linux.go").write_text('package wrong\n')
            (root / "main_wasip1.go").write_text('package main\n')
            (root / "excluded.go").write_text('//go:build linux\npackage wrong\n')
            (root / "foo").mkdir()
            (root / "foo/foo.go").write_text('package foo\nimport "fmt"\n')
            packages = discover(root, "project")
            self.assertEqual([file.name for file in packages["project"].files],
                             ["extra.go", "main.go", "main_wasip1.go"])
            self.assertEqual(packages["project"].imports, {"fmt", "project/foo"})
            toolchain = type("ToolchainStub", (), {
                "stdlib": {"fmt": "/goroot/pkg/wasip1_wasm/fmt.a"},
                "goroot": root,
            })()
            graph = construct(root, toolchain, "project")
            self.assertEqual([node.name for node in graph.order()],
                             ["compile:project/foo", "compile:project", "link"])

    def test_vendored_modules_keep_upstream_import_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "main.go").write_text('package main\nimport "example.org/library"\n')
            library = root / "vendor/example.org/library"
            library.mkdir(parents=True)
            (library / "library.go").write_text('package library\nimport "example.org/library/internal/value"\n')
            internal = library / "internal/value"
            internal.mkdir(parents=True)
            (internal / "value.go").write_text('package value\n')
            toolchain = type("ToolchainStub", (), {"stdlib": {}, "goroot": root})()
            graph = construct(root, toolchain, "project")
            self.assertEqual([node.name for node in graph.order()], [
                "compile:example.org/library/internal/value",
                "compile:example.org/library", "compile:project", "link"])
            self.assertNotIn("compile:project/vendor/example.org/library", graph.nodes)

    def test_import_cycle_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "main.go").write_text('package main\nimport "project/foo"\n')
            (root / "foo").mkdir()
            (root / "foo/foo.go").write_text('package foo\nimport "project"\n')
            toolchain = type("ToolchainStub", (), {"stdlib": {}, "goroot": root})()
            with self.assertRaisesRegex(ValueError, "import cycle"):
                construct(root, toolchain, "project")

    def test_upstream_wit_runtime_uses_wasm_linker_with_sbrk_permission(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = "go.bytecodealliance.org/pkg/wit/runtime"
            (root / "main.go").write_text(f'package main\nimport "{runtime}"\n')
            library = root / "vendor" / runtime
            library.mkdir(parents=True)
            (library / "runtime.go").write_text('package runtime\nimport "unsafe"\n')
            toolchain = type("ToolchainStub", (), {"stdlib": {}, "goroot": root})()
            graph = construct(root, toolchain, "project")
            self.assertEqual([node.tool for node in graph.order()], ["compile", "compile", "link"])
            self.assertIn("-checklinkname=0", graph.nodes["link"].argv)

            (root / "main.go").write_text('package main\n')
            graph = construct(root, toolchain, "project")
            self.assertNotIn("-checklinkname=0", graph.nodes["link"].argv)
            self.assertNotIn(f"compile:{runtime}", graph.nodes)

    def test_comment_does_not_create_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            file = Path(temporary) / "main.go"
            file.write_text('package main\n// import "bad"\nconst s = `import "bad"`\n')
            self.assertEqual(inspect(file), ("main", set()))


if __name__ == "__main__":
    unittest.main()
