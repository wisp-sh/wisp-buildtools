#!/usr/bin/env python3
"""Small executable ABI fixtures. Uses the external Wasmtime 49 host only.

Run directly; the shared memory lock covers Cargo, transformation and small JITs.
Scratch and results live here, outside installed distributions.
"""
import json
import os
from copy import deepcopy
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
if os.environ.get("WISP_WASMTIME_PYTHON"):
    sys.path.insert(0, os.environ["WISP_WASMTIME_PYTHON"])
import shard
import wasmtime as w

WAT = r'''(module
  (import "env" "memory" (memory 1))
  (import "env" "table" (table 32 funcref))
  (import "env" "bump" (func $bump (param i32) (result i32)))
  (import "env" "base" (global $base i32))
  (global $counter (export "counter") (mut i32) (global.get $base))
  (global $fixed (export "fixed") i32 (i32.const 17))
  (func $even (export "even") (param i32) (result i32)
    local.get 0 i32.eqz
    if (result i32) i32.const 1
    else local.get 0 i32.const 1 i32.sub call $odd end)
  (func $run (export "run") (param i32) (result i32)
    local.get 0 call $even
    local.get 0 call $memory i32.add
    i32.const 3 call_indirect (result i32) i32.add
    call $bump)
  (func $odd (export "odd") (param i32) (result i32)
    local.get 0 i32.eqz
    if (result i32) i32.const 0
    else local.get 0 i32.const 1 i32.sub call $even end)
  (func $memory (export "memory_work") (param i32) (result i32)
    i32.const 4 local.get 0 i32.store
    i32.const 4 i32.load)
  (func $callback (export "callback") (result i32)
    global.get $counter i32.const 2 i32.add global.set $counter
    global.get $counter global.get $fixed i32.add)
  (func $pair (export "pair") (result i32 i64)
    call $callback i64.const 123456789)
  (func $pairuser (export "pairuser") (result i32)
    call $pair drop)
  (elem (i32.const 2) func $even $callback $odd $bump)
  (export "bump_alias" (func $bump))
  (export "table_alias" (table 0))
  (export "memory" (memory 0)))'''


def context(engine):
    store = w.Store(engine)
    linker = w.Linker(engine)
    memory = w.Memory(store, w.MemoryType(w.Limits(1, None)))
    table = w.Table(store, w.TableType(w.ValType.funcref(), w.Limits(256, None)), None)
    callback = w.Func(store, w.FuncType([w.ValType.i32()], [w.ValType.i32()]), lambda n: n + 1000)
    for name, value in [("memory", memory), ("table", table), ("bump", callback),
                        ("base", w.Global(store, w.GlobalType(w.ValType.i32(), False), 10))]:
        linker.define(store, "env", name, value)
    return store, linker, memory, table


def check_runtime(engine, original, manifest, offset=2):
    store1, linker1, mem1, table1 = context(engine)
    original_instance = linker1.instantiate(store1, w.Module.from_file(engine, str(original)))
    exp1 = original_instance.exports(store1)
    store2, linker2, mem2, table2 = context(engine)
    dt = manifest["dispatchTable"]
    dispatch = w.Table(store2, w.TableType(w.ValType.funcref(), w.Limits(dt["minimum"], dt["maximum"])), None)
    linker2.define(store2, dt["module"], dt["name"], dispatch)
    state = linker2.instantiate(store2, w.Module.from_file(engine, manifest["state"]["path"]))
    linker2.define_instance(store2, manifest["stateNamespace"], state)
    exp2 = dict(state.exports(store2))
    instances = [state]
    for part in manifest["shards"]:
        instance = linker2.instantiate(store2, w.Module.from_file(engine, part["path"]))
        instances.append(instance)
        exp2.update(instance.exports(store2))
        assert instance.exports(store2)["memory"].size(store2) == 1
    for n in [0, 1, 2, 5, 12, 21, 40]:
        for name in ["even", "odd", "run", "memory_work", "bump_alias"]:
            assert exp1[name](store1, n) == exp2[name](store2, n), (name, n)
        for name in ["pair", "pairuser", "callback"]:
            assert exp1[name](store1) == exp2[name](store2), name
        for name in ["counter", "fixed"]:
            assert exp1[name].value(store1) == exp2[name].value(store2), name
        assert mem1.read(store1) == mem2.read(store2)
    for slot, args in [(offset, [9]), (offset + 1, []), (offset + 2, [8]), (offset + 3, [123])]:
        assert table1.get(store1, slot)(store1, *args) == table2.get(store2, slot)(store2, *args)
    assert all(dispatch.get(store2, n) is not None for n in range(dt["minimum"]))


