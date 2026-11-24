from __future__ import annotations

import concurrent.futures
import hashlib
import os
import re
import struct
import threading
import zlib
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


HEADER_MAGIC = 0x69706B6E  # "nkpi" little-endian
HEADER_SIZE = 48
FILE_ENTRY_SIZE = 24
GROUP_ENTRY_SIZE = 8
LCG_A = 214013
INC1 = 2531011
INC_NAME = 2531017
UINT32_MASK = 0xFFFFFFFF
GUARD_START = 24
GUARD_END = 28
NAME_UTF8_MAGIC = 0xAA74472E
NAME_UTF16_MAGIC = 0x9A82F037


def _mod_inverse_odd_32(value: int) -> int:
    modulus = 1 << 32
    t, new_t = 0, 1
    r, new_r = modulus, value & UINT32_MASK
    while new_r:
        quotient = r // new_r
        t, new_t = new_t, t - quotient * new_t
        r, new_r = new_r, r - quotient * new_r
    if r != 1:
        raise ValueError(f"0x{value:08x} has no inverse modulo 2**32")
    return t % modulus


LCG_A_INV = _mod_inverse_odd_32(LCG_A)


def _decrypt_lcg_reference(data: bytes, seed: int, increment: int) -> bytes:
    out = bytearray(data)
    state = seed & UINT32_MASK
    for offset in range(0, len(out), 4):
        s1 = (LCG_A * state + increment) & UINT32_MASK
        s2 = (LCG_A * s1 + increment) & UINT32_MASK
        state = s2
        xor_key = (s1 & 0xFFFF0000) | ((s2 >> 16) & 0xFFFF)
        key = xor_key.to_bytes(4, "little")
        for i in range(min(4, len(out) - offset)):
            out[offset + i] ^= key[i]
    return bytes(out)


def decrypt_lcg(data: bytes, seed: int, increment: int) -> bytes:
    """Run the production LCG transform through the fixed native ABI."""
    from pvf.native_loader import require_native_module

    return require_native_module().decrypt_lcg(data, seed, increment)


def recover_seed_from_known_dword(encrypted_dword: int, plain_dword: int, increment: int) -> int:
    xor_key = (encrypted_dword ^ plain_dword) & UINT32_MASK
    s1_high = xor_key & 0xFFFF0000
    s2_high = xor_key & 0x0000FFFF
    hits: list[int] = []
    for s1_low in range(0x10000):
        s1 = s1_high | s1_low
        s2 = (LCG_A * s1 + increment) & UINT32_MASK
        if ((s2 >> 16) & 0xFFFF) == s2_high:
            seed = ((s1 - increment) * LCG_A_INV) & UINT32_MASK
            hits.append(seed)
    if len(hits) != 1:
        raise ValueError(f"expected one seed candidate, got {len(hits)}")
    return hits[0]


def key_seed(key: str) -> int:
    raw = key.encode("ascii")
    if len(raw) != 4:
        raise ValueError("NKPI keys are exactly 4 ASCII bytes")
    return (
        1988257537 * raw[0]
        + 449 * (raw[3] + 449 * (raw[2] + 449 * raw[1]))
    ) & UINT32_MASK


UTILITY_SEEDS = {
    "name_utf8": key_seed("sTrA"),
    "name_utf16": key_seed("sTrW"),
    "hash": key_seed("HASH"),
    "group": key_seed("GRPI"),
    "body": key_seed("BodY"),
}


@dataclass(frozen=True)
class NkpiProfile:
    profile_name: str
    guarded: bool
    header_seed: int
    name_utf8_seed: int
    name_utf16_seed: int
    hash_seed: int
    group_seed: int
    body_seed: int
    bridge_required: bool


UTILITY_GUARDED_PROFILE = NkpiProfile(
    profile_name="utility-guarded",
    guarded=True,
    header_seed=0x76C5D612,
    name_utf8_seed=UTILITY_SEEDS["name_utf8"],
    name_utf16_seed=UTILITY_SEEDS["name_utf16"],
    hash_seed=UTILITY_SEEDS["hash"],
    group_seed=UTILITY_SEEDS["group"],
    body_seed=UTILITY_SEEDS["body"],
    bridge_required=True,
)

US_ZIMO_PROFILE = NkpiProfile(
    profile_name="us-zimo",
    guarded=False,
    header_seed=0x4A454634,
    name_utf8_seed=0x712A98D4,
    name_utf16_seed=0x712AE776,
    hash_seed=0x4B449936,
    group_seed=0x1FBB7078,
    body_seed=0xDD4FF706,
    bridge_required=False,
)

SUPPORTED_PROFILES = (UTILITY_GUARDED_PROFILE, US_ZIMO_PROFILE)
_DETECTION_CACHE_LOCK = threading.RLock()
_DETECTION_CACHE: OrderedDict[tuple[str, int, int, str], "PvfDetectionResult"] = OrderedDict()


@dataclass(frozen=True)
class PvfDetectionResult:
    profile_name: str
    source_sha256: str
    file_size: int
    guarded: bool
    bridge_required: bool


@dataclass(frozen=True)
class PvfHeader:
    signature: int
    guid: bytes
    file_count: int
    padding: int
    body_size: int
    group_count: int
    hash_table_size: int
    name_table_size: int
    seed: int
    guarded: bool


@dataclass(frozen=True)
class PvfLayout:
    hash_offset: int
    name_offset: int
    group_offset: int
    body_offset: int
    end_offset: int


@dataclass(frozen=True)
class PvfEntry:
    index: int
    path: str
    name_offset: int
    path_offset: int
    chunk_index: int
    data_offset: int
    data_size: int
    data_type: int


@dataclass(frozen=True)
class _RawEntry:
    name_offset: int
    path_offset: int
    chunk_index: int
    data_offset: int
    data_size: int
    data_type: int


