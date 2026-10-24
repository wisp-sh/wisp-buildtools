fn main() {
    assert!(!acceptance_scalar::negate(true));
    assert_eq!(
        acceptance_scalar::widen(9_007_199_254_740_993),
        9_007_199_254_740_994
    );
    println!("{}", acceptance_scalar::add(20, 22));
}
