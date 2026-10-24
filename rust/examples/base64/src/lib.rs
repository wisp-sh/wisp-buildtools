use base64::{engine::general_purpose::STANDARD, Engine};

pub fn roundtrip(value: u32) -> u32 {
    let text = value.to_string();
    let encoded = STANDARD.encode(text.as_bytes());
    let decoded = STANDARD.decode(encoded).expect("valid base64");
    String::from_utf8(decoded)
        .expect("UTF-8 from integer")
        .parse()
        .expect("integer round trip")
}
