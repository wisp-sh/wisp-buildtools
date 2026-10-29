# JavaScript and TypeScript parity acceptance

Go and Rust accept project roots with offline dependency trees. C++ accepts
multiple translation units and headers. Wisp snapshots their inputs and emits
Components that the library generators turn into installed consumers.
JavaScript and TypeScript use one module entrypoint and its local/npm import
graph. `--module-root` bounds that graph; npm installation happens before the
application build. Application compilation never launches Node or native
compilers.

The following gates must execute against the PR's compiler before merge. A
saved verification report or a skipped source-build job does not satisfy them.

| Workflow | Required evidence |
| --- | --- |
| Project inputs | Local imports, offline npm package, package metadata, tsconfig, dependency edit changes behavior |
| Public API | Explicit scalar TypeScript discovery and authored WIT interfaces with rich values and imports |
| Type checking | Actual tsrs valid/error/valid requests, whole-project errors, checked and bundled input identity |
| Engine | All JavaScript verification cases on QuickJS and StarlingMonkey |
| Canonical ABI | Wasm splicer emits flattened metadata; records, lists, Unicode, result errors and composed imports execute |
| State and errors | Repeated calls preserve instance state; initialization fails atomically; invocation errors are visible |
| Async | StarlingMonkey Promise exports settle microtasks; rejected Promises fail calls |
| Bootstrap | Pinned source, lock correction, metadata patch and runtime; Rust runs only during tool bootstrap |
| Snapshot | Approved native Wizer with explicit preopens/environment; receipt says fullyWasmHostedBuild=false |
| Compiler installation | Validate and install JavaScript, tsrs, Wizer and wasm-tools into a fresh prefix; run installed adapters |
| Consumers | Wisp source builds and generated npm/Rust consumers execute the same Components |
| Real packages | Pinned yaml 2.8.1 and markdown-it 15.0.3 facades, Components and generated consumers |
| CI | Hosted jobs retain fresh commands, receipts and failures; no releases or registry publication |

QuickJS remains the default. StarlingMonkey is selected explicitly with
`--engine starlingmonkey`. Synchronous WIT is the current contract. Promise
exports that settle through microtasks are distinct from asynchronous WIT,
network requests and timers. Node builtins and the DOM are unavailable. yaml
retains its browser Buffer/process substitutions. Top-level initialization
must be safe to snapshot and reuse across Component instances.