def explicit_table_zero(data):
    """Change element flag 0 to flag 2/table 0, matching the real backend."""
    def read(at):
        value, shift = 0, 0
        while True:
            byte = data[at]; at += 1
            value |= (byte & 127) << shift
            if byte < 128: return value, at
            shift += 7
    def leb(value):
        result = bytearray()
        while True:
            byte = value & 127; value >>= 7
            result.append(byte | (128 if value else 0))
            if not value: return result
    at = 8
    while at < len(data):
        kind = data[at]; size, start = read(at + 1); end = start + size
        if kind == 9:
            count, flag = read(start)
            assert count == 1 and data[flag] == 0
            assert data[flag + 1] == 0x41
            _, expr_end = read(flag + 2)
            assert data[expr_end] == 0x0b
            payload = data[start:flag] + b"\x02\x00" + data[flag+1:expr_end+1] + b"\x00" + data[expr_end+1:end]
            return data[:at] + b"\x09" + leb(len(payload)) + payload + data[end:]
        at = end
    raise AssertionError("no element section")


def check_manifest_rejections(source, out, record, checks):
    """Mutate only metadata, keeping every actual module and tool pin intact."""
    mutations = {}
    bad = deepcopy(record); bad["shards"][1] = deepcopy(bad["shards"][0]); mutations["duplicate-part"] = bad
    bad = deepcopy(record); bad["shards"][1]["path"] = bad["shards"][0]["path"]; mutations["duplicate-path"] = bad
    bad = deepcopy(record); bad["shards"][1]["definedFunctionStart"] += 1; bad["shards"][1]["definedFunctions"] -= 1
    mutations["missing-range"] = bad
    bad = deepcopy(record); bad["shards"][1]["definedFunctionStart"] -= 1; bad["shards"][1]["definedFunctions"] += 1
    mutations["overlapping-range"] = bad
    bad = deepcopy(record); bad["shards"][-1]["definedFunctionEndExclusive"] -= 1; bad["shards"][-1]["definedFunctions"] -= 1
    mutations["missing-final-range"] = bad
    bad = deepcopy(record); bad["shards"][0]["definedFunctions"] += 1; mutations["wrong-function-count"] = bad
    bad = deepcopy(record); bad["loadOrder"][2:4] = reversed(bad["loadOrder"][2:4]); mutations["wrong-load-order"] = bad
    bad = deepcopy(record); bad["globalExports"][0] = "wrong"; mutations["wrong-global-names"] = bad
    bad = deepcopy(record); bad["state"]["exports"].remove(bad["globalExports"][0]); mutations["missing-state-global"] = bad
    bad = deepcopy(record); bad["stateNamespace"] = "wrong"; mutations["wrong-state-namespace"] = bad
    bad = deepcopy(record); bad["dispatchTable"]["minimum"] -= 1; mutations["wrong-table-type"] = bad
    bad = deepcopy(record); bad["state"]["path"] = str(out / ".." / out.name / "state.wasm")
    mutations["noncanonical-path"] = bad
    bad = deepcopy(record); bad["totalModuleBytes"] += 1; mutations["wrong-total-bytes"] = bad
    manifest_path = out / "manifest.json"
    original = manifest_path.read_bytes()
    try:
        for name, bad in mutations.items():
            manifest_path.write_text(json.dumps(bad))
            with patch("subprocess.run", side_effect=AssertionError("verify invoked a process")):
                try:
                    shard.verify(source, out, len(record["shards"]), record["name"], tool_sha256=record["toolSha256"])
                except ValueError as error:
                    checks.append({"fixture": "manifest-" + name, "rejected": str(error)})
                else:
                    raise AssertionError("modified manifest accepted: " + name)
    finally:
        manifest_path.write_bytes(original)
    assert shard.verify(source, out, len(record["shards"]), record["name"], tool_sha256=record["toolSha256"]) == record


