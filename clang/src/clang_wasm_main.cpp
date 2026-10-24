// Fixed-target C/C++ to Core WebAssembly compiler hosted by WASIp3.
#include "clang/Basic/Diagnostic.h"
#include "clang/CodeGen/CodeGenAction.h"
#include "clang/Frontend/CompilerInstance.h"
#include "clang/Frontend/CompilerInvocation.h"
#include "clang/Frontend/TextDiagnosticPrinter.h"
#include "lld/Common/Driver.h"
#include "llvm/Support/InitLLVM.h"
#include "llvm/Support/TargetSelect.h"
#include "llvm/Support/raw_ostream.h"

#include <memory>
#include <string>
#include <vector>

LLD_HAS_DRIVER(wasm)

int main(int argc, char **argv) {
  llvm::InitLLVM init(argc, argv);
  llvm::errs() << "clang-wasm: ready\n";

  const char *input = nullptr;
  const char *output = nullptr;
  const char *optimization = "-O0";
  std::vector<const char *> includes;
  std::vector<const char *> definitions;
  const char *standard = nullptr;
  std::string sysroot;
  for (int i = 1; i < argc; ++i) {
    llvm::StringRef arg(argv[i]);
    if (arg == "-o" && i + 1 < argc) {
      output = argv[++i];
    } else if ((arg == "-O0" || arg == "-O1")) {
      optimization = argv[i];
    } else if (arg == "-I" && i + 1 < argc) {
      includes.push_back(argv[++i]);
    } else if (arg.starts_with("-I") && arg.size() > 2) {
      includes.push_back(argv[i] + 2);
    } else if (arg == "-D" && i + 1 < argc) {
      definitions.push_back(argv[++i]);
    } else if (arg.starts_with("-D") && arg.size() > 2) {
      definitions.push_back(argv[i] + 2);
    } else if (arg.starts_with("-std=")) {
      standard = argv[i];
    } else if (arg == "--wasi-sysroot" && i + 1 < argc) {
      sysroot = argv[++i];
    } else if (!arg.starts_with("-") && !input) {
      input = argv[i];
    } else {
      llvm::errs() << "clang-wasm: unsupported argument: " << arg << '\n';
      return 2;
    }
  }
  if (!input || !output) {
    llvm::errs() << "usage: clang-wasm input.cpp -o output.wasm [-O0|-O1] [-I dir] [-D name[=value]] [-std=standard] [--wasi-sysroot dir]\n";
    return 2;
  }

  llvm::InitializeAllTargets();
  llvm::InitializeAllTargetMCs();
  llvm::InitializeAllAsmPrinters();

  const std::string object = std::string(output) + ".o";
  const bool isC = llvm::StringRef(input).ends_with(".c");
  std::vector<const char *> args = {
      "-triple", sysroot.empty() ? "wasm32-unknown-unknown" : "wasm32-wasip1", "-emit-obj", "-x",
      isC ? "c" : "c++", "-ffreestanding", "-nostdsysteminc",
      "-nobuiltininc", "-fno-rtti", "-fno-threadsafe-statics", optimization, "-o", object.c_str()};
  const std::string targetIncludes = sysroot + "/include/wasm32-wasip1";
  const std::string commonIncludes = sysroot + "/include";
  const std::string cppIncludes = sysroot + "/include/wasm32-wasip1/noeh/c++/v1";
  if (!sysroot.empty()) {
    if (!isC) {
      args.push_back("-isystem");
      args.push_back(cppIncludes.c_str());
    }
    args.push_back("-isystem");
    args.push_back(targetIncludes.c_str());
    args.push_back("-isystem");
    args.push_back(commonIncludes.c_str());
  }
  if (standard) args.push_back(standard);
  for (const char *definition : definitions) {
    args.push_back("-D");
    args.push_back(definition);
  }
  for (const char *include : includes) {
    args.push_back("-I");
    args.push_back(include);
  }
  args.push_back(input);

  clang::DiagnosticOptions diagnosticOptions;
  clang::DiagnosticsEngine parseDiagnostics(
      clang::DiagnosticIDs::create(), diagnosticOptions,
      new clang::TextDiagnosticPrinter(llvm::errs(), diagnosticOptions));
  auto invocation = std::make_shared<clang::CompilerInvocation>();
  if (!clang::CompilerInvocation::CreateFromArgs(
          *invocation, args, parseDiagnostics, argv[0]))
    return 1;

  clang::CompilerInstance compiler(std::move(invocation));
  compiler.createDiagnostics();
  clang::EmitObjAction action;
  if (!compiler.ExecuteAction(action) || compiler.getDiagnostics().hasErrorOccurred())
    return 1;
  llvm::errs() << "clang-wasm: codegen-complete\n";

  std::vector<std::string> linkStorage = {"wasm-ld", "--no-entry", "--export-all",
                                         object, "-o", output};
  if (!sysroot.empty()) {
    const std::string library = sysroot + "/lib/wasm32-wasip1";
    linkStorage.push_back(library + "/crt1-reactor.o");
    linkStorage.push_back("-L" + library);
    if (!isC) {
      linkStorage.push_back("-L" + library + "/noeh");
      linkStorage.push_back("-lc++");
      linkStorage.push_back("-lc++abi");
    }
    linkStorage.push_back("-lc");
    linkStorage.push_back(sysroot + "/../../lib/clang/23/lib/wasm32-unknown-wasip1/libclang_rt.builtins.a");
  }
  std::vector<const char *> linkArgs;
  for (const auto &argument : linkStorage) linkArgs.push_back(argument.c_str());
  const lld::DriverDef wasmDriver = {lld::Wasm, &lld::wasm::link};
  const lld::Result result =
      lld::lldMain(linkArgs, llvm::outs(), llvm::errs(), {wasmDriver});
  if (result.retCode == 0)
    llvm::errs() << "clang-wasm: link-complete\n";
  return result.retCode;
}
