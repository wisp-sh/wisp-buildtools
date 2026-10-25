//! Remove the build-only validation export inside WASI after guest validation.
use std::{env, fs, io};
const HEADER: &[u8] = b"\0asm\x0d\0\x01\0";
const CONTROL: &[u8] = b"wisp-python-validate";

fn uleb(bytes: &[u8], position: &mut usize) -> Result<usize, String> {
    let mut result = 0usize;
    for shift in (0..35).step_by(7) {
        let value = *bytes.get(*position).ok_or("truncated LEB")?;
        *position += 1;
        result |= usize::from(value & 127)
            .checked_shl(shift)
            .ok_or("LEB overflow")?;
        if value & 128 == 0 {
            return Ok(result);
        }
    }
    Err("invalid LEB".into())
}
fn encode(mut value: usize, out: &mut Vec<u8>) {
    loop {
        let byte = (value & 127) as u8;
        value >>= 7;
        out.push(byte | if value == 0 { 0 } else { 128 });
        if value == 0 {
            break;
        }
    }
}
fn public(bytes: &[u8]) -> Result<Vec<u8>, String> {
    if !bytes.starts_with(HEADER) || bytes.len() > 64 * 1024 * 1024 {
        return Err("invalid Python Component or size limit".into());
    }
    let mut out = HEADER.to_vec();
    let mut position = 8;
    let mut removed = 0;
    while position < bytes.len() {
        let kind = bytes[position];
        position += 1;
        let len = uleb(bytes, &mut position)?;
        let end = position.checked_add(len).ok_or("section overflow")?;
        let payload = bytes.get(position..end).ok_or("truncated section")?;
        let mut rewritten = Vec::new();
        let output = if kind == 11 {
            let mut cursor = 0;
            let count = uleb(payload, &mut cursor)?;
            let mut kept = Vec::new();
            let mut retained = 0;
            for _ in 0..count {
                let start = cursor;
                if payload.get(cursor) != Some(&0) {
                    return Err("qualified export unsupported".into());
                }
                cursor += 1;
                let length = uleb(payload, &mut cursor)?;
                let end = cursor.checked_add(length).ok_or("export overflow")?;
                let name = payload.get(cursor..end).ok_or("truncated name")?;
                cursor = end;
                let export_kind = *payload.get(cursor).ok_or("truncated export")?;
                cursor += 1;
                uleb(payload, &mut cursor)?;
                if payload.get(cursor) != Some(&0) {
                    return Err("explicit export type unsupported".into());
                }
                cursor += 1;
                if name == CONTROL {
                    if export_kind != 1 {
                        return Err("validation export must be a function".into());
                    }
                    removed += 1;
                } else {
                    kept.extend_from_slice(&payload[start..cursor]);
                    retained += 1;
                }
            }
            if cursor != payload.len() {
                return Err("trailing export data".into());
            }
            encode(retained, &mut rewritten);
            rewritten.extend(kept);
            &rewritten
        } else {
            payload
        };
        out.push(kind);
        encode(output.len(), &mut out);
        out.extend_from_slice(output);
        position = end;
    }
    if removed != 1 {
        return Err(format!("expected one validation export, found {removed}"));
    }
    Ok(out)
}
fn main() -> Result<(), io::Error> {
    let args: Vec<_> = env::args().skip(1).collect();
    let run = || -> Result<(), String> {
        if args.len() != 2 {
            return Err("usage: python-public-exports INPUT OUTPUT".into());
        }
        let bytes = fs::read(&args[0]).map_err(|e| e.to_string())?;
        let result = public(&bytes)?;
        fs::write(&args[1], result).map_err(|e| e.to_string())
    };
    run().map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))
}
#[cfg(test)]
mod tests {
    use super::*;
    fn fixture(names: &[&[u8]]) -> Vec<u8> {
        let mut payload = Vec::new();
        encode(names.len(), &mut payload);
        for name in names {
            payload.push(0);
            encode(name.len(), &mut payload);
            payload.extend_from_slice(name);
            payload.extend_from_slice(&[1, 0, 0]);
        }
        let mut bytes = HEADER.to_vec();
        bytes.push(11);
        encode(payload.len(), &mut bytes);
        bytes.extend(payload);
        bytes
    }
    #[test]
    fn removes_only_control_and_preserves_other_exports() {
        assert_eq!(
            public(&fixture(&[b"hello", CONTROL, b"count"])).unwrap(),
            fixture(&[b"hello", b"count"])
        );
        assert!(public(&fixture(&[b"hello"])).is_err());
        assert!(public(&fixture(&[CONTROL, CONTROL])).is_err());
    }
    #[test]
    fn rejects_truncation() {
        let bytes = fixture(&[CONTROL]);
        for end in 0..bytes.len() {
            assert!(public(&bytes[..end]).is_err());
        }
    }
}
