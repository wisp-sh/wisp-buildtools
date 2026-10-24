//! WASI import stubbing for snapshotted components.
//!
//! The approach:
//! 1. Decode the snapshotted component to extract its WIT world
//! 2. Create a "stub world" where the WASI imports become exports
//! 3. Use `dummy_module` to generate a core module with trap implementations
//! 4. Encode it as a stub component
//! 5. Use `wasm-compose` to compose the stub into the original component

use anyhow::{Context, Result, bail};
use indexmap::IndexMap;
use wasm_compose::graph::{Component, CompositionGraph, EncodeOptions};
use wasm_encoder::reencode::Reencode;
use wit_component::{ComponentEncoder, StringEncoding, dummy_module, embed_component_metadata};
use wit_parser::decoding::{DecodedWasm, decode};
use wit_parser::{Docs, ManglingAndAbi, Resolve, Stability, World, WorldItem, WorldKey};

/// Stub all WASI imports in a component, producing a self-contained component.
pub fn stub_wasi_imports(component: &[u8]) -> Result<Vec<u8>> {
    stub_imports(component, |name| name.starts_with("wasi:"))
}

/// Stub componentization-only imports that must not leak into final components.
pub fn stub_internal_imports(component: &[u8]) -> Result<Vec<u8>> {
    stub_imports(component, |name| name == "local:init/module-loader")
}

/// Validation initializes the source with unavailable authored host calls trapped.
pub fn stub_authored_imports(component: &[u8]) -> Result<Vec<u8>> {
    stub_imports(component, |name| !name.starts_with("wasi:"))
}

fn stub_imports(component: &[u8], should_stub: impl Fn(&str) -> bool) -> Result<Vec<u8>> {
    let decoded = decode(component).context("failed to decode component WIT")?;
    let (resolve, world_id) = match decoded {
        DecodedWasm::Component(resolve, world_id) => (resolve, world_id),
        _ => bail!("expected a component, got a WIT package"),
    };

    let world = &resolve.worlds[world_id];

    let imports: IndexMap<WorldKey, WorldItem> = world
        .imports
        .clone()
        .into_iter()
        .filter(|(key, _)| should_stub(&resolve.name_world_key(key)))
        .collect();

    if imports.is_empty() {
        return Ok(component.to_vec());
    }

    let stub_component =
        make_stub_component(&resolve, world, &imports).context("failed to build stub component")?;

    let mut validator = wasmparser::Validator::new_with_features(wasmparser::WasmFeatures::all());
    let original = Component::from_bytes(&mut validator, "original", component)?;
    let stub = Component::from_bytes(&mut validator, "stubs", stub_component)?;
    let connections = original
        .imports()
        .filter_map(|(import, name, _)| {
            if !should_stub(name) {
                return None;
            }
            Some(
                stub.export_by_name(name)
                    .map(|(export, _, _)| (export, import))
                    .with_context(|| format!("stub component has no export {name}")),
            )
        })
        .collect::<Result<Vec<_>>>()?;
    let mut graph = CompositionGraph::new();
    let original = graph.add_component(original)?;
    let stub = graph.add_component(stub)?;
    let original = graph.instantiate(original)?;
    let stub = graph.instantiate(stub)?;
    for (export, import) in connections {
        graph.connect(stub, Some(export), original, import)?;
    }
    graph
        .encode(EncodeOptions {
            define_components: true,
            export: Some(original),
            validate: true,
        })
        .context("failed to compose stub component")
}

