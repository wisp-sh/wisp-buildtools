/* Additional OpenJDK 23 IO natives required by compiler tools on WASI. */
#include <natives-dsl.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

DECLARE_NATIVE("java/io", FileInputStream, length0, "()J") {
  obj_header *fd = LoadFieldObject(obj->obj, "java/io/FileDescriptor", "fd");
  struct stat info;
  if (fstat(LoadFieldInt(fd, "fd"), &info)) {
    raise_vm_exception(thread, STR("java/io/IOException"), STR("Could not get file size"));
    return value_null();
  }
  return (stack_value){.l = info.st_size};
}

DECLARE_NATIVE("java/io", FileInputStream, position0, "()J") {
  obj_header *fd = LoadFieldObject(obj->obj, "java/io/FileDescriptor", "fd");
  off_t position = lseek(LoadFieldInt(fd, "fd"), 0, SEEK_CUR);
  if (position < 0) {
    raise_vm_exception(thread, STR("java/io/IOException"), STR("Could not get file position"));
    return value_null();
  }
  return (stack_value){.l = position};
}

DECLARE_NATIVE("java/io", FileOutputStream, open0, "(Ljava/lang/String;Z)V") {
  heap_string path = AsHeapString(args[0].handle->obj, on_oom);
  int descriptor = open(path.chars, O_WRONLY | O_CREAT | (args[1].i ? O_APPEND : O_TRUNC), 0666);
  if (descriptor < 0) raise_vm_exception(thread, STR("java/io/FileNotFoundException"), hslc(path));
  else {
    obj_header *fd = LoadFieldObject(obj->obj, "java/io/FileDescriptor", "fd");
    StoreFieldInt(fd, "fd", descriptor);
  }
  free_heap_str(path);
 on_oom:
  return value_null();
}

DECLARE_NATIVE("java/io", UnixFileSystem, list0, "(Ljava/io/File;)[Ljava/lang/String;") {
  obj_header *path_obj = LoadFieldObject(args[0].handle->obj, "java/lang/String", "path");
  heap_string path = AsHeapString(path_obj, on_oom);
  DIR *directory = opendir(path.chars);
  free_heap_str(path);
  if (!directory) return value_null();
  char **names = nullptr;
  struct dirent *entry;
  while ((entry = readdir(directory))) {
    if (!strcmp(entry->d_name, ".") || !strcmp(entry->d_name, "..")) continue;
    arrput(names, strdup(entry->d_name));
  }
  closedir(directory);
  handle *result = make_handle(thread, CreateObjectArray1D(thread, cached_classes(thread->vm)->string, arrlen(names)));
  for (int index = 0; index < arrlen(names); index++) {
    object name = MakeJStringFromCString(thread, names[index], false);
    if (name) ReferenceArrayStore(result->obj, index, name);
    free(names[index]);
  }
  arrfree(names);
  stack_value value = {.obj = result->obj};
  drop_handle(thread, result);
  return value;
 on_oom:
  return value_null();
}

DECLARE_NATIVE("java/io", UnixFileSystem, getLength0, "(Ljava/io/File;)J") {
  obj_header *path_obj = LoadFieldObject(args[0].handle->obj, "java/lang/String", "path");
  heap_string path = AsHeapString(path_obj, on_oom);
  struct stat info;
  int error = stat(path.chars, &info);
  free_heap_str(path);
  return (stack_value){.l = error ? 0 : info.st_size};
 on_oom:
  return value_null();
}

DECLARE_NATIVE("java/io", UnixFileSystem, createDirectory0, "(Ljava/io/File;)Z") {
  obj_header *path_obj = LoadFieldObject(args[0].handle->obj, "java/lang/String", "path");
  heap_string path = AsHeapString(path_obj, on_oom);
  int error = mkdir(path.chars, 0777);
  free_heap_str(path);
  return (stack_value){.i = !error};
 on_oom:
  return value_null();
}

/* OpenJDK IOStatus encodes EOF=-1, UNAVAILABLE=-2, INTERRUPTED=-3. */
static stack_value nio_result(vm_thread *thread, ssize_t count, bool reading, int requested) {
  if (count > 0 || (count == 0 && (!reading || requested == 0))) return (stack_value){.i = count};
  if (count == 0) return (stack_value){.i = -1};
  if (errno == EAGAIN) return (stack_value){.i = -2};
  if (errno == EINTR) return (stack_value){.i = -3};
  raise_vm_exception(thread, STR("java/io/IOException"), (slice){strerror(errno), strlen(strerror(errno))});
  return value_null();
}

