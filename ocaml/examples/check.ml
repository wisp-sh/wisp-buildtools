exception Expected of int

let () =
  assert (Sys.word_size = 32);
  let values = Array.init 100_000 (fun i -> string_of_int i) in
  Gc.full_major ();
  Gc.compact ();
  assert (values.(99_999) = "99999");
  assert (Int64.add 0x1_0000_0000L 42L = 0x1_0000_002aL);
  assert (abs_float (sqrt 2. *. sqrt 2. -. 2.) < 1e-10);
  let answer = try raise (Expected 42) with Expected n -> n in
  let table = Hashtbl.create 3 in
  Hashtbl.add table "answer" answer;
  assert (Hashtbl.find table "answer" = 42);
  let input = open_in_bin "input.bin" in
  let bytes = really_input_string input 4 in
  close_in input;
  assert (bytes = "\000\255\128\001");
  let output = open_out_bin "output.bin" in
  output_string output bytes;
  close_out output;
  Printf.printf "%d\n" answer
