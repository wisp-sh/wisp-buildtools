import { triangular } from './math';

export function add(a: i32, b: i32): i32 {
  const values = new Array<i32>(2);
  values[0] = a;
  values[1] = b;
  let result: i32 = 0;
  for (let i = 0; i < values.length; ++i) result += values[i];
  return result;
}

export function series(n: i32): i32 {
  return triangular(n);
}

export function twice(x: f64): f64 {
  return x * 2.0;
}

export function checked(n: i32): i32 {
  assert(n >= 0);
  return n;
}
