// Wisp WASI driver for the pinned upstream Goose frontend and C backend.
// Native execution, subprocesses and package fetching are deliberately absent.
// Goose compiler — driver. Includes establish the project-wide header order.

#include "includes.h"
#include "utils.h"
#include "small_vector.h"
#include "flat_map.h"
#include "lexer.h"
#include "ast.h"
#include "dump.h"
#include "clone.h"
#include "parser.h"
#include "resolve.h"
#include "builtins.h"
#include "gfx.h"
#include "typecheck.h"
#include "typecheck_types.h"
#include "typecheck_exprs.h"
#include "typecheck_expansion.h"
#include "typecheck_flow.h"
#include "typecheck_calls.h"
#include "typecheck_builtins.h"
#include "typecheck_nodes.h"
#include "optimize.h"
#include "optimize_basecase.h"
#include "optimize_tre.h"
#include "optimize_loops.h"
#include "optimize_reassoc.h"
#include "bce.h"
#include "codegen.h"
#include "codegen_types.h"
#include "codegen_frames.h"
#include "codegen_values.h"
#include "codegen_construct.h"
#include "codegen_stmts.h"
#include "codegen_calls.h"
#include "codegen_builtins.h"
#include "codegen_render.h"
#include "codegen_emit.h"
#include "codegen_stacks.h"
#include "codegen_nodes.h"
#include <filesystem>
#include <fstream>

