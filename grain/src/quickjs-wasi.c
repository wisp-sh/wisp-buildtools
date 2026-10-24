/* QuickJS command guest. All filesystem operations below use WASI libc. */
#include "quickjs.h"
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static JSValue fail(JSContext *ctx, const char *operation) {
    JSValue error = JS_NewError(ctx);
    JS_SetPropertyStr(ctx, error, "message", JS_NewString(ctx, strerror(errno)));
    JS_SetPropertyStr(ctx, error, "code", JS_NewString(ctx, errno == ENOENT ? "ENOENT" : "EIO"));
    JS_SetPropertyStr(ctx, error, "syscall", JS_NewString(ctx, operation));
    JS_SetPropertyStr(ctx, error, "errno", JS_NewInt32(ctx, errno));
    return JS_Throw(ctx, error);
}

static JSValue io(JSContext *ctx, JSValueConst self, int argc, JSValueConst *argv, int operation) {
    int fd = 0, flags = 0, result = 0;
    int64_t offset = 0, length = 0, position = -1;
    const char *path = NULL, *other = NULL;
    JSValue value = JS_UNDEFINED;
    if (operation <= 1 || operation >= 7) path = JS_ToCString(ctx, argv[0]);
    else JS_ToInt32(ctx, &fd, argv[0]);
    switch (operation) {
    case 0:
        JS_ToInt32(ctx, &flags, argv[1]);
        result = open(path, flags, 0666);
        break;
    case 1: case 5: {
        struct stat info;
        result = operation == 1 ? stat(path, &info) : fstat(fd, &info);
        if (!result) {
            value = JS_NewObject(ctx);
#define FIELD(name, expression) JS_SetPropertyStr(ctx, value, name, JS_NewFloat64(ctx, expression))
            FIELD("dev", info.st_dev); FIELD("ino", info.st_ino); FIELD("mode", info.st_mode);
            FIELD("nlink", info.st_nlink); FIELD("uid", info.st_uid); FIELD("gid", info.st_gid);
            FIELD("rdev", info.st_rdev); FIELD("size", info.st_size);
            FIELD("atimeMs", info.st_atim.tv_sec * 1000.0 + info.st_atim.tv_nsec / 1e6);
            FIELD("mtimeMs", info.st_mtim.tv_sec * 1000.0 + info.st_mtim.tv_nsec / 1e6);
            FIELD("ctimeMs", info.st_ctim.tv_sec * 1000.0 + info.st_ctim.tv_nsec / 1e6);
#undef FIELD
        }
        break;
    }
    case 2: result = close(fd); break;
    case 3: case 4: {
        size_t byte_offset, byte_length, element_size, available;
        JSValue buffer = JS_GetTypedArrayBuffer(ctx, argv[1], &byte_offset, &byte_length, &element_size);
        if (JS_IsException(buffer)) return buffer;
        uint8_t *bytes = JS_GetArrayBuffer(ctx, &available, buffer);
        JS_ToInt64(ctx, &offset, argv[2]); JS_ToInt64(ctx, &length, argv[3]);
        if (argc > 4 && !JS_IsNull(argv[4]) && !JS_IsUndefined(argv[4])) JS_ToInt64(ctx, &position, argv[4]);
        if (!bytes || offset < 0 || length < 0 || (uint64_t)offset + length > byte_length) {
            JS_FreeValue(ctx, buffer);
            return JS_ThrowRangeError(ctx, "invalid filesystem buffer range");
        }
        if (position >= 0 && lseek(fd, position, SEEK_SET) < 0) result = -1;
        else if (operation == 3) result = read(fd, bytes + byte_offset + offset, length);
        else result = write(fd, bytes + byte_offset + offset, length);
        JS_FreeValue(ctx, buffer);
        break;
    }
    case 6: JS_ToInt64(ctx, &length, argv[1]); result = ftruncate(fd, length); break;
    case 7: result = mkdir(path, 0777); break;
    case 8: result = unlink(path); break;
    case 9: result = rmdir(path); break;
    case 10: other = JS_ToCString(ctx, argv[1]); result = rename(path, other); break;
    case 11: {
        DIR *directory = opendir(path);
        if (!directory) { result = -1; break; }
        value = JS_NewArray(ctx);
        struct dirent *entry;
        unsigned index = 0;
        while ((entry = readdir(directory))) {
            if (!strcmp(entry->d_name, ".") || !strcmp(entry->d_name, "..")) continue;
            JS_SetPropertyUint32(ctx, value, index++, JS_NewString(ctx, entry->d_name));
        }
        closedir(directory);
        break;
    }
    case 12: {
        char buffer[4096];
        ssize_t count = readlink(path, buffer, sizeof(buffer));
        if (count < 0) result = -1;
        else value = JS_NewStringLen(ctx, buffer, count);
        break;
    }
    default: result = -1; errno = ENOSYS;
    }
    JS_FreeCString(ctx, path); JS_FreeCString(ctx, other);
    if (result < 0) { JS_FreeValue(ctx, value); return fail(ctx, "filesystem"); }
    return JS_IsUndefined(value) ? JS_NewInt32(ctx, result) : value;
}

