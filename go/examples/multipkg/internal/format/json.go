package format

import (
    "bytes"
    "encoding/json"
    "project/internal/shared"
)

func JSON(value string) string {
    encoded, err := json.Marshal(map[string]string{"message": shared.Normalize(value)})
    if err != nil { panic(err) }
    return string(bytes.TrimSpace(encoded))
}
