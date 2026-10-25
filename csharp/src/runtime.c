#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <mono/metadata/appdomain.h>
#include <mono/metadata/class.h>
#include <mono/metadata/loader.h>
#include <driver.h>
extern void mono_free(void *);

#define PAYLOAD_CAPACITY (16 * 1024 * 1024)
#define MAX_CALL_BYTES (64 * 1024 * 1024)
__attribute__((used)) volatile struct {
    unsigned char magic[32];
    uint32_t length;
    unsigned char bytes[PAYLOAD_CAPACITY];
} wisp_payload = {"WISP_CSHARP_BUNDLE_V2", 0, {0}};
#include "bundle.h"

const char *dotnet_wasi_getentrypointassemblyname(void) { return "Application.dll"; }
static MonoMethod *entry;
static int initialized, busy;
static uint64_t arguments[16], results[2];
struct Allocation { void *pointer; uint32_t size; struct Allocation *next; };
static struct Allocation *allocations;
static uint32_t allocated_bytes;
static void fail(const char *message) { fprintf(stderr, "csharp: %s\n", message); abort(); }

__attribute__((export_name("cabi_realloc")))
void *cabi_realloc(void *old, uint32_t old_size, uint32_t alignment, uint32_t size) {
    if (alignment > 16 || (alignment && (alignment & (alignment - 1)))) fail("unsupported canonical alignment");
    if (size > MAX_CALL_BYTES || old_size > MAX_CALL_BYTES) fail("canonical allocation exceeds 64 MiB");
    if (!size) { free(old); return NULL; }
    void *pointer = realloc(old, size);
    if (!pointer) fail("canonical allocation failed");
    return pointer;
}
static void release_managed(int32_t pointer) { free((void *)(uintptr_t)(uint32_t)pointer); }
static int32_t allocate_managed(int32_t size) {
    if (size < 0 || (uint32_t)size > MAX_CALL_BYTES - allocated_bytes) fail("canonical call exceeds 64 MiB");
    void *pointer = cabi_realloc(NULL, 0, 8, (uint32_t)size);
    if (pointer) {
        struct Allocation *allocation = malloc(sizeof(*allocation));
        if (!allocation) fail("canonical allocation tracking failed");
        allocation->pointer = pointer; allocation->size = (uint32_t)size;
        allocation->next = allocations; allocations = allocation; allocated_bytes += (uint32_t)size;
    }
    return (int32_t)(uintptr_t)pointer;
}
static uint32_t word(const unsigned char **cursor, const unsigned char *end) {
    if (end - *cursor < 4) fail("truncated managed bundle");
    uint32_t value; memcpy(&value, *cursor, 4); *cursor += 4; return value;
}
static void initialize(void) {
    if (initialized) return;
    if (!wisp_payload.length || wisp_payload.length > PAYLOAD_CAPACITY) fail("invalid managed bundle length");
    register_bundled_assemblies();
    const unsigned char *cursor = (const unsigned char *)wisp_payload.bytes, *end = cursor + wisp_payload.length;
    uint32_t count = word(&cursor, end);
    if (!count || count > 128) fail("managed assembly count exceeds 128");
    char primary[256] = {0};
    for (uint32_t i = 0; i < count; ++i) {
        uint32_t length = word(&cursor, end);
        if (!length || length > 250 || end - cursor < length) fail("invalid assembly name");
        char name[256]; memcpy(name, cursor, length); name[length] = 0; cursor += length;
        if (!i) memcpy(primary, name, length + 1);
        strcat(name, ".dll");
        uint32_t bytes = word(&cursor, end);
        if (!bytes || end - cursor < bytes) fail("invalid managed assembly length");
        mono_wasm_add_assembly(name, cursor, bytes); cursor += bytes;
    }
    if (cursor != end) fail("trailing managed bundle data");
    setenv("DOTNET_SYSTEM_GLOBALIZATION_INVARIANT", "1", 1);
    mono_wasm_load_runtime("", 0);
    mono_add_internal_call("Wisp.Canonical::Allocate", allocate_managed);
    mono_add_internal_call("Wisp.Canonical::Release", release_managed);
    MonoAssembly *assembly = mono_wasm_assembly_load(primary);
    if (!assembly) fail("cannot load entry assembly");
    MonoClass *klass = mono_wasm_assembly_find_class(assembly, "Wisp.Generated", "Bridge");
    if (!klass) fail("missing generated binding class");
    entry = mono_wasm_assembly_find_method(klass, "Invoke", 4);
    if (!entry) fail("missing generated binding entrypoint");
    initialized = 1;
}
__attribute__((export_name("wisp-validate"))) int32_t wisp_validate(void) { initialize(); return 1; }
__attribute__((export_name("__wisp_csharp_begin"))) int32_t begin_call(void) {
    initialize();
    if (busy) fail("overlapping calls or missing canonical post-return");
    busy = 1; memset(arguments, 0, sizeof(arguments)); memset(results, 0, sizeof(results));
    return (int32_t)(uintptr_t)arguments;
}
__attribute__((export_name("__wisp_csharp_release"))) void release_input(int32_t pointer) { release_managed(pointer); }
__attribute__((export_name("__wisp_csharp_end"))) void end_call(void) {
    while (allocations) { struct Allocation *next = allocations->next; free(allocations->pointer); free(allocations); allocations = next; }
    allocated_bytes = 0; busy = 0;
}
__attribute__((export_name("__wisp_csharp_invoke"))) int32_t invoke(int32_t index, int32_t input, int32_t mode) {
    if (!busy) fail("binding call was not initialized");
    int32_t output = (int32_t)(uintptr_t)results;
    void *args[] = {&index, &input, &mode, &output};
    MonoObject *exception = NULL;
    mono_runtime_invoke(entry, NULL, args, &exception);
    if (exception) {
        MonoClass *klass = mono_object_get_class(exception);
        fprintf(stderr, "csharp: managed %s", mono_class_get_name(klass));
        MonoClassField *field = mono_class_get_field_from_name(mono_get_exception_class(), "_message");
        MonoString *message = NULL;
        if (field) mono_field_get_value(exception, field, &message);
        if (message) { char *text = mono_string_to_utf8(message); if (text) { fprintf(stderr, ": %s", text); mono_free(text); } }
        fprintf(stderr, "\n"); fail("export threw a managed exception");
    }
    return output;
}