@dataclass(frozen=True)
class _NameStream:
    label: str
    magic: int
    header_offset: int
    data_offset: int
    compressed_size: int
    original_size: int


def _apply_header_guard(raw: bytearray) -> None:
    for index in range(GUARD_START, GUARD_END):
        raw[index] ^= 0x55


def _parse_header_candidate(raw_header: bytes, profile: NkpiProfile) -> tuple[PvfHeader, bytes]:
    raw = bytearray(raw_header)
    if profile.guarded:
        _apply_header_guard(raw)
    plain = decrypt_lcg(bytes(raw), profile.header_seed, INC1)
    signature, guid, file_count, padding, body_size, group_count, hash_size, name_size = struct.unpack_from(
        "<I20siiiiii", plain, 0
    )
    header = PvfHeader(
        signature=signature,
        guid=guid,
        file_count=file_count,
        padding=padding,
        body_size=body_size,
        group_count=group_count,
        hash_table_size=hash_size,
        name_table_size=name_size,
        seed=profile.header_seed,
        guarded=profile.guarded,
    )
    return header, plain


def _compute_layout(header: PvfHeader, file_size: int) -> PvfLayout | None:
    if header.signature != HEADER_MAGIC:
        return None
    if min(header.file_count, header.body_size, header.group_count, header.hash_table_size, header.name_table_size) < 0:
        return None
    hash_offset = HEADER_SIZE + header.file_count * FILE_ENTRY_SIZE
    name_offset = hash_offset + header.hash_table_size
    group_offset = name_offset + header.name_table_size
    body_offset = group_offset + header.group_count * GROUP_ENTRY_SIZE
    end_offset = body_offset + header.body_size
    if end_offset != file_size:
        return None
    return PvfLayout(hash_offset, name_offset, group_offset, body_offset, end_offset)


def _read_c_string(pool: bytes, offset: int, encoding: str) -> str:
    if offset < 0 or offset >= len(pool):
        return f"<bad-name-offset:0x{offset:x}>"
    if encoding == "utf-16le":
        end = offset
        while end + 1 < len(pool):
            if pool[end] == 0 and pool[end + 1] == 0:
                break
            end += 2
        return repair_pvf_text(pool[offset:end].decode("utf-16le", errors="replace"))
    end = pool.find(b"\x00", offset)
    if end < 0:
        end = len(pool)
    raw = pool[offset:end]
    if encoding == "pvf-narrow":
        return _decode_pvf_narrow_text(raw)
    return repair_pvf_text(raw.decode(encoding, errors="replace"))


def _contains_cjk(text: str) -> bool:
    return any(
        "\u3400" <= character <= "\u4dbf"
        or "\u4e00" <= character <= "\u9fff"
        or "\uf900" <= character <= "\ufaff"
        for character in text
    )


def _east_asian_decode_score(text: str) -> int:
    """Prefer a candidate with plausible East-Asian glyphs."""
    score = 0
    for character in text:
        if "\u3400" <= character <= "\u4dbf" or "\u4e00" <= character <= "\u9fff":
            score += 4
        elif "\uf900" <= character <= "\ufaff":
            score += 1
        elif (
            "\u1100" <= character <= "\u11ff"
            or "\u3040" <= character <= "\u30ff"
            or "\u3100" <= character <= "\u318f"
            or "\u31a0" <= character <= "\u31bf"
            or "\uac00" <= character <= "\ud7af"
            or "\uff65" <= character <= "\uff9f"
        ):
            score += 5 if "\uac00" <= character <= "\ud7af" else 3
        elif "\u2e80" <= character <= "\u2fff":
            score += 1
    return score


# These are display words seen in stackable, skill and upgrade scripts. They
# only break ties between otherwise reversible CP949/GBK candidates.
_PVF_HAN_PHRASES = frozenset(
    {
        "经验", "膠囊", "胶囊", "经验书", "經驗", "經驗書", "升级", "升級",
        "强化", "強化", "增幅", "技能", "点数", "點數", "物品", "道具", "装备", "裝備",
        "等级", "等級", "角色", "任务", "任務", "奖励", "獎勵", "使用", "增加", "消耗",
        "概率", "機率", "成功", "失败", "失敗", "金币", "金幣", "攻击", "攻擊", "防御",
        "防禦", "复活", "復活", "券", "书", "書", "点", "點",
    }
)
_PVF_HAN_SIGNAL_CHARS = frozenset(
    "的经验膠囊胶升级升級强化強化增幅技能点数點數物品道具装备裝備"
    "等级等級角色任务任務奖励獎勵使用增加消耗概率機率成功失败失敗"
    "金币金幣攻击攻擊防御防禦复活復活券书書"
)
_MOJIBAKE_MARKERS = frozenset(
    "ÃÂÐÑåæçèéêëìíîïðñòóôõö÷øùúûüýþÿ"
    "ï»¿âœžŸ"
)
_MOJIBAKE_RE = re.compile(r"[\u0080-\u009f\ufffd]")


def _has_pvf_han_phrase(text: str) -> bool:
    return any(phrase in text for phrase in _PVF_HAN_PHRASES)


def _text_quality(text: str) -> int:
    """Score readable PVF text without assuming one player locale."""
    value = str(text or "")
    han = sum("\u3400" <= c <= "\u4dbf" or "\u4e00" <= c <= "\u9fff" for c in value)
    hangul = sum("\uac00" <= c <= "\ud7af" for c in value)
    kana = sum("\u3040" <= c <= "\u30ff" for c in value)
    common = sum(c in _PVF_HAN_SIGNAL_CHARS for c in value)
    phrase_hits = sum(value.count(phrase) for phrase in _PVF_HAN_PHRASES)
    controls = sum(
        (ord(c) < 32 and c not in "\r\n\t") or "\u007f" <= c <= "\u009f"
        for c in value
    )
    replacement = value.count("\ufffd")
    markers = sum(c in _MOJIBAKE_MARKERS for c in value)
    compatibility = sum("\uf900" <= c <= "\ufaff" for c in value)
    return (
        han * 4
        + hangul * 5
        + kana * 3
        + common * 2
        + phrase_hits * 8
        - controls * 20
        - replacement * 30
        - markers * 4
        - compatibility * 3
    )


