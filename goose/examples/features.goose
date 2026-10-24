import helpers;
import math;
var count: i32 = 0;
fn main() { count = count + 1; }
export fn initialized() -> i32 { count }
export fn doubled(x: i32) -> i32 { twice(x) }
export fn sum(n: i32) -> i32 {
    var result: i32 = 0;
    for i in 0..n { result = result + i; }
    result
}
export fn quotient(a: i32, b: i32) -> i32 { a / b }

export fn root(x: f64) -> f64 { sqrt(x) }