DECLARE_NATIVE("sun/nio/ch", UnixFileDispatcherImpl, read0, "(Ljava/io/FileDescriptor;JI)I") {
  int descriptor = LoadFieldInt(args[0].handle->obj, "fd");
  return nio_result(thread, read(descriptor, (void *)(uintptr_t)args[1].l, args[2].i), true, args[2].i);
}

DECLARE_NATIVE("sun/nio/ch", UnixFileDispatcherImpl, pread0, "(Ljava/io/FileDescriptor;JIJ)I") {
  int descriptor = LoadFieldInt(args[0].handle->obj, "fd");
  return nio_result(thread, pread(descriptor, (void *)(uintptr_t)args[1].l, args[2].i, args[3].l), true, args[2].i);
}

DECLARE_NATIVE("sun/nio/ch", UnixFileDispatcherImpl, write0, "(Ljava/io/FileDescriptor;JI)I") {
  int descriptor = LoadFieldInt(args[0].handle->obj, "fd");
  return nio_result(thread, write(descriptor, (void *)(uintptr_t)args[1].l, args[2].i), false, args[2].i);
}

DECLARE_NATIVE("sun/nio/ch", UnixFileDispatcherImpl, pwrite0, "(Ljava/io/FileDescriptor;JIJ)I") {
  int descriptor = LoadFieldInt(args[0].handle->obj, "fd");
  return nio_result(thread, pwrite(descriptor, (void *)(uintptr_t)args[1].l, args[2].i, args[3].l), false, args[2].i);
}

DECLARE_NATIVE("sun/nio/ch", UnixFileDispatcherImpl, seek0, "(Ljava/io/FileDescriptor;J)J") {
  int descriptor = LoadFieldInt(args[0].handle->obj, "fd");
  off_t position = args[1].l < 0 ? lseek(descriptor, 0, SEEK_CUR) : lseek(descriptor, args[1].l, SEEK_SET);
  if (position < 0) {
    raise_vm_exception(thread, STR("java/io/IOException"), STR("Could not seek file"));
    return value_null();
  }
  return (stack_value){.l = position};
}

DECLARE_NATIVE("sun/nio/ch", UnixFileDispatcherImpl, truncate0, "(Ljava/io/FileDescriptor;J)I") {
  int descriptor = LoadFieldInt(args[0].handle->obj, "fd");
  if (ftruncate(descriptor, args[1].l)) {
    raise_vm_exception(thread, STR("java/io/IOException"), STR("Could not truncate file"));
    return value_null();
  }
  return (stack_value){.i = 0};
}

DECLARE_NATIVE("sun/nio/ch", UnixFileDispatcherImpl, closeIntFD, "(I)V") {
  if (close(args[0].i) && errno != EINTR) raise_vm_exception(thread, STR("java/io/IOException"), STR("Could not close file"));
  return value_null();
}

DECLARE_NATIVE("sun/nio/ch", UnixFileDispatcherImpl, force0, "(Ljava/io/FileDescriptor;Z)I") {
  int descriptor = LoadFieldInt(args[0].handle->obj, "fd");
  if (fsync(descriptor)) {
    raise_vm_exception(thread, STR("java/io/IOException"), STR("Could not sync file"));
    return value_null();
  }
  return (stack_value){.i = 0};
}

DECLARE_NATIVE("java/io", FileInputStream, skip0, "(J)J") {
  obj_header *fd = LoadFieldObject(obj->obj, "java/io/FileDescriptor", "fd");
  int descriptor = LoadFieldInt(fd, "fd");
  off_t before = lseek(descriptor, 0, SEEK_CUR);
  off_t after = before < 0 ? -1 : lseek(descriptor, args[0].l, SEEK_CUR);
  if (after < 0) {
    raise_vm_exception(thread, STR("java/io/IOException"), STR("Could not skip file bytes"));
    return value_null();
  }
  return (stack_value){.l = after - before};
}

DECLARE_NATIVE("java/io", UnixFileSystem, getLastModifiedTime0, "(Ljava/io/File;)J") {
  obj_header *path_obj = LoadFieldObject(args[0].handle->obj, "java/lang/String", "path");
  heap_string path = AsHeapString(path_obj, on_oom);
  struct stat info;
  int error = stat(path.chars, &info);
  free_heap_str(path);
  return (stack_value){.l = error ? 0 : info.st_mtim.tv_sec * 1000 + info.st_mtim.tv_nsec / 1000000};
 on_oom:
  return value_null();
}

DECLARE_NATIVE("java/io", UnixFileSystem, delete0, "(Ljava/io/File;)Z") {
  obj_header *path_obj = LoadFieldObject(args[0].handle->obj, "java/lang/String", "path");
  heap_string path = AsHeapString(path_obj, on_oom);
  int error = remove(path.chars);
  free_heap_str(path);
  return (stack_value){.i = !error};
 on_oom:
  return value_null();
}