def check_aliased_tables(engine, binary, scratch, checks):
    # Original writes b then a. With one function per owner, owner-order writes
    # a then b if table aliases are ignored. Test both distinct and equal names.
    for second in ["table-alias", "table"]:
        wat = f'''(module
          (import "env" "memory" (memory 1))
          (import "env" "table" (table 2 funcref))
          (import "env" "{second}" (table 2 funcref))
          (func $a (result i32) i32.const 11)
          (func $b (result i32) i32.const 22)
          (func (export "read") (result i32) i32.const 0 call_indirect (result i32))
          (elem (table 0) (i32.const 0) func $b)
          (elem (table 1) (i32.const 0) func $a))'''
        source = scratch / ("alias-" + second + ".wasm")
        source.write_bytes(w.wat2wasm(wat))
        store, linker, _, table = context(engine)
        if second != "table": linker.define(store, "env", second, table)
        instance = linker.instantiate(store, w.Module.from_file(engine, str(source)))
        assert instance.exports(store)["read"](store) == 11
        assert table.get(store, 0)(store) == 11
        out = scratch / ("alias-" + second + "-parts")
        if out.exists(): shutil.rmtree(out)
        result = subprocess.run([str(binary), "--input", str(source), "--out", str(out), "--shards", "3"], capture_output=True, text=True)
        assert result.returncode != 0 and "potentially aliased" in result.stderr and not out.exists()
        assert not list(scratch.glob(f".{out.name}.pending-*"))
        checks.append({"fixture": "aliased-import-" + second, "originalCallback": 11, "rejected": result.stderr.strip()})


