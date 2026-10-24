template <typename T>
T add(T a, T b) { return a + b; }
extern "C" int test() { return add<int>(20, 22); }
