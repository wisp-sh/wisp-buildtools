package main

import arithmetic "example.org/rust-arithmetic"

func DoubledSum(a, b uint32) uint32 {
	return arithmetic.Add(a, b) * 2
}

func main() {}
