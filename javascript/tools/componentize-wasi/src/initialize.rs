//! Initialize QuickJS on the first exported call, without Wizer.
use anyhow::{Context, Result, bail};
use wasm_encoder::reencode::{Reencode, ReencodeComponent, RoundtripReencoder};
use wasm_encoder::{
    Alias, CanonicalFunctionSection, CodeSection, Component, ComponentAliasSection,
    ComponentExportKind, ComponentExportSection, ComponentTypeSection, ConstExpr, DataSection,
    EntityType, ExportKind, ExportSection, Function, FunctionSection, GlobalSection, GlobalType,
    ImportSection, InstanceSection, Instruction, MemArg, MemoryType, Module, ModuleArg,
    ModuleSection, RawSection, TypeSection, ValType,
};
use wasmparser::{Parser, Payload, Validator, WasmFeatures};

/// Strip custom sections recursively while preserving all executable bytes.
pub fn strip_custom_sections(bytes: &[u8]) -> Result<Vec<u8>> {
    if bytes.len() < 8 || &bytes[..4] != b"\0asm" {
        bail!("invalid Wasm header");
    }
    let is_component = bytes[4..8] == [13, 0, 1, 0];
    let mut output = bytes[..8].to_vec();
    let mut cursor = 8;
    while cursor < bytes.len() {
        let id = bytes[cursor];
        cursor += 1;
        let mut length = 0_usize;
        let mut shift = 0;
        loop {
            let byte = *bytes.get(cursor).context("truncated section length")?;
            cursor += 1;
            if shift >= 35 {
                bail!("section length overflow");
            }
            length |= usize::from(byte & 0x7f) << shift;
            if byte & 0x80 == 0 {
                break;
            }
            shift += 7;
        }
        let end = cursor
            .checked_add(length)
            .context("section length overflow")?;
        let body = bytes.get(cursor..end).context("truncated section")?;
        if id != 0 {
            let body = if is_component && (id == 1 || id == 4) {
                strip_custom_sections(body)?
            } else {
                body.to_vec()
            };
            output.push(id);
            let mut remaining = body.len();
            loop {
                let mut byte = (remaining & 0x7f) as u8;
                remaining >>= 7;
                if remaining > 0 {
                    byte |= 0x80;
                }
                output.push(byte);
                if remaining == 0 {
                    break;
                }
            }
            output.extend_from_slice(&body);
        }
        cursor = end;
    }
    Ok(output)
}

fn checked_types(bytes: &[u8]) -> Result<wasmparser::types::Types> {
    Validator::new_with_features(WasmFeatures::all())
        .validate_all(bytes)
        .context("validating linked Component")
}

