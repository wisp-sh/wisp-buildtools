//! Exact-image compiler artifact sharding. No application compiler runs here.
use anyhow::{bail, ensure, Context, Result};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{borrow::Cow, fs, path::{Path, PathBuf}, ops::Range};
use wasm_encoder::{self as e, reencode::{Reencode, RoundtripReencoder}, Encode};
use wasmparser::{self as p, Operator as Op, Payload};

const DISPATCH: &str = "wisp-swift-shards";
const STATE: &str = "wisp-swift-state";
fn global_name(n: usize) -> String { format!("__wisp_global_{n}") }
fn hash(data: &[u8]) -> String { format!("{:x}", Sha256::digest(data)) }
fn validate(data: &[u8]) -> Result<()> {
    p::Validator::new_with_features(p::WasmFeatures::all()).validate_all(data)?;
    Ok(())
}

struct Segment { table: u32, offset: u32, funcs: Vec<u32> }
struct Image<'a> {
    data: &'a [u8], types: Range<usize>, imports: Vec<p::Import<'a>>,
    function_types: Vec<u32>, imported_functions: u32, tables: u32,
    imported_globals: u32, memories: u32, global_types: Vec<p::GlobalType>,
    globals: Option<Range<usize>>, exports: Vec<p::Export<'a>>,
    segments: Vec<Segment>, bodies: Vec<p::FunctionBody<'a>>, customs: Vec<String>,
}

impl<'a> Image<'a> {
    fn read(data: &'a [u8]) -> Result<Self> {
        validate(data).context("input validation")?;
        let mut image = Self { data, types: 0..0, imports: vec![], function_types: vec![],
            imported_functions: 0, tables: 0, imported_globals: 0, memories: 0,
            global_types: vec![], globals: None, exports: vec![], segments: vec![],
            bodies: vec![], customs: vec![] };
        for payload in p::Parser::new(0).parse_all(data) {
            match payload? {
                Payload::Version { encoding, .. } => ensure!(encoding == p::Encoding::Module, "core modules only"),
                Payload::TypeSection(reader) => {
                    image.types = reader.range();
                    for ty in reader.into_iter_err_on_gc_types() { ty.context("GC/recursive types unsupported")?; }
                }
                Payload::ImportSection(reader) => for import in reader.into_imports() {
                    let import = import?;
                    ensure!(import.module != STATE && import.module != DISPATCH, "reserved import namespace");
                    match import.ty {
                        p::TypeRef::Func(_) => image.imported_functions += 1,
                        p::TypeRef::Table(t) => { ensure!(!t.table64 && !t.shared, "table64/shared tables unsupported"); image.tables += 1; }
                        p::TypeRef::Memory(_) => image.memories += 1,
                        p::TypeRef::Global(_) => image.imported_globals += 1,
                        p::TypeRef::Tag(_) => (),
                        _ => bail!("exact function imports unsupported"),
                    }
                    image.imports.push(import);
                },
                Payload::FunctionSection(reader) => image.function_types = reader.into_iter().collect::<p::Result<_>>()?,
                Payload::GlobalSection(reader) => {
                    image.globals = Some(reader.range());
                    for global in reader {
                        let global = global?;
                        let mut ops = global.init_expr.get_operators_reader();
                        while !ops.eof() { ensure!(!matches!(ops.read()?, Op::RefFunc { .. }), "global ref.func unsupported"); }
                        image.global_types.push(global.ty);
                    }
                }
                Payload::ExportSection(reader) => image.exports = reader.into_iter().collect::<p::Result<_>>()?,
                Payload::ElementSection(reader) => for segment in reader {
                    let segment = segment?;
                    let (table, expr) = match segment.kind {
                        p::ElementKind::Active { table_index, offset_expr } => (table_index.unwrap_or(0), offset_expr),
                        _ => bail!("passive/declared elements unsupported"),
                    };
                    let mut ops = expr.get_operators_reader();
                    let offset = match ops.read()? { Op::I32Const { value } => value as u32, _ => bail!("element offset must be i32.const") };
                    ensure!(matches!(ops.read()?, Op::End) && ops.eof(), "extended element offset unsupported");
                    let funcs = match segment.items {
                        p::ElementItems::Functions(reader) => reader.into_iter().collect::<p::Result<Vec<_>>>()?,
                        _ => bail!("expression/null elements unsupported"),
                    };
                    let end = u64::from(offset) + funcs.len() as u64;
                    ensure!(end <= u64::from(u32::MAX), "element range overflow");
                    for old in &image.segments {
                        // Distinct imported table indices may resolve to the
                        // same host table, even under different import names.
                        // Ownership changes initialization order, so overlapping
                        // slots are unsafe across every imported table index.
                        ensure!(funcs.is_empty() || old.funcs.is_empty() || end <= u64::from(old.offset) ||
                            u64::from(offset) >= u64::from(old.offset) + old.funcs.len() as u64,
                            "overlapping element segments unsupported across potentially aliased imported tables");
                    }
                    image.segments.push(Segment { table, offset, funcs });
                },
                Payload::CodeSectionStart { .. } | Payload::End(_) => (),
                Payload::CodeSectionEntry(body) => image.bodies.push(body),
                Payload::CustomSection(reader) => image.customs.push(reader.name().to_string()),
                _ => bail!("unsupported section: defined tables/memories/tags, data, data-count, or start"),
            }
        }
        ensure!(!image.function_types.is_empty(), "no defined functions");
        ensure!(image.bodies.len() == image.function_types.len(), "function/code mismatch");
        ensure!(image.memories >= 1, "an imported memory 0 is required for WASI caller access");
        for export in &image.exports {
            ensure!(!(export.name == "memory" && (export.kind != p::ExternalKind::Memory || export.index != 0)), "conflicting memory export");
            ensure!(!(0..image.global_types.len()).any(|n| export.name == global_name(n)), "reserved state export name");
        }
        Ok(image)
    }

