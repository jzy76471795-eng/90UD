# -*- coding: utf-8 -*-
"""Synchronize the native adventure-group tooltip before Stage3 starts.

The frozen client builds status 135 with DSTR 31514 as one fixed description
row.  USERINFO1 still owns the native type 0x65 effect and its attributes; this
module only projects the already-warmed PVF values into that description row.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

from game.character.adventure_group import calculate_adventure_group_for_account


ADVENTURE_TOOLTIP_DSTR_ID = 31514
DSTR_RESOURCE_IDS = (102, 104, 105, 106)
SC01_SEED = 0x65248038

_ENTRY_RE = re.compile(r"(?m)^(\d+)>([^\r\n]*)(?=\r?$)")
_RT_NAMES = {
    1: "CURSOR",
    2: "BITMAP",
    3: "ICON",
    4: "MENU",
    5: "DIALOG",
    6: "STRING",
    7: "FONTDIR",
    8: "FONT",
    9: "ACCELERATOR",
    10: "RCDATA",
    11: "MESSAGETABLE",
    12: "GROUP_CURSOR",
    14: "GROUP_ICON",
    16: "VERSION",
    24: "MANIFEST",
}


@dataclass(frozen=True)
class PeSection:
    name: str
    virtual_address: int
    virtual_size: int
    raw_offset: int
    raw_size: int
    header_offset: int


@dataclass(frozen=True)
class ResourceLeaf:
    type_name: str
    resource_id: Union[int, str]
    language_id: Union[int, str]
    entry_offset: int
    data_rva: int
    data_size: int
    data_offset: int


@dataclass(frozen=True)
class DstrRewriteResult:
    data: bytes
    changed_resource_ids: Tuple[int, ...]


@dataclass(frozen=True)
class AdventureBuffDstrSyncResult:
    path: Path
    account_id: int
    adventure_level: int
    tooltip_text: str
    changed: bool
    before_sha256: str
    after_sha256: str
    used_client_default: bool = False
    sync_error: str = ""

    def describe(self) -> str:
        if self.used_client_default:
            return (
                "account_id=%d level=%d changed=0 mode=client-default "
                "intended_text=%s reason=%s"
                % (
                    self.account_id,
                    self.adventure_level,
                    self.tooltip_text,
                    self.sync_error,
                )
            )
        return (
            "account_id=%d level=%d changed=%d text=%s sha256=%s"
            % (
                self.account_id,
                self.adventure_level,
                int(self.changed),
                self.tooltip_text,
                self.after_sha256,
            )
        )


def _require_range(data: bytes, offset: int, size: int, label: str) -> None:
    if offset < 0 or size < 0 or offset > len(data) or size > len(data) - offset:
        raise ValueError(
            "%s is outside the file: offset=%d size=%d file_size=%d"
            % (label, offset, size, len(data))
        )


def _u16(data: bytes, offset: int, label: str = "u16") -> int:
    _require_range(data, offset, 2, label)
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int, label: str = "u32") -> int:
    _require_range(data, offset, 4, label)
    return struct.unpack_from("<I", data, offset)[0]


def _align_up(value: int, alignment: int) -> int:
    unit = int(alignment)
    if unit <= 0 or unit & (unit - 1):
        raise ValueError("PE alignment must be a positive power of two: %r" % alignment)
    return (int(value) + unit - 1) & ~(unit - 1)


def _parse_pe_layout(data: bytes) -> Tuple[int, List[PeSection], int, int, int]:
    _require_range(data, 0, 0x40, "DOS header")
    if data[:2] != b"MZ":
        raise ValueError("dstr.dat is not an MZ image")
    pe_offset = _u32(data, 0x3C, "PE header offset")
    _require_range(data, pe_offset, 24, "PE header")
    if data[pe_offset : pe_offset + 4] != b"PE\x00\x00":
        raise ValueError("dstr.dat has an invalid PE signature")

    section_count = _u16(data, pe_offset + 6, "PE section count")
    optional_size = _u16(data, pe_offset + 20, "PE optional header size")
    optional_offset = pe_offset + 24
    _require_range(data, optional_offset, optional_size, "PE optional header")
    if optional_size < 0x78 or _u16(data, optional_offset, "PE magic") != 0x10B:
        raise ValueError("dstr.dat must be a PE32 image")

    section_alignment = _u32(data, optional_offset + 32, "SectionAlignment")
    file_alignment = _u32(data, optional_offset + 36, "FileAlignment")
    resource_rva = _u32(data, optional_offset + 112, "resource RVA")
    resource_size = _u32(data, optional_offset + 116, "resource size")
    if resource_rva == 0 or resource_size == 0:
        raise ValueError("dstr.dat has no resource directory")

    section_table = optional_offset + optional_size
    _require_range(data, section_table, section_count * 40, "PE section table")
    sections: List[PeSection] = []
    for index in range(section_count):
        offset = section_table + index * 40
        raw_name = data[offset : offset + 8].split(b"\x00", 1)[0]
        try:
            name = raw_name.decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("PE section name is not ASCII") from exc
        virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from(
            "<IIII", data, offset + 8
        )
        if raw_size:
            if name == ".rsrc" and raw_offset <= len(data):
                # Localized copies may omit only trailing PE file-alignment padding.
                pass
            else:
                _require_range(data, raw_offset, raw_size, "PE section %s" % name)
        sections.append(
            PeSection(
                name=name,
                virtual_address=virtual_address,
                virtual_size=virtual_size,
                raw_offset=raw_offset,
                raw_size=raw_size,
                header_offset=offset,
            )
        )
    return optional_offset, sections, resource_rva, section_alignment, file_alignment


def _rva_to_offset(sections: Sequence[PeSection], rva: int) -> int:
    value = int(rva)
    for section in sections:
        delta = value - section.virtual_address
        if 0 <= delta < section.raw_size:
            return section.raw_offset + delta
    raise ValueError("resource RVA 0x%x is not backed by a PE section" % value)


def _parse_resource_leaves(data: bytes) -> Tuple[List[ResourceLeaf], PeSection, int, int, int]:
    optional_offset, sections, resource_rva, section_alignment, file_alignment = (
        _parse_pe_layout(data)
    )
    resource_offset = _rva_to_offset(sections, resource_rva)
    resource_section = next(
        (
            section
            for section in sections
            if section.virtual_address <= resource_rva
            < section.virtual_address + max(section.virtual_size, section.raw_size)
        ),
        None,
    )
    if resource_section is None:
        raise ValueError("resource directory has no owning PE section")

    leaves: List[ResourceLeaf] = []
    visited = set()

    def read_name(is_string: bool, value: int) -> Union[int, str]:
        if not is_string:
            return value
        name_offset = resource_offset + value
        length = _u16(data, name_offset, "resource name length")
        _require_range(data, name_offset + 2, length * 2, "resource name")
        return data[name_offset + 2 : name_offset + 2 + length * 2].decode(
            "utf-16le", errors="strict"
        )

    def walk(relative_offset: int, depth: int, path: List[Union[int, str]]) -> None:
        if depth > 2:
            raise ValueError("resource tree is deeper than type/id/language")
        visit_key = (relative_offset, depth)
        if visit_key in visited:
            raise ValueError("resource directory cycle detected")
        visited.add(visit_key)

        directory_offset = resource_offset + relative_offset
        _require_range(data, directory_offset, 16, "resource directory")
        named_count, id_count = struct.unpack_from("<HH", data, directory_offset + 12)
        entry_count = named_count + id_count
        entries_offset = directory_offset + 16
        _require_range(data, entries_offset, entry_count * 8, "resource entries")
        for index in range(entry_count):
            name_value, child_value = struct.unpack_from(
                "<II", data, entries_offset + index * 8
            )
            name = read_name(bool(name_value & 0x80000000), name_value & 0x7FFFFFFF)
            if depth == 0 and isinstance(name, int):
                name = _RT_NAMES.get(name, "TYPE_%d" % name)
            child_is_directory = bool(child_value & 0x80000000)
            child_relative = child_value & 0x7FFFFFFF
            child_path = path + [name]
            if child_is_directory:
                walk(child_relative, depth + 1, child_path)
                continue
            if depth != 2:
                raise ValueError("resource data entry appeared before language level")
            entry_offset = resource_offset + child_relative
            _require_range(data, entry_offset, 16, "resource data entry")
            data_rva, data_size = struct.unpack_from("<II", data, entry_offset)
            data_offset = _rva_to_offset(sections, data_rva)
            _require_range(data, data_offset, data_size, "resource payload")
            leaves.append(
                ResourceLeaf(
                    type_name=str(child_path[0]),
                    resource_id=child_path[1],
                    language_id=child_path[2],
                    entry_offset=entry_offset,
                    data_rva=data_rva,
                    data_size=data_size,
                    data_offset=data_offset,
                )
            )

    walk(0, 0, [])
    return leaves, resource_section, optional_offset, section_alignment, file_alignment


def _dstr_leaves_by_id(data: bytes) -> Tuple[Dict[int, ResourceLeaf], List[ResourceLeaf], PeSection, int, int, int]:
    leaves, resource_section, optional_offset, section_alignment, file_alignment = (
        _parse_resource_leaves(data)
    )
    result: Dict[int, ResourceLeaf] = {}
    for leaf in leaves:
        if leaf.type_name != "FILE" or not isinstance(leaf.resource_id, int):
            continue
        resource_id = int(leaf.resource_id)
        if resource_id in result:
            raise ValueError("FILE/%d has more than one language leaf" % resource_id)
        result[resource_id] = leaf
    return (
        result,
        leaves,
        resource_section,
        optional_offset,
        section_alignment,
        file_alignment,
    )


def _decrypt_sc01(blob: bytes) -> bytes:
    if len(blob) < 12 or blob[:4] != b"SC01":
        raise ValueError("DSTR resource is not an SC01 blob")
    payload_size = _u32(blob, 4, "SC01 payload size")
    if payload_size != len(blob) - 8 or payload_size % 4:
        raise ValueError(
            "SC01 payload size mismatch: header=%d actual=%d"
            % (payload_size, len(blob) - 8)
        )
    dword_count = payload_size // 4
    cipher = struct.unpack_from("<%dI" % dword_count, blob, 8)
    previous = cipher[0]
    plain_words: List[int] = []
    for word in cipher:
        plain_words.append(word ^ previous)
        previous = word
    plain = struct.pack("<%dI" % dword_count, *plain_words)
    if plain[:4] != b"\x00\x00\x00\x00":
        raise ValueError("SC01 plaintext is missing its zero prefix")
    return plain


def _encrypt_sc01(plain: bytes) -> bytes:
    source = bytes(plain)
    if source[:4] != b"\x00\x00\x00\x00":
        raise ValueError("SC01 plaintext must start with four zero bytes")
    if len(source) % 4:
        source += b"\x00" * (4 - len(source) % 4)
    plain_words = struct.unpack("<%dI" % (len(source) // 4), source)
    cipher_words = [SC01_SEED]
    for index in range(1, len(plain_words)):
        cipher_words.append(plain_words[index] ^ cipher_words[index - 1])
    payload = struct.pack("<%dI" % len(cipher_words), *cipher_words)
    return b"SC01" + struct.pack("<I", len(payload)) + payload


def _plain_to_text(plain: bytes) -> str:
    if plain[:4] != b"\x00\x00\x00\x00":
        raise ValueError("DSTR plaintext is missing its zero prefix")
    return plain[4:].rstrip(b"\x00").decode("utf-8", errors="strict")


def _text_to_plain(text: str) -> bytes:
    return b"\x00\x00\x00\x00" + str(text).encode("utf-8")


def _parse_entries(text: str) -> Dict[int, str]:
    entries: Dict[int, str] = {}
    for match in _ENTRY_RE.finditer(text):
        entry_id = int(match.group(1), 10)
        if entry_id in entries:
            raise ValueError("DSTR contains duplicate entry id %d" % entry_id)
        entries[entry_id] = match.group(2)
    return entries


def _replace_entry(text: str, entry_id: int, value: str) -> str:
    replacement = str(value)
    if "\r" in replacement or "\n" in replacement:
        raise ValueError("DSTR replacement must remain one native tooltip row")
    pattern = re.compile(
        r"(?m)^(%d>)[^\r\n]*(?=\r?$)" % int(entry_id)
    )
    updated, count = pattern.subn(
        lambda match: match.group(1) + replacement,
        text,
    )
    if count != 1:
        raise ValueError(
            "DSTR entry %d occurrence count must be one, got %d"
            % (int(entry_id), count)
        )
    return updated


def load_dstr_entries(data: bytes, resource_id: int) -> Dict[int, str]:
    leaves, _all, _section, _optional, _section_align, _file_align = (
        _dstr_leaves_by_id(data)
    )
    leaf = leaves.get(int(resource_id))
    if leaf is None:
        raise ValueError("dstr.dat is missing FILE/%d" % int(resource_id))
    blob = data[leaf.data_offset : leaf.data_offset + leaf.data_size]
    return _parse_entries(_plain_to_text(_decrypt_sc01(blob)))


def _resource_capacity(
    leaf: ResourceLeaf,
    all_leaves: Sequence[ResourceLeaf],
    resource_section: PeSection,
) -> int:
    section_end = resource_section.raw_offset + resource_section.raw_size
    if not resource_section.raw_offset <= leaf.data_offset < section_end:
        raise ValueError("DSTR FILE resource is outside .rsrc")
    next_offsets = sorted(
        {
            other.data_offset
            for other in all_leaves
            if leaf.data_offset < other.data_offset < section_end
        }
    )
    next_offset = next_offsets[0] if next_offsets else section_end
    return next_offset - leaf.data_offset


def _set_resource_data_entry(
    data: bytearray,
    leaf: ResourceLeaf,
    data_rva: int,
    data_size: int,
) -> None:
    struct.pack_into("<II", data, leaf.entry_offset, int(data_rva), int(data_size))


def rewrite_dstr_entry(
    source_data: bytes,
    entry_id: int,
    value: str,
    resource_ids: Sequence[int] = DSTR_RESOURCE_IDS,
) -> DstrRewriteResult:
    """Return a verified PE image with one logical DSTR entry replaced."""
    source = bytes(source_data)
    (
        leaves_by_id,
        all_leaves,
        resource_section,
        optional_offset,
        section_alignment,
        file_alignment,
    ) = _dstr_leaves_by_id(source)
    required_ids = tuple(int(resource_id) for resource_id in resource_ids)
    missing = [resource_id for resource_id in required_ids if resource_id not in leaves_by_id]
    if missing:
        raise ValueError("dstr.dat is missing FILE resources %r" % missing)

    grouped: Dict[Tuple[int, int, int], List[int]] = {}
    offset_sizes: Dict[int, int] = {}
    before_entries: Dict[int, Dict[int, str]] = {}
    for resource_id in required_ids:
        leaf = leaves_by_id[resource_id]
        previous_size = offset_sizes.setdefault(leaf.data_offset, leaf.data_size)
        if previous_size != leaf.data_size:
            raise ValueError("aliased DSTR resources disagree on payload size")
        grouped.setdefault(
            (leaf.data_offset, leaf.data_rva, leaf.data_size), []
        ).append(resource_id)
        before_entries[resource_id] = load_dstr_entries(source, resource_id)
        if int(entry_id) not in before_entries[resource_id]:
            raise ValueError(
                "FILE/%d is missing DSTR entry %d" % (resource_id, int(entry_id))
            )

    data = bytearray(source)
    changed_ids: List[int] = []
    declared_section_end = resource_section.raw_offset + resource_section.raw_size
    if declared_section_end < len(source):
        raise ValueError(
            ".rsrc must be the final file section for transactional DSTR growth"
        )
    # The actual file may omit unused trailing alignment bytes while all DSTR
    # payloads remain readable.  New payloads are appended from the real end.
    section_end = min(declared_section_end, len(source))
    append_cursor = section_end

    for group_key, group_ids in grouped.items():
        data_offset, data_rva, data_size = group_key
        original_blob = source[data_offset : data_offset + data_size]
        original_text = _plain_to_text(_decrypt_sc01(original_blob))
        updated_text = _replace_entry(original_text, int(entry_id), str(value))
        if updated_text == original_text:
            continue
        updated_blob = _encrypt_sc01(_text_to_plain(updated_text))
        representative = leaves_by_id[group_ids[0]]
        capacity = _resource_capacity(representative, all_leaves, resource_section)
        if len(updated_blob) <= capacity:
            target_offset = data_offset
            target_rva = data_rva
            clear_size = max(data_size, len(updated_blob))
            data[target_offset : target_offset + clear_size] = b"\x00" * clear_size
            data[target_offset : target_offset + len(updated_blob)] = updated_blob
        else:
            append_cursor = _align_up(append_cursor, 4)
            target_offset = append_cursor
            target_rva = resource_section.virtual_address + (
                target_offset - resource_section.raw_offset
            )
            end_offset = target_offset + len(updated_blob)
            if end_offset > len(data):
                data.extend(b"\x00" * (end_offset - len(data)))
            data[target_offset:end_offset] = updated_blob
            append_cursor = end_offset
        for resource_id in group_ids:
            _set_resource_data_entry(
                data,
                leaves_by_id[resource_id],
                target_rva,
                len(updated_blob),
            )
            changed_ids.append(resource_id)

    if append_cursor > section_end:
        new_raw_size = _align_up(
            append_cursor - resource_section.raw_offset,
            file_alignment,
        )
        new_file_size = resource_section.raw_offset + new_raw_size
        if new_file_size > len(data):
            data.extend(b"\x00" * (new_file_size - len(data)))
        new_virtual_size = max(
            resource_section.virtual_size,
            append_cursor - resource_section.raw_offset,
        )
        struct.pack_into(
            "<I", data, resource_section.header_offset + 8, new_virtual_size
        )
        struct.pack_into(
            "<I", data, resource_section.header_offset + 16, new_raw_size
        )
        new_image_size = _align_up(
            resource_section.virtual_address + new_virtual_size,
            section_alignment,
        )
        struct.pack_into("<I", data, optional_offset + 56, new_image_size)
        struct.pack_into("<I", data, optional_offset + 116, new_virtual_size)

    output = bytes(data)
    for resource_id in required_ids:
        actual = load_dstr_entries(output, resource_id)
        expected = dict(before_entries[resource_id])
        expected[int(entry_id)] = str(value)
        if actual != expected:
            raise ValueError(
                "FILE/%d verification changed entries outside DSTR %d"
                % (resource_id, int(entry_id))
            )
    return DstrRewriteResult(
        data=output,
        changed_resource_ids=tuple(sorted(changed_ids)),
    )


def rewrite_dstr_file_entry(
    path: Path,
    entry_id: int,
    value: str,
    resource_ids: Sequence[int] = DSTR_RESOURCE_IDS,
) -> DstrRewriteResult:
    """Rewrite one entry through a verified same-directory temporary file."""
    target = Path(path).resolve()
    source = target.read_bytes()
    result = rewrite_dstr_entry(source, entry_id, value, resource_ids)
    if result.data == source:
        return result

    target_stat = target.stat()
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".%s." % target.name,
            suffix=".tmp",
            dir=str(target.parent),
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(result.data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(str(temporary_path), stat.S_IMODE(target_stat.st_mode))
        written = temporary_path.read_bytes()
        if written != result.data:
            raise OSError("temporary dstr.dat verification did not match written bytes")
        for resource_id in resource_ids:
            entries = load_dstr_entries(written, int(resource_id))
            if entries.get(int(entry_id)) != str(value):
                raise ValueError(
                    "temporary FILE/%d did not retain DSTR %d"
                    % (int(resource_id), int(entry_id))
                )
        os.replace(str(temporary_path), str(target))
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass

    if target.read_bytes() != result.data:
        raise OSError("committed dstr.dat does not match the verified image")
    return result


def format_adventure_buff_tooltip(attributes: object) -> str:
    values = (
        int(getattr(attributes, "strength", 0)),
        int(getattr(attributes, "vitality", 0)),
        int(getattr(attributes, "intelligence", 0)),
        int(getattr(attributes, "spirit", 0)),
    )
    if any(value < 0 for value in values):
        raise ValueError("adventure-group tooltip attributes must be non-negative")
    return "力量+%d 体力+%d 智力+%d 精神+%d" % values


def _commit_adventure_buff_tooltip(
    target: Path,
    account_id: int,
    adventure_level: int,
    tooltip_text: str,
) -> AdventureBuffDstrSyncResult:
    before = target.read_bytes()
    before_sha256 = hashlib.sha256(before).hexdigest()
    rewrite_dstr_file_entry(
        target,
        ADVENTURE_TOOLTIP_DSTR_ID,
        tooltip_text,
        DSTR_RESOURCE_IDS,
    )
    after = target.read_bytes()
    after_sha256 = hashlib.sha256(after).hexdigest()
    for resource_id in DSTR_RESOURCE_IDS:
        entries = load_dstr_entries(after, resource_id)
        if entries.get(ADVENTURE_TOOLTIP_DSTR_ID) != tooltip_text:
            raise ValueError(
                "committed FILE/%d has the wrong adventure tooltip" % resource_id
            )
    return AdventureBuffDstrSyncResult(
        path=target,
        account_id=int(account_id),
        adventure_level=int(adventure_level),
        tooltip_text=tooltip_text,
        changed=before != after,
        before_sha256=before_sha256,
        after_sha256=after_sha256,
    )


def _calculate_adventure_buff_tooltip(
    repository: object,
    account_id: int,
) -> Tuple[int, str]:
    aid = int(account_id)
    if aid <= 0:
        raise ValueError("adventure-group tooltip account id must be positive")
    progression = calculate_adventure_group_for_account(repository, aid)
    return (
        int(progression.adventure_level),
        format_adventure_buff_tooltip(progression.attributes),
    )


def sync_adventure_buff_tooltip(
    dstr_path: Path,
    repository: object,
    account_id: int,
) -> AdventureBuffDstrSyncResult:
    """Project the current account's warmed PVF attributes into DSTR 31514."""
    target = Path(dstr_path).resolve()
    aid = int(account_id)
    adventure_level, tooltip_text = _calculate_adventure_buff_tooltip(
        repository,
        aid,
    )
    return _commit_adventure_buff_tooltip(
        target,
        aid,
        adventure_level,
        tooltip_text,
    )