static JSValue print(JSContext *ctx, JSValueConst self, int argc, JSValueConst *argv, int stream) {
    FILE *output = stream ? stderr : stdout;
    for (int i = 0; i < argc; i++) {
        const char *text = JS_ToCString(ctx, argv[i]);
        if (!text) return JS_EXCEPTION;
        if (i) fputc(' ', output);
        fputs(text, output); JS_FreeCString(ctx, text);
    }
    fputc('\n', output); fflush(output);
    return JS_UNDEFINED;
}

static JSValue quit(JSContext *ctx, JSValueConst self, int argc, JSValueConst *argv) {
    int code = 0;
    if (argc) JS_ToInt32(ctx, &code, argv[0]);
    exit(code);
}

static int evaluate(JSContext *ctx, const char *path, const char *bytecode_output) {
    FILE *file = fopen(path, "rb");
    if (!file) { perror(path); return 1; }
    fseek(file, 0, SEEK_END); long size = ftell(file); rewind(file);
    char *source = malloc(size + 1);
    if (!source || fread(source, 1, size, file) != (size_t)size) { fclose(file); free(source); return 1; }
    source[size] = 0; fclose(file);
    JSValue result;
    size_t path_length = strlen(path);
    if (path_length > 4 && !strcmp(path + path_length - 4, ".qbc")) {
        result = JS_ReadObject(ctx, (const uint8_t *)source, size, JS_READ_OBJ_BYTECODE);
        if (!JS_IsException(result)) result = JS_EvalFunction(ctx, result);
    } else {
        result = JS_Eval(ctx, source, size, "grainc.js", JS_EVAL_TYPE_GLOBAL | (bytecode_output ? JS_EVAL_FLAG_COMPILE_ONLY : 0));
    }
    free(source);
    if (JS_IsException(result)) {
        JSValue error = JS_GetException(ctx);
        const char *message = JS_ToCString(ctx, error);
        fprintf(stderr, "%s\n", message ? message : "JavaScript exception");
        JS_FreeCString(ctx, message);
        JSValue stack = JS_GetPropertyStr(ctx, error, "stack");
        message = JS_ToCString(ctx, stack);
        if (message) fprintf(stderr, "%s\n", message);
        JS_FreeCString(ctx, message); JS_FreeValue(ctx, stack); JS_FreeValue(ctx, error);
        JS_FreeValue(ctx, result); return 1;
    }
    if (bytecode_output) {
        size_t bytecode_size = 0;
        uint8_t *bytecode = JS_WriteObject(ctx, &bytecode_size, result, JS_WRITE_OBJ_BYTECODE);
        if (!bytecode) { JS_FreeValue(ctx, result); return 1; }
        FILE *output = fopen(bytecode_output, "wb");
        int failed = !output || fwrite(bytecode, 1, bytecode_size, output) != bytecode_size;
        if (output) failed |= fclose(output) != 0;
        js_free(ctx, bytecode);
        JS_FreeValue(ctx, result);
        return failed;
    }
    JS_FreeValue(ctx, result); return 0;
}