/// Initialize during the first lifted call. Core start functions have no caller
/// task in Component hosts such as JCO; they must not enter canonical adapters.
/// Wrap the linked runtime's core exports before any stub composition nests it.
pub fn initialize_deferred_component(
    bytes: &[u8],
    shim: &str,
    source: &str,
    validation: bool,
) -> Result<Vec<u8>> {
    let types = checked_types(bytes)?;
    let types = types.as_ref();
    let mut init_func = None;
    let mut metadata_module = None;
    let mut metadata_instance = None;
    let mut module_count = 0;
    let mut instance_count = 0;
    let mut depth = 0;
    for payload in Parser::new(0).parse_all(bytes) {
        let payload = payload?;
        if depth == 0 {
            match &payload {
                Payload::ModuleSection {
                    unchecked_range, ..
                } => {
                    for inner in Parser::new(0).parse_all(
                        &bytes[unchecked_range.start as usize..unchecked_range.end as usize],
                    ) {
                        if let Payload::ExportSection(exports) = inner? {
                            for export in exports {
                                if export?.name == "wisp-initialize-metadata" {
                                    metadata_module = Some(module_count);
                                }
                            }
                        }
                    }
                    module_count += 1;
                }
                Payload::InstanceSection(reader) => {
                    for instance in reader.clone() {
                        if let wasmparser::Instance::Instantiate { module_index, .. } = instance? {
                            if Some(module_index) == metadata_module {
                                metadata_instance = Some(instance_count);
                            }
                        }
                        instance_count += 1;
                    }
                }
                _ => {}
            }
            if let Payload::ComponentExportSection(reader) = &payload {
                for export in reader.clone() {
                    let export = export?;
                    if export.name.name == "init" {
                        init_func = Some(export.index);
                    }
                }
            }
        }
        match payload {
            Payload::ModuleSection { .. } | Payload::ComponentSection { .. } => depth += 1,
            Payload::End(_) if depth > 0 => depth -= 1,
            _ => {}
        }
    }
    let init_func = init_func.context("linked runtime has no init export")?;
    let mut component = Component::new();
    let mut remap = InitRemap::default();
    let (mut modules, mut instances, mut functions, mut component_functions) = (0, 0, 0, 0);
    let mut ensure_init = None;
    let mut private_init_lift = None;
    let mut depth = 0;
    for payload in Parser::new(0).parse_all(bytes) {
        let payload = payload?;
        if depth > 0 {
            match payload {
                Payload::ModuleSection { .. } | Payload::ComponentSection { .. } => depth += 1,
                Payload::End(_) => depth -= 1,
                _ => {}
            }
            continue;
        }
        match payload {
            Payload::ModuleSection {
                unchecked_range: range,
                ..
            }
            | Payload::ComponentSection {
                unchecked_range: range,
                ..
            } => {
                let id = if bytes[range.start as usize + 4] == 1 {
                    1
                } else {
                    4
                };
                component.section(&RawSection {
                    id,
                    data: &bytes[range.start as usize..range.end as usize],
                });
                if id == 1 {
                    modules += 1;
                }
                depth = 1;
            }
            Payload::ComponentCanonicalSection(reader) => {
                for canonical in reader {
                    let mut canonical = canonical?;
                    if let wasmparser::CanonicalFunction::Lift {
                        core_func_index,
                        type_index,
                        options,
                    } = &mut canonical
                    {
                        if component_functions == init_func {
                            let memory = options
                                .iter()
                                .find_map(|o| {
                                    if let wasmparser::CanonicalOption::Memory(i) = o {
                                        Some(*i)
                                    } else {
                                        None
                                    }
                                })
                                .context("init memory")?;
                            let realloc = options
                                .iter()
                                .find_map(|o| {
                                    if let wasmparser::CanonicalOption::Realloc(i) = o {
                                        Some(*i)
                                    } else {
                                        None
                                    }
                                })
                                .context("init realloc")?;
                            let post = options
                                .iter()
                                .find_map(|o| {
                                    if let wasmparser::CanonicalOption::PostReturn(i) = o {
                                        Some(*i)
                                    } else {
                                        None
                                    }
                                })
                                .context("init post-return")?;
                            let metadata_func = remap.function_index(functions)?;
                            let mut aliases = ComponentAliasSection::new();
                            aliases.alias(Alias::CoreInstanceExport {
                                instance: remap.instance_index(
                                    metadata_instance
                                        .context("bindings metadata initializer instance")?,
                                ),
                                kind: ExportKind::Func,
                                name: "wisp-initialize-metadata",
                            });
                            component.section(&aliases);
                            remap.functions.push((functions, 1));
                            let module = deferred_initializer(shim, source)?;
                            let init = remap.function_index(*core_func_index)?;
                            let realloc = remap.function_index(realloc)?;
                            let post = remap.function_index(post)?;
                            let f = append_helper(
                                &mut component,
                                &mut remap,
                                modules,
                                instances,
                                functions,
                                &module,
                                [
                                    ("init", init),
                                    ("realloc", realloc),
                                    ("post", post),
                                    ("metadata", metadata_func),
                                ]
                                .as_slice(),
                                Some(memory),
                                "ensure-init",
                            )?;
                            ensure_init = Some(f);
                            let saved_options = options
                                .iter()
                                .map(|o| remap.canonical_option(*o))
                                .collect::<Result<Vec<_>, _>>()?;
                            private_init_lift = Some((init, *type_index, saved_options));
                        } else if let Some(ensure) = ensure_init {
                            if options.contains(&wasmparser::CanonicalOption::Async) {
                                bail!("deferred initialization supports synchronous exports only");
                            }
                            let signature =
                                types[types.core_function_at(*core_func_index)].unwrap_func();
                            let module = deferred_forwarder(signature)?;
                            let target = remap.function_index(*core_func_index)?;
                            let wrapped = append_helper(
                                &mut component,
                                &mut remap,
                                modules,
                                instances,
                                functions,
                                &module,
                                [("ensure-init", ensure), ("target", target)].as_slice(),
                                None,
                                "call",
                            )?;
                            // This is already a remapped function index.
                            *core_func_index = wrapped;
                            let wasmparser::CanonicalFunction::Lift {
                                type_index,
                                options,
                                ..
                            } = canonical
                            else {
                                unreachable!()
                            };
                            let mut section = CanonicalFunctionSection::new();
                            let options = options
                                .iter()
                                .map(|o| remap.canonical_option(*o))
                                .collect::<Result<Vec<_>, _>>()?;
                            section.lift(wrapped, type_index, options);
                            component.section(&section);
                            component_functions += 1;
                            continue;
                        } else {
                            bail!("linked runtime lifts an export before init");
                        }
                        component_functions += 1;
                    } else {
                        functions += 1;
                    }
                    let mut section = CanonicalFunctionSection::new();
                    remap.parse_component_canonical(&mut section, canonical)?;
                    component.section(&section);
                }
            }
            Payload::ComponentAliasSection(reader) => {
                for alias in reader.clone() {
                    match alias? {
                        wasmparser::ComponentAlias::CoreInstanceExport {
                            kind: wasmparser::ExternalKind::Func,
                            ..
                        } => functions += 1,
                        wasmparser::ComponentAlias::InstanceExport {
                            kind: wasmparser::ComponentExternalKind::Func,
                            ..
                        } => component_functions += 1,
                        _ => {}
                    }
                }
                let mut section = ComponentAliasSection::new();
                remap.parse_component_alias_section(&mut section, reader)?;
                component.section(&section);
            }
            Payload::InstanceSection(reader) => {
                instances += reader.count();
                let mut section = InstanceSection::new();
                remap.parse_instance_section(&mut section, reader)?;
                component.section(&section);
            }
            Payload::ComponentExportSection(reader) => {
                // Export entries create aliases too; preserve their index spaces.
                for export in reader.clone() {
                    if export?.kind == wasmparser::ComponentExternalKind::Func {
                        component_functions += 1;
                    }
                }
                for export in reader {
                    let export = export?;
                    if export.name.name == "init" {
                        // A second private lift occupies the alias index that the
                        // hidden export used to introduce. All later indices stay put.
                        let (func, ty, options) =
                            private_init_lift.as_ref().context("private init lift")?;
                        let mut section = CanonicalFunctionSection::new();
                        section.lift(*func, *ty, options.iter().copied());
                        component.section(&section);
                    } else {
                        let mut section = ComponentExportSection::new();
                        remap.parse_component_export(&mut section, export)?;
                        component.section(&section);
                    }
                }
            }
            other => {
                if let Some((id, range)) = other.as_section() {
                    component.section(&RawSection {
                        id,
                        data: &bytes[range.start as usize..range.end as usize],
                    });
                }
            }
        }
    }
    let ensure_init = ensure_init.context("linked runtime init is not a canonical lift")?;
    if validation {
        let mut ty = ComponentTypeSection::new();
        ty.function()
            .params([] as [(&str, wasm_encoder::ComponentValType); 0])
            .result(None);
        component.section(&ty);
        let mut canonical = CanonicalFunctionSection::new();
        canonical.lift(ensure_init, types.component_type_count(), []);
        component.section(&canonical);
        let mut exports = ComponentExportSection::new();
        exports.export(
            "wisp-validate",
            ComponentExportKind::Func,
            component_functions,
            None,
        );
        component.section(&exports);
    }
    let result = component.finish();
    checked_types(&result).context("validating deferred initialization")?;
    Ok(result)
}