def sync_adventure_buff_tooltip_or_default(
    dstr_path: Path,
    repository: object,
    account_id: int,
) -> AdventureBuffDstrSyncResult:
    """Best-effort DSTR projection that preserves the client's own text on error."""
    target = Path(dstr_path).resolve()
    aid = int(account_id)
    adventure_level, tooltip_text = _calculate_adventure_buff_tooltip(
        repository,
        aid,
    )
    try:
        return _commit_adventure_buff_tooltip(
            target,
            aid,
            adventure_level,
            tooltip_text,
        )
    except Exception as exc:
        return AdventureBuffDstrSyncResult(
            path=target,
            account_id=aid,
            adventure_level=adventure_level,
            tooltip_text=tooltip_text,
            changed=False,
            before_sha256="",
            after_sha256="",
            used_client_default=True,
            sync_error="%s: %s" % (type(exc).__name__, exc),
        )


__all__ = [
    "ADVENTURE_TOOLTIP_DSTR_ID",
    "DSTR_RESOURCE_IDS",
    "AdventureBuffDstrSyncResult",
    "DstrRewriteResult",
    "format_adventure_buff_tooltip",
    "load_dstr_entries",
    "rewrite_dstr_entry",
    "rewrite_dstr_file_entry",
    "sync_adventure_buff_tooltip",
    "sync_adventure_buff_tooltip_or_default",
]
