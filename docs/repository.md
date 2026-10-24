# Repository migration

The buildtools source now has one Git root at `wisp-buildtools`. Directory names
are `clang`, `go`, `rust`, `zig`, `assemblyscript`, `nim`, `javascript`, `python`,
`grain`, `swift`, `java`, `kotlin`, `jvm`, `teavm`, `llvm` and `wasm-tools`.
JavaScript and TypeScript share the `javascript` package.
Workflow definitions belong to the root `.github/workflows` directory.

The consolidation commit has the existing C/C++ and Go heads as parents, so their
original commit IDs remain reachable. Their original references are also kept
under `refs/imported/cpp` and `refs/imported/go`. No other child repository had
commits. Uncommitted files were moved with their directories.

For this local migration, the original child Git metadata is backed up under
`.git/imported-repositories`, including an inventory of the original heads,
references and working-tree status. The root has no publishing remote configured.
The former Go compiler archives remain on disk under `go/artifacts`, ignored by
the unified source tree. They are also retained in the imported Go history.

Build scripts, Wisp configuration and environment variables use the canonical
names directly. There are no aliases for previous compiler names or directories.
C/C++ bootstrap regenerates its CMake configuration before compiling. Historical
receipts and imported Git history retain their original contents.

`env.sh` exports `WISP_CLANG` and `WISP_GO_TOOLCHAIN`. `WISP_GO` selects the native
Go installer used during dependency setup.
