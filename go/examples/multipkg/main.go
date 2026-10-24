package main

import (
    "fmt"
    "project/internal/format"
    "project/internal/hash"
)

func main() {
    value := message()
    fmt.Println(format.JSON(value))
    fmt.Println(hash.Sum(value))
}
