# Shared JVM hosted in Wasm

This provider runs Java bytecode inside the Bovine JVM interpreter compiled to
WASI. Java and Kotlin compiler packages share it. Wisp supplies Wasmtime outside
this package; application builds need no native Java runtime, Node or private
execution engine.

The distribution contains `jvm.wasm`, a deterministic `boot.jar` assembled from
Temurin 23+37, the original JDK module image and its configuration, security data
and licenses. Native JRE executables and shared libraries are excluded. Exploded
JDK modules are bootstrap data and stay in `.cache`.

```sh
export WISP_JVM_WASI_SDK=/path/to/wasi-sdk-34.0-x86_64-linux
export WISP_BOOTSTRAP_JAVA=/path/to/jdk-21/bin/java
python3 bootstrap.py
python3 bootstrap.py --check
python3 verify.py
```

Bootstrap verifies pinned Bovine, zlib, Temurin and WASI SDK inputs, applies the
tracked port patches and compiles the interpreter with the native bootstrap SDK.
Native Java 21 extracts the JDK image and compiles a small verification fixture.
`--offline` uses cached verified archives; `--rebuild` repeats the derivation.
Bootstrap tools stay outside the runtime package.

Archive acquisition makes at most two attempts per locked URL. zlib may fall back
to the official `zlib.net/fossils` archive with the same size and SHA-256 pin.
Failed responses report their HTTP metadata, received byte count and hash; they
never replace a cached archive. Downloads stop one byte beyond the pinned size.
`python3 jvm/test_bootstrap.py` from the repository root checks these failure paths
without downloads or compiler execution. Set `WISP_TEST_JVM_DOWNLOADS=1` to also
check both official zlib locations with fresh temporary files.

The interpreter dispatch uses Wasm tail calls. Only `interpreter2.c` is compiled
with `-mtail-call`; enabling that option across all VM files broke bootstrap
helper calls in verification. The final link also enables tail
calls and Wasm exception handling. The guest reserves a 512 MiB Java heap and
allows at most 2 GiB of linear memory. Its main Java frame buffer is 16 MiB, separate
from the 16 MiB C ABI stack reserved by the Wasm linker.

`runner.command(main_class, classpath, mounts=..., arguments=..., properties=...)`
constructs a Wasmtime invocation with explicit directory mappings. It mounts
provider data at `/runtime`, sets `JAVA_HOME=/runtime/jdk` and automatically maps
classpath directories. Arguments containing paths must use those guest paths.
`WISP_WASMTIME` selects Wisp's execution host. Compiler invocations create their
outputs and caches in caller-owned mounted directories.

`runtime.integrity()` verifies every installed artifact against the distribution
manifest and its upstream lock. Verification runs the installed fixture with
`PATH=/nonexistent`, checking virtual object returns, module and loader identity,
nest hosts, interface defaults and initialization, exception dispatch, file I/O
and random-access reads, seeks and channel sizes. The port is an interpreter for
compiler workloads; it does not include a native JVM or a JIT.

The Bovine implementation and Wisp port use the MIT license in `LICENSE`. zlib's
notice is in `dist/licenses`; OpenJDK's notices and Classpath exception remain
under `dist/jdk/legal`.