    fn imports(&self, shard: bool) -> Result<e::ImportSection> {
        let mut result = e::ImportSection::new();
        let mut reencoder = RoundtripReencoder;
        for import in &self.imports {
            result.import(import.module, import.name, reencoder.entity_type(import.ty)?);
        }
        if shard {
            result.import(DISPATCH, "functions", e::TableType { element_type: e::RefType::FUNCREF,
                table64: false, minimum: self.bodies.len() as u64, maximum: Some(self.bodies.len() as u64), shared: false });
            for (n, ty) in self.global_types.iter().enumerate() {
                result.import(STATE, &global_name(n), e::EntityType::Global(reencoder.global_type(*ty)?));
            }
        }
        Ok(result)
    }

    fn exports(&self, owned: Option<Range<u32>>) -> Result<(e::ExportSection, Vec<String>)> {
        let mut result = e::ExportSection::new();
        let mut names = vec![];
        for export in &self.exports {
            let index = if export.kind == p::ExternalKind::Func {
                if export.index < self.imported_functions {
                    if owned.is_some() { continue; }
                    export.index
                } else if let Some(range) = &owned {
                    let ordinal = export.index - self.imported_functions;
                    if !range.contains(&ordinal) { continue; }
                    self.imported_functions + ordinal - range.start
                } else { continue; }
            } else {
                if owned.is_some() && export.name != "memory" { continue; }
                export.index
            };
            result.export(export.name, RoundtripReencoder.export_kind(export.kind)?, index);
            names.push(export.name.to_string());
        }
        if !names.iter().any(|name| name == "memory") {
            result.export("memory", e::ExportKind::Memory, 0); names.push("memory".to_string());
        }
        if owned.is_none() {
            for n in 0..self.global_types.len() {
                let name = global_name(n);
                result.export(&name, e::ExportKind::Global, self.imported_globals + n as u32);
                names.push(name);
            }
        }
        Ok((result, names))
    }

    fn elements(&self, owned: Option<&Range<u32>>) -> e::ElementSection {
        let mut elements = e::ElementSection::new();
        for segment in &self.segments {
            // Runs avoid writing another shard's slots, including temporary nulls.
            let mut cursor = 0;
            while cursor < segment.funcs.len() {
                let belongs = |f: u32| match owned {
                    Some(range) => f >= self.imported_functions && range.contains(&(f - self.imported_functions)),
                    None => f < self.imported_functions,
                };
                if !belongs(segment.funcs[cursor]) { cursor += 1; continue; }
                let start = cursor;
                let mut funcs = vec![];
                while cursor < segment.funcs.len() && belongs(segment.funcs[cursor]) {
                    let f = segment.funcs[cursor];
                    funcs.push(match owned { Some(range) => f - range.start, None => f });
                    cursor += 1;
                }
                elements.active(Some(segment.table), &e::ConstExpr::i32_const((segment.offset + start as u32) as i32),
                    e::Elements::Functions(Cow::Owned(funcs)));
            }
        }
        if let Some(range) = owned {
            elements.active(Some(self.tables), &e::ConstExpr::i32_const(range.start as i32),
                e::Elements::Functions(Cow::Owned((0..range.len() as u32).map(|n| self.imported_functions + n).collect())));
        }
        elements
    }