def main():
    scratch = HERE / "fixture-cache"
    scratch.mkdir(exist_ok=True)
    with shard.memory_lock():
        binary = shard.build(lock_held=True)
        config = w.Config(); config.parallel_compilation = False
        engine = w.Engine(config)
        original = scratch / "fixture.wasm"
        original.write_bytes(w.wat2wasm(WAT))
        checks = []
        for count in [1, 2, 3, 7]:
            out = scratch / f"parts-{count}"
            if out.exists(): shutil.rmtree(out)
            record = shard.ensure(original, out, count, "fixture", lock_held=True)
            check_runtime(engine, original, record)
            checks.append({"fixture": "globals-memory-elements-imports-recursion-results", "shards": count,
                           "crossCalls": record["rewrittenCrossShardCalls"], "bytes": record["totalModuleBytes"]})
        # Exercise a positive offset whose signed encoding needs two bytes.
        large_wat = WAT.replace("(table 32 funcref)", "(table 256 funcref)")
        large_wat = large_wat.replace("(i32.const 2) func", "(i32.const 130) func")
        large_wat = large_wat.replace("i32.const 3 call_indirect", "i32.const 131 call_indirect")
        original = scratch / "large-offset.wasm"
        original.write_bytes(w.wat2wasm(large_wat))
        out = scratch / "large-offset-parts"
        if out.exists(): shutil.rmtree(out)
        record = shard.ensure(original, out, 3, "large-offset", lock_held=True)
        check_runtime(engine, original, record, offset=130)
        checks.append({"fixture": "signed-leb-element-offset-130", "shards": 3})
        source = scratch / "explicit-table-zero.wasm"
        source.write_bytes(explicit_table_zero(w.wat2wasm(large_wat)))
        out = scratch / "explicit-table-zero-parts"
        if out.exists(): shutil.rmtree(out)
        record = shard.ensure(source, out, 3, "explicit-table-zero", lock_held=True)
        check_runtime(engine, source, record, offset=130)
        with patch("subprocess.run", side_effect=AssertionError("read-only verify invoked a process")):
            assert shard.verify(source, out, 3, "explicit-table-zero") == record
        target = Path(record["shards"][0]["path"])
        correct = target.read_bytes()
        target.write_bytes(correct[:-1] + bytes([correct[-1] ^ 1]))
        try:
            try: shard.verify(source, out, 3, "explicit-table-zero")
            except ValueError: pass
            else: raise AssertionError("modified artifact was accepted")
        finally: target.write_bytes(correct)
        checks.append({"fixture": "explicit-table-zero-offset-130-and-read-only-verification", "shards": 3})
        check_manifest_rejections(source, out, record, checks)
        check_aliased_tables(engine, binary, scratch, checks)
        # Tail calls use a separate fixture so its engine support is explicit.
        tail = scratch / "tail.wasm"
        tail.write_bytes(w.wat2wasm('''(module (import "env" "memory" (memory 1))
          (func $a (export "a") (param i32) (result i32) local.get 0 return_call $b)
          (func $b (export "b") (param i32) (result i32) local.get 0 i32.const 5 i32.add))'''))
        out = scratch / "tail-parts"
        if out.exists(): shutil.rmtree(out)
        record = shard.ensure(tail, out, 2, "tail", lock_held=True)
        store, linker, _, _ = context(engine)
        dt = record["dispatchTable"]
        linker.define(store, dt["module"], dt["name"], w.Table(store, w.TableType(w.ValType.funcref(), w.Limits(2, 2)), None))
        state = linker.instantiate(store, w.Module.from_file(engine, record["state"]["path"]))
        linker.define_instance(store, record["stateNamespace"], state)
        instances = [linker.instantiate(store, w.Module.from_file(engine, part["path"])) for part in record["shards"]]
        assert instances[0].exports(store)["a"](store, 37) == 42
        checks.append({"fixture": "cross-shard-tail-call", "result": 42})
        rejects = {
            "ref-func": '''(module (import "env" "memory" (memory 1))
                (func $a (result funcref) ref.func $a) (elem declare func $a))''',
            "start": '''(module (import "env" "memory" (memory 1)) (func $a) (start $a))''',
            "overlap": '''(module (import "env" "memory" (memory 1)) (import "env" "table" (table 2 funcref))
                (func $a) (elem (i32.const 0) $a) (elem (i32.const 0) $a))''',
            "global-offset": '''(module (import "env" "memory" (memory 1)) (import "env" "table" (table 2 funcref))
                (import "env" "base" (global i32)) (func $a) (elem (global.get 0) $a))''',
            "global-ref": '''(module (import "env" "memory" (memory 1)) (import "env" "table" (table 2 funcref))
                (global funcref (ref.func $a)) (func $a) (elem (i32.const 0) $a))''',
            "element-index": '''(module (import "env" "memory" (memory 1)) (import "env" "table" (table 2 funcref))
                (func $a i32.const 0 i32.const 0 i32.const 0 table.init 0) (elem (i32.const 0) $a))''',
        }
        for name, wat in rejects.items():
            source, out = scratch / (name + ".wasm"), scratch / (name + "-parts")
            source.write_bytes(w.wat2wasm(wat))
            if out.exists(): shutil.rmtree(out)
            result = subprocess.run([str(binary), "--input", str(source), "--out", str(out), "--shards", "1"], capture_output=True, text=True)
            assert result.returncode != 0 and not out.exists(), name
            assert not list(scratch.glob(f".{out.name}.pending-*")), name
            checks.append({"fixture": name, "rejected": result.stderr.strip()})
        # A reference in an otherwise supported active function-list module must
        # reach the body-specific rejection, rather than the declared-element guard.
        source = scratch / "body-ref.wasm"
        source.write_bytes(w.wat2wasm('''(module (import "env" "memory" (memory 1))
          (import "env" "table" (table 2 funcref)) (func $a (result funcref) ref.func $a)
          (elem (i32.const 0) func $a))'''))
        out = scratch / "body-ref-parts"
        result = subprocess.run([str(binary), "--input", str(source), "--out", str(out), "--shards", "1"], capture_output=True, text=True)
        assert result.returncode != 0 and "ref.func in body" in result.stderr and not out.exists()
        checks.append({"fixture": "body-ref", "rejected": result.stderr.strip()})
        (scratch / "results.json").write_text(json.dumps(checks, indent=2) + "\n")
        print(json.dumps(checks, indent=2))


if __name__ == "__main__": main()
