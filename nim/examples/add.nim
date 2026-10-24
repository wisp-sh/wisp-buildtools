proc add*(a, b: int32): int32 {.exportc, cdecl.} =
  a + b
