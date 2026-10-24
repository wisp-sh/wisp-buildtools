constexpr int answer() { return 42; }
extern "C" int test() { return answer(); }
