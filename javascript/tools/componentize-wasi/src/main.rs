//! WIT binding generation and Component linking, hosted entirely in WASIp1.
//! Runtime source initialization is supplied by the embedded QuickJS runtime.
mod codegen;
mod initialize;
mod stubwasi;

use anyhow::{Context, Result, bail};
use std::path::PathBuf;
use wasi_preview1_component_adapter_provider::WASI_SNAPSHOT_PREVIEW1_REACTOR_ADAPTER;
use wit_parser::Resolve;

fn main() {
    if let Err(error) = run() {
        eprintln!("wisp-javascript-componentize: {error:#}");
        std::process::exit(1);
    }
}

fn run() -> Result<()> {
    let mut wit = None;
    let mut world = None;
    let mut runtime = None;
    let mut output = None;
    let mut shim_output = None;
    let mut source = None;
    let mut validation_output = None;
    let mut stub_wasi = false;
    let mut keep_custom_sections = false;
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--wit" => wit = Some(PathBuf::from(args.next().context("--wit needs a path")?)),
            "--world" => world = Some(args.next().context("--world needs a name")?),
            "--runtime" => {
                runtime = Some(PathBuf::from(
                    args.next().context("--runtime needs a path")?,
                ))
            }
            "--output" | "-o" => {
                output = Some(PathBuf::from(args.next().context("--output needs a path")?))
            }
            "--shim-output" => {
                shim_output = Some(PathBuf::from(
                    args.next().context("--shim-output needs a path")?,
                ))
            }
            "--source" => {
                source = Some(PathBuf::from(args.next().context("--source needs a path")?))
            }
            "--validation-output" => {
                validation_output = Some(PathBuf::from(
                    args.next().context("--validation-output needs a path")?,
                ))
            }
            "--stub-wasi" => stub_wasi = true,
            "--keep-custom-sections" => keep_custom_sections = true,
            "--help" | "-h" => {
                println!(
                    "Usage: wisp-javascript-componentize --wit PATH --runtime PATH --source PATH --output PATH [--world NAME] [--stub-wasi] [--validation-output PATH]"
                );
                return Ok(());
            }
            _ => bail!("unknown argument: {arg}"),
        }
    }
    let wit = wit.context("--wit is required")?;
    let runtime = runtime.context("--runtime is required")?;
    let output = output.context("--output is required")?;
    let source = source.context("--source is required")?;
    let source = std::fs::read_to_string(&source).context("reading bundled JavaScript source")?;
    let runtime_bytes =
        std::fs::read(&runtime).with_context(|| format!("reading {}", runtime.display()))?;
    let mut resolve = Resolve::default();
    let (pkg, _) = resolve.push_path(&wit).context("parsing WIT")?;
    let world = resolve
        .select_world(&[pkg], world.as_deref())
        .context("selecting WIT world")?;
    let (mut bindings, metadata) = wit_dylib::create_with_metadata(&resolve, world, None);
    let shim = codegen::generate_shim(&resolve, &metadata);
    bindings = initialize::defer_bindings_constructor(&bindings)?;
    wit_component::embed_component_metadata(
        &mut bindings,
        &resolve,
        world,
        wit_component::StringEncoding::UTF8,
    )?;
    let mut linker = wit_component::Linker::default();
    linker.encoder().validate(true);
    linker.library("componentize_qjs_runtime.wasm", &runtime_bytes, false)?;
    linker.library("wit-dylib.wasm", &bindings, false)?;
    linker.encoder().adapter(
        "wasi_snapshot_preview1",
        WASI_SNAPSHOT_PREVIEW1_REACTOR_ADAPTER,
    )?;
    let mut component = linker
        .encode()
        .context("linking QuickJS and WIT bindings")?;
    // The compiler invokes a private validation export to catch source errors.
    // Published components initialize under their first ordinary export call.
    if let Some(validation_output) = validation_output {
        let validation =
            initialize::initialize_deferred_component(&component, &shim, &source, true)?;
        let mut validation = stubwasi::stub_internal_imports(&validation)?;
        if stub_wasi {
            validation = stubwasi::stub_wasi_imports(&validation)?;
        }
        validation = stubwasi::stub_authored_imports(&validation)?;
        std::fs::write(validation_output, validation)
            .context("writing initialization validator")?;
    }
    component = initialize::initialize_deferred_component(&component, &shim, &source, false)?;
    component = stubwasi::stub_internal_imports(&component)?;
    if stub_wasi {
        component = stubwasi::stub_wasi_imports(&component)?;
    }
    if !keep_custom_sections {
        component = initialize::strip_custom_sections(&component)?;
    }
    if let Some(shim_output) = shim_output {
        std::fs::write(&shim_output, &shim).context("writing generated JavaScript shim")?;
    }
    std::fs::write(&output, component).context("writing linked Component")?;
    Ok(())
}
