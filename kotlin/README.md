# Kotlin

This toolchain runs JetBrains Kotlin 2.4.21 inside the shared WASI JVM. Kotlin/JVM bytecode then passes through the shared TeaVM WASI backend and Component assembler. Application builds need Wisp's Wasmtime host; they do not run Node or a native Java runtime.

```sh
python3 build.py build kotlin
python3 kotlin/compile.py kotlin/examples/Math.kt --main MathKt \
  --component --wit kotlin/examples/math.wit -o /tmp/kotlin-math.wasm
```

The compiler emits JVM 17 bytecode. Bootstrap adds a checked bytecode guard so ordinary compilation skips CPU-profiling setup when detailed profiling is disabled. The Wasm JVM does not implement the native management API needed for that optional profiler. Every other compiler class is preserved. Packaging also removes optional native Jansi/LZ4 helpers.

The compiler uses its standard JAR reader because the fast reader needs a JVM native inflater method unavailable in this port. Trove4j remains pinned for that reader's IntelliJ compatibility classes. These choices affect compiler startup and JAR loading; Kotlin language processing uses the upstream 2.4.21 compiler.

`--main` names the generated JVM class containing `main(String[])`. A top-level `main` in `Math.kt` produces `MathKt`. Export scalar functions with `org.teavm.interop.Export`; the example covers 32 bit integers, 64 bit integers and doubles. WIT functions must match those exported names and signatures.

The JVM compiler supports Kotlin syntax and its standard library, using the shared JVM's JDK signatures during source compilation. Generated applications use TeaVM's Java/WASI runtime, which checks whether reachable Java APIs are supported. Native JNI libraries, dynamic class loading and arbitrary JVM reflection are not part of the target profile. The Component adapter currently supports scalar exports; general WIT objects, strings and resources need explicit bindings.

`dist/` contains compiler and library JAR data plus license notices. `wisp:jvm` owns the Wasm interpreter and JDK data, and `wisp:teavm` owns the shared backend. No JVM, engine or LLVM package is copied into this compiler package. Compiler inputs and licenses are pinned in `toolchain.lock.json`; `bootstrap.py --check` verifies the installed payload.