#[derive(Default)]
struct InitRemap {
    functions: Vec<(u32, u32)>,
    modules: Vec<(u32, u32)>,
    instances: Vec<(u32, u32)>,
}
fn shifted(index: u32, insertions: &[(u32, u32)]) -> u32 {
    index
        + insertions
            .iter()
            .filter(|(at, _)| *at <= index)
            .map(|(_, n)| n)
            .sum::<u32>()
}
impl Reencode for InitRemap {
    type Error = std::convert::Infallible;
    fn function_index(
        &mut self,
        index: u32,
    ) -> Result<u32, wasm_encoder::reencode::Error<Self::Error>> {
        Ok(shifted(index, &self.functions))
    }
}
impl ReencodeComponent for InitRemap {
    fn module_index(&mut self, index: u32) -> u32 {
        shifted(index, &self.modules)
    }
    fn instance_index(&mut self, index: u32) -> u32 {
        shifted(index, &self.instances)
    }
}

fn append_helper(
    component: &mut Component,
    remap: &mut InitRemap,
    modules: u32,
    instances: u32,
    functions: u32,
    module: &Module,
    imports: &[(&str, u32)],
    memory: Option<u32>,
    export: &str,
) -> Result<u32> {
    let module_index = remap.module_index(modules);
    let instance_index = remap.instance_index(instances);
    let function_index = remap.function_index(functions)?;
    component.section(&ModuleSection(module));
    let mut section = InstanceSection::new();
    let mut items = imports
        .iter()
        .map(|(name, index)| (*name, ExportKind::Func, *index))
        .collect::<Vec<_>>();
    if let Some(memory) = memory {
        items.push(("memory", ExportKind::Memory, memory));
    }
    section.export_items(items);
    section.instantiate(module_index, [("env", ModuleArg::Instance(instance_index))]);
    component.section(&section);
    let mut aliases = ComponentAliasSection::new();
    aliases.alias(Alias::CoreInstanceExport {
        instance: instance_index + 1,
        kind: ExportKind::Func,
        name: export,
    });
    component.section(&aliases);
    remap.modules.push((modules, 1));
    remap.instances.push((instances, 2));
    remap.functions.push((functions, 1));
    Ok(function_index)
}

