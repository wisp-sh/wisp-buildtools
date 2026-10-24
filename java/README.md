# Java

Java source compilation runs inside the shared WASI JVM. ECJ 3.46.100 compiles
Java 21 source to JVM bytecode, then the shared TeaVM 0.16.0 C-to-WASI backend compiles
that bytecode to Core Wasm. `--component` wraps its scalar exports using WIT.
Wisp provides Wasmtime. Application builds require neither Node nor native Java.

From the repository root:

```sh
python3 build.py build java
export WISP_WASMTIME=/path/to/wisp-provided/wasmtime
python3 java/compile.py java/examples/Math.java --main Math \
  --component --wit java/examples/math.wit --world math -o /tmp/math.wasm
python3 java/verify.py
```

Pass several `.java` files or a source directory to compile a project. Use
`--classpath path.jar` for additional dependencies. Sources target Java 21 and
compile against the portable Java API signatures in the TeaVM package. Annotation
processing is disabled. Third-party libraries must use APIs implemented by the
pinned TeaVM class library. This profile does not turn arbitrary OpenJDK or JNI
applications into standalone Wasm applications.

For Component exports, use `@org.teavm.interop.Export(name = "add")` on a static
method. The supplied example maps Java `int`, `long`, and `double` to WIT
`u32`, `u64`, and `float64`. Integer values preserve their bit patterns. Java
objects, strings, and arrays do not automatically implement the WIT canonical
ABI. A public static `main(String[])` initializes the application before the
Component's exported functions can run.

Each build writes the Wasm output and `.build.json` receipt. Component builds
also write `.wit` containing the complete generated contract, including retained
WASI imports. Existing outputs are never overwritten, and failed builds publish
no output. Receipts record source hashes, compiler manifests, actual Wasm JVM
commands, backend inputs, adapter memory, and final artifact hashes.

`dist/ecj.jar`, `dist/messages.properties`, and `dist/launcher/` are the Java
frontend. The separate `teavm` package owns the backend and API signatures, and
`jvm` owns the shared interpreter and JDK class-library data. Runtime packages
contain no native Java binary or Wasmtime binary. Standard Wisp packages and
size reports come from the repository's root packaging commands.

Bootstrap uses a native JDK 21 or newer to compile the small driver. Set
`WISP_BOOTSTRAP_JAVA` to its `java` executable; a `javac` executable is unnecessary.
Downloads and Maven inputs are checksum pinned in `toolchain.lock.json`.
`bootstrap.py --offline` uses cached inputs; `bootstrap.py --check` verifies the
existing build without changing it. The upstream ECJ JAR is verified before its
signature sidecars are removed. The resulting portable JAR has a separate hash
in the distribution manifest. This avoids requiring a certificate verifier just
to load the already verified compiler. English diagnostic messages remain the
unmodified upstream ECJ resource.

The [TeaVM backend documentation](../teavm/README.md) explains the C-to-WASI compilation path and target limits. ECJ's license and upstream notices are in
`dist/licenses/`.
