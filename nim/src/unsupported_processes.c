// WASI has no child processes. Report a real failure to Nim's process APIs.
#include <errno.h>
#include <sys/types.h>
int system(const char *command) { if (!command) return 0; errno = ENOSYS; return -1; }
pid_t fork(void) { errno = ENOSYS; return -1; }
int execve(const char *path, char *const args[], char *const env[]) { errno = ENOSYS; return -1; }
pid_t waitpid(pid_t pid, int *status, int flags) { errno = ENOSYS; return -1; }
int kill(pid_t pid, int signal) { errno = ENOSYS; return -1; }
// wasi-libc cannot discover its executable through Linux procfs. The launcher
// mounts the package at /nim, so keep Nim's prefix lookup in that mount.
#include <string.h>
#include <unistd.h>
ssize_t nim_wasi_readlink(const char *path, char *buffer, size_t capacity) {
  if (strcmp(path, "/proc/self/exe") == 0) {
    const char name[] = "/nim/bin/nim.wasm";
    size_t length = sizeof(name) - 1;
    if (length > capacity) length = capacity;
    memcpy(buffer, name, length);
    return length;
  }
  return readlink(path, buffer, capacity);
}
