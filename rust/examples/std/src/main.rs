fn main() {
    assert!(!acceptance_std::negate(true));
    assert_eq!(
        acceptance_std::widen(9_007_199_254_740_993),
        9_007_199_254_740_994
    );
    println!("{}", acceptance_std::score(20, 22));
}
