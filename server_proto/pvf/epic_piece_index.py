# -*- coding: utf-8 -*-
"""PVF-backed Epic Book fragment and crafting recipe index.

The index only models ``etc/epicpieceinfo.etc``.  It deliberately contains no
wire serialization: the verified account-balance response lives in
``game.character.epic_book``; ITEM_DICTIONARY and EPIC_BOOK_MAKE_ITEM remain
gated on their separate Stage3 reader and native-vector evidence.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from pvf import pvf_runtime


EPIC_PIECE_INFO_PATH = "etc/epicpieceinfo.etc"
EQUIPMENT_LST_PATH = "equipment/equipment.lst"
STACKABLE_LST_PATH = "stackable/stackable.lst"
_EQUIPMENT_LST_ENTRY_RE = re.compile(r"(\d+)\s+`([^`]*)`")


def _extract_int_field(text: str, name: str) -> int:
    tag = "[%s]" % name
    tag_match = re.search(re.escape(tag), text, flags=re.IGNORECASE | re.ASCII)
    if tag_match is None:
        return 0
    body = text[tag_match.end():]
    end = body.find("[")
    if end >= 0:
        body = body[:end]
    match = re.search(r"-?\d+", body)
    return int(match.group(0)) if match else 0


def _equipment_paths() -> Dict[int, str]:
    paths: Dict[int, str] = {}
    text = pvf_runtime.read_text(EQUIPMENT_LST_PATH)
    for match in _EQUIPMENT_LST_ENTRY_RE.finditer(text):
        item_id = int(match.group(1))
        relative = (match.group(2) or "").replace("\\", "/").strip().lstrip("/")
        if item_id <= 0 or not relative:
            continue
        paths[item_id] = (
            relative
            if relative.lower().startswith("equipment/")
            else "equipment/" + relative
        )
    if not paths:
        raise RuntimeError("equipment.lst empty")
    return paths


def _stackable_item_ids() -> set[int]:
    item_ids = {
        int(match.group(1))
        for match in _EQUIPMENT_LST_ENTRY_RE.finditer(
            pvf_runtime.read_text(STACKABLE_LST_PATH)
        )
        if int(match.group(1)) > 0
    }
    if not item_ids:
        raise RuntimeError("stackable.lst empty")
    return item_ids


def _extract_tagged_block(text: str, name: str) -> str:
    open_tag = "[%s]" % name
    close_tag = "[/%s]" % name
    open_match = re.search(
        re.escape(open_tag), text, flags=re.IGNORECASE | re.ASCII
    )
    if open_match is None:
        return ""
    start = open_match.end()
    close_match = re.search(
        re.escape(close_tag), text[start:], flags=re.IGNORECASE | re.ASCII
    )
    return text[start:] if close_match is None else text[start:start + close_match.start()]


def _parse_ints(text: str) -> list[int]:
    cleaned = re.sub(r"//.*?$", "", text or "", flags=re.MULTILINE)
    values: list[int] = []
    for token in cleaned.replace("\r", " ").replace("\n", " ").split():
        try:
            values.append(int(token, 10))
        except ValueError:
            pass
    return values


@dataclass(frozen=True)
class EpicPieceRecipe:
    output_item_id: int
    piece_id: int
    piece_count: int
    materials: Tuple[Tuple[int, int], ...]


@dataclass(frozen=True)
class EpicPieceDropEntry:
    """A fragment together with the actual level of the equipment it crafts."""

    piece_id: int
    output_item_id: int
    output_level: int
    weight: int


class EpicPieceIndex:
    def __init__(self) -> None:
        self.piece_by_equipment: Dict[int, int] = {}
        self.recipe_by_output: Dict[int, EpicPieceRecipe] = {}
        self.pieces_by_output_level: Dict[int, Tuple[EpicPieceDropEntry, ...]] = {}
        self.registered_piece_count = 0
        self.loaded = False

    def load_from_text(self, text: str) -> None:
        drop_block = _extract_tagged_block(text, "equipment piece drop info")
        piece_values = _parse_ints(_extract_tagged_block(drop_block, "piece list"))
        if not piece_values or len(piece_values) % 2:
            raise RuntimeError("epicpieceinfo [piece list] pair shape invalid")
        self.piece_by_equipment = {
            int(piece_values[index]): int(piece_values[index + 1])
            for index in range(0, len(piece_values), 2)
            if int(piece_values[index]) > 0 and int(piece_values[index + 1]) > 0
        }
        recipes: Dict[int, EpicPieceRecipe] = {}
        recipe_block = _extract_tagged_block(text, "equipment recipe info")
        pattern = re.compile(
            r"\[info\]\s*\[output\]\s*(-?\d+)\s*\[recipe\](.*?)\[/recipe\]\s*\[/info\]",
            re.IGNORECASE | re.DOTALL,
        )
        for match in pattern.finditer(recipe_block):
            output = int(match.group(1))
            values = _parse_ints(match.group(2))
            if output <= 0 or len(values) < 2 or len(values) % 2:
                raise RuntimeError("epicpieceinfo recipe shape output=%d" % output)
            pairs = tuple(
                (int(values[index]), int(values[index + 1]))
                for index in range(0, len(values), 2)
            )
            if any(item_id <= 0 or count <= 0 for item_id, count in pairs):
                raise RuntimeError("epicpieceinfo recipe value output=%d" % output)
            expected_piece = self.piece_by_equipment.get(output)
            if expected_piece is None or pairs[0][0] != expected_piece:
                raise RuntimeError("epicpieceinfo recipe piece mismatch output=%d" % output)
            recipes[output] = EpicPieceRecipe(
                output_item_id=output,
                piece_id=pairs[0][0],
                piece_count=pairs[0][1],
                materials=pairs[1:],
            )
        if not self.piece_by_equipment or not recipes:
            raise RuntimeError("epicpieceinfo empty")
        if set(recipes) != set(self.piece_by_equipment):
            raise RuntimeError("epicpieceinfo mapping/recipe coverage mismatch")
        paths = _equipment_paths()
        registered_item_ids = set(paths)
        registered_item_ids.update(_stackable_item_ids())
        by_level: Dict[int, List[EpicPieceDropEntry]] = {}
        for output_item_id, piece_id in self.piece_by_equipment.items():
            path = paths.get(int(output_item_id))
            if not path:
                raise RuntimeError("epicpieceinfo output missing equipment.lst id=%d" % output_item_id)
            if int(piece_id) not in registered_item_ids:
                raise RuntimeError(
                    "epicpieceinfo piece missing equipment/stackable.lst id=%d output=%d"
                    % (piece_id, output_item_id)
                )
            try:
                equipment = pvf_runtime.read_text(path)
            except Exception as exc:
                raise RuntimeError(
                    "epicpieceinfo output read failed id=%d" % output_item_id
                ) from exc
            output_level = _extract_int_field(equipment, "minimum level")
            rarity = _extract_int_field(equipment, "rarity")
            if output_level <= 0:
                raise RuntimeError(
                    "epicpieceinfo output missing minimum level id=%d" % output_item_id
                )
            if rarity != 4:
                raise RuntimeError(
                    "epicpieceinfo output is not epic id=%d rarity=%d"
                    % (output_item_id, rarity)
                )
            by_level.setdefault(output_level, []).append(
                EpicPieceDropEntry(
                    piece_id=int(piece_id),
                    output_item_id=int(output_item_id),
                    output_level=int(output_level),
                    weight=max(1, _extract_int_field(equipment, "creation rate")),
                )
            )
        self.recipe_by_output = recipes
        self.pieces_by_output_level = {
            int(level): tuple(entries)
            for level, entries in by_level.items()
        }
        self.registered_piece_count = sum(
            len(entries) for entries in self.pieces_by_output_level.values()
        )
        self.loaded = True

    def piece_for_equipment(self, item_id: int) -> int:
        return int(self.piece_by_equipment.get(int(item_id), 0))

    def recipe_for_output(self, item_id: int) -> EpicPieceRecipe | None:
        return self.recipe_by_output.get(int(item_id))

    def entries_for_output_levels(
        self, output_levels: Sequence[int]
    ) -> Tuple[EpicPieceDropEntry, ...]:
        entries: List[EpicPieceDropEntry] = []
        for level in sorted({int(value) for value in output_levels if int(value) > 0}):
            entries.extend(self.pieces_by_output_level.get(level, ()))
        return tuple(entries)

    def choose_piece(
        self,
        lcg: object,
        *,
        output_levels: Sequence[int],
        preferred_output_item_ids: Sequence[int] = (),
        preferred_weight: int = 1,
    ) -> EpicPieceDropEntry | None:
        """Pick directly from fragments whose crafted equipment matches a level tier."""
        candidates = self.entries_for_output_levels(output_levels)
        if not candidates:
            return None
        preferred = {
            int(item_id)
            for item_id in preferred_output_item_ids
            if int(item_id) > 0
        }
        multiplier = max(1, int(preferred_weight))

        def _weight(entry: EpicPieceDropEntry) -> int:
            value = max(1, int(entry.weight))
            if int(entry.output_item_id) in preferred:
                value *= multiplier
            return value

        total = sum(_weight(entry) for entry in candidates)
        if total <= 0:
            return None
        roll = int(getattr(lcg, "next")(total))
        cursor = 0
        for entry in candidates:
            cursor += _weight(entry)
            if roll < cursor:
                return entry
        return candidates[-1]


_LOCK = threading.RLock()
_INDEX: EpicPieceIndex | None = None


def get_epic_piece_index() -> EpicPieceIndex:
    with _LOCK:
        if _INDEX is None or not _INDEX.loaded:
            raise RuntimeError("EpicPieceIndex not warmed; call warmup first")
        return _INDEX


def warmup_epic_piece_index(*, force: bool = False) -> dict:
    global _INDEX
    with _LOCK:
        if not force and _INDEX is not None and _INDEX.loaded:
            index = _INDEX
        else:
            index = EpicPieceIndex()
            index.load_from_text(pvf_runtime.read_text(EPIC_PIECE_INFO_PATH))
            _INDEX = index
    return {
        "ok": 1,
        "path": EPIC_PIECE_INFO_PATH,
        "equipment_count": len(index.piece_by_equipment),
        "recipe_count": len(index.recipe_by_output),
        "drop_level_count": len(index.pieces_by_output_level),
        "drop_piece_count": sum(
            len(entries) for entries in index.pieces_by_output_level.values()
        ),
        "registered_piece_count": index.registered_piece_count,
    }


def clear_epic_piece_index() -> None:
    global _INDEX
    with _LOCK:
        _INDEX = None
