"""Bootstrap-only generation of fixed adapters between the pinned compiler ABIs."""
import json
from .runtime import ROOT, wasmtime


def build_shims():
    config = wasmtime.Config()
    config.parallel_compilation = False
    engine = wasmtime.Engine(config)
    frontend = wasmtime.Module.from_file(engine, str(ROOT / "dist/assemblyscript.wasm"))
    backend = wasmtime.Module.from_file(engine, str(ROOT / "dist/binaryen.wasm"))
    aliases = json.loads((ROOT / "dist/binaryen-aliases.json").read_text())
    exports = {item.name: item.type for item in backend.exports}
    directory = ROOT / "dist/abi-shims"
    directory.mkdir(exist_ok=True)
    for item in frontend.imports:
        if item.module != "binaryen":
            continue
        actual = exports[aliases[item.name]]
        expected = list(item.type.params)
        if list(map(str, expected)) == list(map(str, actual.params)):
            continue
        if item.name not in ("__i32_store8", "__i32_store", "_BinaryenAddTable"):
            raise RuntimeError(f"Unexpected Binaryen ABI change: {item.name}")
        instructions = []
        for index, kind in enumerate(expected):
            instructions.append(f"local.get {index}")
            if str(kind) == "f64" and str(actual.params[index]) == "i32":
                instructions += ["i64.trunc_sat_f64_s", "i32.wrap_i64"]
        instructions += ["i32.const 0"] * (len(actual.params) - len(expected))
        def signature(types):
            return " ".join(map(str, types))
        wat = ('(module (import "backend" "target" (func $target '
               f"(param {signature(actual.params)}) (result {signature(actual.results)}))) "
               f'(func (export "adapt") (param {signature(expected)}) '
               f"(result {signature(item.type.results)}) " + " ".join(instructions) + " call $target))")
        (directory / f"{item.name}.wasm").write_bytes(wasmtime.wat2wasm(wat))
