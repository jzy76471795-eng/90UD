// Package launcherbundle describes the bounded, checksummed executable overlays
// shared by the GM packager and the native portable launcher host.
package launcherbundle

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"io"
	"os"
)

const FooterSize = 72
const ConfigMagic = "PY90_LAUNCHER_CONFIG_V2"
const HostMagic = "PY90_MANAGED_HOST_V1"
const MaxConfig = 256 << 10

func Footer(length int64, sum [32]byte, magic string) []byte {
	b := make([]byte, FooterSize)
	binary.LittleEndian.PutUint64(b, uint64(length))
	copy(b[8:40], sum[:])
	copy(b[40:], []byte(magic))
	return b
}

func Read(f *os.File, end, maximum int64, magic string) (data []byte, start int64, err error) {
	data, start, _, err = ReadWithDigest(f, end, maximum, magic)
	return
}

// ReadWithDigest returns the digest already computed while authenticating data.
func ReadWithDigest(f *os.File, end, maximum int64, magic string) (data []byte, start int64, digest [32]byte, err error) {
	if end < FooterSize {
		return nil, 0, digest, errors.New("登录器缺少内置配置")
	}
	footer := make([]byte, FooterSize)
	if _, err = f.ReadAt(footer, end-FooterSize); err != nil {
		return
	}
	want := make([]byte, 32)
	copy(want, magic)
	n := binary.LittleEndian.Uint64(footer)
	if !bytes.Equal(footer[40:], want) || n == 0 || n > uint64(maximum) || n > uint64(end-FooterSize) {
		return nil, 0, digest, errors.New("登录器内置数据格式无效")
	}
	start = end - FooterSize - int64(n)
	data = make([]byte, int(n))
	if _, err = f.ReadAt(data, start); err != nil {
		return
	}
	sum := sha256.Sum256(data)
	digest = sum
	if !bytes.Equal(sum[:], footer[8:40]) {
		return nil, 0, digest, errors.New("登录器内置数据校验失败")
	}
	return
}

func Append(f *os.File, data []byte, magic string) error {
	if _, err := f.Write(data); err != nil {
		return err
	}
	_, err := f.Write(Footer(int64(len(data)), sha256.Sum256(data), magic))
	return err
}

func AppendFile(out *os.File, path, magic string) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	hash := sha256.New()
	n, err := io.Copy(io.MultiWriter(out, hash), f)
	if err != nil {
		return err
	}
	var sum [32]byte
	copy(sum[:], hash.Sum(nil))
	_, err = out.Write(Footer(n, sum, magic))
	return err
}
