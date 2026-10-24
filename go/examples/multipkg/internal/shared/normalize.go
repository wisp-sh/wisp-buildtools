package shared

import "strings"

func Normalize(value string) string { return strings.ToLower(strings.TrimSpace(value)) }
