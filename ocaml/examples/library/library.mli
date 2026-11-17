type mode = Quiet | Loud
type profile = { name : string; payload : bytes; mode : mode }
type point = { x : float; y : float }
val add : int32 -> int32 -> int32
val echo : profile -> profile
val points : point list -> point list
val maybe : string option -> string option
val safe_divide : int32 -> int32 -> (int32, string) result
val check : bool -> (unit, string) result
val wide : int64 -> int64
val next : unit -> int32
val collect : unit -> unit
val explode : unit -> int32
