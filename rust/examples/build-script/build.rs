use std::{env, fs, path::PathBuf};

fn main() {
    let root = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").expect("manifest directory"));
    let out = PathBuf::from(env::var_os("OUT_DIR").expect("build output directory"));
    let value = fs::read_to_string(root.join("answer.txt")).expect("fixture input");
    let answer: u32 = value.trim().parse().expect("integer fixture input");
    fs::write(
        out.join("generated.rs"),
        format!("const GENERATED_ANSWER: u32 = {answer};\n"),
    )
    .expect("generated Rust source");
    println!("cargo:rerun-if-changed=answer.txt");
    println!("cargo:rustc-env=WISP_ACCEPTANCE_ANSWER={answer}");
    println!("cargo:rustc-check-cfg=cfg(wisp_generated)");
    println!("cargo:rustc-cfg=wisp_generated");
}
