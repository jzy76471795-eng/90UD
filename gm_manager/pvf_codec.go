package main

import (
	"bytes"
	"compress/zlib"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"hash"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"time"
)

const maxPVFInflatedSection = 512 * 1024 * 1024

type PVFProgress struct {
	Phase       string  `json:"phase"`
	Progress    float64 `json:"progress"`
	GroupsDone  int     `json:"groups_done"`
	GroupsTotal int     `json:"groups_total"`
	BytesDone   int64   `json:"bytes_done"`
	BytesTotal  int64   `json:"bytes_total"`
}
type PVFLayoutSummary struct {
	TreeOffset  int64 `json:"tree_offset"`
	HashOffset  int64 `json:"hash_offset"`
	NameOffset  int64 `json:"name_offset"`
	GroupOffset int64 `json:"group_offset"`
	BodyOffset  int64 `json:"body_offset"`
	EndOffset   int64 `json:"end_offset"`
}
type PVFResult struct {
	NewKeySet                     string            `json:"new_keyset"`
	OutputPath                    string            `json:"output_path"`
	OutputSHA256                  string            `json:"output_sha256"`
	SourceSHA256                  string            `json:"source_sha256"`
	SourceProfile                 string            `json:"source_profile"`
	OutputProfile                 string            `json:"output_profile"`
	SourceHeaderPadding           uint32            `json:"source_header_padding"`
	Size                          int64             `json:"size"`
	Files                         int               `json:"files"`
	Groups                        int               `json:"groups"`
	Verified                      bool              `json:"verified"`
	GuardNormalized               bool              `json:"guard_normalized"`
	HeaderNormalized              bool              `json:"header_normalized"`
	DecompressedContentEquivalent bool              `json:"decompressed_content_equivalent"`
	CompressedContentSHA256       string            `json:"compressed_content_sha256"`
	DecompressedContentSHA256     string            `json:"decompressed_content_sha256"`
	SectionSHA256                 map[string]string `json:"section_sha256"`
	Layout                        PVFLayoutSummary  `json:"layout"`
	ElapsedSeconds                float64           `json:"elapsed_seconds"`
}
type PVFCodecError struct {
	Code    string
	Message string
}

func (e *PVFCodecError) Error() string      { return e.Message + " (" + e.Code + ")" }
func codecError(code, message string) error { return &PVFCodecError{Code: code, Message: message} }
func invalidPVF() error {
	return codecError("corrupt_pvf", "PVF 结构、压缩数据或源密钥校验失败")
}
func pvfContext(ctx context.Context) error {
	if err := ctx.Err(); err != nil {
		return fmt.Errorf("PVF 操作已取消，未提交产物: %w", err)
	}
	return nil
}
func emitPVF(ctx context.Context, cb func(PVFProgress), p PVFProgress) (err error) {
	if err = pvfContext(ctx); err != nil {
		return err
	}
	if cb != nil {
		defer func() {
			if recover() != nil {
				err = codecError("progress_callback_failed", "PVF 进度回调失败，未提交产物")
			}
		}()
		cb(p)
	}
	return pvfContext(ctx)
}

