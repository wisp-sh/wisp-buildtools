#pragma once
#include <sys/types.h>
#define WNOHANG 1
#define WIFEXITED(s) (((s) & 0x7f) == 0)
#define WEXITSTATUS(s) (((s) >> 8) & 0xff)
#define WIFSIGNALED(s) ((((s) & 0x7f) + 1) >> 1 > 0)
#define WTERMSIG(s) ((s) & 0x7f)
pid_t waitpid(pid_t, int *, int);