int main(int argc, char **argv) {
    if (argc < 3) { fprintf(stderr, "usage: grainc.wasm SHIM.js COMPILER.js [GRAINC_ARGS...]\n"); return 2; }
    JSRuntime *runtime = JS_NewRuntime();
    JS_SetMemoryLimit(runtime, 1800 * 1024 * 1024);
    JS_SetMaxStackSize(runtime, 12 * 1024 * 1024);
    JSContext *ctx = JS_NewContext(runtime);
    if (argc == 4 && !strcmp(argv[1], "--compile-bytecode")) {
        int result = evaluate(ctx, argv[2], argv[3]);
        JS_FreeContext(ctx); JS_FreeRuntime(runtime);
        return result;
    }
    JSValue global = JS_GetGlobalObject(ctx), host = JS_NewObject(ctx), args = JS_NewArray(ctx);
    const char *names[] = {"open", "stat", "close", "read", "write", "fstat", "truncate", "mkdir", "unlink", "rmdir", "rename", "readdir", "readlink"};
    for (int i = 0; i < 13; i++) JS_SetPropertyStr(ctx, host, names[i], JS_NewCFunctionMagic(ctx, io, names[i], 5, JS_CFUNC_generic_magic, i));
    JS_SetPropertyStr(ctx, host, "print", JS_NewCFunctionMagic(ctx, print, "print", 1, JS_CFUNC_generic_magic, 0));
    JS_SetPropertyStr(ctx, host, "error", JS_NewCFunctionMagic(ctx, print, "error", 1, JS_CFUNC_generic_magic, 1));
    JS_SetPropertyStr(ctx, host, "exit", JS_NewCFunction(ctx, quit, "exit", 1));
    JS_SetPropertyStr(ctx, host, "O_RDONLY", JS_NewInt32(ctx, O_RDONLY));
    JS_SetPropertyStr(ctx, host, "O_WRONLY", JS_NewInt32(ctx, O_WRONLY));
    JS_SetPropertyStr(ctx, host, "O_APPEND", JS_NewInt32(ctx, O_APPEND));
    JS_SetPropertyStr(ctx, host, "O_CREAT", JS_NewInt32(ctx, O_CREAT));
    JS_SetPropertyStr(ctx, host, "O_TRUNC", JS_NewInt32(ctx, O_TRUNC));
    JS_SetPropertyStr(ctx, host, "O_EXCL", JS_NewInt32(ctx, O_EXCL));
    JS_SetPropertyStr(ctx, global, "__host", host);
    JS_SetPropertyUint32(ctx, args, 0, JS_NewString(ctx, "grainc"));
    JS_SetPropertyUint32(ctx, args, 1, JS_NewString(ctx, "grainc"));
    for (int i = 3; i < argc; i++) JS_SetPropertyUint32(ctx, args, i - 1, JS_NewString(ctx, argv[i]));
    JS_SetPropertyStr(ctx, global, "__args", args); JS_FreeValue(ctx, global);
    int result = evaluate(ctx, argv[1], NULL) || evaluate(ctx, argv[2], NULL);
    if (!result) {
        const char *entry = "typeof grainc === 'function' ? grainc(globalThis) : undefined";
        JSValue call = JS_Eval(ctx, entry, strlen(entry), "<grain-entry>", JS_EVAL_TYPE_GLOBAL);
        if (JS_IsException(call)) {
            JSValue error = JS_GetException(ctx);
            const char *message = JS_ToCString(ctx, error);
            fprintf(stderr, "%s\n", message ? message : "compiler failed");
            JS_FreeCString(ctx, message); JS_FreeValue(ctx, error); result = 1;
        }
        JS_FreeValue(ctx, call);
    }
    JS_FreeContext(ctx); JS_FreeRuntime(runtime);
    return result;
}
