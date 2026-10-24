#![feature(proc_macro_internals)]
#![allow(internal_features)]
extern crate wisp_proc_macro_port as proc_macro;
use proc_macro::{Span, TokenStream};
use proc_macro::bridge::client::ProcMacro;

fn identity(input: TokenStream) -> TokenStream {
    assert_eq!(input.to_string(), "handle-42");
    assert_eq!(Span::call_site().source_text().as_deref(), Some("span-2"));
    input
}
fn attribute(arguments: TokenStream, input: TokenStream) -> TokenStream {
    assert_eq!(arguments.to_string(), "handle-7");
    assert_eq!(input.to_string(), "handle-42");
    input
}
fn derive(input: TokenStream) -> TokenStream {
    assert_eq!(input.to_string(), "handle-42");
    TokenStream::new()
}
#[no_mangle]
pub static __rustc_proc_macro_decls_wisp_port__: &[ProcMacro] = &[
    ProcMacro::bang("identity", identity),
    ProcMacro::attr("identity_attr", attribute),
    ProcMacro::custom_derive("Identity", &["identity_helper"], derive),
];
