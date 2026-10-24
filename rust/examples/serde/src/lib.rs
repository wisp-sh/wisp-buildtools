use serde::{Deserialize, Serialize};

#[derive(Debug, PartialEq, Serialize, Deserialize)]
struct Entry {
    name: String,
    value: u32,
}

#[derive(Debug, PartialEq, Serialize, Deserialize)]
struct Score {
    entries: Vec<Entry>,
    note: Option<String>,
}

#[derive(Debug, PartialEq, Serialize, Deserialize)]
struct Envelope<'a, T> {
    #[serde(rename = "display_name", borrow)]
    label: &'a str,
    payload: T,
}

pub fn roundtrip_score(a: u32, b: u32) -> u32 {
    let envelope = Envelope {
        label: "borrowed",
        payload: vec![a, b],
    };
    let borrowed_json = serde_json::to_string(&envelope).expect("serialize generic borrowed value");
    assert!(borrowed_json.contains("\"display_name\""));
    assert!(!borrowed_json.contains("\"label\""));
    let borrowed: Envelope<'_, Vec<u32>> =
        serde_json::from_str(&borrowed_json).expect("deserialize generic borrowed value");
    assert_eq!(envelope, borrowed);
    assert_eq!(borrowed.payload.into_iter().sum::<u32>(), a + b);
    let score = Score {
        entries: vec![
            Entry {
                name: "first".to_owned(),
                value: a,
            },
            Entry {
                name: "second".to_owned(),
                value: b,
            },
        ],
        note: Some("compiled inside Wasm".to_owned()),
    };
    let json = serde_json::to_string(&score).expect("serialize score");
    let decoded: Score = serde_json::from_str(&json).expect("deserialize score");
    assert_eq!(score, decoded);
    decoded.entries.into_iter().map(|entry| entry.value).sum()
}