namespace goose {
namespace fs = std::filesystem;

static void ParseProject(Ast &ast, const string &entry, const string &stdlib) {
    const auto root = fs::path(entry).parent_path();
    ast.packages.push_back(Package { "application", root.string() + "/", false });
    ast.packages.push_back(Package { "stdlib", stdlib + "/", true });
    vector<pair<string, int>> queue { {fs::path(entry).lexically_normal().string(), 0} };
    set<string> loaded;
    map<string, int> fileof;
    vector<vector<string>> edges;
    while (!queue.empty()) {
        auto [path, pkg] = queue.back(); queue.pop_back();
        if (!loaded.insert(path).second) continue;
        auto contents = make_unique<string>();
        if (!LoadSource(path, *contents)) throw CompileError {cat("cannot open file: ", path)};
        int index = (int)ast.sources.size();
        fileof[path] = index;
        ast.sources.emplace_back(path, std::move(contents));
        ast.filepkg.push_back(pkg);
        edges.emplace_back();
        Parser parser(ast, path, ast.sources.back().second->c_str(), index);
        parser.ParseTop();
        for (const auto &imp : parser.imports) {
            if (!imp.source.empty())
                throw CompileError {"Goose WASI does not fetch packages; vendor local modules instead"};
            auto base = imp.relative ? fs::path(path).parent_path()
                                    : fs::path(ast.packages[pkg].root);
            auto file = (base / (imp.path + ".goose")).lexically_normal();
            int importedpkg = pkg;
            if (!fs::is_regular_file(file) && !imp.relative) {
                file = (fs::path(stdlib) / (imp.path + ".goose")).lexically_normal();
                importedpkg = 1;
            }
            if (!fs::is_regular_file(file))
                throw CompileError {cat(path, ":", imp.line.line, ": cannot find module ", imp.path)};
            edges[index].push_back(file.string());
            ast.imports.push_back({imp.line, imp.end, file.string()});
            queue.push_back({file.string(), importedpkg});
        }
    }
    vector<int> rank(ast.sources.size());
    vector<bool> visited(ast.sources.size());
    int next = 0;
    function<void(int)> visit = [&](int f) {
        if (visited[f]) return;
        visited[f] = true;
        for (auto &path : edges[f]) visit(fileof.at(path));
        rank[f] = next++;
    };
    for (size_t i=0; i<ast.sources.size(); ++i) visit((int)i);
    stable_sort(ast.globals.begin(), ast.globals.end(), [&](VarDecl *a, VarDecl *b) {
        return rank[a->line.fileidx] < rank[b->line.fileidx];
    });
}
static void Write(const string &path, const string &text) {
    std::ofstream out(path, std::ios::binary);
    out.write(text.data(), text.size());
    if (!out) throw CompileError {cat("cannot write ", path)};
}
static string Read(const string &path) {
    string text;
    if (!LoadFile(path, text)) throw CompileError {cat("cannot read ", path)};
    return text;
}
static int Compile(int argc, char **argv) {
    string entry, output, header, stdlib="/goose/stdlib", runtime="/goose/runtime";
    bool check=false;
    int level=1;
    for (int i=1; i<argc; ++i) {
        string a=argv[i];
        if (a=="--version") { puts("goose 94cc64daeabc / Wisp WASI scalar-v1"); return 0; }
        if (a=="--check") check=true;
        else if (a=="-O0") level=0;
        else if (a=="-O1") level=1;
        else if (a=="-O2") level=2;
        else if (a=="-o" || a=="--header" || a=="--stdlib" || a=="--runtime") {
            if (++i==argc) throw CompileError {cat("missing value for ", a)};
            (a=="-o" ? output : a=="--header" ? header : a=="--stdlib" ? stdlib : runtime)=argv[i];
        } else if (!a.empty() && a[0]=='-') throw CompileError {cat("unsupported Goose WASI option ", a)};
        else if (!entry.empty()) throw CompileError {"expected one Goose entrypoint"};
        else entry=a;
    }
    if (entry.empty() || (!check && (output.empty() || header.empty())))
        throw CompileError {"usage: goose.wasm SOURCE --header OUT.h -o OUT.c [--check]"};
    // The compiler runs on WASI's 64 MiB shadow stack, without native threads.
    stackfloor = StackPointer() > (60u<<20) ? StackPointer()-(60u<<20) : 0;
    Ast ast;
    ParseProject(ast, entry, stdlib);
    if (!ast.errors.empty()) return 1;
    ResolveTypeNames(ast);
    TypeCheckProgram(ast, true);
    if (!ast.errors.empty()) return 1;
    Optimizer opt(ast, level);
    BCE bce(ast); bce.RunAll();
    if (!bce.checkfreefails.empty()) throw bce.CheckfreeError();
    auto ext=Read(runtime+"/runtime_ext.h");
    CodeGen cg(ast, ext, {}, false, true);
    // Fail before emitting an artifact if native stack-region semantics are
    // required. Never replace mmap/guard pages with unchecked heap storage.
    if (cg.usesthreads || cg.globalregions || cg.ProgramStacks(cg.MainRoots()))
        throw CompileError {"Goose WASI scalar-v1 does not support dynamic data stacks, threads or queues"};
    for (auto sf : ast.functions) {
        if (!sf->isexport) continue;
        auto scalar = [](TypeExpr *t) {
            return t->kind == TY_BOOL || t->kind == TY_FLT ||
                   (t->kind == TY_INT && t->intstorage != IS_VARINT);
        };
        for (auto sp : sf->specs) {
            for (auto t : sp->argtypes)
                if (!scalar(t)) throw CompileError {"Goose WASI scalar-v1 requires scalar export parameters"};
            for (auto t : sp->rets)
                if (!scalar(t)) throw CompileError {"Goose WASI scalar-v1 requires scalar export results"};
        }
    }
    for (auto sp : cg.usedexterns) {
        auto name=string(sp->sf->cname);
        if (!sp->sf->dylib.empty() || ext.find(cat(" ", name, "("))==string::npos)
            throw CompileError {cat("Goose WASI scalar-v1 does not support external function ", name)};
        if (name=="gs_ffi_cstr" || name=="gs_ffi_bytes")
            throw CompileError {"Goose WASI scalar-v1 does not support foreign memory"};
    }
    if (!check) {
        string c="/* Generated by Wisp's WASI Goose compiler, scalar-v1. */\n#define GS_DEBUG 1\n";
        c+=Read(runtime+"/runtime.h");
        c+=Read(runtime+"/support.h");
        c+=cg.head;
        // Upstream's aggregate layouts assume 64-bit pointers. Reject any
        // emitted fixed aggregate whose actual wasm32 C layout differs,
        // rather than copying the compiler's larger byte count silently.
        auto types=ast.alltypes;
        for (auto t : types) {
            if (t->kind!=TY_STRUCT && t->kind!=TY_ENUM &&
                t->kind!=TY_VARIANT && t->kind!=TY_ARRAY) continue;
            if (!cg.IsFix(t) || !cg.cdefined.count(cg.Mangle(t))) continue;
            auto size=cg.FixedSize(t);
            if (size>0) Append(c,"_Static_assert(sizeof(",cg.CT(t),") == ",size,
                ", \"Goose scalar-v1: unsupported wasm32 aggregate layout\");\n");
        }
        c+=ext;
        c+=cg.result;
        Write(output,c); Write(header,cg.ExportHeader());
    }
    return 0;
}
} // namespace goose

extern "C" gs_shaderc_result gs_shaderc_compile(const char *, const char *, int) {
    gs_shaderc_result result{};
    result.error=strdup("shader compilation is unavailable in Goose WASI scalar-v1");
    return result;
}
extern "C" void gs_shaderc_free(gs_shaderc_result *r) { free(r->blob); free(r->error); }
int main(int argc, char **argv) {
    try { return goose::Compile(argc,argv); }
    catch (const goose::CompileError &e) { fprintf(stderr,"%s\n",e.msg.c_str()); return 1; }
    catch (const std::exception &e) { fprintf(stderr,"goose: %s\n",e.what()); return 1; }
}
