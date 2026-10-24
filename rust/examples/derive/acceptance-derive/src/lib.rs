use proc_macro::{Delimiter, TokenStream, TokenTree};

#[proc_macro_derive(Answer, attributes(answer))]
pub fn answer(input: TokenStream) -> TokenStream {
    let mut value = None;
    for token in input.clone() {
        if let TokenTree::Group(attribute) = token {
            if attribute.delimiter() != Delimiter::Bracket {
                continue;
            }
            let mut contents = attribute.stream().into_iter();
            if matches!(contents.next(), Some(TokenTree::Ident(name)) if name.to_string() == "answer")
            {
                let arguments = match contents.next() {
                    Some(TokenTree::Group(arguments))
                        if arguments.delimiter() == Delimiter::Parenthesis =>
                    {
                        arguments
                    }
                    _ => panic!("Answer requires #[answer(integer)]"),
                };
                value = Some(
                    arguments
                        .stream()
                        .to_string()
                        .parse::<u32>()
                        .expect("integer helper attribute"),
                );
            }
        }
    }
    let value = value.expect("Answer requires #[answer(integer)]");
    let mut tokens = input.into_iter();
    while let Some(token) = tokens.next() {
        if let TokenTree::Ident(ident) = token {
            if ident.to_string() == "struct" {
                let name = match tokens.next() {
                    Some(TokenTree::Ident(name)) => name,
                    _ => panic!("Answer requires a struct name"),
                };
                return format!("impl {name} {{ fn generated_answer() -> u32 {{ {value} }} }}")
                    .parse()
                    .expect("generated implementation");
            }
        }
    }
    panic!("Answer requires a struct");
}

#[proc_macro]
pub fn answer_literal(input: TokenStream) -> TokenStream {
    assert!(input.is_empty(), "answer_literal accepts no arguments");
    "42u32".parse().expect("integer expression")
}

#[proc_macro_attribute]
pub fn checked_answer(attribute: TokenStream, item: TokenStream) -> TokenStream {
    assert_eq!(attribute.to_string(), "42", "checked_answer expects 42");
    item
}