def _repair_mojibake_once(text: str) -> str:
    value = str(text or "")
    if not value or not any(ord(c) >= 0x80 for c in value):
        return value
    han = 0
    hangul = 0
    compatibility = 0
    suspicious = 0
    for character in value:
        code = ord(character)
        if "\u3400" <= character <= "\u4dbf" or "\u4e00" <= character <= "\u9fff":
            han += 1
        elif "\uac00" <= character <= "\ud7af":
            hangul += 1
        elif "\uf900" <= character <= "\ufaff":
            compatibility += 1
        if (code < 32 and character not in "\r\n\t") or 0x7F <= code <= 0x9F:
            suspicious += 1
        elif character == "\ufffd" or character in _MOJIBAKE_MARKERS:
            suspicious += 1

    codec_pairs: list[tuple[str, str]] = []
    if suspicious:
        codec_pairs.extend(
            (
                ("latin1", "utf-8"),
                ("cp1252", "utf-8"),
                ("latin1", "gbk"),
                ("cp1252", "gbk"),
            )
        )
    # A GBK string decoded as CP949 normally contains both Hangul and Han.
    # Pure Korean is already valid display text and must not be reinterpreted.
    if hangul and (han or compatibility):
        codec_pairs.extend((("cp949", "gbk"), ("cp949", "utf-8")))
    # UTF-8 bytes decoded as GBK produce Han-looking text.  Do not also try
    # GBK -> CP949: that direction changed valid Chinese and Korean into other
    # valid-looking glyphs in large localized PVFs.
    if han:
        codec_pairs.append(("gbk", "utf-8"))
    if not codec_pairs:
        return value

    raw_candidates: list[str] = []
    for source, target in codec_pairs:
        try:
            candidate = value.encode(source).decode(target)
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if not candidate or candidate == value or "\ufffd" in candidate:
            continue
        cross_codec = source in {"cp949", "gbk"} and target in {"cp949", "gbk", "utf-8"}
        if cross_codec and not (
            _has_pvf_han_phrase(candidate)
            or compatibility
        ):
            continue
        raw_candidates.append(candidate)
    if not raw_candidates:
        return value

    baseline = _text_quality(value)
    candidates: list[tuple[int, str]] = []
    for candidate in raw_candidates:
        candidate_suspicious = len(_MOJIBAKE_RE.findall(candidate)) + sum(
            c in _MOJIBAKE_MARKERS for c in candidate
        )
        gain = _text_quality(candidate) - baseline
        if gain < 8 or (suspicious and candidate_suspicious >= suspicious and gain < 16):
            continue
        candidates.append((gain, candidate))
    return max(candidates, key=lambda item: item[0])[1] if candidates else value


def repair_pvf_text(text: str) -> str:
    """Repair only reversible, strongly scored mojibake in decoded PVF text."""
    value = str(text or "")
    # Work line-by-line so a translated CJK field beside an ASCII machine tag
    # does not prevent repairing one corrupted display field.
    repaired_lines: list[str] = []
    for line in value.splitlines(keepends=True):
        ending = ""
        body = line
        if line.endswith("\r\n"):
            body, ending = line[:-2], "\r\n"
        elif line.endswith(("\r", "\n")):
            body, ending = line[:-1], line[-1]
        candidate = body
        for _ in range(2):
            updated = _repair_mojibake_once(candidate)
            if updated == candidate:
                break
            candidate = updated
        repaired_lines.append(candidate + ending)
    return "".join(repaired_lines)