/// Build a component that exports trap implementations for the given imports.
fn make_stub_component(
    resolve: &Resolve,
    original_world: &World,
    imports: &IndexMap<WorldKey, WorldItem>,
) -> Result<Vec<u8>> {
    let mut stub_resolve = resolve.clone();
    let stub_world_id = stub_resolve.worlds.alloc(World {
        name: "wasi-stubs".to_string(),
        imports: IndexMap::new(),
        exports: imports.clone(),
        package: original_world.package,
        docs: Docs::default(),
        stability: Stability::default(),
        includes: Vec::new(),
        span: Default::default(),
    });

    let mut core_module = dummy_module(&stub_resolve, stub_world_id, ManglingAndAbi::Standard32);
    core_module = deterministic_startup_stubs(&core_module)?;

    embed_component_metadata(
        &mut core_module,
        &stub_resolve,
        stub_world_id,
        StringEncoding::UTF8,
    )
    .context("failed to embed component metadata in stub module")?;

    ComponentEncoder::default()
        .module(&core_module)
        .unwrap()
        .validate(true)
        .encode()
        .context("failed to encode stub component")
}

/// libc and QuickJS initialize their RNG/time bookkeeping before evaluating user
/// code. Supply only these three constant APIs; every other WASI call still traps.
fn deterministic_startup_stubs(module: &[u8]) -> Result<Vec<u8>> {
    use std::collections::BTreeMap;
    use wasmparser::{ExternalKind, Parser, Payload, TypeRef};
    #[derive(Clone, Copy)]
    enum Constant {
        Pointer,
        U64,
    }
    let mut replacements = BTreeMap::new();
    let mut imports = 0_u32;
    for payload in Parser::new(0).parse_all(module) {
        match payload? {
            Payload::ImportSection(reader) => {
                for import in reader.into_imports() {
                    if matches!(import?.ty, TypeRef::Func(_)) {
                        imports += 1;
                    }
                }
            }
            Payload::ExportSection(reader) => {
                for export in reader {
                    let export = export?;
                    if export.kind != ExternalKind::Func {
                        continue;
                    }
                    let value = if (export.name.starts_with("cm32p2|wasi:random/insecure-seed@")
                        && export.name.ends_with("|insecure-seed"))
                        || (export.name.starts_with("cm32p2|wasi:clocks/wall-clock@")
                            && export.name.ends_with("|now"))
                    {
                        Some(Constant::Pointer)
                    } else if export
                        .name
                        .starts_with("cm32p2|wasi:clocks/monotonic-clock@")
                        && export.name.ends_with("|now")
                    {
                        Some(Constant::U64)
                    } else {
                        None
                    };
                    if let Some(value) = value {
                        replacements.insert(export.index, value);
                    }
                }
            }
            _ => {}
        }
    }
    if replacements.is_empty() {
        return Ok(module.to_vec());
    }
    struct Stubs {
        next_function: u32,
        replacements: BTreeMap<u32, Constant>,
    }
    impl Reencode for Stubs {
        type Error = std::convert::Infallible;
        fn memory_type(
            &mut self,
            mut ty: wasmparser::MemoryType,
        ) -> std::result::Result<wasm_encoder::MemoryType, wasm_encoder::reencode::Error<Self::Error>>
        {
            ty.initial = ty.initial.max(1);
            Ok(wasm_encoder::reencode::utils::memory_type(self, ty))
        }
        fn parse_function_body(
            &mut self,
            code: &mut wasm_encoder::CodeSection,
            body: wasmparser::FunctionBody<'_>,
        ) -> std::result::Result<(), wasm_encoder::reencode::Error<Self::Error>> {
            let index = self.next_function;
            self.next_function += 1;
            if let Some(value) = self.replacements.get(&index) {
                let mut function = wasm_encoder::Function::new([]);
                function.instruction(&match value {
                    Constant::Pointer => wasm_encoder::Instruction::I32Const(0),
                    Constant::U64 => wasm_encoder::Instruction::I64Const(0),
                });
                function.instruction(&wasm_encoder::Instruction::End);
                code.function(&function);
                Ok(())
            } else {
                wasm_encoder::reencode::utils::parse_function_body(self, code, body)
            }
        }
    }
    let mut output = wasm_encoder::Module::new();
    Stubs {
        next_function: imports,
        replacements,
    }
    .parse_core_module(&mut output, Parser::new(0), module)
    .context("generating deterministic WASI initialization stubs")?;
    Ok(output.finish())
}
