#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <mono/metadata/attrdefs.h>
#include <mono/metadata/class.h>
#include <mono/metadata/debug-helpers.h>
#include <mono/metadata/metadata.h>
#include <driver.h>

#define IL_CAPACITY (1024 * 1024)
// The Wasm-hosted packager fills this fixed data segment without relinking.
__attribute__((used)) volatile struct {
    unsigned char magic[32];
    uint32_t length;
    unsigned char il[IL_CAPACITY];
} wisp_payload = {"WISP_CSHARP_IL_PAYLOAD_V1", 0, {0}};

#include "bundle.h"

const char *dotnet_wasi_getentrypointassemblyname(void) { return "Application.dll"; }

static MonoMethod *entry;
static int initialized;
static void fail(const char *message) {
    fprintf(stderr, "csharp: %s\n", message);
    abort();
}

static void initialize(void) {
    if (initialized) return;
    if (!wisp_payload.length || wisp_payload.length > IL_CAPACITY) fail("invalid embedded IL length");
    register_bundled_assemblies();
    mono_wasm_add_assembly("Application.dll", (const unsigned char *)wisp_payload.il, wisp_payload.length);
    setenv("DOTNET_SYSTEM_GLOBALIZATION_INVARIANT", "1", 1);
    mono_wasm_load_runtime("", 0);
    MonoAssembly *assembly = mono_wasm_assembly_load("Application");
    if (!assembly) fail("cannot load Application assembly");
    MonoClass *klass = mono_wasm_assembly_find_class(assembly, "", "Calculator");
    if (!klass) fail("missing public Calculator class");
    if ((mono_class_get_flags(klass) & MONO_TYPE_ATTR_VISIBILITY_MASK) != MONO_TYPE_ATTR_PUBLIC)
        fail("Calculator class must be public");
    void *iter = NULL;
    MonoMethod *method;
    while ((method = mono_class_get_methods(klass, &iter))) {
        if (strcmp(mono_method_get_name(method), "Add")) continue;
        if (entry) fail("overloaded Calculator.Add is unsupported");
        entry = method;
    }
    if (!entry) fail("missing Calculator.Add method");
    uint32_t flags = mono_method_get_flags(entry, NULL);
    if (!(flags & MONO_METHOD_ATTR_STATIC) || (flags & MONO_METHOD_ATTR_ACCESS_MASK) != MONO_METHOD_ATTR_PUBLIC)
        fail("Calculator.Add must be public and static");
    MonoMethodSignature *signature = mono_method_signature(entry);
    if (mono_signature_get_param_count(signature) != 2 || mono_type_is_byref(mono_signature_get_return_type(signature)) ||
        mono_type_get_type(mono_signature_get_return_type(signature)) != MONO_TYPE_I4)
        fail("Calculator.Add must return int and accept two int parameters");
    iter = NULL;
    MonoType *type;
    while ((type = mono_signature_get_params(signature, &iter))) {
        if (mono_type_is_byref(type) || mono_type_get_type(type) != MONO_TYPE_I4)
            fail("Calculator.Add parameters must be int values");
    }
    initialized = 1;
}

__attribute__((export_name("wisp-validate"))) int32_t wisp_validate(void) {
    initialize();
    return 1;
}

__attribute__((export_name("add"))) int32_t add(int32_t left, int32_t right) {
    initialize();
    void *arguments[] = {&left, &right};
    MonoObject *exception = NULL;
    // This runtime is single-threaded. Avoid the upstream helper's call to
    // Exception.ToString(), which may itself require unsupported BCL APIs.
    MonoObject *result = mono_runtime_invoke(entry, NULL, arguments, &exception);
    if (exception) {
        fail("Calculator.Add threw a managed exception");
    }
    if (!result) fail("Calculator.Add returned no value");
    return mono_unbox_int(result);
}
