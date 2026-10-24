@_expose(wasm, "stress")
@_cdecl("stress")
public func stress() -> UInt32 {
    print("swift-ready")
    let values = Array(repeating: UInt32(42), count: 2_100_000)
    precondition(values[2_099_999] == 42)
    print("swift-allocation-ready")
    fflush(nil)
    return values.last!
}
import WASILibc
