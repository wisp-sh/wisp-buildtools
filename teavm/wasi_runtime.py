"""Port TeaVM's fixed C runtime resources to WASI during bootstrap."""
from __future__ import annotations

import hashlib
import zipfile


def patch_runtime(source, target):
    changed = []
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        for name in sorted(original.namelist()):
            if name.endswith("/"):
                continue
            data = original.read(name)
            before = data
            if name == "org/teavm/backend/c/fiber.c":
                text = data.decode()
                text = text.replace("defined(__EMSCRIPTEN__)", "(defined(__EMSCRIPTEN__) || defined(__wasi__))")
                text = text.replace("#ifdef __EMSCRIPTEN__", "#if defined(__EMSCRIPTEN__) || defined(__wasi__)")
                old = """        void teavm_waitFor(int64_t timeout) {
            abort();
        }"""
                new = """        void teavm_waitFor(int64_t timeout) {
            if (timeout < 0) abort();
            struct timespec request = { timeout / 1000, (timeout % 1000) * 1000000 };
            nanosleep(&request, NULL);
        }"""
                if text.count(old) != 1:
                    raise ValueError("TeaVM fiber wait implementation changed")
                text = text.replace(old, new).replace("void teavm_interrupt() {\n            abort();", "void teavm_interrupt() {\n            /* The WASI profile runs one cooperative scheduler thread. */")
                data = text.encode()
            elif name == "org/teavm/backend/c/arrayclass.c":
                text = data.decode()
                replacements = {
                    "static TEAVM_OBJECT_CLASS teavm_dynamicClassPool[TEAVM_DYNAMIC_CLASS_POOL_CAPACITY];":
                        "typedef struct { _Alignas(8) TEAVM_OBJECT_CLASS value; } TeaVM_AlignedArrayClass;\n"
                        "static TeaVM_AlignedArrayClass teavm_dynamicClassPool[TEAVM_DYNAMIC_CLASS_POOL_CAPACITY];",
                    "&teavm_dynamicClassPool[teavm_dynamicClassPoolSize++]":
                        "&teavm_dynamicClassPool[teavm_dynamicClassPoolSize++].value",
                    "&teavm_dynamicClassPool[index].parent": "&teavm_dynamicClassPool[index].value.parent",
                }
                for old, new in replacements.items():
                    if text.count(old) != 1:
                        raise ValueError("TeaVM dynamic array class pool changed")
                    text = text.replace(old, new)
                data = text.encode()
            elif name == "org/teavm/backend/c/memory.c":
                text = data.decode()
                marker = "#if defined(__EMSCRIPTEN__)"
                if text.count(marker) != 1:
                    raise ValueError("TeaVM virtual memory implementation changed")
                text = text.replace(marker, "#if defined(__EMSCRIPTEN__) || defined(__wasi__)")
                text = text.replace("return malloc(size);", "return calloc(1, size);")
                marker = "static void teavm_virtualUncommit(void* address, int64_t size) {\n        mprotect(address, size, PROT_NONE);\n    }"
                if text.count(marker) < 1:
                    raise ValueError("TeaVM virtual uncommit implementation changed")
                text = text.replace(marker, "static void teavm_virtualUncommit(void* address, int64_t size) {\n        memset(address, 0, size);\n    }", 1)
                data = text.encode()
            elif name == "org/teavm/backend/c/file.c":
                text = data.decode()
                start = text.index("    struct passwd *pw = getpwuid(getuid());")
                end = text.index("\n}", start)
                text = text[:start] + """    const char *home = getenv("HOME");
    if (!home) home = "/";
    int32_t length;
    *result = teavm_mbToChar16((char *)home, &length);
    return length;""" + text[end:]
                data = text.replace("#include <pwd.h>", "").encode()
            if data != before:
                changed.append({"resource": name, "originalSha256": hashlib.sha256(before).hexdigest(),
                                "patchedSha256": hashlib.sha256(data).hexdigest()})
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            output.writestr(info, data)
    if len(changed) != 4:
        raise ValueError("expected four TeaVM C runtime resource patches")
    return changed
