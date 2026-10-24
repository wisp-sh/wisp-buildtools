export function triangular(n: i32): i32 {
  let sum: i32 = 0;
  for (let i = 0; i <= n; ++i) sum += i;
  return sum;
}