    fn module(&self, owned: Option<Range<u32>>, calls: &mut u64) -> Result<(Vec<u8>, Vec<String>)> {
        let mut module = e::Module::new();
        module.section(&e::RawSection { id: 1, data: &self.data[self.types.clone()] });
        module.section(&self.imports(owned.is_some())?);
        if let Some(range) = &owned {
            let mut functions = e::FunctionSection::new();
            for n in range.clone() { functions.function(self.function_types[n as usize]); }
            module.section(&functions);
        } else if let Some(range) = &self.globals {
            module.section(&e::RawSection { id: 6, data: &self.data[range.clone()] });
        }
        let (exports, names) = self.exports(owned.clone())?;
        module.section(&exports);
        let elements = self.elements(owned.as_ref());
        if !elements.is_empty() { module.section(&elements); }
        if let Some(range) = owned {
            let mut code = e::CodeSection::new();
            for ordinal in range.clone() {
                let body = &self.bodies[ordinal as usize];
                let mut ops = body.get_operators_reader()?;
                let mut rewritten = Vec::new();
                let mut copied = body.range().start;
                while !ops.eof() {
                    let start = ops.original_position();
                    let op = ops.read()?;
                    match op {
                        Op::Call { function_index } | Op::ReturnCall { function_index } => {
                            let tail = matches!(op, Op::ReturnCall { .. });
                            if function_index < self.imported_functions { continue; }
                            rewritten.extend_from_slice(&self.data[copied..start]);
                            let dest = function_index - self.imported_functions;
                            if range.contains(&dest) {
                                if tail { e::Instruction::ReturnCall(self.imported_functions + dest - range.start) }
                                else { e::Instruction::Call(self.imported_functions + dest - range.start) }.encode(&mut rewritten);
                            } else {
                                e::Instruction::I32Const(dest as i32).encode(&mut rewritten);
                                let ty = self.function_types[dest as usize];
                                if tail { e::Instruction::ReturnCallIndirect { type_index: ty, table_index: self.tables } }
                                else { e::Instruction::CallIndirect { type_index: ty, table_index: self.tables } }.encode(&mut rewritten);
                                *calls += 1;
                            }
                            copied = ops.original_position();
                        }
                        Op::RefFunc { .. } => bail!("ref.func in body {ordinal} unsupported; nullable table.get is not a typed replacement"),
                        Op::TableInit { .. } | Op::ElemDrop { .. } => bail!("element-index instructions in body {ordinal} unsupported"),
                        _ => (),
                    }
                }
                rewritten.extend_from_slice(&self.data[copied..body.range().end]);
                code.raw(&rewritten);
            }
            module.section(&code);
        }
        let bytes = module.finish();
        validate(&bytes).context("derived module validation")?;
        Ok((bytes, names))
    }
}

fn write_module(out: &Path, filename: &str, bytes: Vec<u8>, names: Vec<String>) -> Result<Value> {
    let record = json!({"file": filename, "path": out.join(filename), "bytes": bytes.len(), "sha256": hash(&bytes), "exports": names});
    let path = out.join(filename);
    let mut file = fs::File::create(&path)?;
    std::io::Write::write_all(&mut file, &bytes)?;
    file.sync_all()?;
    Ok(record)
}

