#include <vector>

extern "C" __attribute__((export_name("test")))
int test() {
    std::vector<int> values{1, 2, 3};
    return values.size();
}
