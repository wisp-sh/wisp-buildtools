use std::fmt::Display;

fn parse<T: Display>(value: T) -> Result<u32, std::num::ParseIntError> {
    value.to_string().parse()
}

pub fn score(a: u32, b: u32) -> u32 {
    let values = vec![a, b];
    let total: u32 = values.into_iter().sum();
    let text = format!("{}", total);
    parse(text).expect("formatted integer")
}

pub fn add(a: u32, b: u32) -> u32 {
    score(a, b)
}

pub fn negate(value: bool) -> bool {
    !value
}

pub fn widen(value: u64) -> u64 {
    value + 1
}
