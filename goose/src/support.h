/* Wisp's WASI runtime for Goose scalar-v1. No native address reservations.
   The compiler rejects every program requiring data-stack regions. */
#include <stdarg.h>
static GS_NORETURN void gs_end(int code) { fflush(NULL); _Exit(code); }
GS_API GS_NORETURN void gs_panic(const char *msg) {
    fprintf(stderr, "goose runtime error: %s\n", msg); gs_end(1);
}
static GS_NORETURN void gs_fail(const void *msg, int64_t n, const char *file, int line) {
    fputs("goose runtime error: ", stderr);
    fwrite(msg, 1, (size_t)n, stderr);
    fprintf(stderr, " (%s:%d)\n", file, line); gs_end(1);
}
static GS_NORETURN void gs_failf(const char *file, int line, const char *fmt, ...) {
    char buf[512]; va_list ap; va_start(ap, fmt);
    int n=vsnprintf(buf,sizeof buf,fmt,ap); va_end(ap);
    gs_fail(buf,n<0 ? 0 : n<(int)sizeof buf ? n : (int)sizeof buf-1,file,line);
}
GS_API GS_NORETURN void gs_abort(int err, const char *file, int line) {
    gs_failf(file,line,"failed Goose check %d",err);
}
GS_API GS_NORETURN void gs_abort_msg(const uint8_t *msg,int64_t n,const char *file,int line) {
    gs_fail(msg,n,file,line);
}
GS_API GS_NORETURN void gs_exit(int64_t code) { gs_end((int)code); }
GS_API GS_NORETURN void gs_idxfail(int64_t i,int64_t n,const char *file,int line) {
    gs_failf(file,line,"index %lld out of bounds (length %lld)",(long long)i,(long long)n);
}
GS_API GS_NORETURN void gs_divfail(const char *file,int line) { gs_failf(file,line,"division by zero"); }
GS_API GS_NORETURN void gs_divovf(const char *file,int line) { gs_failf(file,line,"integer overflow in division"); }
GS_API GS_NORETURN void gs_ovf(int64_t a,const char *op,int64_t b,const char *type,const char *file,int line) {
    gs_failf(file,line,"integer overflow: %lld %s %lld at %s",(long long)a,op,(long long)b,type);
}
GS_API GS_NORETURN void gs_ovf_neg(int64_t a,const char *type,const char *file,int line) {
    gs_failf(file,line,"integer overflow: -%lld at %s",(long long)a,type);
}
GS_API GS_NORETURN void gs_asfail_i(const char *why,int64_t v,const char *type,const char *file,int line) {
    gs_failf(file,line,"as conversion %s: %lld as %s",why,(long long)v,type);
}
GS_API GS_NORETURN void gs_asfail_u(const char *why,uint64_t v,const char *type,const char *file,int line) {
    gs_failf(file,line,"as conversion %s: %llu as %s",why,(unsigned long long)v,type);
}
GS_API GS_NORETURN void gs_asfail_f(const char *why,double v,int f32,const char *type,const char *file,int line) {
    (void)f32; gs_failf(file,line,"as conversion %s: %.17g as %s",why,v,type);
}
GS_API void gs_rt_start(int argc,char **argv,uint64_t reserve,uint64_t gap,uint64_t budget,
                       int64_t mainregions,int64_t workerregions) {
    (void)argc;(void)argv;(void)reserve;(void)gap;(void)budget;
    if (mainregions || workerregions) gs_panic("dynamic data stacks are unsupported in scalar-v1");
}
GS_API uint8_t *gs_reserve_region(gs_stack *stack) {
    (void)stack; gs_panic("dynamic data stacks are unsupported in scalar-v1");
}
GS_API void gs_stack_stats(int64_t stacks) { (void)stacks; }
GS_API void gs_release_regions(void) {}
GS_API int64_t gs_hardware_threads(void) { return 1; }
