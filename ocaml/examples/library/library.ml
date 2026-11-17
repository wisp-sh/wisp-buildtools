type mode = Quiet | Loud
type profile = { name : string; payload : bytes; mode : mode }
type point = { x : float; y : float }
let add a b = Int32.add (Int32.add a b) Support.offset
let echo p = { p with name = p.name ^ "!"; payload = Bytes.copy p.payload }
let points xs = List.map (fun p -> { x = p.x +. 0.5; y = p.y -. 0.5 }) xs
let maybe x = Option.map (fun s -> s ^ "!") x
let safe_divide a b = if b = 0l then Error "division by zero" else Ok (Int32.div a b)
let check b = if b then Ok () else Error "false"
let wide n = Int64.add n 1L
let counter = ref 0l
let next () = counter := Int32.add !counter 1l; !counter
let collect () = Gc.full_major (); Gc.compact ()
let explode () = failwith "expected OCaml exception"