func throttlePVFProgress(callback func(PVFProgress)) func(PVFProgress) {
	if callback == nil {
		return nil
	}
	phase := ""
	last := time.Time{}
	return func(p PVFProgress) {
		now := time.Now()
		if p.Phase == phase && p.GroupsDone < p.GroupsTotal && now.Sub(last) < 100*time.Millisecond {
			return
		}
		phase, last = p.Phase, now
		callback(p)
	}
}
func keySeeds(keys map[string][]byte) (map[string]uint32, error) {
	out := map[string]uint32{}
	for _, name := range keyOrder {
		key := keys[name]
		if len(key) != 8 {
			return nil, codecError("invalid_keyset", "单段密钥必须为 8 字节")
		}
		out[name] = seedFromKey(key)
	}
	return out, nil
}
func narrowSeed(s string) uint32 {
	b := []byte(s)
	return uint32(uint64(b[0])*1988257537 + 449*(uint64(b[3])+449*(uint64(b[2])+449*uint64(b[1]))))
}
func utilitySeeds() map[string]uint32 {
	return map[string]uint32{"head": 0x76c5d612, "hash": narrowSeed("HASH"), "grpi": narrowSeed("GRPI"), "body": narrowSeed("BodY"), "stra": narrowSeed("sTrA"), "strb": narrowSeed("sTrW")}
}
func cryptSeed(ctx context.Context, data []byte, seed, inc uint32) ([]byte, error) {
	out := append([]byte(nil), data...)
	for i := 0; i < len(out); i += 4 {
		if i&16383 == 0 {
			if err := pvfContext(ctx); err != nil {
				return nil, err
			}
		}
		first := 214013*seed + inc
		seed = 214013*first + inc
		key := (first & 0xffff0000) | (seed >> 16)
		for j := 0; j < 4 && i+j < len(out); j++ {
			out[i+j] ^= byte(key >> uint(j*8))
		}
	}
	return out, nil
}
func leU32(b []byte) uint32     { return binary.LittleEndian.Uint32(b) }
func putU32(b []byte, v uint32) { binary.LittleEndian.PutUint32(b, v) }
func digestPVF(b []byte) string { s := sha256.Sum256(b); return hex.EncodeToString(s[:]) }

type pvfGroup struct{ end, original uint32 }
type pvfLayout struct {
	data                                      []byte
	seeds                                     map[string]uint32
	f                                         [12]uint32
	profile                                   string
	guarded                                   bool
	nFiles, nBlocks                           uint32
	treeOff, st1Off, st2Off, pairOff, bodyOff int
	st1Plain, st2Hdr, segAZ, segBZ, pairPlain []byte
	compA, decompA, compB, decompB            uint32
	groups                                    []pvfGroup
	sections                                  map[string]string
}

func (l *pvfLayout) summary() PVFLayoutSummary {
	return PVFLayoutSummary{48, int64(l.st1Off), int64(l.st2Off), int64(l.pairOff), int64(l.bodyOff), int64(len(l.data))}
}
func parsePVF(data []byte, keys map[string][]byte) (*pvfLayout, error) {
	seeds, err := keySeeds(keys)
	if err != nil {
		return nil, err
	}
	return parsePVFProfile(context.Background(), data, seeds, false, "keyset96")
}
func detectPVF(ctx context.Context, data []byte, keyset string) (*pvfLayout, error) {
	if keyset != "" {
		keys, err := parseKeySet(keyset)
		if err != nil {
			return nil, err
		}
		seeds, _ := keySeeds(keys)
		return parsePVFProfile(ctx, data, seeds, false, "keyset96")
	}
	keys, err := standardOldKeys()
	if err != nil {
		return nil, err
	}
	seeds, _ := keySeeds(keys)
	for _, candidate := range []struct {
		seeds   map[string]uint32
		guard   bool
		profile string
	}{{seeds, false, "standard-us-zimo"}, {utilitySeeds(), true, "utility-guarded"}} {
		if len(data) < 48 {
			return nil, invalidPVF()
		}
		header := append([]byte(nil), data[:48]...)
		if candidate.guard {
			for i := 24; i < 28; i++ {
				header[i] ^= 0x55
			}
		}
		plain, err := cryptSeed(ctx, header, candidate.seeds["head"], lcgMain)
		if err != nil {
			return nil, err
		}
		if leU32(plain) == pvfMagic {
			return parsePVFProfile(ctx, data, candidate.seeds, candidate.guard, candidate.profile)
		}
	}
	return nil, codecError("source_key_mismatch", "无法识别 PVF 源密钥或格式，文件也可能已损坏")
}

