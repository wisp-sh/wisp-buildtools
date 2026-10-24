#[cfg(not(wisp_generated))]
compile_error!("build script must provide wisp_generated");

include!(concat!(env!("OUT_DIR"), "/generated.rs"));

pub fn answer() -> u32 {
    let value: u32 = env!("WISP_ACCEPTANCE_ANSWER")
        .parse()
        .expect("build-script value");
    assert_eq!(value, GENERATED_ANSWER);
    value
}