def decode_pvf_text_bytes(raw: bytes) -> str:
    """Restore PVF narrow text before any field or token parser consumes it.

    This mirrors the client Chinese compatibility path: strict UTF-8 first,
    then CP949/GBK only when the decoded candidate contains East-Asian text,
    and CP437 as the legacy fallback. UTF-16 BOM entries are handled before
    narrow encodings so a wide display field is not treated as binary text.
    """
    payload = bytes(raw or b"")
    if not payload:
        return ""
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        for encoding in ("utf-16", "utf-16-le", "utf-16-be"):
            try:
                return repair_pvf_text(payload.decode(encoding))
            except UnicodeDecodeError:
                continue
    if len(payload) >= 4 and len(payload) % 2 == 0:
        odd_zeroes = sum(byte == 0 for byte in payload[1::2])
        even_zeroes = sum(byte == 0 for byte in payload[0::2])
        if max(odd_zeroes, even_zeroes) >= max(2, len(payload) // 4):
            for encoding in ("utf-16-le", "utf-16-be"):
                try:
                    wide = payload.decode(encoding)
                except UnicodeDecodeError:
                    continue
                if wide and all(ord(c) >= 32 or c in "\r\n\t" for c in wide):
                    return repair_pvf_text(wide)
    try:
        return repair_pvf_text(payload.decode("utf-8-sig"))
    except UnicodeDecodeError:
        pass
    candidates: list[tuple[int, str]] = []
    for encoding in ("cp949", "gbk"):
        try:
            decoded = payload.decode(encoding)
        except UnicodeDecodeError:
            continue
        if _contains_cjk(decoded):
            candidates.append((_east_asian_decode_score(decoded), decoded))
    if candidates:
        scored = [
            (score + (32 if _has_pvf_han_phrase(decoded) else 0), decoded)
            for score, decoded in candidates
        ]
        return repair_pvf_text(max(scored, key=lambda candidate: candidate[0])[1])
    return repair_pvf_text(payload.decode("cp437", errors="replace"))


def _decode_pvf_narrow_text(raw: bytes) -> str:
    """Compatibility alias retained for name-pool and profile tests."""
    return decode_pvf_text_bytes(raw)


class PvfReader:
    """Read-only NKPI reader for the local Stage3 server prototype."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        file_bytes: bytes | None = None,
        preload_groups: bool = False,
        group_cache_limit: int = 8,
        source_sha256: str | None = None,
    ) -> None:
        self.path = Path(path)
        self._file = None
        self._file_bytes = file_bytes
        self._expected_source_sha256 = str(source_sha256 or "").lower()
        self.source_sha256 = ""
        if self._file_bytes is None:
            self._file = self.path.open("rb")
            self.file_size = os.fstat(self._file.fileno()).st_size
        else:
            self.file_size = len(self._file_bytes)
        if self._file_bytes is not None:
            actual_sha256 = hashlib.sha256(self._file_bytes).hexdigest()
            if (
                self._expected_source_sha256
                and self._expected_source_sha256 != actual_sha256
            ):
                raise ValueError("provided PVF SHA-256 does not match source bytes")
            self.source_sha256 = actual_sha256
        self.profile, self.header, self.layout = self._parse_profile_header_and_layout()
        self._raw_entries: list[_RawEntry] | None = None
        self._utf8_pool: bytes | None = None
        self._utf16_pool: bytes | None = None
        self._groups: list[tuple[int, int]] | None = None
        self._path_cache: list[str] | None = None
        self._path_to_index: dict[str, int] | None = None
        self._native_directory = None
        self._directory_cache_status = "uninitialized"
        self._directory_cache_path = ""
        self._directory_lock = threading.RLock()
        self._group_cache: OrderedDict[int, bytes] = OrderedDict()
        self._group_lock = threading.RLock()
        self._warmup_text_cache: dict[int, str] | None = None
        self._warmup_text_cache_lock = threading.RLock()
        self._warmup_text_cache_hits = 0
        self._warmup_text_cache_misses = 0
        self._warmup_text_cache_chars = 0
        # 0 / negative means keep every decompressed group in RAM.
        self._group_cache_limit = int(group_cache_limit)
        self._groups_preloaded = False
        self._runtime_frozen = False
        self._runtime_partial_groups_allowed = False
        self._runtime_group_load_count = 0
        self._runtime_group_load_bytes = 0
        if preload_groups:
            self.preload_all_groups()

    @classmethod
    def open(
        cls,
        path: str | os.PathLike[str],
        *,
        memory: bool = False,
        preload_groups: bool = False,
        group_cache_limit: int = 8,
        source_sha256: str | None = None,
    ) -> "PvfReader":
        file_bytes = None
        if memory:
            file_bytes = Path(path).read_bytes()
        return cls(
            path,
            file_bytes=file_bytes,
            preload_groups=preload_groups,
            group_cache_limit=group_cache_limit,
            source_sha256=source_sha256,
        )

    def close(self) -> None:
        with self._warmup_text_cache_lock:
            self._warmup_text_cache = None
            self._warmup_text_cache_hits = 0
            self._warmup_text_cache_misses = 0
            self._warmup_text_cache_chars = 0
        if self._file is not None:
            self._file.close()
            self._file = None

    def __enter__(self) -> "PvfReader":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def is_memory_backed(self) -> bool:
        return self._file_bytes is not None

    @property
    def groups_preloaded(self) -> bool:
        return self._groups_preloaded

    @property
    def path_index_ready(self) -> bool:
        return self._native_directory is not None

    @property
    def directory_cache_status(self) -> str:
        return self._directory_cache_status

    @property
    def directory_cache_path(self) -> str:
        return self._directory_cache_path

    @property
    def name_pools_ready(self) -> bool:
        return self._utf8_pool is not None and self._utf16_pool is not None

    @property
    def all_groups_resident(self) -> bool:
        with self._group_lock:
            return (
                self._groups is not None
                and self._groups_preloaded
                and len(self._group_cache) == len(self._groups)
            )

    @property
    def resident_group_count(self) -> int:
        """Number of decompressed body groups currently retained in memory."""
        with self._group_lock:
            return len(self._group_cache)

    @property
    def resident_decompressed_bytes(self) -> int:
        """Total bytes held by the resident decompressed group cache."""
        with self._group_lock:
            return sum(len(data) for data in self._group_cache.values())

    @property
    def runtime_partial_groups_allowed(self) -> bool:
        """Whether a frozen reader may materialize a missing in-memory group."""
        return self._runtime_partial_groups_allowed

    @property
    def runtime_group_load_count(self) -> int:
        """Groups first materialized after a selective runtime freeze."""
        with self._group_lock:
            return self._runtime_group_load_count

    @property
    def runtime_group_load_bytes(self) -> int:
        with self._group_lock:
            return self._runtime_group_load_bytes

    @property
    def runtime_frozen(self) -> bool:
        """Whether runtime reads are constrained to the prepared RAM source."""
        return self._runtime_frozen

    @property
    def warmup_text_cache_active(self) -> bool:
        with self._warmup_text_cache_lock:
            return self._warmup_text_cache is not None

    def begin_warmup_text_cache(self) -> None:
        """Reuse decoded entries only while boot domains inspect the same PVF."""
        with self._warmup_text_cache_lock:
            if self._warmup_text_cache is not None:
                raise RuntimeError("PVF warmup text cache is already active")
            self._warmup_text_cache = {}
            self._warmup_text_cache_hits = 0
            self._warmup_text_cache_misses = 0
            self._warmup_text_cache_chars = 0

    def end_warmup_text_cache(self) -> dict[str, int]:
        """Release all decoded boot text and return bounded diagnostic counts."""
        with self._warmup_text_cache_lock:
            cache = self._warmup_text_cache
            if cache is None:
                raise RuntimeError("PVF warmup text cache is not active")
            stats = {
                "enabled": 1,
                "entry_count": len(cache),
                "hits": int(self._warmup_text_cache_hits),
                "misses": int(self._warmup_text_cache_misses),
                "decoded_chars": int(self._warmup_text_cache_chars),
            }
            self._warmup_text_cache = None
            self._warmup_text_cache_hits = 0
            self._warmup_text_cache_misses = 0
            self._warmup_text_cache_chars = 0
            return stats

    def freeze_runtime(self, *, allow_partial_groups: bool = False) -> None:
        """Seal a memory-backed reader after startup preparation.

        The default keeps the historical full-resident contract.  Selective
        startup passes ``allow_partial_groups=True`` after domain warmup: path
        and name indexes are still complete, while a rare future request may
        decompress its missing body group from the already memory-backed PVF.
        """
        if not self.is_memory_backed:
            raise RuntimeError("PVF runtime freeze requires memory-backed source bytes")
        self._ensure_groups()
        if not allow_partial_groups and (
            not self._groups_preloaded or not self.all_groups_resident
        ):
            raise RuntimeError("PVF runtime freeze requires every group resident in memory")
        if allow_partial_groups and self._group_cache_limit > 0:
            raise RuntimeError(
                "selective PVF runtime freeze requires an unbounded memory group cache"
            )
        if not self.path_index_ready:
            raise RuntimeError("PVF runtime freeze requires a complete path index")
        if not self.name_pools_ready:
            raise RuntimeError("PVF runtime freeze requires decoded name pools")
        self._runtime_partial_groups_allowed = bool(allow_partial_groups)
        self._runtime_frozen = True

    def _read_at(self, offset: int, size: int) -> bytes:
        if self._file_bytes is not None:
            end = offset + size
            if offset < 0 or end > len(self._file_bytes):
                raise EOFError(
                    f"short read at 0x{offset:x}: wanted {size}, file_bytes={len(self._file_bytes)}"
                )
            return self._file_bytes[offset:end]
        assert self._file is not None
        self._file.seek(offset)
        data = self._file.read(size)
        if len(data) != size:
            raise EOFError(f"short read at 0x{offset:x}: wanted {size}, got {len(data)}")
        return data

    def preload_all_groups(self) -> int:
        # Decompress every body group into the in-process group cache.
        # Returns total decompressed bytes retained in memory.
        with self._group_lock:
            self._ensure_groups()
            assert self._groups is not None
            previous_limit = self._group_cache_limit
            self._group_cache_limit = 0
            total = 0
            for index in range(len(self._groups)):
                total += len(self._load_group(index))
            self._group_cache_limit = previous_limit if previous_limit > 0 else 0
            self._groups_preloaded = True
            return total

    def preload_all_groups_parallel(self, max_workers: Optional[int] = None) -> int:
        """Materialize groups across worker threads using native transforms."""
        if not self.is_memory_backed:
            raise RuntimeError("parallel PVF group load requires a memory-backed reader")
        with self._group_lock:
            self._ensure_groups()
            assert self._groups is not None
            groups = tuple(self._groups)
            indexes = tuple(range(len(groups)))
            previous_limit = self._group_cache_limit
            self._group_cache_limit = 0

        def decode(index: int) -> tuple[int, bytes]:
            previous_cumulative = 0 if index == 0 else groups[index - 1][0]
            cumulative, original_size = groups[index]
            encrypted = self._read_at(
                self.layout.body_offset + previous_cumulative,
                cumulative - previous_cumulative,
            )
            compressed = decrypt_lcg(encrypted, self.profile.body_seed, INC1)
            data = zlib.decompress(compressed)
            if len(data) != original_size:
                raise ValueError(
                    f"group {index} original size mismatch: {len(data)} != {original_size}"
                )
            return index, data

        default_workers = min(32, max(2, os.cpu_count() or 2))
        workers = int(max_workers or default_workers)
        workers = min(64, max(1, workers))
        total = 0
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                for index, data in pool.map(decode, indexes):
                    with self._group_lock:
                        self._group_cache[index] = data
                    total += len(data)
        finally:
            with self._group_lock:
                self._group_cache_limit = previous_limit if previous_limit > 0 else 0
        with self._group_lock:
            self._groups_preloaded = True
        return total

    def _parse_profile_header_and_layout(self) -> tuple[NkpiProfile, PvfHeader, PvfLayout]:
        raw = self._read_at(0, HEADER_SIZE)
        errors: list[str] = []
        for profile in SUPPORTED_PROFILES:
            try:
                header, _plain = _parse_header_candidate(raw, profile)
                layout = _compute_layout(header, self.file_size)
                if layout is None:
                    errors.append(f"{profile.profile_name}: header/layout mismatch")
                    continue
                self._validate_profile_sections(profile, header, layout)
                return profile, header, layout
            except Exception as exc:
                errors.append(f"{profile.profile_name}: {type(exc).__name__}: {exc}")
        raise ValueError("unsupported or damaged NKPI profile:\n  " + "\n  ".join(errors))

    def _validate_profile_sections(
        self,
        profile: NkpiProfile,
        header: PvfHeader,
        layout: PvfLayout,
    ) -> None:
        """Validate enough independent sections to identify one native profile."""
        name_table = self._read_at(layout.name_offset, header.name_table_size)
        if len(name_table) < 8:
            raise ValueError("name table is too small")
        utf8_stream = self._parse_name_stream(name_table, 8, "utf8", NAME_UTF8_MAGIC)
        utf16_offset = utf8_stream.data_offset + utf8_stream.compressed_size
        utf16_stream = self._parse_name_stream(
            name_table, utf16_offset, "utf16", NAME_UTF16_MAGIC
        )
        for stream, seed in (
            (utf8_stream, profile.name_utf8_seed),
            (utf16_stream, profile.name_utf16_seed),
        ):
            encrypted = name_table[
                stream.data_offset : stream.data_offset + stream.compressed_size
            ]
            pool = zlib.decompress(decrypt_lcg(encrypted, seed, INC_NAME))
            if len(pool) != stream.original_size:
                raise ValueError(f"{stream.label} name pool size mismatch")

        encrypted_groups = self._read_at(
            layout.group_offset, header.group_count * GROUP_ENTRY_SIZE
        )
        group_plain = decrypt_lcg(encrypted_groups, profile.group_seed, INC1)
        previous = 0
        first_compressed = 0
        first_original = 0
        for index in range(header.group_count):
            cumulative, original = struct.unpack_from(
                "<ii", group_plain, index * GROUP_ENTRY_SIZE
            )
            if not (previous < cumulative <= header.body_size):
                raise ValueError(f"group {index} invalid cumulative size {cumulative}")
            if original <= 0:
                raise ValueError(f"group {index} invalid original size {original}")
            if index == 0:
                first_compressed = cumulative
                first_original = original
            previous = cumulative
        if header.group_count == 0:
            if header.body_size != 0:
                raise ValueError("empty group table has a non-empty body")
            return
        if previous != header.body_size:
            raise ValueError("group table does not end at body_size")

        encrypted_body = self._read_at(layout.body_offset, first_compressed)
        first_body = zlib.decompress(
            decrypt_lcg(encrypted_body, profile.body_seed, INC1)
        )
        if len(first_body) != first_original:
            raise ValueError("first body group size mismatch")

    def _ensure_entries(self) -> None:
        if self._raw_entries is not None:
            return
        if self._runtime_frozen:
            if self._native_directory is None:
                raise RuntimeError("frozen PVF reader has no entry table")
            self._raw_entries = [
                _RawEntry(*self._native_directory.entry_at(index))
                for index in range(len(self._native_directory))
            ]
            return
        raw = self._read_at(HEADER_SIZE, self.header.file_count * FILE_ENTRY_SIZE)
        entries: list[_RawEntry] = []
        for i in range(self.header.file_count):
            values = struct.unpack_from("<iiiiii", raw, i * FILE_ENTRY_SIZE)
            entries.append(_RawEntry(*values))
        self._raw_entries = entries

    @staticmethod
    def _parse_name_stream(name_table: bytes, offset: int, label: str, magic: int) -> _NameStream:
        encrypted_compressed, encrypted_original = struct.unpack_from("<II", name_table, offset)
        compressed_size = encrypted_compressed ^ magic
        original_size = encrypted_original ^ compressed_size
        data_offset = offset + 8
        if compressed_size <= 0 or original_size <= 0:
            raise ValueError(f"{label}: invalid name stream sizes")
        if data_offset + compressed_size > len(name_table):
            raise ValueError(f"{label}: stream exceeds name table bounds")
        return _NameStream(label, magic, offset, data_offset, compressed_size, original_size)

    def _ensure_name_pools(self) -> None:
        if self._utf8_pool is not None and self._utf16_pool is not None:
            return
        if self._runtime_frozen:
            raise RuntimeError("frozen PVF reader has no decoded name pools")
        name_table = self._read_at(self.layout.name_offset, self.header.name_table_size)
        utf8_stream = self._parse_name_stream(name_table, 8, "utf8", NAME_UTF8_MAGIC)
        utf16_offset = utf8_stream.data_offset + utf8_stream.compressed_size
        utf16_stream = self._parse_name_stream(name_table, utf16_offset, "utf16", NAME_UTF16_MAGIC)

        utf8_enc = name_table[utf8_stream.data_offset : utf8_stream.data_offset + utf8_stream.compressed_size]
        utf16_enc = name_table[utf16_stream.data_offset : utf16_stream.data_offset + utf16_stream.compressed_size]
        utf8_compressed = decrypt_lcg(
            utf8_enc, self.profile.name_utf8_seed, INC_NAME
        )
        utf16_compressed = decrypt_lcg(
            utf16_enc, self.profile.name_utf16_seed, INC_NAME
        )
        utf8_pool = zlib.decompress(utf8_compressed)
        utf16_pool = zlib.decompress(utf16_compressed)
        if len(utf8_pool) != utf8_stream.original_size:
            raise ValueError("UTF-8 name pool size mismatch")
        if len(utf16_pool) != utf16_stream.original_size:
            raise ValueError("UTF-16 name pool size mismatch")
        self._utf8_pool = utf8_pool
        self._utf16_pool = utf16_pool

    def resolve_name(self, magic_offset: int) -> str:
        self._ensure_name_pools()
        assert self._utf8_pool is not None and self._utf16_pool is not None
        if magic_offset < 0:
            return f"<negative-name-offset:{magic_offset}>"
        if (magic_offset & 1) == 0:
            return _read_c_string(self._utf8_pool, magic_offset >> 1, "pvf-narrow")
        return _read_c_string(self._utf16_pool, (magic_offset >> 1) * 2, "utf-16le")

    def _entry_path(self, raw: _RawEntry) -> str:
        folder = self.resolve_name(raw.path_offset).replace("\\", "/").strip("/")
        name = self.resolve_name(raw.name_offset).replace("\\", "/")
        return f"{folder}/{name}" if folder else name

    def _ensure_source_sha256(self) -> None:
        if self.source_sha256:
            return
        if self._file_bytes is not None:
            actual_sha256 = hashlib.sha256(self._file_bytes).hexdigest()
        else:
            if self._file is None:
                raise RuntimeError("PVF source file is closed")
            position = self._file.tell()
            before = os.fstat(self._file.fileno())
            digest = hashlib.sha256()
            try:
                self._file.seek(0)
                while True:
                    chunk = self._file.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            finally:
                self._file.seek(position)
            after = os.fstat(self._file.fileno())
            if (
                before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or after.st_size != self.file_size
            ):
                raise RuntimeError("Script.pvf changed while its identity was calculated")
            actual_sha256 = digest.hexdigest()
        if (
            self._expected_source_sha256
            and self._expected_source_sha256 != actual_sha256
        ):
            raise ValueError("provided PVF SHA-256 does not match the opened source")
        self.source_sha256 = actual_sha256

    def _ensure_path_index(self) -> None:
        if self._native_directory is not None:
            return
        with self._directory_lock:
            if self._native_directory is not None:
                return
            if self._runtime_frozen:
                raise RuntimeError("frozen PVF reader has no path index")
            from pvf.directory_cache import (
                build_identity,
                load_directory,
                store_directory,
            )
            from pvf.native_loader import require_native_module

            native = require_native_module()
            self._ensure_groups()
            self._ensure_source_sha256()
            assert self._groups is not None
            identity = build_identity(
                path=self.path,
                source_sha256=self.source_sha256,
                source_size=self.file_size,
                file_count=self.header.file_count,
                group_count=self.header.group_count,
                profile_name=self.profile_name,
                native_abi_version=native.abi_version(),
            )
            group_sizes = [original_size for _cumulative, original_size in self._groups]
            directory, status, cache_path = load_directory(
                identity,
                native,
                group_sizes,
            )
            if directory is None:
                self._ensure_name_pools()
                assert self._utf8_pool is not None and self._utf16_pool is not None
                entry_table = self._read_at(
                    HEADER_SIZE,
                    self.header.file_count * FILE_ENTRY_SIZE,
                )
                directory = native.build_directory(
                    entry_table,
                    self._utf8_pool,
                    self._utf16_pool,
                )
                if len(directory) != self.header.file_count:
                    raise ValueError("native PVF directory file count mismatch")
                directory.validate(group_sizes)
                store_directory(identity, native, directory)
            else:
                self._ensure_name_pools()
            self._native_directory = directory
            self._directory_cache_status = status
            self._directory_cache_path = str(cache_path)

    def list_paths(self, prefix: str | None = None) -> list[str]:
        self._ensure_path_index()
        assert self._native_directory is not None
        if prefix is None:
            paths = list(self._native_directory.iter_prefix(""))
            self._path_cache = paths
            return list(paths)
        return list(self._native_directory.iter_prefix(prefix))

    def search_paths(
        self,
        query: str,
        *,
        prefix: str | None = None,
        limit: int = 0,
    ) -> list[str]:
        self._ensure_path_index()
        assert self._native_directory is not None
        return list(
            self._native_directory.search(query, prefix=prefix, limit=limit)
        )

    def get_entry(self, path: str) -> PvfEntry:
        self._ensure_path_index()
        assert self._native_directory is not None
        index = self._native_directory.lookup(path)
        if index is None:
            raise KeyError(path)
        raw = _RawEntry(*self._native_directory.entry_at(index))
        return PvfEntry(
            index=index,
            path=self._native_directory.path_at(index),
            name_offset=raw.name_offset,
            path_offset=raw.path_offset,
            chunk_index=raw.chunk_index,
            data_offset=raw.data_offset,
            data_size=raw.data_size,
            data_type=raw.data_type,
        )

    def _ensure_groups(self) -> None:
        if self._groups is not None:
            return
        if self._runtime_frozen:
            raise RuntimeError("frozen PVF reader has no group table")
        encrypted = self._read_at(self.layout.group_offset, self.header.group_count * GROUP_ENTRY_SIZE)
        plain = decrypt_lcg(encrypted, self.profile.group_seed, INC1)
        groups: list[tuple[int, int]] = []
        previous = 0
        for index in range(self.header.group_count):
            cumulative, original = struct.unpack_from("<ii", plain, index * GROUP_ENTRY_SIZE)
            if not (previous < cumulative <= self.header.body_size):
                raise ValueError(f"group {index} invalid cumulative size {cumulative}")
            if original <= 0:
                raise ValueError(f"group {index} invalid original size {original}")
            groups.append((cumulative, original))
            previous = cumulative
        if groups and groups[-1][0] != self.header.body_size:
            raise ValueError("group table does not end at body_size")
        self._groups = groups

    def _load_group(self, index: int) -> bytes:
        with self._group_lock:
            self._ensure_groups()
            assert self._groups is not None
            if not 0 <= index < len(self._groups):
                raise IndexError(index)
            if index in self._group_cache:
                if not self._runtime_frozen:
                    data = self._group_cache.pop(index)
                    self._group_cache[index] = data
                    return data
                return self._group_cache[index]
            if self._runtime_frozen and not self._runtime_partial_groups_allowed:
                raise RuntimeError(f"frozen PVF reader is missing group {index}")
            previous_cumulative = 0 if index == 0 else self._groups[index - 1][0]
            cumulative, original_size = self._groups[index]
            compressed_size = cumulative - previous_cumulative
            encrypted = self._read_at(
                self.layout.body_offset + previous_cumulative,
                compressed_size,
            )
            compressed = decrypt_lcg(encrypted, self.profile.body_seed, INC1)
            data = zlib.decompress(compressed)
            if len(data) != original_size:
                raise ValueError(
                    f"group {index} original size mismatch: {len(data)} != {original_size}"
                )
            self._group_cache[index] = data
            if self._runtime_frozen:
                self._runtime_group_load_count += 1
                self._runtime_group_load_bytes += len(data)
            if self._group_cache_limit > 0:
                while len(self._group_cache) > self._group_cache_limit:
                    self._group_cache.popitem(last=False)
            return data

    def _read_entry_raw(
        self,
        *,
        chunk_index: int,
        data_offset: int,
        data_size: int,
        label: str,
    ) -> bytes:
        group = self._load_group(int(chunk_index))
        start = int(data_offset)
        end = start + int(data_size)
        if start < 0 or end < start or end > len(group):
            raise ValueError(f"entry {label!r} slice outside group")
        return group[start:end]

    def read_raw(self, path: str) -> bytes:
        entry = self.get_entry(path)
        return self._read_entry_raw(
            chunk_index=entry.chunk_index,
            data_offset=entry.data_offset,
            data_size=entry.data_size,
            label=entry.path,
        )

    def _decode_token_text(self, data: bytes) -> str:
        self._ensure_name_pools()
        assert self._utf8_pool is not None and self._utf16_pool is not None
        from pvf.native_loader import require_native_module

        return repair_pvf_text(require_native_module().decode_token_text(
            data,
            self._utf8_pool,
            self._utf16_pool,
        ))

    def _decode_entry_text(self, raw: bytes, data_type: int) -> str:
        if int(data_type) == 1:
            return self._decode_token_text(raw)
        return decode_pvf_text_bytes(raw)

    def _read_entry_text(
        self,
        *,
        entry_index: int,
        chunk_index: int,
        data_offset: int,
        data_size: int,
        data_type: int,
        label: str,
    ) -> str:
        index = int(entry_index)
        with self._warmup_text_cache_lock:
            cache = self._warmup_text_cache
            if cache is not None:
                if index in cache:
                    self._warmup_text_cache_hits += 1
                    return cache[index]

        raw = self._read_entry_raw(
            chunk_index=chunk_index,
            data_offset=data_offset,
            data_size=data_size,
            label=label,
        )
        decoded = self._decode_entry_text(raw, data_type)

        with self._warmup_text_cache_lock:
            if self._warmup_text_cache is cache and cache is not None:
                if index in cache:
                    self._warmup_text_cache_hits += 1
                    return cache[index]
                cache[index] = decoded
                self._warmup_text_cache_misses += 1
                self._warmup_text_cache_chars += len(decoded)
        return decoded

    def read_text(self, path: str) -> str:
        entry = self.get_entry(path)
        return self._read_entry_text(
            entry_index=entry.index,
            chunk_index=entry.chunk_index,
            data_offset=entry.data_offset,
            data_size=entry.data_size,
            data_type=entry.data_type,
            label=entry.path,
        )

    def read_text_by_index(self, index: int) -> str:
        """Read one entry by its stable NativeDirectory enumeration index."""
        self._ensure_path_index()
        assert self._native_directory is not None
        entry_index = int(index)
        raw_entry = _RawEntry(*self._native_directory.entry_at(entry_index))
        return self._read_entry_text(
            entry_index=entry_index,
            chunk_index=raw_entry.chunk_index,
            data_offset=raw_entry.data_offset,
            data_size=raw_entry.data_size,
            data_type=raw_entry.data_type,
            label="#%d" % entry_index,
        )

    def first_entry_by_type(self, data_type: int) -> PvfEntry:
        self._ensure_path_index()
        assert self._native_directory is not None
        index = self._native_directory.first_by_type(data_type)
        if index is None:
            raise KeyError(f"no entry with DataType={data_type}")
        raw = _RawEntry(*self._native_directory.entry_at(index))
        return PvfEntry(
            index=index,
            path=self._native_directory.path_at(index),
            name_offset=raw.name_offset,
            path_offset=raw.path_offset,
            chunk_index=raw.chunk_index,
            data_offset=raw.data_offset,
            data_size=raw.data_size,
            data_type=raw.data_type,
        )

    @property
    def file_count(self) -> int:
        return self.header.file_count

    @property
    def group_count(self) -> int:
        return self.header.group_count

    @property
    def profile_name(self) -> str:
        return self.profile.profile_name


def _stable_file_bytes(path: Path) -> tuple[bytes, int]:
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            raw = stream.read()
            after = os.fstat(stream.fileno())
    except FileNotFoundError:
        raise FileNotFoundError(f"missing Script.pvf: {path}")
    final = path.stat()
    before_id = (before.st_size, before.st_mtime_ns)
    after_id = (after.st_size, after.st_mtime_ns)
    final_id = (final.st_size, final.st_mtime_ns)
    if before_id != after_id or after_id != final_id or len(raw) != final.st_size:
        raise RuntimeError(f"Script.pvf changed while being read: {path}")
    return raw, final.st_mtime_ns


def detect_pvf_profile(path: str | os.PathLike[str]) -> PvfDetectionResult:
    """Hash and strictly identify one supported Script.pvf without full preload."""
    target = Path(path).resolve()
    raw, mtime_ns = _stable_file_bytes(target)
    source_sha256 = hashlib.sha256(raw).hexdigest()
    cache_key = (str(target).lower(), len(raw), mtime_ns, source_sha256)
    with _DETECTION_CACHE_LOCK:
        cached = _DETECTION_CACHE.get(cache_key)
        if cached is not None:
            _DETECTION_CACHE.move_to_end(cache_key)
            return cached
    reader = PvfReader(target, file_bytes=raw)
    try:
        detected = PvfDetectionResult(
            profile_name=reader.profile_name,
            source_sha256=source_sha256,
            file_size=len(raw),
            guarded=reader.profile.guarded,
            bridge_required=reader.profile.bridge_required,
        )
        with _DETECTION_CACHE_LOCK:
            _DETECTION_CACHE[cache_key] = detected
            while len(_DETECTION_CACHE) > 8:
                _DETECTION_CACHE.popitem(last=False)
        return detected
    finally:
        reader.close()

