// WASI hosted cc1 and Wasm LLD. The host only supplies paths and arguments.
#include "clang/Basic/Diagnostic.h"
#include "clang/CodeGen/CodeGenAction.h"
#include "clang/Frontend/CompilerInstance.h"
#include "clang/Frontend/CompilerInvocation.h"
#include "clang/Frontend/TextDiagnosticPrinter.h"
#include "lld/Common/Driver.h"
#include "llvm/Support/InitLLVM.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/TargetSelect.h"
#include "llvm/Support/raw_ostream.h"
#include <memory>
#include <string>
#include <vector>
LLD_HAS_DRIVER(wasm)

static int compile(const std::vector<std::string> &strings) {
  std::vector<const char *> args;
  for (const auto &arg : strings) args.push_back(arg.c_str());
  clang::DiagnosticOptions options;
  clang::DiagnosticsEngine diagnostics(clang::DiagnosticIDs::create(), options,
      new clang::TextDiagnosticPrinter(llvm::errs(), options));
  auto invocation = std::make_shared<clang::CompilerInvocation>();
  if (!clang::CompilerInvocation::CreateFromArgs(*invocation, args, diagnostics, "c-backend")) return 1;
  clang::CompilerInstance compiler(std::move(invocation));
  compiler.createDiagnostics();
  clang::EmitObjAction action;
  return !compiler.ExecuteAction(action) || compiler.getDiagnostics().hasErrorOccurred();
}
static int link(const std::vector<std::string> &strings) {
  std::vector<const char *> args{"wasm-ld"};
  for (const auto &arg : strings) args.push_back(arg.c_str());
  const lld::DriverDef driver = {lld::Wasm, &lld::wasm::link};
  return lld::lldMain(args, llvm::outs(), llvm::errs(), {driver}).retCode;
}
static bool arguments(const llvm::json::Array *array, std::vector<std::string> &out) {
  if (!array) return false;
  for (const auto &item : *array) {
    auto value = item.getAsString();
    if (!value) return false;
    out.push_back(value->str());
  }
  return true;
}
int main(int argc, char **argv) {
  llvm::InitLLVM init(argc, argv);
  llvm::InitializeAllTargets();
  llvm::InitializeAllTargetMCs();
  llvm::InitializeAllAsmPrinters();
  if (argc < 3) {
    llvm::errs() << "usage: c-backend cc1 ARGS... | link ARGS... | build MANIFEST.json\n";
    return 2;
  }
  llvm::StringRef mode(argv[1]);
  if (mode == "cc1" || mode == "link") {
    std::vector<std::string> args(argv + 2, argv + argc);
    return mode == "cc1" ? compile(args) : link(args);
  }
  if (mode != "build" || argc != 3) return 2;
  auto buffer = llvm::MemoryBuffer::getFile(argv[2]);
  if (!buffer) { llvm::errs() << buffer.getError().message() << '\n'; return 1; }
  auto value = llvm::json::parse((*buffer)->getBuffer());
  if (!value) { llvm::errs() << llvm::toString(value.takeError()) << '\n'; return 1; }
  auto *object = value->getAsObject();
  auto *units = object ? object->getArray("compile") : nullptr;
  if (!units) return 2;
  for (const auto &unit : *units) {
    std::vector<std::string> args;
    if (!arguments(unit.getAsArray(), args)) return 2;
    if (compile(args)) return 1;
  }
  std::vector<std::string> args;
  if (!arguments(object->getArray("link"), args)) return 2;
  return link(args);
}
