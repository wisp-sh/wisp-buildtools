import std/[macros, sequtils]

macro compileTimeAnswer(): untyped =
  newLit(40'i32)

var values = @[compileTimeAnswer(), 2'i32]

proc sumGeneric[T: SomeInteger](items: openArray[T]): T =
  for value in items:
    result += value

proc initialized*(): int32 {.exportc, cdecl.} =
  sumGeneric(values)

proc doubled*(): int32 {.exportc, cdecl.} =
  values.mapIt(it * 2).foldl(a + b)
