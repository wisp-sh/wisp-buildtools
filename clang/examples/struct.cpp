struct Foo { int value; };
extern "C" int test() { Foo f{42}; return f.value; }
