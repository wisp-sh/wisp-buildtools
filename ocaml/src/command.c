#include <caml/callback.h>
int main(int argc, char **argv) {
  caml_startup(argv);
  caml_shutdown();
  return 0;
}
