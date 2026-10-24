/* Freestanding C memory support for Zig's C backend. MIT, Wisp contributors.
 * Volatile byte accesses prevent compilers from turning these loops back into
 * calls to the very runtime functions implemented here. */
void *memcpy(void *destination, const void *source, size_t count) {
    volatile unsigned char *d = (volatile unsigned char *)destination;
    const volatile unsigned char *s = (const volatile unsigned char *)source;
    for (size_t i = 0; i < count; ++i) d[i] = s[i];
    return destination;
}
void *memmove(void *destination, const void *source, size_t count) {
    volatile unsigned char *d = (volatile unsigned char *)destination;
    const volatile unsigned char *s = (const volatile unsigned char *)source;
    if ((uintptr_t)d < (uintptr_t)s) {
        for (size_t i = 0; i < count; ++i) d[i] = s[i];
    } else {
        while (count) { --count; d[count] = s[count]; }
    }
    return destination;
}
void *memset(void *destination, int value, size_t count) {
    volatile unsigned char *d = (volatile unsigned char *)destination;
    for (size_t i = 0; i < count; ++i) d[i] = (unsigned char)value;
    return destination;
}
int memcmp(const void *left, const void *right, size_t count) {
    const volatile unsigned char *a = (const volatile unsigned char *)left;
    const volatile unsigned char *b = (const volatile unsigned char *)right;
    for (size_t i = 0; i < count; ++i) if (a[i] != b[i]) return (int)a[i] - (int)b[i];
    return 0;
}
