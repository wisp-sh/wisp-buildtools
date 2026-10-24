package hash

import (
    "crypto/sha256"
    "encoding/hex"
    "project/internal/shared"
)

func Sum(value string) string {
    digest := sha256.Sum256([]byte(shared.Normalize(value)))
    return hex.EncodeToString(digest[:])
}