func parsePVFProfile(ctx context.Context, data []byte, seeds map[string]uint32, guard bool, profile string) (*pvfLayout, error) {
	if len(data) < 48 {
		return nil, invalidPVF()
	}
	header := append([]byte(nil), data[:48]...)
	if guard {
		for i := 24; i < 28; i++ {
			header[i] ^= 0x55
		}
	}
	plain, err := cryptSeed(ctx, header, seeds["head"], lcgMain)
	if err != nil {
		return nil, err
	}
	l := &pvfLayout{data: data, seeds: seeds, guarded: guard, profile: profile, treeOff: 48}
	for i := range l.f {
		l.f[i] = leU32(plain[i*4:])
	}
	if l.f[0] != pvfMagic {
		return nil, codecError("source_key_mismatch", "PVF 源密钥不匹配")
	}
	l.nFiles, l.nBlocks = l.f[6], l.f[9]
	hashSize, nameSize := uint64(l.f[10]), uint64(l.f[11])
	hashOff := 48 + uint64(l.nFiles)*24
	nameOff := hashOff + hashSize
	pairOff := nameOff + nameSize
	bodyOff := pairOff + uint64(l.nBlocks)*8
	for _, value := range []uint64{uint64(l.nFiles), uint64(l.nBlocks), hashSize, nameSize, uint64(l.f[8]), bodyOff} {
		if value >= 0x80000000 {
			return nil, invalidPVF()
		}
	}
	if hashSize < 4 || nameSize < 24 || bodyOff+uint64(l.f[8]) != uint64(len(data)) {
		return nil, invalidPVF()
	}
	l.st1Off, l.st2Off, l.pairOff, l.bodyOff = int(hashOff), int(nameOff), int(pairOff), int(bodyOff)
	l.st1Plain, err = cryptSeed(ctx, data[l.st1Off:l.st2Off], seeds["hash"], lcgMain)
	if err != nil {
		return nil, err
	}
	hashEnd := 4 + uint64(l.nFiles)*8
	if hashEnd > hashSize || leU32(l.st1Plain) != l.nFiles {
		return nil, invalidPVF()
	}
	lookupCount := uint32(0)
	if hashEnd < hashSize {
		if hashEnd+4 > hashSize {
			return nil, invalidPVF()
		}
		lookupCount = leU32(l.st1Plain[int(hashEnd):])
		if hashEnd+4+uint64(lookupCount)*4 != hashSize {
			return nil, invalidPVF()
		}
	}
	l.pairPlain, err = cryptSeed(ctx, data[l.pairOff:l.bodyOff], seeds["grpi"], lcgMain)
	if err != nil {
		return nil, err
	}
	previous := uint32(0)
	for i := uint32(0); i < l.nBlocks; i++ {
		if i&4095 == 0 {
			if err := pvfContext(ctx); err != nil {
				return nil, err
			}
		}
		end, original := leU32(l.pairPlain[int(i)*8:]), leU32(l.pairPlain[int(i)*8+4:])
		if end <= previous || end > l.f[8] || original == 0 || original > maxPVFInflatedSection {
			return nil, invalidPVF()
		}
		l.groups = append(l.groups, pvfGroup{end, original})
		previous = end
	}
	if previous != l.f[8] {
		return nil, invalidPVF()
	}
	names := data[l.st2Off:l.pairOff]
	l.st2Hdr = append([]byte(nil), names[:8]...)
	position := 8
	var compressed, pools [2][]byte
	var originals [2]uint32
	for index, spec := range []struct {
		name string
		mask uint32
	}{{"stra", xorA}, {"strb", xorB}} {
		if position > len(names)-8 {
			return nil, invalidPVF()
		}
		size := leU32(names[position:]) ^ spec.mask
		original := leU32(names[position+4:]) ^ size
		position += 8
		if size == 0 || uint64(position)+uint64(size) > uint64(len(names)) {
			return nil, invalidPVF()
		}
		compressed[index], err = cryptSeed(ctx, names[position:position+int(size)], seeds[spec.name], lcgStr)
		if err != nil {
			return nil, err
		}
		pools[index], err = inflatePVF(ctx, compressed[index], original, true, nil)
		if err != nil {
			return nil, err
		}
		originals[index] = original
		position += int(size)
	}
	if position != len(names) || len(pools[0]) == 0 || pools[0][len(pools[0])-1] != 0 || len(pools[1]) < 2 || len(pools[1])%2 != 0 || pools[1][len(pools[1])-1] != 0 || pools[1][len(pools[1])-2] != 0 {
		return nil, invalidPVF()
	}
	validName := func(offset int32, empty bool) bool {
		if empty && offset == -1 {
			return true
		}
		if offset < 0 {
			return false
		}
		if offset&1 != 0 {
			return uint64(uint32(offset)>>1)*2+1 < uint64(len(pools[1]))
		}
		return uint64(uint32(offset)>>1) < uint64(len(pools[0]))
	}
	for i := uint32(0); i < l.nFiles; i++ {
		if i&4095 == 0 {
			if err := pvfContext(ctx); err != nil {
				return nil, err
			}
		}
		entry := data[48+int(i)*24:]
		name, path := int32(leU32(entry)), int32(leU32(entry[4:]))
		group, offset, size := int32(leU32(entry[8:])), int32(leU32(entry[12:])), int32(leU32(entry[16:]))
		hashEntry := l.st1Plain[4+int(i)*8:]
		if !validName(name, false) || !validName(path, true) || uint32(name) != leU32(hashEntry) || uint32(path) != leU32(hashEntry[4:]) || group < 0 || int64(group) >= int64(len(l.groups)) || offset < 0 || size < 0 || uint64(offset)+uint64(size) > uint64(l.groups[group].original) {
			return nil, invalidPVF()
		}
	}
	for i := uint32(0); i < lookupCount; i++ {
		if i&4095 == 0 {
			if err := pvfContext(ctx); err != nil {
				return nil, err
			}
		}
		if !validName(int32(leU32(l.st1Plain[int(hashEnd)+4+int(i)*4:])), false) {
			return nil, invalidPVF()
		}
	}
	l.segAZ, l.segBZ = compressed[0], compressed[1]
	l.compA, l.compB = uint32(len(compressed[0])), uint32(len(compressed[1]))
	l.decompA, l.decompB = originals[0], originals[1]
	l.sections = map[string]string{"tree": digestPVF(data[48:l.st1Off]), "hash": digestPVF(l.st1Plain), "name_prefix": digestPVF(l.st2Hdr), "name_a_compressed": digestPVF(l.segAZ), "name_b_compressed": digestPVF(l.segBZ), "groups": digestPVF(l.pairPlain)}
	return l, nil
}

