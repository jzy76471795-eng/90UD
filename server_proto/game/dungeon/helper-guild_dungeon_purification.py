"""PVF-backed guild-dungeon purification state for the Stage3 selector."""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Mapping, Tuple

from pvf import pvf_runtime


GUILD_DUNGEON_WORLDMAP_ID = 37
_STYLE_LIST_PATH = "etc/guild/guilddungeon/guilddungeonstyle.lst"
_STYLE_DIRECTORY = "etc/guild/guilddungeon"
_TOKEN = re.compile(r"`([^`]*)`|([^\s`]+)")


def _tokens(text: str) -> Tuple[str, ...]:
    values = []
    for quoted, plain in _TOKEN.findall(str(text or "")):
        value = (quoted if quoted != "" else plain).strip()
        if value:
            values.append(value)
    return tuple(values)


def _normalize_style_path(path: str) -> str:
    value = str(path or "").replace("\\", "/").strip("/").lower()
    if not value:
        raise ValueError("guild dungeon style path is empty")
    if not value.startswith(_STYLE_DIRECTORY + "/"):
        value = _STYLE_DIRECTORY + "/" + value
    return value


def parse_guild_dungeon_style_list(text: str) -> Mapping[int, str]:
    values = _tokens(text)
    if not values or len(values) % 2:
        raise ValueError("guild dungeon style list must contain id/path pairs")
    result = {}
    for offset in range(0, len(values), 2):
        try:
            style_id = int(values[offset], 0)
        except ValueError as exc:
            raise ValueError("invalid guild dungeon style id") from exc
        if style_id < 0 or style_id in result:
            raise ValueError("invalid or duplicate guild dungeon style id: %d" % style_id)
        result[style_id] = _normalize_style_path(values[offset + 1])
    return result


def _closed_blocks(text: str, name: str) -> Tuple[str, ...]:
    escaped = re.escape(str(name or ""))
    return tuple(
        match.group(1).strip()
        for match in re.finditer(
            r"(?is)\[" + escaped + r"\](.*?)\[/" + escaped + r"\]",
            str(text or ""),
        )
    )


def _section_body(text: str, name: str) -> str:
    escaped = re.escape(str(name or ""))
    closed = re.search(
        r"(?is)\[" + escaped + r"\](.*?)\[/" + escaped + r"\]",
        str(text or ""),
    )
    if closed is not None:
        return closed.group(1).strip()
    opened = re.search(
        r"(?is)\[" + escaped + r"\](.*?)(?=\r?\n\s*\[|\Z)",
        str(text or ""),
    )
    return opened.group(1).strip() if opened is not None else ""


def _section_ints(text: str, name: str) -> Tuple[int, ...]:
    return tuple(
        int(value, 0)
        for value in _tokens(_section_body(text, name))
        if re.fullmatch(r"-?(?:0[xX][0-9a-fA-F]+|\d+)", value)
    )


def _required_int(text: str, name: str) -> int:
    values = _section_ints(text, name)
    if len(values) != 1:
        raise ValueError("guild dungeon [%s] must contain one integer" % name)
    return int(values[0])


@dataclass(frozen=True)
class GuildDungeonPurificationRow:
    dungeon_id: int
    gauge_limit: int
    gauge_on_clear: int
    conquest_gauge_ratio: int
    prerequisites: Tuple[int, ...]

    @property
    def full_purification_clear_count(self) -> int:
        return (int(self.gauge_limit) + int(self.gauge_on_clear) - 1) // int(
            self.gauge_on_clear
        )


@dataclass(frozen=True)
class GuildDungeonPurificationConfig:
    style_id: int
    path: str
    final_dungeon_ids: Tuple[int, ...]
    rows: Tuple[GuildDungeonPurificationRow, ...]

    def full_clear_counts(self) -> Tuple[Tuple[int, int], ...]:
        return tuple(
            (row.dungeon_id, row.full_purification_clear_count)
            for row in self.rows
        )


def parse_guild_dungeon_purification_config(
    style_id: int,
    path: str,
    text: str,
) -> GuildDungeonPurificationConfig:
    rows = []
    seen = set()
    for block in _closed_blocks(text, "dungeon"):
        dungeon_id = _required_int(block, "dungeon index")
        gauge_limit = _required_int(block, "gauge limit")
        gauge_on_clear = _required_int(block, "gauge on clear")
        conquest_ratio = _required_int(block, "conquest gauge ratio")
        prerequisites = tuple(
            value
            for value in _section_ints(block, "required dungeon conquest")
            if value > 0
        )
        if dungeon_id <= 0 or dungeon_id in seen:
            raise ValueError("invalid or duplicate guild dungeon id: %d" % dungeon_id)
        if gauge_limit <= 0 or gauge_on_clear <= 0:
            raise ValueError("guild dungeon %d has invalid gauge values" % dungeon_id)
        if not 0 <= conquest_ratio <= 100:
            raise ValueError("guild dungeon %d has invalid conquest ratio" % dungeon_id)
        if dungeon_id in prerequisites or len(prerequisites) != len(set(prerequisites)):
            raise ValueError("guild dungeon %d has invalid prerequisites" % dungeon_id)
        seen.add(dungeon_id)
        rows.append(
            GuildDungeonPurificationRow(
                dungeon_id=dungeon_id,
                gauge_limit=gauge_limit,
                gauge_on_clear=gauge_on_clear,
                conquest_gauge_ratio=conquest_ratio,
                prerequisites=prerequisites,
            )
        )
    if not rows:
        raise ValueError("guild dungeon style has no [dungeon] rows")
    missing = sorted(
        prerequisite
        for row in rows
        for prerequisite in row.prerequisites
        if prerequisite not in seen
    )
    if missing:
        raise ValueError("guild dungeon prerequisites are missing: %r" % missing)
    final_ids = tuple(
        value
        for value in _section_ints(_section_body(text, "weekly reward"), "final dungeon")
        if value > 0
    )
    if not final_ids or any(value not in seen for value in final_ids):
        raise ValueError("guild dungeon final dungeon list is invalid")
    return GuildDungeonPurificationConfig(
        style_id=int(style_id),
        path=_normalize_style_path(path),
        final_dungeon_ids=final_ids,
        rows=tuple(rows),
    )


@lru_cache(maxsize=1)
def resolve_guild_dungeon_purification_config() -> GuildDungeonPurificationConfig:
    styles = parse_guild_dungeon_style_list(pvf_runtime.read_text(_STYLE_LIST_PATH))
    if len(styles) != 1:
        raise RuntimeError(
            "current guild dungeon style selection is ambiguous: %r"
            % sorted(styles)
        )
    style_id, path = next(iter(styles.items()))
    return parse_guild_dungeon_purification_config(
        style_id,
        path,
        pvf_runtime.read_text(path),
    )


def warmup_guild_dungeon_purification_index() -> dict[str, int]:
    config = resolve_guild_dungeon_purification_config()
    return {
        "guild_dungeon_style_id": int(config.style_id),
        "guild_dungeon_count": len(config.rows),
        "guild_dungeon_prerequisite_count": sum(
            len(row.prerequisites) for row in config.rows
        ),
        "guild_dungeon_final_count": len(config.final_dungeon_ids),
    }
