package artifacts

import (
	"crypto/sha256"
	"encoding/hex"
)

const MaxBinary = 64 << 20

func SHA(raw []byte) string { sum := sha256.Sum256(raw); return hex.EncodeToString(sum[:]) }