type pvfContextReader struct {
	ctx context.Context
	r   io.Reader
}

func (r pvfContextReader) Read(b []byte) (int, error) {
	if err := pvfContext(r.ctx); err != nil {
		return 0, err
	}
	return r.r.Read(b)
}
func inflatePVF(ctx context.Context, compressed []byte, expected uint32, keep bool, destination io.Writer) ([]byte, error) {
	if expected == 0 || expected > maxPVFInflatedSection {
		return nil, codecError("decompression_limit", "PVF 声明的解压大小超过安全限制")
	}
	input := bytes.NewReader(compressed)
	reader, err := zlib.NewReader(input)
	if err != nil {
		return nil, invalidPVF()
	}
	defer reader.Close()
	var buffer bytes.Buffer
	writer := destination
	if writer == nil {
		writer = io.Discard
	}
	if keep {
		writer = &buffer
	}
	count, err := io.CopyBuffer(writer, io.LimitReader(pvfContextReader{ctx, reader}, int64(expected)+1), make([]byte, 64*1024))
	if err != nil {
		if ctx.Err() != nil {
			return nil, pvfContext(ctx)
		}
		return nil, invalidPVF()
	}
	if count != int64(expected) || input.Len() != 0 {
		return nil, invalidPVF()
	}
	if err := pvfContext(ctx); err != nil {
		return nil, err
	}
	return buffer.Bytes(), nil
}
func (l *pvfLayout) blockZContext(ctx context.Context, index int) ([]byte, error) {
	if index < 0 || index >= len(l.groups) {
		return nil, invalidPVF()
	}
	start := uint32(0)
	if index > 0 {
		start = l.groups[index-1].end
	}
	end := l.groups[index].end
	return cryptSeed(ctx, l.data[l.bodyOff+int(start):l.bodyOff+int(end)], l.seeds["body"], lcgMain)
}
func (l *pvfLayout) blockZ(index uint32) ([]byte, error) {
	return l.blockZContext(context.Background(), int(index))
}
func hashLength(h hash.Hash, size uint32) { var b [4]byte; putU32(b[:], size); _, _ = h.Write(b[:]) }
func validatePVFGroups(ctx context.Context, l *pvfLayout, progress func(PVFProgress), phase string, from, to float64) (string, string, error) {
	zippedHash, plainHash := sha256.New(), sha256.New()
	for index, group := range l.groups {
		z, err := l.blockZContext(ctx, index)
		if err != nil {
			return "", "", err
		}
		hashLength(zippedHash, uint32(len(z)))
		_, _ = zippedHash.Write(z)
		hashLength(plainHash, group.original)
		if _, err = inflatePVF(ctx, z, group.original, false, plainHash); err != nil {
			return "", "", err
		}
		if err = emitPVF(ctx, progress, PVFProgress{phase, from + (to-from)*float64(index+1)/float64(len(l.groups)), index + 1, len(l.groups), int64(l.bodyOff) + int64(group.end), int64(len(l.data))}); err != nil {
			return "", "", err
		}
	}
	return hex.EncodeToString(zippedHash.Sum(nil)), hex.EncodeToString(plainHash.Sum(nil)), nil
}

