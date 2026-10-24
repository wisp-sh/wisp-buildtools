#include <stdint.h>
#include <stdlib.h>

extern int wisp_java_main(int argc, char **argv);
extern void __wasm_call_ctors(void);

#ifdef WISP_REACTOR
__attribute__((constructor)) static void wisp_java_initialize(void) {
    wisp_java_main(0, NULL);
}
#else
int main(int argc, char **argv) {
    return wisp_java_main(argc, argv);
}

// Keep Core command output usable by the standalone Component assembler too.
__attribute__((export_name("wisp_component_initialize"))) void wisp_component_initialize(void) {
    __wasm_call_ctors();
    wisp_java_main(0, NULL);
}
#endif

__attribute__((export_name("cabi_realloc_adapter")))
uintptr_t cabi_realloc_adapter(uintptr_t old, size_t old_size, size_t align, size_t size) {
    if (old || old_size || size != 65536 || !align || align > 65536 || (align & (align - 1))) abort();
    void *result = aligned_alloc(align, size);
    if (!result) abort();
    return (uintptr_t)result;
}
