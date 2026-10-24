#include "bjvm.h"
#include "arrays.h"
#include "cached_classdescs.h"
#include "objects.h"
#include "roundrobin_scheduler.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static void show_exception(vm_thread *thread) {
  if (!thread->current_exception) return;
    object exception = thread->current_exception;
    fprintf(stderr, "Java exception %.*s\n", fmt_slice(exception->descriptor->name));
    thread->current_exception = nullptr;
    cp_method *print = method_lookup(exception->descriptor, STR("printStackTrace"), STR("()V"), true, false);
    if (print) call_interpreter_synchronous(thread, print, (stack_value[]){{.obj = exception}});
}

int main(int argc, char **argv) {
  char **properties = argv + 1;
  int property_count = 0;
  while (argc > property_count + 1 && !strncmp(argv[property_count + 1], "-D", 2)) property_count++;
  argc -= property_count; argv += property_count;
  if (argc < 4) {
    fprintf(stderr, "usage: jvm.wasm [-Dname=value] BOOT.jar CLASSPATH MAIN_CLASS [ARGS...]\n");
    return 2;
  }
  vm_options options = default_vm_options();
  options.runtime_classpath = (slice){argv[1], strlen(argv[1])};
  options.classpath = (slice){argv[2], strlen(argv[2])};
  options.heap_size = 512 * 1024 * 1024;
  vm *jvm = create_vm(options);
  rr_scheduler scheduler;
  rr_scheduler_init(&scheduler, jvm);
  jvm->scheduler = &scheduler;
  thread_options thread_opts = default_thread_options();
  thread_opts.js_jit_enabled = false;
  thread_opts.stack_space = 16 * 1024 * 1024;
  vm_thread *thread = create_main_thread(jvm, thread_opts);
  cp_method *set_property = method_lookup(cached_classes(jvm)->system, STR("setProperty"), STR("(Ljava/lang/String;Ljava/lang/String;)Ljava/lang/String;"), false, false);
  for (int i = 0; i < property_count; i++) {
    char *name = strdup(properties[i] + 2);
    char *value = strchr(name, '=');
    if (!value) { fprintf(stderr, "invalid system property\n"); free(name); return 2; }
    *value++ = 0;
    handle *key = make_handle(thread, MakeJStringFromCString(thread, name, false));
    object val = MakeJStringFromCString(thread, value, false);
    call_interpreter_synchronous(thread, set_property, (stack_value[]){{.obj = key->obj}, {.obj = val}});
    drop_handle(thread, key); free(name);
    if (thread->current_exception) { fprintf(stderr, "could not set system property\n"); return 1; }
  }
  cp_method *get_loader = method_lookup(cached_classes(jvm)->class_loader, STR("getSystemClassLoader"), STR("()Ljava/lang/ClassLoader;"), false, false);
  stack_value loader = call_interpreter_synchronous(thread, get_loader, nullptr);
  if (!loader.obj || thread->current_exception) { fprintf(stderr,"could not initialize system classloader\n"); show_exception(thread); return 1; }
  lookup_class_t lookup = {.args = {thread, (slice){argv[3], strlen(argv[3])}, unmirror_classloader(jvm, loader.obj), true}};
  future_t loaded = lookup_class(&lookup);
  if (loaded.status != FUTURE_READY) { fprintf(stderr,"class loader unexpectedly suspended\n"); return 1; }
  classdesc *desc = lookup._result;
  if (!desc || thread->current_exception) {
    fprintf(stderr, "could not load main class %s\n", argv[3]);
    show_exception(thread);
    return 1;
  }
  initialize_class_t init = {.args = {thread, desc}};
  if (initialize_class(&init).status != FUTURE_READY || thread->current_exception) {
    fprintf(stderr, "could not initialize main class %s\n", argv[3]);
    show_exception(thread);
    return 1;
  }
  cp_method *method = method_lookup(desc, STR("main"), STR("([Ljava/lang/String;)V"), false, false);
  if (!method) { fprintf(stderr, "no static main(String[]) in %s\n", argv[3]); return 1; }
  handle *args_handle = make_handle(thread, CreateObjectArray1D(thread, cached_classes(jvm)->string, argc - 4));
  for (int i = 4; i < argc; ++i) {
    object str = MakeJStringFromCString(thread, argv[i], true);
    ReferenceArrayStore(args_handle->obj, i - 4, str);
  }
  stack_value arguments[] = {{.obj = args_handle->obj}};
  drop_handle(thread, args_handle);
  call_interpreter_t call = {.args = {thread, method, arguments}};
  rr_scheduler_run(&scheduler, call);
  while (rr_scheduler_step(&scheduler) != SCHEDULER_RESULT_DONE) {
    u64 delay = rr_scheduler_may_sleep_us(&scheduler);
    if (delay) usleep(delay);
  }
  if (thread->current_exception) {
    show_exception(thread);
    return 1;
  }
  return 0;
}