func resultForLayout(l *pvfLayout) *PVFResult {
	return &PVFResult{SourceSHA256: digestPVF(l.data), SourceProfile: l.profile, SourceHeaderPadding: l.f[7], Size: int64(len(l.data)), Files: int(l.nFiles), Groups: int(l.nBlocks), SectionSHA256: l.sections, Layout: l.summary()}
}
func inspectPVFFile(ctx context.Context, path, oldKeySet string) (*PVFResult, error) {
	started := time.Now()
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	layout, err := detectPVF(ctx, data, oldKeySet)
	if err != nil {
		return nil, err
	}
	result := resultForLayout(layout)
	result.CompressedContentSHA256, result.DecompressedContentSHA256, err = validatePVFGroups(ctx, layout, nil, "validate_source", 0, 1)
	if err != nil {
		return nil, err
	}
	result.Verified = true
	result.ElapsedSeconds = time.Since(started).Seconds()
	return result, nil
}

func writePVF(ctx context.Context, writer io.Writer, l *pvfLayout, newSeeds map[string]uint32, progress func(PVFProgress)) error {
	fields := l.f
	fields[7] = uint32(l.bodyOff)
	header := make([]byte, 48)
	for i, value := range fields {
		putU32(header[i*4:], value)
	}
	writeCrypt := func(data []byte, name string, inc uint32) error {
		encrypted, err := cryptSeed(ctx, data, newSeeds[name], inc)
		if err != nil {
			return err
		}
		_, err = writer.Write(encrypted)
		return err
	}
	if err := writeCrypt(header, "head", lcgMain); err != nil {
		return err
	}
	if _, err := writer.Write(l.data[48:l.st1Off]); err != nil {
		return err
	}
	if err := writeCrypt(l.st1Plain, "hash", lcgMain); err != nil {
		return err
	}
	if _, err := writer.Write(l.st2Hdr); err != nil {
		return err
	}
	for _, part := range []struct {
		name           string
		mask, original uint32
		z              []byte
	}{{"stra", xorA, l.decompA, l.segAZ}, {"strb", xorB, l.decompB, l.segBZ}} {
		var segmentHeader [8]byte
		putU32(segmentHeader[:], uint32(len(part.z))^part.mask)
		putU32(segmentHeader[4:], uint32(len(part.z))^part.original)
		if _, err := writer.Write(segmentHeader[:]); err != nil {
			return err
		}
		if err := writeCrypt(part.z, part.name, lcgStr); err != nil {
			return err
		}
	}
	if err := writeCrypt(l.pairPlain, "grpi", lcgMain); err != nil {
		return err
	}
	for index, group := range l.groups {
		z, err := l.blockZContext(ctx, index)
		if err != nil {
			return err
		}
		if err = writeCrypt(z, "body", lcgMain); err != nil {
			return err
		}
		if err = emitPVF(ctx, progress, PVFProgress{"rekey_groups", 0.35 + 0.3*float64(index+1)/float64(len(l.groups)), index + 1, len(l.groups), int64(l.bodyOff) + int64(group.end), int64(len(l.data))}); err != nil {
			return err
		}
	}
	return nil
}

