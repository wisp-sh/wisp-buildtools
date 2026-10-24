#include "lld/Common/Driver.h"
#include "llvm/IR/IRBuilder.h"
#include "llvm/IR/LegacyPassManager.h"
#include "llvm/IR/Module.h"
#include "llvm/MC/TargetRegistry.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/TargetSelect.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/Target/TargetMachine.h"
#include "llvm/Target/TargetOptions.h"

#include <memory>

LLD_HAS_DRIVER(wasm)

// This program itself runs inside Wasm. It creates and links a second module.
int main(int argc, char **argv) {
  if (argc != 3) {
    llvm::errs() << "usage: sdk-smoke output.o output.wasm\n";
    return 2;
  }
  llvm::InitializeAllTargetInfos();
  llvm::InitializeAllTargets();
  llvm::InitializeAllTargetMCs();
  llvm::InitializeAllAsmPrinters();

  llvm::LLVMContext context;
  llvm::Module module("llvm-sdk-smoke", context);
  const llvm::Triple triple("wasm32-unknown-unknown");
  module.setTargetTriple(triple);
  std::string error;
  const auto *target = llvm::TargetRegistry::lookupTarget(triple, error);
  if (!target) {
    llvm::errs() << error << "\n";
    return 1;
  }
  llvm::TargetOptions options;
  std::unique_ptr<llvm::TargetMachine> machine(target->createTargetMachine(
      triple, "generic", "", options, llvm::Reloc::Static));
  module.setDataLayout(machine->getTargetTriple().computeDataLayout());
  auto *i32 = llvm::Type::getInt32Ty(context);
  auto *signature = llvm::FunctionType::get(i32, {i32, i32}, false);
  auto *function = llvm::Function::Create(
      signature, llvm::GlobalValue::ExternalLinkage, "add", module);
  llvm::IRBuilder<> builder(llvm::BasicBlock::Create(context, "entry", function));
  builder.CreateRet(builder.CreateAdd(function->getArg(0), function->getArg(1)));

  std::error_code ec;
  llvm::raw_fd_ostream object(argv[1], ec, llvm::sys::fs::OF_None);
  if (ec) {
    llvm::errs() << ec.message() << "\n";
    return 1;
  }
  llvm::legacy::PassManager passes;
  if (machine->addPassesToEmitFile(passes, object, nullptr,
                                 llvm::CodeGenFileType::ObjectFile))
    return 1;
  passes.run(module);
  object.close();

  const char *arguments[] = {"wasm-ld", "--no-entry", "--export=add", argv[1],
                             "-o", argv[2]};
  const lld::DriverDef driver = {lld::Wasm, &lld::wasm::link};
  const auto result = lld::lldMain(arguments, llvm::outs(), llvm::errs(), {driver});
  if (result.retCode == 0)
    llvm::outs() << "LLVM and LLD produced " << argv[2] << "\n";
  return result.retCode;
}
