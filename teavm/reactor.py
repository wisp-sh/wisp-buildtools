"""Prepare TeaVM C/WASI reactors, with support for the legacy 0.13 WASI layout."""
from __future__ import annotations
import re

PAGE = 65536


def data_size(value: str) -> int:
    """Count bytes in wasm-tools' canonical data string syntax."""
    size = 0
    position = 0
    while position < len(value):
        if value[position] != "\\":
            size += len(value[position].encode("utf-8"))
            position += 1
        elif re.match(r"[0-9a-fA-F]{2}", value[position + 1:position + 3]):
            size += 1
            position += 3
        elif position + 1 < len(value) and value[position + 1] in "tnr\"'\\":
            size += 1
            position += 2
        else:
            raise ValueError("unexpected escape in TeaVM static data")
    return size


def prepare(text: str) -> tuple[str, dict]:
    # Current TeaVM uses the C backend. Its WASI reactor and aligned libc
    # allocator already initialize after the Preview 1 adapter is ready.
    if (not re.search(r"^  \(start ", text, re.MULTILINE)
            and re.search(r'\(export "_initialize" \(func ', text)
            and re.search(r'\(export "cabi_realloc_adapter" \(func ', text)):
        return text, {"backend": "TeaVM C/WASI", "allocator": "WASI libc aligned_alloc",
                      "initialization": "crt1-reactor constructors"}
    if (not re.search(r"^  \(start ", text, re.MULTILINE)
            and '(export "wisp_component_initialize" (func ' in text
            and '(export "cabi_realloc_adapter" (func ' in text):
        text = text.replace('(export "wisp_component_initialize" (func ', '(export "_initialize" (func ')
        text = re.sub(r'^  \(export "_start" \(func \d+\)\)\n', '', text, flags=re.MULTILINE)
        return text, {"backend": "TeaVM C/WASI command", "allocator": "WASI libc aligned_alloc",
                      "initialization": "explicit reactor initializer, command entry removed"}
    starts = list(re.finditer(r"^  \(start (\d+)\)\n", text, re.MULTILINE))
    mains = re.findall(r'^  \(export "_start" \(func (\d+)\)\)\n', text, re.MULTILINE)
    if len(starts) != 1 or len(mains) != 1:
        raise ValueError("expected the pinned TeaVM WASI startup and _start export")
    start = starts[0][1]
    pattern = r"(^  \(func (?:\$[^\s()]+ )?\(;" + start + r";\)[^\n]*\n)(.*?)(^  \))"
    initializer = re.search(pattern, text, re.MULTILINE | re.DOTALL)
    if initializer is None:
        raise ValueError("cannot locate TeaVM heap initializer")
    heap_pattern = (r"(call \$initclass_[^\s]*WasmHeap\s+)(i32.const (\d+))"
                    r"(\s+i32.const (\d+)\s+i32.const (\d+)\s+i32.const (\d+)"
                    r"\s+i32.const (\d+)\s+call [^\s]+)")
    heap = re.search(heap_pattern, initializer[2])
    if heap is None:
        raise ValueError("TeaVM heap layout differs from the pinned backend")
    old_heap = int(heap[3])
    if old_heap <= 0 or old_heap >= 1 << 30:
        raise ValueError("invalid TeaVM static data boundary")
    data = list(re.finditer(r'^  \(data [^\n]*?\(i32.const (\d+)\) "((?:[^"\\]|\\.)*)"\)\n',
                           text, re.MULTILINE))
    if len(data) != len(re.findall(r"^  \(data ", text, re.MULTILINE)):
        raise ValueError("expected active TeaVM data segments with constant offsets")
    if any(int(segment[1]) + data_size(segment[2]) > old_heap for segment in data):
        raise ValueError("TeaVM static data overlaps the heap boundary")
    arena_start = (old_heap + PAGE - 1) & -PAGE
    arena_end = arena_start + 2 * PAGE
    if re.search(r'\(export\s+"(?:_initialize|cabi_realloc_adapter)"\s', text):
        raise ValueError("reserved TeaVM reactor export already exists")
    memories = list(re.finditer(r"^  \(memory \(;0;\) (\d+)(?: (\d+))?\)\n", text, re.MULTILINE))
    if len(memories) != 1 or re.search(r'\(import[^\n]*\(memory', text):
        raise ValueError("TeaVM reactor requires one owned unshared wasm32 memory")
    memory = memories[0]
    pages = arena_end // PAGE
    maximum = int(memory[2]) + (arena_end - old_heap + PAGE - 1) // PAGE if memory[2] else None
    replacement = f"  (memory (;0;) {max(int(memory[1]), pages)}" + (f" {maximum}" if maximum else "") + ")\n"
    body = initializer[2][:heap.start(2)] + f"i32.const {arena_end}" + initializer[2][heap.end(2):]
    text = text[:initializer.start(2)] + body + text[initializer.end(2):]
    text = re.sub(r"^  \(memory \(;0;\) \d+(?: \d+)?\)\n", replacement, text, flags=re.MULTILINE)
    text = re.sub(r"^  \(start \d+\)\n", "", text, flags=re.MULTILINE)
    text = re.sub(r'^  \(export "_start" \(func \d+\)\)\n', "", text, flags=re.MULTILINE)
    functions = f"""
  (global $wispAdapterNext (mut i32) (i32.const {arena_start}))
  (func $wispAdapterRealloc (export "cabi_realloc_adapter")
    (param $old i32) (param $oldLen i32) (param $align i32) (param $size i32)
    (result i32) (local $ptr i32)
    local.get $old local.get $oldLen i32.or
    local.get $size i32.const {PAGE} i32.ne i32.or
    local.get $align i32.const {PAGE} i32.gt_u i32.or
    local.get $align i32.eqz i32.or
    local.get $align local.get $align i32.const 1 i32.sub i32.and i32.or
    if unreachable end
    global.get $wispAdapterNext local.tee $ptr
    i32.const {PAGE} i32.add i32.const {arena_end} i32.gt_u
    if unreachable end
    local.get $ptr i32.const {PAGE} i32.add global.set $wispAdapterNext
    local.get $ptr)
  (func $wispJavaInitialize (export "_initialize")
    call {start}
    call {mains[0]})
"""
    end = text.rfind(")")
    return text[:end] + functions + text[end:], {"start": arena_start, "end": arena_end,
            "originalHeapStart": old_heap, "heapStart": arena_end, "backend": "TeaVM 0.13.1 WASI"}