fn run() -> Result<()> {
    let mut input = None; let mut out = None; let mut shards = 8usize; let mut name = "swift-frontend".to_string();
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--input" => input = args.next().map(PathBuf::from),
            "--out" => out = args.next().map(PathBuf::from),
            "--shards" => shards = args.next().context("--shards value")?.parse()?,
            "--name" => name = args.next().context("--name value")?,
            _ => bail!("usage: wisp-swift-shard --input MODULE --out NEW_DIRECTORY [--shards 8] [--name swift-frontend]"),
        }
    }
    let input = fs::canonicalize(input.context("--input required")?)?;
    let requested_out = out.context("--out required")?;
    let parent = requested_out.parent().filter(|p| !p.as_os_str().is_empty()).unwrap_or(Path::new("."));
    fs::create_dir_all(parent)?;
    let out = fs::canonicalize(parent)?.join(requested_out.file_name().context("output directory name required")?);
    ensure!(!out.exists(), "output must not exist; publication is an atomic directory rename");
    ensure!(shards > 0 && shards <= 1024, "shards must be 1..1024");
    let data = fs::read(&input)?;
    let image = Image::read(&data)?;
    ensure!(shards <= image.bodies.len(), "more shards than functions");
    let temporary = out.with_file_name(format!(".{}.pending-{}", out.file_name().unwrap().to_string_lossy(), std::process::id()));
    fs::create_dir(&temporary)?;
    let result = (|| -> Result<Value> {
        let mut cross_calls = 0;
        let (bytes, exports) = image.module(None, &mut cross_calls)?;
        let mut state = write_module(&temporary, "state.wasm", bytes, exports)?;
        state["path"] = json!(out.join("state.wasm"));
        let mut parts = vec![];
        for n in 0..shards {
            let range = (image.bodies.len() * n / shards) as u32..(image.bodies.len() * (n + 1) / shards) as u32;
            let (bytes, exports) = image.module(Some(range.clone()), &mut cross_calls)?;
            let filename = format!("shard-{n:03}.wasm");
            let mut part = write_module(&temporary, &filename, bytes, exports)?;
            part["path"] = json!(out.join(&filename));
            part["definedFunctionStart"] = json!(range.start);
            part["definedFunctionEndExclusive"] = json!(range.end);
            part["definedFunctions"] = json!(range.end - range.start);
            parts.push(part);
        }
        let tool = fs::read(std::env::current_exe()?)?;
        let total: u64 = std::iter::once(&state).chain(parts.iter()).map(|p| p["bytes"].as_u64().unwrap()).sum();
        let mut load_order = vec!["original-primary".to_string(), "state.wasm".to_string()];
        load_order.extend(parts.iter().map(|p| p["file"].as_str().unwrap().to_string()));
        Ok(json!({"schemaVersion": 1, "kind": "wisp-exact-image-function-shards", "name": name,
            "source": input, "sourceSha256": hash(&data), "sourceBytes": data.len(),
            "toolSha256": hash(&tool), "wasmparserVersion": "0.245.1", "wasmEncoderVersion": "0.245.1",
            "nativeBootstrapOnly": true, "nativeApplicationCompilerUsed": false,
            "definedFunctions": image.bodies.len(), "importedFunctions": image.imported_functions,
            "originalImports": image.imports.len(), "importedGlobals": image.imported_globals,
            "definedGlobals": image.global_types.len(), "originalElementSegments": image.segments.len(),
            "rewrittenCrossShardCalls": cross_calls, "totalModuleBytes": total,
            "dispatchTable": {"module": DISPATCH, "name": "functions", "element": "funcref",
                "minimum": image.bodies.len(), "maximum": image.bodies.len(), "table64": false,
                "shared": false, "shardTableIndex": image.tables, "initialValue": "null"},
            "stateNamespace": STATE, "globalExports": (0..image.global_types.len()).map(global_name).collect::<Vec<_>>(),
            "loadOrder": load_order, "state": state, "shards": parts,
            "droppedCustomSections": image.customs,
            "invariants": ["original imports, type indices, memory/table/tag/global indices preserved",
                "defined globals initialized exactly once by state; every shard imports those same globals",
                "every original defined function occurs exactly once; cross-shard direct calls use dedicated dispatch table",
                "original active element slots written only by their function owner; no shard writes null over another shard",
                "all shards export memory 0 for WASI immediate-caller access",
                "instantiate primary, state, every shard before invoking any guest function; do not expose or modify dispatch table",
                "all source and output modules validated without JIT; custom metadata removed"]}))
    })();
    match result {
        Ok(manifest) => {
            let manifest_bytes = serde_json::to_vec_pretty(&manifest)?;
            let mut file = fs::File::create(temporary.join("manifest.json"))?;
            std::io::Write::write_all(&mut file, &manifest_bytes)?; file.sync_all()?;
            // Another successful writer must never be overwritten.
            ensure!(!out.exists(), "output appeared during transformation");
            fs::rename(&temporary, &out)?;
            println!("{}", serde_json::to_string(&manifest)?);
            Ok(())
        }
        Err(error) => { let _ = fs::remove_dir_all(&temporary); Err(error) }
    }
}
fn main() { if let Err(error) = run() { eprintln!("wisp-swift-shard: {error:#}"); std::process::exit(1); } }
