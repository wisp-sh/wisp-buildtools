#include <mutex>
int main() { std::mutex m; m.lock(); m.unlock(); return 0; }
