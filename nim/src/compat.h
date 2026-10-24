#pragma once
#include <time.h>
#include <sys/types.h>
#include <unistd.h>
#ifndef CLOCK_THREAD_CPUTIME_ID
#define CLOCK_THREAD_CPUTIME_ID CLOCK_MONOTONIC
#endif
#ifndef FIOCLEX
#define FIOCLEX 0x5451
#define FIONCLEX 0x5450
#endif

pid_t fork(void);
int execve(const char *, char *const [], char *const []);
int kill(pid_t, int);