func protectPVFFileDetailed(ctx context.Context, srcPath, dstPath, oldKeySet, newKeySet string, progress func(PVFProgress)) (*PVFResult, error) {
	started := time.Now()
	progress = throttlePVFProgress(progress)
	if ctx == nil {
		return nil, errors.New("PVF context 不能为空")
	}
	if err := pvfContext(ctx); err != nil {
		return nil, err
	}
	if oldKeySet != "" {
		if _, err := parseKeySet(oldKeySet); err != nil {
			return nil, err
		}
	}
	var err error
	if newKeySet == "" {
		newKeySet, err = newRandomKeySet()
		if err != nil {
			return nil, err
		}
	}
	keys, err := parseKeySet(newKeySet)
	if err != nil {
		return nil, err
	}
	newSeeds, _ := keySeeds(keys)
	source, err := filepath.Abs(srcPath)
	if err != nil {
		return nil, err
	}
	destination, err := filepath.Abs(dstPath)
	if err != nil {
		return nil, err
	}
	if _, err := os.Lstat(destination); err == nil {
		return nil, codecError("destination_exists", "PVF 输出已存在，拒绝覆盖")
	} else if !os.IsNotExist(err) {
		return nil, err
	}
	before, err := os.Stat(source)
	if err != nil {
		return nil, err
	}
	if !before.Mode().IsRegular() {
		return nil, codecError("source_not_file", "PVF 源必须是普通文件")
	}
	if before.Size() < 48 || before.Size() >= 0x80000000 {
		return nil, invalidPVF()
	}
	if err := emitPVF(ctx, progress, PVFProgress{Phase: "parse_source", Progress: 0.01, BytesTotal: before.Size()}); err != nil {
		return nil, err
	}
	data, err := os.ReadFile(source)
	if err != nil {
		return nil, err
	}
	layout, err := detectPVF(ctx, data, oldKeySet)
	if err != nil {
		return nil, err
	}
	result := resultForLayout(layout)
	result.CompressedContentSHA256, result.DecompressedContentSHA256, err = validatePVFGroups(ctx, layout, progress, "validate_source", 0.05, 0.3)
	if err != nil {
		return nil, err
	}
	temporary, err := os.CreateTemp(filepath.Dir(destination), ".pvf-*.partial")
	if err != nil {
		return nil, err
	}
	tempPath := temporary.Name()
	defer os.Remove(tempPath)
	if err = temporary.Chmod(0644); err == nil {
		err = writePVF(ctx, temporary, layout, newSeeds, progress)
	}
	if err == nil {
		err = temporary.Sync()
	}
	closeErr := temporary.Close()
	if err != nil {
		return nil, err
	}
	if closeErr != nil {
		return nil, closeErr
	}
	if err = emitPVF(ctx, progress, PVFProgress{Phase: "verify_output", Progress: 0.67, GroupsTotal: len(layout.groups), BytesTotal: int64(len(data))}); err != nil {
		return nil, err
	}
	rewritten, err := os.ReadFile(tempPath)
	if err != nil {
		return nil, err
	}
	checked, err := detectPVF(ctx, rewritten, newKeySet)
	if err != nil {
		return nil, err
	}
	expectedFields := layout.f
	expectedFields[7] = uint32(layout.bodyOff)
	if checked.f != expectedFields || checked.summary() != layout.summary() || !reflect.DeepEqual(checked.sections, layout.sections) {
		return nil, codecError("verification_failed", "PVF 输出区段独立重读不一致，未提交")
	}
	compressedDigest, plainDigest, err := validatePVFGroups(ctx, checked, progress, "verify_output", 0.7, 0.93)
	if err != nil {
		return nil, err
	}
	if compressedDigest != result.CompressedContentSHA256 || plainDigest != result.DecompressedContentSHA256 {
		return nil, codecError("verification_failed", "PVF 输出内容不一致，未提交")
	}
	outputHash := digestPVF(rewritten)
	if err = emitPVF(ctx, progress, PVFProgress{Phase: "commit", Progress: 0.99, GroupsDone: len(layout.groups), GroupsTotal: len(layout.groups), BytesDone: int64(len(data)), BytesTotal: int64(len(data))}); err != nil {
		return nil, err
	}
	after, err := os.Stat(source)
	if err != nil {
		return nil, err
	}
	if !os.SameFile(before, after) || before.Size() != after.Size() || !before.ModTime().Equal(after.ModTime()) {
		return nil, codecError("source_changed", "PVF 源文件在处理中发生变化，未提交")
	}
	currentHash, err := sha256FileContext(ctx, source)
	if err != nil {
		return nil, err
	}
	if currentHash != result.SourceSHA256 {
		return nil, codecError("source_changed", "PVF 源文件在处理中发生变化，未提交")
	}
	// Link is an atomic create-if-absent on Windows/Unix. Never use a replacing rename.
	if err = os.Link(tempPath, destination); err != nil {
		return nil, fmt.Errorf("PVF 原子提交失败，未覆盖任何目标: %w", err)
	}
	result.NewKeySet = hex.EncodeToString(mustKeysetBytes(keys))
	result.OutputPath = destination
	result.OutputSHA256 = outputHash
	result.OutputProfile = "keyset96"
	result.Verified = true
	result.GuardNormalized = layout.guarded
	result.HeaderNormalized = layout.f[7] != uint32(layout.bodyOff)
	result.DecompressedContentEquivalent = true
	result.ElapsedSeconds = time.Since(started).Seconds()
	return result, nil
}
func mustKeysetBytes(keys map[string][]byte) []byte {
	var out []byte
	for _, name := range keyOrder {
		out = append(out, keys[name]...)
	}
	return out
}
func protectPVFFile(src, dst, oldKeySet, newKeySet string) (string, string, error) {
	result, err := protectPVFFileDetailed(context.Background(), src, dst, oldKeySet, newKeySet, nil)
	if err != nil {
		return "", "", err
	}
	return result.NewKeySet, result.OutputSHA256, nil
}
func rekeyPVFFile(src, dst, oldKeySet, newKeySet string) (string, string, error) {
	return protectPVFFile(src, dst, oldKeySet, newKeySet)
}
func rekeyPVF(src []byte, oldKeys, newKeys map[string][]byte, newXorA, newXorB uint32) ([]byte, error) {
	if newXorA != 0 && newXorA != xorA || newXorB != 0 && newXorB != xorB {
		return nil, codecError("unsupported_masks", "仅支持原生 PVF 段头掩码")
	}
	layout, err := parsePVF(src, oldKeys)
	if err != nil {
		return nil, err
	}
	if _, _, err = validatePVFGroups(context.Background(), layout, nil, "validate_source", 0, 1); err != nil {
		return nil, err
	}
	seeds, err := keySeeds(newKeys)
	if err != nil {
		return nil, err
	}
	var out bytes.Buffer
	if err = writePVF(context.Background(), &out, layout, seeds, nil); err != nil {
		return nil, err
	}
	return out.Bytes(), nil
}
func newRandomKeySet() (string, error) {
	raw := make([]byte, 48)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	return hex.EncodeToString(raw), nil
}
func sha256FileContext(ctx context.Context, path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err = io.CopyBuffer(h, pvfContextReader{ctx, f}, make([]byte, 64*1024)); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}
func sha256File(path string) (string, error) { return sha256FileContext(context.Background(), path) }
