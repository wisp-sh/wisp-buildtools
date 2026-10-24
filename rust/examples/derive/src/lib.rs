use acceptance_derive::{answer_literal, checked_answer, Answer};

#[derive(Answer)]
#[answer(42)]
struct Marker;

#[checked_answer(42)]
fn attributed_answer() -> u32 {
    answer_literal!()
}

pub fn answer() -> u32 {
    let derived = Marker::generated_answer();
    assert_eq!(derived, attributed_answer());
    derived
}