fn deferred_initializer(shim: &str, source: &str) -> Result<Module> {
    let mut module = Module::new();
    let mut types = TypeSection::new();
    types.ty().function([ValType::I32; 8], [ValType::I32]);
    types.ty().function([ValType::I32; 4], [ValType::I32]);
    types.ty().function([ValType::I32], []);
    types.ty().function([], []);
    module.section(&types);
    let mut imports = ImportSection::new();
    imports.import("env", "init", EntityType::Function(0));
    imports.import("env", "realloc", EntityType::Function(1));
    imports.import("env", "post", EntityType::Function(2));
    imports.import("env", "metadata", EntityType::Function(3));
    imports.import(
        "env",
        "memory",
        EntityType::Memory(MemoryType {
            minimum: 0,
            maximum: None,
            memory64: false,
            shared: false,
            page_size_log2: None,
        }),
    );
    module.section(&imports);
    let mut functions = FunctionSection::new();
    functions.function(3);
    module.section(&functions);
    let mut globals = GlobalSection::new();
    globals.global(
        GlobalType {
            val_type: ValType::I32,
            mutable: true,
            shared: false,
        },
        &ConstExpr::i32_const(0),
    );
    module.section(&globals);
    let mut exports = ExportSection::new();
    exports.export("ensure-init", ExportKind::Func, 4);
    module.section(&exports);
    // Passive segments copy into allocations in the runtime's own memory.
    module.section(&wasm_encoder::DataCountSection { count: 2 });
    let mut function = Function::new([(3, ValType::I32)]);
    function.instruction(&Instruction::GlobalGet(0));
    function.instruction(&Instruction::I32Const(2));
    function.instruction(&Instruction::I32Eq);
    function.instruction(&Instruction::If(wasm_encoder::BlockType::Empty));
    function.instruction(&Instruction::Return);
    function.instruction(&Instruction::End);
    // A failed or reentrant initializer must never evaluate the module twice.
    function.instruction(&Instruction::GlobalGet(0));
    function.instruction(&Instruction::If(wasm_encoder::BlockType::Empty));
    function.instruction(&Instruction::Unreachable);
    function.instruction(&Instruction::End);
    function.instruction(&Instruction::I32Const(1));
    function.instruction(&Instruction::GlobalSet(0));
    function.instruction(&Instruction::Call(3));
    for (index, text) in [shim, source].iter().enumerate() {
        let length: i32 = text.len().try_into()?;
        for n in [0, 0, 1, length] {
            function.instruction(&Instruction::I32Const(n));
        }
        function.instruction(&Instruction::Call(1));
        function.instruction(&Instruction::LocalTee(index as u32));
        function.instruction(&Instruction::I32Const(0));
        function.instruction(&Instruction::I32Const(length));
        function.instruction(&Instruction::MemoryInit {
            mem: 0,
            data_index: index as u32,
        });
    }
    function.instruction(&Instruction::LocalGet(0));
    function.instruction(&Instruction::I32Const(shim.len().try_into()?));
    function.instruction(&Instruction::LocalGet(1));
    function.instruction(&Instruction::I32Const(source.len().try_into()?));
    for _ in 0..4 {
        function.instruction(&Instruction::I32Const(0));
    }
    function.instruction(&Instruction::Call(0));
    function.instruction(&Instruction::LocalTee(2));
    function.instruction(&Instruction::I32Load8U(MemArg {
        offset: 0,
        align: 0,
        memory_index: 0,
    }));
    function.instruction(&Instruction::If(wasm_encoder::BlockType::Empty));
    function.instruction(&Instruction::Unreachable);
    function.instruction(&Instruction::End);
    function.instruction(&Instruction::LocalGet(2));
    function.instruction(&Instruction::Call(2));
    // Canonical lifted string parameters transfer allocation ownership to init.
    // QuickJS's Rust bindings free them; only the passive copies remain ours.
    function.instruction(&Instruction::DataDrop(0));
    function.instruction(&Instruction::DataDrop(1));
    function.instruction(&Instruction::I32Const(2));
    function.instruction(&Instruction::GlobalSet(0));
    function.instruction(&Instruction::End);
    let mut code = CodeSection::new();
    code.function(&function);
    module.section(&code);
    let mut data = DataSection::new();
    data.passive(shim.bytes());
    data.passive(source.bytes());
    module.section(&data);
    Ok(module)
}

