@_expose(wasm, "add")
@_cdecl("add")
public func add(_ a: UInt32, _ b: UInt32) -> UInt32 { a &+ b }

@_expose(wasm, "widen")
@_cdecl("widen")
public func widen(_ value: UInt64) -> UInt64 { value &+ 1 }

@_expose(wasm, "scale")
@_cdecl("scale")
public func scale(_ value: Double) -> Double { value * 1.5 }
