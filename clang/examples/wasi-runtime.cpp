#include <cstddef>
#include <cstdarg>
#include <cstdlib>
#include <string>
#include <vector>

// This global needs the reactor's _initialize export before test is called.
static std::string label = "wasi-runtime";

extern "C" int test() {
  auto *allocation = static_cast<int *>(std::malloc(sizeof(int)));
  if (!allocation) return -1;
  *allocation = 20;
  std::vector<int> values{*allocation, 22};
  std::free(allocation);
  return label == "wasi-runtime" ? values.at(0) + values.at(1) : -2;
}
