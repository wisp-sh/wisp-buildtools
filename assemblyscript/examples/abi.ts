import { scale } from './host';
import { square } from './support';

function increment(value: i32): i32 { return value + 1; }
const operations: ((value: i32) => i32)[] = [square, increment];

export function dispatch(operation: i32, value: i32): i32 {
  return scale(operations[operation](value));
}

export function large(value: i64): i64 { return value + 4294967296; }
export function unicode(): i32 { return "🦎".length; }