fn deferred_forwarder(signature: &wasmparser::FuncType) -> Result<Module> {
    let mut convert = RoundtripReencoder;
    let params = signature
        .params()
        .iter()
        .map(|ty| convert.val_type(*ty))
        .collect::<Result<Vec<_>, _>>()?;
    let results = signature
        .results()
        .iter()
        .map(|ty| convert.val_type(*ty))
        .collect::<Result<Vec<_>, _>>()?;
    let mut module = Module::new();
    let mut types = TypeSection::new();
    types.ty().function([], []);
    types.ty().function(params.iter().copied(), results);
    module.section(&types);
    let mut imports = ImportSection::new();
    imports.import("env", "ensure-init", EntityType::Function(0));
    imports.import("env", "target", EntityType::Function(1));
    module.section(&imports);
    let mut functions = FunctionSection::new();
    functions.function(1);
    module.section(&functions);
    let mut exports = ExportSection::new();
    exports.export("call", ExportKind::Func, 2);
    module.section(&exports);
    let mut function = Function::new([]);
    function.instruction(&Instruction::Call(0));
    for index in 0..params.len() {
        function.instruction(&Instruction::LocalGet(index as u32));
    }
    function.instruction(&Instruction::Call(1));
    function.instruction(&Instruction::End);
    let mut code = CodeSection::new();
    code.function(&function);
    module.section(&code);
    Ok(module)
}

/// The bindings ctor calls into QuickJS, which can enter canonical WASI imports.
/// Keep it callable but exclude it from the linker's startup constructor list.
pub fn defer_bindings_constructor(bytes: &[u8]) -> Result<Vec<u8>> {
    let mut module = Module::new();
    let mut found = false;
    for payload in Parser::new(0).parse_all(bytes) {
        let payload = payload?;
        if let Payload::ExportSection(reader) = payload {
            let mut exports = ExportSection::new();
            for export in reader {
                let export = export?;
                let name = if export.name == "__wasm_call_ctors" {
                    found = true;
                    "wisp-initialize-metadata"
                } else {
                    export.name
                };
                exports.export(
                    name,
                    RoundtripReencoder.export_kind(export.kind)?,
                    export.index,
                );
            }
            module.section(&exports);
        } else if let Some((id, range)) = payload.as_section() {
            module.section(&RawSection {
                id,
                data: &bytes[range.start as usize..range.end as usize],
            });
        }
    }
    if !found {
        bail!("bindings missing metadata constructor");
    }
    Ok(module.finish())
}
