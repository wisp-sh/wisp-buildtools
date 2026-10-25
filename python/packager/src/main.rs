//! Package source bytes into the prelinked CPython template entirely inside WASI.
use std::{env, fs, io};

const CORE: &[u8] = b"\0asm\x01\0\0\0";
const COMPONENT: &[u8] = b"\0asm\x0d\0\x01\0";

fn uleb(mut value: usize) -> Vec<u8> {
    let mut bytes = Vec::new();
    loop {
        let byte = (value & 127) as u8;
        value >>= 7;
        bytes.push(byte | if value != 0 { 128 } else { 0 });
        if value == 0 { return bytes; }
    }
}

fn read_uleb(bytes: &[u8], position: &mut usize) -> Result<usize, String> {
    let mut value = 0usize;
    for shift in (0..35).step_by(7) {
        let byte = *bytes.get(*position).ok_or("truncated section length")?;
        *position += 1;
        value |= usize::from(byte & 127).checked_shl(shift).ok_or("section length overflow")?;
        if byte & 128 == 0 { return Ok(value); }
    }
    Err("invalid section length".into())
}

fn section(kind: u8, payload: &[u8]) -> Vec<u8> {
    let mut result = vec![kind];
    result.extend(uleb(payload.len()));
    result.extend(payload);
    result
}

fn name(value: &str) -> Vec<u8> {
    let mut result = uleb(value.len());
    result.extend(value.as_bytes());
    result
}

fn marker() -> Vec<u8> {
    let mut payload = name("wisp.python.source");
    payload.extend(b"v2");
    section(0, &payload)
}

fn source_component(source: &str, entry: &str) -> Result<Vec<u8>, String> {
    let payload = format!("{{\"source\":{},\"entry\":{}}}",
                          serde_json::to_string(source).map_err(|e| e.to_string())?,
                          serde_json::to_string(entry).map_err(|e| e.to_string())?);
    let mut data = Vec::new();
    data.extend(8u32.to_le_bytes());
    data.extend((payload.len() as u32).to_le_bytes());
    data.extend(payload.as_bytes());
    let pages = data.len().div_ceil(65536).max(1);
    let mut core = CORE.to_vec();
    core.extend(section(1, &[1, 0x60, 0, 1, 0x7f]));
    core.extend(section(3, &[1, 0]));
    let mut memory = vec![1, 0];
    memory.extend(uleb(pages));
    core.extend(section(5, &memory));
    let mut exports = vec![2];
    exports.extend(name("memory"));
    exports.extend([2, 0]);
    exports.extend(name("get"));
    exports.extend([0, 0]);
    core.extend(section(7, &exports));
    core.extend(section(10, &[1, 4, 0, 0x41, 0, 0x0b]));
    let mut segment = vec![1, 0, 0x41, 0, 0x0b];
    segment.extend(uleb(data.len()));
    segment.extend(data);
    core.extend(section(11, &segment));
    let mut result = COMPONENT.to_vec();
    result.extend(marker());
    result.extend(section(1, &core));
    result.extend(section(2, &[1, 0, 0, 0]));
    result.extend(section(7, &[1, 0x40, 0, 0, 0x73]));
    let mut aliases = vec![2, 0, 0, 1, 0];
    aliases.extend(name("get"));
    aliases.extend([0, 2, 1, 0]);
    aliases.extend(name("memory"));
    result.extend(section(6, &aliases));
    result.extend(section(8, &[1, 0, 0, 0, 1, 3, 0, 0]));
    let mut instance = vec![1, 1, 1, 0];
    instance.extend(name("get"));
    instance.extend([1, 0]);
    result.extend(section(5, &instance));
    let mut export = vec![1, 0];
    export.extend(name("wisp:python/source@0.1.0"));
    export.extend([5, 0, 0]);
    result.extend(section(11, &export));
    Ok(result)
}

fn replace(component: &[u8], provider: &[u8], count: &mut usize, depth: usize) -> Result<Vec<u8>, String> {
    if !component.starts_with(COMPONENT) { return Err("invalid Component template".into()); }
    if depth > 64 { return Err("Component template nesting exceeds 64".into()); }
    let mut expected = COMPONENT.to_vec();
    expected.extend(marker());
    let mut result = COMPONENT.to_vec();
    let mut position = 8;
    while position < component.len() {
        let kind = component[position];
        position += 1;
        let length = read_uleb(component, &mut position)?;
        let end = position.checked_add(length).ok_or("section length overflow")?;
        let payload = component.get(position..end).ok_or("truncated Component section")?;
        if kind == 4 {
            if payload.starts_with(&expected) {
                result.extend(section(kind, provider));
                *count += 1;
            } else {
                result.extend(section(kind, &replace(payload, provider, count, depth + 1)?));
            }
        } else {
            result.extend(section(kind, payload));
        }
        position = end;
    }
    Ok(result)
}

fn main() -> Result<(), io::Error> {
    let arguments: Vec<_> = env::args().skip(1).collect();
    let run = || -> Result<(), String> {
        if arguments.len() != 4 { return Err("usage: python-packager TEMPLATE SOURCE ENTRY OUTPUT".into()); }
        let source = fs::read(&arguments[1]).map_err(|e| e.to_string())?;
        if source.len() > 32 * 1024 * 1024 { return Err("Python guest program exceeds 32 MiB".into()); }
        let source = std::str::from_utf8(&source).map_err(|e| e.to_string())?;
        let provider = source_component(source, &arguments[2])?;
        let template = fs::read(&arguments[0]).map_err(|e| e.to_string())?;
        let mut count = 0;
        let result = replace(&template, &provider, &mut count, 0)?;
        if count != 1 { return Err(format!("expected one source Component, found {count}")); }
        if result.len() > 64 * 1024 * 1024 { return Err("Python Component exceeds 64 MiB".into()); }
        fs::write(&arguments[3], result).map_err(|e| e.to_string())
    };
    run().map_err(|error| io::Error::new(io::ErrorKind::Other, error))
}
