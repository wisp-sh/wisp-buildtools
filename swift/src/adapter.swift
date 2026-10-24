import WASILibc

// The Preview 1 adapter requests two persistent pages, for its stack and state.
// Swift and this allocator use the same libc heap, including before _initialize.
@_expose(wasm, "cabi_realloc_adapter")
@_cdecl("cabi_realloc_adapter")
public func wispAdapterRealloc(
    _ oldPointer: UnsafeMutableRawPointer?,
    _ oldSize: Int,
    _ alignment: Int,
    _ newSize: Int
) -> UnsafeMutableRawPointer? {
    precondition(oldPointer == nil && oldSize == 0 && newSize == 65536)
    precondition(alignment > 0 && alignment <= 65536)
    var pointer: UnsafeMutableRawPointer? = nil
    let result = posix_memalign(&pointer, max(16, alignment), newSize)
    precondition(result == 0 && pointer != nil)
    return pointer
}
