#include <caml/callback.h>
__attribute__((constructor)) static void initialize_ocaml(void) {
  static char *argv[] = {"ocaml-component", 0};
  caml_startup(argv);
}
