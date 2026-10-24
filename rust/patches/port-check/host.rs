#![feature(proc_macro_internals)]
#![allow(dead_code, unused_variables, internal_features)]
extern crate wisp_proc_macro_port as proc_macro;
extern crate self as rustc_data_structures;
pub mod sync { pub struct IntoDynSyncSend<T>(pub T); }
#[path = "@SOURCE@/compiler/rustc_metadata/src/wasm_proc_macro.rs"]
mod wasm_proc_macro;
use proc_macro::bridge::*;
use std::ops::{Range,Bound};
struct TestServer;
impl server::Types for TestServer {
 type FreeFunctions=(); type TokenStream=u32; type SourceFile=String; type Span=u32; type Symbol=String;
}
impl server::Server for TestServer {
 fn globals(&mut self)->ExpnGlobals<u32>{ ExpnGlobals{def_site:1,call_site:2,mixed_site:3} }
 fn intern_symbol(ident:&str)->String{ident.into()}
 fn with_symbol_string(symbol:&String,f:impl FnOnce(&str)){ f(symbol) }
}
impl server::FreeFunctions for TestServer {
fn injected_env_var(&mut self, var: &str) -> Option<String> { unimplemented!("FreeFunctions::injected_env_var") }
fn track_env_var(&mut self, var: &str, value: Option<&str>) { unimplemented!("FreeFunctions::track_env_var") }
fn track_path(&mut self, path: &str) { unimplemented!("FreeFunctions::track_path") }
fn literal_from_str(&mut self, s: &str) -> Result<Literal<Self::Span, Self::Symbol>, ()> { unimplemented!("FreeFunctions::literal_from_str") }
fn emit_diagnostic(&mut self, diagnostic: Diagnostic<Self::Span>) { unimplemented!("FreeFunctions::emit_diagnostic") }
}
impl server::TokenStream for TestServer {
fn is_empty(&mut self, self_: &Self::TokenStream) -> bool { *self_ == 0 }
fn expand_expr(&mut self, self_: &Self::TokenStream) -> Result<Self::TokenStream, ()> { unimplemented!("TokenStream::expand_expr") }
fn from_str(&mut self, src: &str) -> Self::TokenStream { unimplemented!("TokenStream::from_str") }
fn to_string(&mut self, self_: &Self::TokenStream) -> String { format!("handle-{}", self_) }
fn from_token_tree(&mut self, tree: TokenTree<Self::TokenStream, Self::Span, Self::Symbol>,) -> Self::TokenStream { unimplemented!("TokenStream::from_token_tree") }
fn concat_trees(&mut self, base: Option<Self::TokenStream>, trees: Vec<TokenTree<Self::TokenStream, Self::Span, Self::Symbol>>,) -> Self::TokenStream { unimplemented!("TokenStream::concat_trees") }
fn concat_streams(&mut self, base: Option<Self::TokenStream>, streams: Vec<Self::TokenStream>,) -> Self::TokenStream { unimplemented!("TokenStream::concat_streams") }
fn into_trees(&mut self, self_: Self::TokenStream) -> Vec<TokenTree<Self::TokenStream, Self::Span, Self::Symbol>> { unimplemented!("TokenStream::into_trees") }
}
impl server::SourceFile for TestServer {
fn eq(&mut self, self_: &Self::SourceFile, other: &Self::SourceFile) -> bool { unimplemented!("SourceFile::eq") }
fn path(&mut self, self_: &Self::SourceFile) -> String { unimplemented!("SourceFile::path") }
fn is_real(&mut self, self_: &Self::SourceFile) -> bool { unimplemented!("SourceFile::is_real") }
}
impl server::Span for TestServer {
fn debug(&mut self, self_: Self::Span) -> String { unimplemented!("Span::debug") }
fn source_file(&mut self, self_: Self::Span) -> Self::SourceFile { unimplemented!("Span::source_file") }
fn parent(&mut self, self_: Self::Span) -> Option<Self::Span> { unimplemented!("Span::parent") }
fn source(&mut self, self_: Self::Span) -> Self::Span { unimplemented!("Span::source") }
fn byte_range(&mut self, self_: Self::Span) -> Range<usize> { unimplemented!("Span::byte_range") }
fn start(&mut self, self_: Self::Span) -> Self::Span { unimplemented!("Span::start") }
fn end(&mut self, self_: Self::Span) -> Self::Span { unimplemented!("Span::end") }
fn line(&mut self, self_: Self::Span) -> usize { unimplemented!("Span::line") }
fn column(&mut self, self_: Self::Span) -> usize { unimplemented!("Span::column") }
fn join(&mut self, self_: Self::Span, other: Self::Span) -> Option<Self::Span> { unimplemented!("Span::join") }
fn subspan(&mut self, self_: Self::Span, start: Bound<usize>, end: Bound<usize>) -> Option<Self::Span> { unimplemented!("Span::subspan") }
fn resolved_at(&mut self, self_: Self::Span, at: Self::Span) -> Self::Span { unimplemented!("Span::resolved_at") }
fn source_text(&mut self, self_: Self::Span) -> Option<String> { Some(format!("span-{}", self_)) }
fn save_span(&mut self, self_: Self::Span) -> usize { unimplemented!("Span::save_span") }
fn recover_proc_macro_span(&mut self, id: usize) -> Self::Span { unimplemented!("Span::recover_proc_macro_span") }
}
impl server::Symbol for TestServer {
fn normalize_and_validate_ident(&mut self, string: &str) -> Result<Self::Symbol, ()> { unimplemented!("Symbol::normalize_and_validate_ident") }
}

fn main() {
 let path=std::env::args().nth(1).expect("macro module");
 let mut macros=wasm_proc_macro::load(std::path::Path::new(&path),"__rustc_proc_macro_decls_wisp_port__").unwrap();
 assert_eq!(macros.len(),3);
 for item in macros.drain(..) {
  match item.0 {
   server::DynProcMacro::Bang{name,client}=>{
    assert_eq!(name,"identity");
    let output=client.run(&server::SameThread,TestServer,42,false).unwrap_or_else(|e|panic!("{}",e.as_str().unwrap_or("macro error")));
    assert_eq!(output,42);
   },
   server::DynProcMacro::Attr{name,client}=>{
    assert_eq!(name,"identity_attr");
    let output=client.run(&server::SameThread,TestServer,7,42,false).unwrap_or_else(|e|panic!("{}",e.as_str().unwrap_or("macro error")));
    assert_eq!(output,42);
   },
   server::DynProcMacro::CustomDerive{trait_name,attributes,client}=>{
    assert_eq!(trait_name,"Identity");assert_eq!(attributes,["identity_helper"]);
    let output=client.run(&server::SameThread,TestServer,42,false).unwrap_or_else(|e|panic!("{}",e.as_str().unwrap_or("macro error")));
    assert_eq!(output,0);
   },
  }
 }
 println!("identity, attribute, derive helpers, token and span RPC passed");
}
