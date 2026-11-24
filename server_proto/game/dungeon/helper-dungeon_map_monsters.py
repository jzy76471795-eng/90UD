"""Load dungeon map actors from shared PVF.

Stage3 / friend doc section 4.6 + S4A12 ``Dungeon.ParseMapActors`` +
90qiyuan ``currentDungeonAICharacterHostile`` / ``monsterSpawnBlocksRoom``.

Runtime uses ``pvf_runtime`` only. 除 ``[monster]`` 外，通用附带：

- ``[ai character]``：任务 / 剧情 APC（含 hunt enemy enemyType=2）
  - 阵营 ``[monster]`` = **敌人**（当怪物：可击杀 / 挡门清房 / 计任务 / 可给经验；
    BOSS 型 APC 须击败才通关结算）
  - 阵营 ``[character]`` = **队友**（刷出但不敌对、不挡门；不给经验/任务 residual）
  - 阵营 ``[neutral]`` = 中立（不敌对、不挡门；不给经验/任务 residual）
- ``[monster team]``：与 ``[monster]`` 位置对齐；team=0 为剧情友方怪（不挡门）
- ``[special passive object]`` / ``[passive object]``：地图中的被动对象。
  这些段落通常只是客户端静态布景，默认绝不写入 ``001D``；只有当前活跃任务
  明确要求 ``hunt enemy`` type=3 的同一对象时，才作为任务建筑生成。
- **剧情 face 预装**（实验性，**默认关闭**）：曾假设 001D 缺发言 mon 导致无头像；
  2026-07-21 live 证伪（多友方怪可见 + 头像仍无）。代码保留
  ``inject_story_face_preloads`` 供窄 A/B，日常勿开。

``[NPC]`` 段在 S4A12 仅计 NpcCount，不进 001D mon 表（客户端读地图脚本）。
客户端敌友表现以 map 脚本 faction 为准；服务端必须按同字段分流战斗/任务逻辑。
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Mapping, Sequence, Tuple

from pvf import pvf_runtime

_MONSTER_BLOCK = re.compile(r"(?is)\[monster\](.*?)\[/monster\]")
_MONSTER_CONDITION_BLOCK = re.compile(
    r"(?is)`?\[monster\s+condition\]`?(.*?)"
    r"`?\[/monster\s+condition\]`?"
)
_CONDITIONAL_SUMMON_BLOCK = re.compile(
    r"(?is)`?\[conditional\s+summon\s+monster\]`?(.*?)"
    r"`?\[/conditional\s+summon\s+monster\]`?"
)
_AI_BLOCK = re.compile(r"(?is)\[ai character\](.*?)\[/ai character\]")
_SPECIAL_BLOCK = re.compile(
    r"(?is)\[special passive object\](.*?)\[/special passive object\]"
)
_HELLPARTY_INLINE_BLOCK = re.compile(
    r"(?is)" + re.escape(chr(96)) + r"?\[hellparty\]"
    + re.escape(chr(96)) + r"?.*?"
    + re.escape(chr(96)) + r"?\[/hellparty\]"
    + re.escape(chr(96)) + r"?"
)
# 普通可破坏物：与 special 分开；90qiyuan parsePassiveObjects 4 元组 code x y flags
_PASSIVE_BLOCK = re.compile(r"(?is)\[passive object\](.*?)\[/passive object\]")
# 90qiyuan: [monster team] 与 [monster] 位置一一对应；无闭合标签时读到下一 section
_MONSTER_TEAM_BLOCK = re.compile(
    r"(?is)\[monster team\](.*?)(?=\n\s*\[|/monster team\]|\Z)"
)
_MAP_LST_PAIR = re.compile(r"(?m)^\s*(\d+)\s+`?([^`\r\n]+)`?")
_AI_LST_PAIR = re.compile(r"(?m)^\s*(\d+)\s+`?([^`\r\n]+)`?")
_SECTION = re.compile(r"(?is)\[minimum info\](.*?)(?=\n\s*\[|\Z)")
_APPEARANCE_POINT_SECTION = re.compile(
    r"(?is)\[appearance point\](.*?)(?=\n\s*\[|\Z)"
)
_DEATH_TOWER_ITEM_SECTION = re.compile(
    r"(?is)\[death tower item\](.*?)(?=\n\s*\[|\Z)"
)

# S4A12 Dungeon.cs
_SPECIAL_PASSIVE_WIRE_TYPE = 9  # StartMapSpecialPassiveObjectType
# S4A12 QuestDropProvider / hunt enemy domain（与 wire mon rank 不同命名空间）
_QUEST_ENEMY_MONSTER = 1
_QUEST_ENEMY_AI = 2
_QUEST_ENEMY_PASSIVE = 3

# 服务端 combat role（与 map faction / monster team 对齐）
_ROLE_ENEMY = "enemy"
_ROLE_ALLY = "ally"
_ROLE_NEUTRAL = "neutral"

# 剧情对话头像预装：
# 2026-07-21 live：默认多只注入会刷出一堆友方怪，且头像仍不显示 → **默认关闭**。
# 保留代码作窄 A/B：USDOF_DUNGEON_STORY_FACE_PRELOAD=1 且 MAX=1 仅试验。
_ENV_STORY_FACE_PRELOAD = "USDOF_DUNGEON_STORY_FACE_PRELOAD"
_ENV_STORY_FACE_PRELOAD_MAX = "USDOF_DUNGEON_STORY_FACE_PRELOAD_MAX"
_DEFAULT_STORY_FACE_PRELOAD_MAX = 1
_ENV_MAP_TEMPLATE_CACHE = "USDOF_GAME_MAP_TEMPLATE_CACHE"
_EVENT_MONSTER_POS_BLOCK = re.compile(
    r"(?is)\[event monster position\](.*?)\[/event monster position\]"
)
_FACE_IMAGE_BLOCK = re.compile(r"(?is)\[face\s*image\](.*?)(?=\n\s*\[|\Z)")
_WAITING_ACTION_BLOCK = re.compile(
    r"(?is)`?\[waiting\s+action\]`?(.*?)(?=\n\s*`?\[|\Z)"
)
_MAP_DUNGEON_BLOCK = re.compile(r"(?is)\[dungeon\](.*?)\[/dungeon\]")
_MAP_TYPE_BLOCK = re.compile(r"(?is)\[type\](.*?)(?=\n\s*\[|\Z)")
_MAP_DUNGEON_START_AREA_BLOCK = re.compile(
    r"(?is)\[dungeon\s+start\s+area\](.*?)(?=\n\s*\[|\Z)"
)

_QUEST_COMPANION_START_MAPS: dict[tuple[int, int], int] = {}
_QUEST_COMPANION_START_LOCK = threading.RLock()
_OMITTED_START_MAPS: dict[tuple[int, int, int, int], int] = {}
_OMITTED_START_MAP_LOCK = threading.RLock()
_TOWER_DUNGEON_MAP_PATH_MARKERS = ("towerofdespair", "towerofgrief")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "n", ""}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return int(default)
    try:
        return int(str(raw).strip(), 0)
    except ValueError:
        return int(default)


def _strip_tick(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == "`" and text[-1] == "`":
        return text[1:-1].strip()
    return text


def _tokenize(text: str) -> list[str]:
    toks: list[str] = []
    for a, b in re.findall(r"`([^`]*)`|(\S+)", str(text or "")):
        toks.append(a if a != "" else b)
    return toks


@lru_cache(maxsize=1)
def _map_lst() -> dict[int, str]:
    try:
        text = pvf_runtime.read_text("map/map.lst")
    except Exception:
        return {}
    out: dict[int, str] = {}
    for mid_s, rel in _MAP_LST_PAIR.findall(text or ""):
        try:
            mid = int(mid_s, 0)
        except ValueError:
            continue
        rel = str(rel or "").strip().replace("\\", "/")
        if not rel:
            continue
        if not rel.lower().startswith("map/"):
            rel = "map/" + rel.lstrip("/")
        out[mid] = rel
    return out


def resolve_map_path(map_index: int) -> str:
    mid = int(map_index or 0)
    if mid <= 0:
        return ""
    return _map_lst().get(mid, "")


def warmup_map_index() -> dict[str, int]:
    """Prime the shared-PVF map.lst index before dungeon entry handlers run."""
    companion_count = _warm_quest_companion_start_map_index()
    omitted_start_report = _warm_omitted_start_map_index()
    tower_reverse = _tower_dungeon_reverse_maps()
    return {
        "map_count": len(_map_lst()),
        "quest_companion_start_count": companion_count,
        "map_native_start_area_count": int(
            omitted_start_report.get("resolved_count") or 0
        ),
        "map_native_start_area_ambiguous_count": int(
            omitted_start_report.get("ambiguous_count") or 0
        ),
        "tower_reverse_dungeon_count": len(tower_reverse),
        "tower_reverse_map_count": sum(len(values) for values in tower_reverse.values()),
    }


def _map_declared_dungeon_ids(raw: str) -> set[int]:
    match = _MAP_DUNGEON_BLOCK.search(str(raw or ""))
    if match is None:
        return set()
    out: set[int] = set()
    for token in re.findall(r"-?\d+", match.group(1) or ""):
        value = int(token)
        if value > 0:
            out.add(value)
    return out


def _map_directory(path: str) -> str:
    normalized = str(path or "").replace("\\", "/").strip("/").lower()
    if "/" not in normalized:
        return ""
    return normalized.rsplit("/", 1)[0]


def _maze_has_exact_map_spec(maze: Any, cell_x: int, cell_y: int) -> bool:
    for spec in list(getattr(maze, "map_specs", None) or []):
        try:
            if (
                int(spec.get("x", -1)) == int(cell_x)
                and int(spec.get("y", -1)) == int(cell_y)
                and int(spec.get("map_id") or 0) > 0
            ):
                return True
        except (AttributeError, TypeError, ValueError):
            continue
    return False


def _maze_map_directories(maze: Any) -> set[str]:
    directories: set[str] = set()
    for spec in list(getattr(maze, "map_specs", None) or []):
        try:
            map_id = int(spec.get("map_id") or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        directory = _map_directory(resolve_map_path(map_id))
        if directory:
            directories.add(directory)
    return directories


def _maze_explicit_map_ids(maze: Any) -> set[int]:
    """Return every MAP resource already claimed by one maze declaration."""
    map_ids: set[int] = set()
    for spec in list(getattr(maze, "map_specs", None) or []):
        if not isinstance(spec, dict):
            continue
        values = [spec.get("map_id")]
        candidates = spec.get("candidates")
        if isinstance(candidates, (tuple, list, set)):
            values.extend(candidates)
        for value in values:
            try:
                map_id = int(value or 0)
            except (TypeError, ValueError):
                continue
            if map_id > 0:
                map_ids.add(map_id)
    return map_ids


def _select_omitted_start_map_candidate(
    candidates: Sequence[int],
    explicit_map_ids: Sequence[int],
) -> int:
    """Resolve an omitted MAP-native start only when PVF affinity is unique.

    A maze can omit its logical start cell while several same-directory MAPs
    carry ``[dungeon start area]``. Explicit MAP ids are already assigned to
    other cells and therefore cannot fill that omission. For the remaining
    candidates, map-list locality is the only PVF relation available at boot;
    select it only when one candidate is uniquely closest to the maze's
    explicit resource group. Ties remain unresolved so the established
    fallback path retains control.
    """
    explicit: set[int] = set()
    for value in explicit_map_ids:
        try:
            map_id = int(value)
        except (TypeError, ValueError):
            continue
        if map_id > 0:
            explicit.add(map_id)

    usable: set[int] = set()
    for value in candidates:
        try:
            map_id = int(value)
        except (TypeError, ValueError):
            continue
        if map_id > 0 and map_id not in explicit:
            usable.add(map_id)
    ordered = sorted(usable)
    if len(ordered) == 1:
        return ordered[0]
    if not ordered or not explicit:
        return 0

    distances = {
        map_id: min(abs(map_id - anchor_id) for anchor_id in explicit)
        for map_id in ordered
    }
    best_distance = min(distances.values())
    best = [
        map_id
        for map_id in ordered
        if distances[map_id] == best_distance
    ]
    return best[0] if len(best) == 1 else 0


def _warm_omitted_start_map_index() -> dict[str, int]:
    """Resolve PVF-native start maps omitted from DGN specifications.

    Some story dungeons declare a maze start cell but omit that cell from
    ``[map specification]``.  Their real start map is still authoritative in
    the MAP script: it belongs to the same dungeon and has
    ``[dungeon start area]``. The resolver is deliberately conservative: it
    accepts a lone candidate, or one with a unique nearest relationship to the
    maze's explicit MAP resources. Ambiguous or unrelated maps keep the legacy
    fallback path.
    """
    targets: dict[tuple[int, int, int, int], dict[str, set[Any]]] = {}
    try:
        from game.quest.quest_index import get_quest_index

        quest_index = get_quest_index()
        quest_index.ensure_all_dungeons_loaded()
        for dungeon_id, dungeon in list(quest_index.dungeon_cache.items()):
            for maze in list(getattr(dungeon, "mazes", None) or []):
                directories = _maze_map_directories(maze)
                if not directories:
                    continue
                maze_index = int(getattr(maze, "index", 0) or 0)
                for start in list(getattr(maze, "start_maps", None) or []):
                    if not isinstance(start, (tuple, list)) or len(start) < 2:
                        continue
                    cell_x = int(start[0])
                    cell_y = int(start[1])
                    if _maze_has_exact_map_spec(maze, cell_x, cell_y):
                        continue
                    key = (int(dungeon_id), maze_index, cell_x, cell_y)
                    target = targets.setdefault(
                        key,
                        {"directories": set(), "explicit_map_ids": set()},
                    )
                    target["directories"].update(directories)
                    target["explicit_map_ids"].update(
                        _maze_explicit_map_ids(maze)
                    )
    except Exception:
        with _OMITTED_START_MAP_LOCK:
            _OMITTED_START_MAPS.clear()
        return {"target_count": 0, "resolved_count": 0, "ambiguous_count": 0}

    candidate_directories = {
        directory
        for target in targets.values()
        for directory in target["directories"]
    }
    starts_by_dungeon_directory: dict[tuple[int, str], set[int]] = {}
    for map_id, path in _map_lst().items():
        directory = _map_directory(path)
        if directory not in candidate_directories:
            continue
        try:
            raw_map = pvf_runtime.read_text(path)
        except Exception:
            continue
        if _MAP_DUNGEON_START_AREA_BLOCK.search(raw_map) is None:
            continue
        for dungeon_id in _map_declared_dungeon_ids(raw_map):
            starts_by_dungeon_directory.setdefault(
                (int(dungeon_id), directory), set()
            ).add(int(map_id))

    resolved: dict[tuple[int, int, int, int], int] = {}
    ambiguous_count = 0
    for key, target in targets.items():
        dungeon_id = int(key[0])
        candidates = sorted(
            {
                map_id
                for directory in target["directories"]
                for map_id in starts_by_dungeon_directory.get(
                    (dungeon_id, directory), set()
                )
            }
        )
        selected = _select_omitted_start_map_candidate(
            candidates,
            target["explicit_map_ids"],
        )
        if selected > 0:
            resolved[key] = selected
        elif len(candidates) > 1:
            ambiguous_count += 1

    with _OMITTED_START_MAP_LOCK:
        _OMITTED_START_MAPS.clear()
        _OMITTED_START_MAPS.update(resolved)
    return {
        "target_count": len(targets),
        "resolved_count": len(resolved),
        "ambiguous_count": ambiguous_count,
    }


def resolve_omitted_start_map(
    dungeon_id: int,
    maze_index: int,
    cell_x: int,
    cell_y: int,
) -> int:
    """Return a prewarmed MAP-native start map for one omitted maze cell."""
    key = (
        int(dungeon_id),
        int(maze_index),
        int(cell_x) & 0xFF,
        int(cell_y) & 0xFF,
    )
    with _OMITTED_START_MAP_LOCK:
        return int(_OMITTED_START_MAPS.get(key, 0) or 0)


@lru_cache(maxsize=1)
def _tower_dungeon_reverse_index() -> tuple[
    Mapping[int, tuple[int, ...]],
    Mapping[int, str],
]:
    """Build MAP ownership and actor-room types for the two 100-floor towers."""
    resolved: dict[int, list[int]] = {}
    map_types: dict[int, str] = {}
    for map_id, path in _map_lst().items():
        normalized = str(path or "").replace("\\", "/").lower()
        if not any(marker in normalized for marker in _TOWER_DUNGEON_MAP_PATH_MARKERS):
            continue
        try:
            raw_map = pvf_runtime.read_text(path)
        except Exception:
            continue
        type_match = _MAP_TYPE_BLOCK.search(raw_map)
        if type_match is not None:
            type_token = re.search(r"\[([^\]]+)\]", type_match.group(1) or "")
            if type_token is not None:
                map_types[int(map_id)] = type_token.group(1).strip().lower()
        for dungeon_id in _map_declared_dungeon_ids(raw_map):
            resolved.setdefault(int(dungeon_id), []).append(int(map_id))
    return (
        MappingProxyType(
            {
                dungeon_id: tuple(sorted(set(map_ids)))
                for dungeon_id, map_ids in sorted(resolved.items())
            }
        ),
        MappingProxyType(dict(sorted(map_types.items()))),
    )


def _tower_dungeon_reverse_maps() -> Mapping[int, tuple[int, ...]]:
    return _tower_dungeon_reverse_index()[0]


def _is_hundred_floor_tower_dungeon_path(dungeon_path: str) -> bool:
    normalized = str(dungeon_path or "").replace("\\", "/").strip("/").lower()
    if normalized.startswith("dungeon/towers/grief/"):
        return True
    filename = normalized.rsplit("/", 1)[-1]
    return filename.startswith("ztowerofdespair") and filename.endswith(".dgn")


def resolve_tower_dungeon_cell_map(
    dungeon_id: int,
    dungeon_path: str,
    *,
    is_start_cell: bool,
    is_boss_cell: bool,
    excluded_map_ids: Sequence[int] = (),
) -> int:
    """Resolve a MAP-native tower endpoint omitted from DGN map specifications."""
    if not _is_hundred_floor_tower_dungeon_path(dungeon_path):
        return 0
    excluded = {int(value) for value in excluded_map_ids if int(value) > 0}
    candidates = [
        int(map_id)
        for map_id in _tower_dungeon_reverse_maps().get(int(dungeon_id), ())
        if int(map_id) not in excluded
    ]
    if not candidates:
        return 0
    if len(candidates) == 1:
        return candidates[0]

    map_types = _tower_dungeon_reverse_index()[1]
    if is_boss_cell:
        boss_maps = [
            map_id for map_id in candidates if map_types.get(map_id) == "boss"
        ]
        if len(boss_maps) == 1:
            return boss_maps[0]
    if is_start_cell:
        start_maps = [
            map_id for map_id in candidates if map_types.get(map_id) != "boss"
        ]
        if len(start_maps) == 1:
            return start_maps[0]
    return 0


def resolve_tower_dungeon_start_map(
    dungeon_id: int,
    dungeon_path: str,
) -> int:
    """Resolve a Grief/Despair DGN start map from MAP-native ownership."""
    return resolve_tower_dungeon_cell_map(
        int(dungeon_id),
        dungeon_path,
        is_start_cell=True,
        is_boss_cell=False,
    )


def _warm_quest_companion_start_map_index() -> int:
    """Index map-native companions referenced by ``clear map`` quests.

    The frozen PVF has quest mazes whose start cell is omitted from
    ``[map specification]``.  Their quest dialog still names the start map,
    and that map declares both the dungeon id and the companion APC from the
    quest's ``int data``.  Build this narrow relation at boot so dungeon entry
    never scans or guesses map files on the hot path.
    """
    resolved: dict[tuple[int, int], int] = {}
    try:
        from game.quest.quest_index import get_quest_index

        index = get_quest_index()
        index.ensure_all_metas_loaded()
    except Exception:
        with _QUEST_COMPANION_START_LOCK:
            _QUEST_COMPANION_START_MAPS.clear()
        return 0

    for quest_id in list(getattr(index, "sorted_quest_ids", None) or []):
        meta = index.get_meta(int(quest_id))
        if meta is None:
            continue
        quest_type = str(getattr(meta, "quest_type", "") or "").strip().lower()
        if quest_type.startswith("[") and quest_type.endswith("]"):
            quest_type = quest_type[1:-1].strip()
        if quest_type != "clear map" or int(getattr(meta, "sub_type", -1)) != 0:
            continue
        int_values = [int(v) for v in re.findall(r"-?\d+", str(meta.int_data or ""))]
        if len(int_values) < 2 or int_values[1] <= 0:
            continue
        companion_code = int(int_values[1])
        dungeon_values = list(getattr(meta, "dungeon_info", None) or ())
        dungeon_ids = [int(v) for v in dungeon_values[0::2] if int(v) > 0]
        dialog_map_ids = list(getattr(meta, "dialog_map_ids", None) or ())
        for dungeon_id in dungeon_ids:
            key = (int(meta.quest_id), dungeon_id)
            for raw_map_id in dialog_map_ids:
                map_id = int(raw_map_id)
                path = resolve_map_path(map_id)
                if not path:
                    continue
                try:
                    raw_map = pvf_runtime.read_text(path)
                except Exception:
                    continue
                if dungeon_id not in _map_declared_dungeon_ids(raw_map):
                    continue
                ai_codes = {
                    int(row.get("code") or 0)
                    for row in parse_map_ai_character_tokens(raw_map)
                }
                if companion_code not in ai_codes:
                    continue
                resolved[key] = map_id
                break

    with _QUEST_COMPANION_START_LOCK:
        _QUEST_COMPANION_START_MAPS.clear()
        _QUEST_COMPANION_START_MAPS.update(resolved)
    return len(resolved)


def resolve_quest_companion_start_map(
    dungeon_id: int,
    quest_ids: Sequence[int],
) -> int:
    """Return a boot-verified map-native companion start map, if present."""
    did = int(dungeon_id or 0)
    if did <= 0:
        return 0
    with _QUEST_COMPANION_START_LOCK:
        for raw_quest_id in quest_ids or ():
            map_id = _QUEST_COMPANION_START_MAPS.get(
                (int(raw_quest_id) & 0xFFFF, did),
                0,
            )
            if int(map_id) > 0:
                return int(map_id)
    return 0


_MapActorRows = Tuple[Mapping[str, Any], ...]
_MapActorTemplate = Tuple[
    _MapActorRows,
    _MapActorRows,
    _MapActorRows,
    _MapActorRows,
    _MapActorRows,
]
_MAP_ACTOR_TEMPLATE_CACHE: dict[int, _MapActorTemplate] = {}
_MAP_ACTOR_TEMPLATE_CACHE_LOCK = threading.RLock()


def clear_map_actor_template_cache() -> None:
    """Clear lazily parsed map actor templates (primarily for PVF tests/tools)."""
    with _MAP_ACTOR_TEMPLATE_CACHE_LOCK:
        _MAP_ACTOR_TEMPLATE_CACHE.clear()


def map_actor_template_cache_size() -> int:
    with _MAP_ACTOR_TEMPLATE_CACHE_LOCK:
        return len(_MAP_ACTOR_TEMPLATE_CACHE)


def map_actor_template_cache_source(map_index: int) -> str:
    """Return the expected source for the next template load."""
    if not _env_bool(_ENV_MAP_TEMPLATE_CACHE, True):
        return "off"
    with _MAP_ACTOR_TEMPLATE_CACHE_LOCK:
        return "hit" if int(map_index) in _MAP_ACTOR_TEMPLATE_CACHE else "cold"


def _immutable_actor_rows(rows: Sequence[dict[str, Any]]) -> _MapActorRows:
    return tuple(MappingProxyType(dict(row)) for row in rows)


def _parse_map_actor_template(map_index: int) -> _MapActorTemplate | None:
    path = resolve_map_path(int(map_index))
    if not path:
        return None
    try:
        text = pvf_runtime.read_text(path)
    except Exception:
        return None
    return (
        _immutable_actor_rows(parse_map_monster_tokens(text)),
        _immutable_actor_rows(parse_map_ai_character_tokens(text)),
        _immutable_actor_rows(
            parse_map_special_passive_tokens(text)
            + parse_map_passive_object_tokens(text)
        ),
        _immutable_actor_rows(parse_map_monster_condition_tokens(text)),
        _immutable_actor_rows(parse_map_conditional_summon_tokens(text)),
    )


def _load_map_actor_template(map_index: int) -> tuple[_MapActorTemplate | None, str]:
    mid = int(map_index)
    cache_enabled = _env_bool(_ENV_MAP_TEMPLATE_CACHE, True)
    if not cache_enabled:
        return _parse_map_actor_template(mid), "off"

    with _MAP_ACTOR_TEMPLATE_CACHE_LOCK:
        cached = _MAP_ACTOR_TEMPLATE_CACHE.get(mid)
    if cached is not None:
        return cached, "hit"

    parsed = _parse_map_actor_template(mid)
    if parsed is None:
        return None, "error"
    with _MAP_ACTOR_TEMPLATE_CACHE_LOCK:
        cached = _MAP_ACTOR_TEMPLATE_CACHE.get(mid)
        if cached is not None:
            return cached, "hit"
        _MAP_ACTOR_TEMPLATE_CACHE[mid] = parsed
    return parsed, "cold"


def _parse_type_token(tok: str) -> int:
    """怪物 rank → wire mon type 0..3。"""
    t = _strip_tick(tok).lower().replace(" ", "")
    if "superchampion" in t or t in {"super", "2"}:
        return 2
    if "champion" in t or t == "1":
        return 1
    if "boss" in t or t == "3":
        return 3
    return 0


def _standalone_monster_rank(tok: str) -> int | None:
    """Return a rank carried by a MAP token before its actor row.

    Most ``[monster]`` rows place the rank in their tenth field. Story maps
    may emit a rank marker on its own line before the next row instead. It is
    metadata, not the first field of a second row.
    """
    value = _strip_tick(tok).strip().lower().replace(" ", "")
    if value in {"[champion]", "[superchampion]", "[boss]"}:
        return _parse_type_token(value)
    return None


def _is_scene_dummy_map_mode(tok: str) -> bool:
    """Whether a MAP actor's rank/mode field explicitly marks a scene dummy."""
    return _strip_tick(tok).strip().lower().replace(" ", "") == "[dummy]"


def _is_scene_dummy_script_candidate(row: Mapping[str, Any]) -> bool:
    """Limit script inspection to high-layer PVF scene-actor candidates.

    ``z=10000`` alone has no combat meaning. It only avoids a monster-script
    lookup for ordinary rows; callers still require the explicit dummy action
    before changing a room-clear latch.
    """
    try:
        return int(row.get("z") or 0) == 10000
    except (TypeError, ValueError):
        return False


def _parse_apc_ai_type(tok: str) -> int:
    """AI character AIType → wire type 5/6/8（S4A12 ApcAIType）。"""
    t = _strip_tick(tok).lower().replace(" ", "")
    if "boss" in t or t in {"8", "[boss]"}:
        return 8
    if "champion" in t or t in {"6", "[champion]"}:
        return 6
    # normal / character / default
    return 5


def _parse_apc_faction(tok: str) -> str:
    """Normalize map AI faction tag → monster / character / neutral.

    S4A12 ``ApcFaction`` / 90qiyuan ``currentDungeonAICharacterHostile``：
      - monster  → 敌对
      - character → 队友（友方）
      - neutral  → 中立（非敌对）
    """
    t = _strip_tick(tok).lower().replace(" ", "")
    t = t.strip("[]")
    if t in {"monster", "100", "enemy", "hostile"}:
        return "monster"
    if t in {"neutral", "200"}:
        return "neutral"
    if t in {"character", "0", "friend", "ally", "team", "party"}:
        return "character"
    return t or "character"


def _apc_combat_flags(faction: str) -> dict[str, Any]:
    """APC 敌友分流（通用，不绑单一任务）。

    敌对 APC 与怪物同等：挡门 / 计清房 / BOSS 房须击杀才 ENABLE_CLEAR。
    队友 / 中立 APC：刷出但不挡门、不给经验/任务 residual。
    """
    fac = str(faction or "character").lower()
    if fac == "monster":
        return {
            "role": _ROLE_ENEMY,
            "is_hostile": True,
            "is_ally": False,
            "killable": True,
            "grants_exp": True,
            # hunt enemy enemyType=2 仅敌对 AI
            "quest_enemy_type": _QUEST_ENEMY_AI,
            # 敌对 NPC 当怪物：未击杀不算清房，阻止进下一房；BOSS APC 挡通关
            "is_blocking": True,
        }
    if fac == "neutral":
        return {
            "role": _ROLE_NEUTRAL,
            "is_hostile": False,
            "is_ally": False,
            "killable": False,
            "grants_exp": False,
            "quest_enemy_type": 0,
            "is_blocking": False,
        }
    # character / friend / default → 队友
    return {
        "role": _ROLE_ALLY,
        "is_hostile": False,
        "is_ally": True,
        "killable": False,
        "grants_exp": False,
        "quest_enemy_type": 0,
        "is_blocking": False,
    }


def parse_map_monster_team(raw: str) -> list[int]:
    """Parse ``[monster team]`` 整数序列（与 monster 行位置对齐）。

    90qiyuan ``monsterSpawnBlocksRoom``：team==0 为剧情友方，不挡门；
    缺省/非 0 视为敌对挡路。
    """
    text = str(raw or "")
    m = _MONSTER_TEAM_BLOCK.search(text)
    if not m:
        return []
    body = m.group(1) or ""
    out: list[int] = []
    for tok in _tokenize(body):
        try:
            out.append(int(_strip_tick(tok), 0))
        except ValueError:
            # 跳过非数字 token（闭合标签残留等）
            continue
    return out


def parse_map_monster_tokens(raw: str) -> list[dict[str, Any]]:
    text = str(raw or "")
    m = _MONSTER_BLOCK.search(text)
    body = m.group(1) if m else ""
    toks = _tokenize(body)
    teams = parse_map_monster_team(text)
    rows: list[dict[str, Any]] = []
    cursor = 0
    while cursor < len(toks):
        standalone_rank: int | None = None
        while cursor < len(toks):
            parsed_rank = _standalone_monster_rank(toks[cursor])
            if parsed_rank is None:
                break
            standalone_rank = parsed_rank
            cursor += 1

        chunk = toks[cursor : cursor + 10]
        if len(chunk) < 10:
            break
        try:
            mon_id = int(_strip_tick(chunk[0]), 0)
            lv = int(_strip_tick(chunk[1]), 0)
            autolv = int(_strip_tick(chunk[2]), 0)
        except ValueError:
            # Preserve the legacy malformed-row behavior. Standalone rank
            # markers are consumed above before this fixed-width row attempt.
            cursor += 10
            continue
        cursor += 10
        if mon_id <= 0:
            continue
        is_scene_dummy = _is_scene_dummy_map_mode(chunk[9])
        mon_type = (
            standalone_rank
            if standalone_rank is not None
            else _parse_type_token(chunk[9])
        )
        idx = len(rows)
        # team 缺省 = 敌对挡路；team==0 = 剧情友方怪（不挡门、不给经验）
        team = int(teams[idx]) if idx < len(teams) else 100
        is_ally_team = team == 0
        rows.append(
            {
                "kind": "monster",
                "code": mon_id,
                "lv": lv,
                "autolv": autolv,
                "x": chunk[3],
                "y": chunk[4],
                "z": chunk[5],
                "type": mon_type,
                "team": team,
                "faction": "character" if is_ally_team else "monster",
                "role": _ROLE_ALLY if is_ally_team else _ROLE_ENEMY,
                "is_hostile": not is_ally_team,
                "is_ally": is_ally_team,
                "quest_enemy_type": 0 if is_ally_team else _QUEST_ENEMY_MONSTER,
                # MAP ``[dummy]`` is a client scene actor, not a room-clear
                # target. Keep its combat/quest identity for native events.
                "is_scene_dummy": is_scene_dummy,
                "is_blocking": not is_ally_team and not is_scene_dummy,
                "grants_exp": not is_ally_team,
                "killable": True,  # 友方怪仍可能被误伤消亡，但不计任务/经验
                "slot_index": idx,
                "template_order": idx,
            }
        )
    return rows


def parse_map_monster_condition_tokens(raw: str) -> list[dict[str, Any]]:
    """Parse hidden MAP ``[monster condition]`` templates.

    These rows use the same ten-token layout as ``[monster]`` but are only
    instantiated when a DGN special-room condition assigns their code to the
    current room.
    """
    text = str(raw or "")
    match = _MONSTER_CONDITION_BLOCK.search(text)
    body = match.group(1) if match else ""
    tokens = _tokenize(body)
    rows: list[dict[str, Any]] = []
    for offset in range(0, len(tokens), 10):
        chunk = tokens[offset : offset + 10]
        if len(chunk) < 10:
            break
        try:
            monster_code = int(_strip_tick(chunk[0]), 0)
            level = int(_strip_tick(chunk[1]), 0)
            auto_level = int(_strip_tick(chunk[2]), 0)
            pos_x = int(_strip_tick(chunk[3]), 0)
            pos_y = int(_strip_tick(chunk[4]), 0)
            pos_z = int(_strip_tick(chunk[5]), 0)
        except ValueError:
            continue
        if monster_code <= 0:
            continue
        index = len(rows)
        rows.append(
            {
                "kind": "monster_condition",
                "code": monster_code,
                "lv": level,
                "autolv": auto_level,
                "x": pos_x,
                "y": pos_y,
                "z": pos_z,
                "type": _parse_type_token(chunk[9]),
                "team": 100,
                "faction": "monster",
                "role": _ROLE_ENEMY,
                "is_hostile": True,
                "is_ally": False,
                "quest_enemy_type": _QUEST_ENEMY_MONSTER,
                "is_blocking": True,
                "grants_exp": True,
                "killable": True,
                "slot_index": index,
                "template_order": index,
            }
        )
    return rows


def parse_map_conditional_summon_tokens(raw: str) -> list[dict[str, Any]]:
    """Parse hidden MAP ``[conditional summon monster]`` templates.

    Stage3's Meltdown maps use a 13-token row: the ordinary ten monster
    fields carry three condition parameters, followed by the actor's real
    ``x/y/z`` position.  Older rows without the tail retain their three
    condition values as the position, matching the established PVF parser.
    """
    text = str(raw or "")
    match = _CONDITIONAL_SUMMON_BLOCK.search(text)
    tokens = _tokenize(match.group(1) if match else "")
    rows: list[dict[str, Any]] = []
    index = 0
    while index + 9 < len(tokens):
        has_tail_position = False
        if index + 12 < len(tokens):
            try:
                int(_strip_tick(tokens[index + 10]), 0)
                int(_strip_tick(tokens[index + 11]), 0)
                int(_strip_tick(tokens[index + 12]), 0)
                has_tail_position = True
            except ValueError:
                has_tail_position = False
        row_length = 13 if has_tail_position else 10
        chunk = tokens[index : index + row_length]
        if len(chunk) < row_length:
            break
        try:
            monster_code = int(_strip_tick(chunk[0]), 0)
            level = int(_strip_tick(chunk[1]), 0)
            auto_level = int(_strip_tick(chunk[2]), 0)
            conditional_param0 = int(_strip_tick(chunk[3]), 0)
            conditional_param1 = int(_strip_tick(chunk[4]), 0)
            conditional_param2 = int(_strip_tick(chunk[5]), 0)
            pos_x = int(
                _strip_tick(chunk[10] if has_tail_position else chunk[3]),
                0,
            )
            pos_y = int(
                _strip_tick(chunk[11] if has_tail_position else chunk[4]),
                0,
            )
            pos_z = int(
                _strip_tick(chunk[12] if has_tail_position else chunk[5]),
                0,
            )
        except ValueError:
            index += row_length
            continue
        if monster_code > 0:
            template_index = len(rows)
            rows.append(
                {
                    "kind": "conditional_summon_monster",
                    "code": monster_code,
                    "lv": level,
                    "autolv": auto_level,
                    "conditional_param0": conditional_param0,
                    "conditional_param1": conditional_param1,
                    "conditional_param2": conditional_param2,
                    "x": pos_x,
                    "y": pos_y,
                    "z": pos_z,
                    "type": _parse_type_token(chunk[9]),
                    "team": 100,
                    "faction": "monster",
                    "role": _ROLE_ENEMY,
                    "is_hostile": True,
                    "is_ally": False,
                    "quest_enemy_type": _QUEST_ENEMY_MONSTER,
                    "is_blocking": True,
                    "grants_exp": True,
                    "killable": True,
                    "slot_index": template_index,
                    "template_order": template_index,
                }
            )
        index += row_length
    return rows


def parse_map_ai_character_tokens(raw: str) -> list[dict[str, Any]]:
    """Parse ``[ai character]``：code x y dir faction aiType [p0 p1]。

    敌友由 faction 决定（通用，不硬编码任务 id）：
      ``[monster]`` 敌人 · ``[character]`` 队友 · ``[neutral]`` 中立
    """
    text = str(raw or "")
    m = _AI_BLOCK.search(text)
    body = m.group(1) if m else ""
    toks = _tokenize(body)
    rows: list[dict[str, Any]] = []
    i = 0
    while i < len(toks):
        try:
            code = int(_strip_tick(toks[i]), 0)
        except ValueError:
            break
        if code <= 0:
            break
        x = y = direction = 0
        if i + 1 < len(toks):
            try:
                x = int(_strip_tick(toks[i + 1]), 0)
            except ValueError:
                x = 0
        if i + 2 < len(toks):
            try:
                y = int(_strip_tick(toks[i + 2]), 0)
            except ValueError:
                y = 0
        if i + 3 < len(toks):
            try:
                direction = int(_strip_tick(toks[i + 3]), 0)
            except ValueError:
                direction = 0
        i += 4
        faction = "character"
        if i < len(toks):
            faction = _parse_apc_faction(toks[i])
            i += 1
        ai_type = 5
        if i < len(toks):
            ai_type = _parse_apc_ai_type(toks[i])
            i += 1
        # 末尾两个数值（S4A12 未使用）
        for _ in range(2):
            if i < len(toks):
                try:
                    int(_strip_tick(toks[i]), 0)
                    i += 1
                except ValueError:
                    break
        flags = _apc_combat_flags(faction)
        # 敌对 APC：is_blocking=True（与怪同权清房/通关）；友方/中立不挡门
        rows.append(
            {
                "kind": "ai_character",
                "code": code,
                "x": x,
                "y": y,
                "direction": direction,
                "faction": faction,
                "type": int(ai_type) & 0xFF,
                "slot_index": len(rows),
                "template_order": len(rows),
                **flags,
            }
        )
    return rows


def _passive_object_row(
    object_code: int,
    x: int,
    y: int,
    flags: int,
    object_index: int,
    *,
    kind: str = "special_passive",
) -> dict[str, Any]:
    """type9 可破坏物行（special / 普通 passive 共用）。

    - 不挡门、不给经验；可击杀 → DIE 走 hunt enemy type=3 residual
    - kind=``passive_object`` 来自 map ``[passive object]``（蚁卵/蚁穴等）
    - kind=``special_passive`` 来自 ``[special passive object]``
    """
    kind_norm = str(kind or "special_passive").strip().lower() or "special_passive"
    return {
        "kind": kind_norm,
        "code": int(object_code),
        "x": int(x),
        "y": int(y),
        "flags": int(flags),
        "type": _SPECIAL_PASSIVE_WIRE_TYPE,
        "quest_enemy_type": _QUEST_ENEMY_PASSIVE,
        "faction": "neutral",
        "role": _ROLE_NEUTRAL,
        "is_hostile": False,
        "is_ally": False,
        "is_blocking": False,
        "grants_exp": False,
        "killable": True,
        "packet_index": int(object_index),
        "slot_index": int(object_index),
        "template_order": 0,
        "level": 0,
    }


def _special_passive_object_row(
    object_code: int,
    x: int,
    y: int,
    flags: int,
    object_index: int,
) -> dict[str, Any]:
    return _passive_object_row(
        object_code, x, y, flags, object_index, kind="special_passive"
    )


def is_passive_actor(mon: dict[str, Any] | None) -> bool:
    """是否 type9 / 可破坏建筑（含 special 与普通 passive object）。"""
    if not mon:
        return False
    kind = str(mon.get("kind") or "").lower()
    if kind in {"special_passive", "passive_object", "passive", "map_passive"}:
        return True
    try:
        if int(mon.get("type") or mon.get("mon_type") or 0) == _SPECIAL_PASSIVE_WIRE_TYPE:
            return True
    except Exception:
        pass
    try:
        if int(mon.get("quest_enemy_type") or 0) == _QUEST_ENEMY_PASSIVE:
            return True
    except Exception:
        pass
    return False


def load_map_passive_objects(map_index: int) -> list[dict[str, Any]]:
    """Return copies of the passive-object templates physically in one map.

    This reads the shared map actor-template cache.  It does not turn static
    client-owned geometry into START_MAP rows; callers decide whether a
    server-only shadow or a task-controlled wire row is required.
    """
    template, _source = _load_map_actor_template(int(map_index or 0))
    if template is None:
        return []
    (
        _monster_rows,
        _ai_rows,
        passive_rows,
        _condition_rows,
        _conditional_summon_rows,
    ) = template
    return [dict(row) for row in passive_rows if is_passive_actor(dict(row))]


def map_condition_monster_codes(map_index: int) -> tuple[int, ...]:
    """Return the warmed hidden condition codes available in one MAP."""
    template, _source = _load_map_actor_template(int(map_index or 0))
    if template is None:
        return ()
    (
        _monster_rows,
        _ai_rows,
        _passive_rows,
        condition_rows,
        _conditional_summon_rows,
    ) = template
    return tuple(
        int(row.get("code") or 0)
        for row in condition_rows
        if int(row.get("code") or 0) > 0
    )


def map_monster_codes(map_index: int) -> tuple[int, ...]:
    """Return ordinary ``[monster]`` codes in PVF declaration order.

    Boss-entry conditions normally use hidden ``[monster condition]`` rows,
    but some maps, such as Mount Hardt Showdown, declare their fixed targets
    in the ordinary map monster table.  Duplicates are significant there:
    each row is one server-published actor that can satisfy the condition.
    """
    template, _source = _load_map_actor_template(int(map_index or 0))
    if template is None:
        return ()
    (
        monster_rows,
        _ai_rows,
        _passive_rows,
        _condition_rows,
        _conditional_summon_rows,
    ) = template
    return tuple(
        int(row.get("code") or 0)
        for row in monster_rows
        if int(row.get("code") or 0) > 0
    )


def load_map_condition_monsters(
    map_index: int,
    target_codes: Sequence[int],
    *,
    dungeon_basic_lv: int = 0,
    first_seq: int = 0,
) -> list[dict[str, Any]]:
    """Materialize selected hidden condition templates as normal actors."""
    wanted = {int(code) for code in target_codes or () if int(code) > 0}
    if not wanted:
        return []
    template, _source = _load_map_actor_template(int(map_index or 0))
    if template is None:
        return []
    (
        _monster_rows,
        _ai_rows,
        _passive_rows,
        condition_rows,
        _conditional_summon_rows,
    ) = template
    basic = max(0, int(dungeon_basic_lv or 0)) & 0xFF
    out: list[dict[str, Any]] = []
    for row in condition_rows:
        code = int(row.get("code") or 0)
        if code not in wanted:
            continue
        index = len(out)
        out.append(
            {
                "kind": "monster_condition",
                "template_order": int(row.get("template_order") or 0) & 0xFFFF,
                "aux": 0,
                "aux2": 0,
                "slot_index": int(row.get("slot_index") or 0) & 0xFFFFFFFF,
                "seq": (int(first_seq) + index + 1) & 0xFFFF,
                "code": code & 0xFFFFFFFF,
                "level": _monster_level(dict(row), basic) & 0xFF,
                "type": int(row.get("type") or 0) & 0xFF,
                "flag0": 0,
                "flag1": 0,
                "extra_state": 0,
                "extra_flag": 0,
                "quest_enemy_type": _QUEST_ENEMY_MONSTER,
                "is_blocking": True,
                "grants_exp": True,
                "killable": True,
                "faction": "monster",
                "role": _ROLE_ENEMY,
                "is_hostile": True,
                "is_ally": False,
                "team": 100,
                "x": int(row.get("x") or 0),
                "y": int(row.get("y") or 0),
                "z": int(row.get("z") or 0),
            }
        )
    return out


def load_map_conditional_summon_monsters(
    map_index: int,
    target_codes: Sequence[int],
    *,
    dungeon_basic_lv: int = 0,
    first_seq: int = 0,
) -> list[dict[str, Any]]:
    """Materialize authorized MAP conditional-summon templates."""
    wanted = {int(code) for code in target_codes or () if int(code) > 0}
    if not wanted:
        return []
    template, _source = _load_map_actor_template(int(map_index or 0))
    if template is None:
        return []
    (
        _monster_rows,
        _ai_rows,
        _passive_rows,
        _condition_rows,
        conditional_summon_rows,
    ) = template
    basic = max(0, int(dungeon_basic_lv or 0)) & 0xFF
    out: list[dict[str, Any]] = []
    for row in conditional_summon_rows:
        code = int(row.get("code") or 0)
        if code not in wanted:
            continue
        actor_index = len(out)
        out.append(
            {
                "kind": "conditional_summon_monster",
                "template_order": int(row.get("template_order") or 0) & 0xFFFF,
                "aux": 0,
                "aux2": 0,
                "slot_index": int(row.get("slot_index") or 0) & 0xFFFFFFFF,
                "seq": (int(first_seq) + actor_index + 1) & 0xFFFF,
                "code": code & 0xFFFFFFFF,
                "level": _monster_level(dict(row), basic) & 0xFF,
                "type": int(row.get("type") or 0) & 0xFF,
                "flag0": 0,
                "flag1": 0,
                "extra_state": 0,
                "extra_flag": 0,
                "quest_enemy_type": _QUEST_ENEMY_MONSTER,
                "is_blocking": True,
                "grants_exp": True,
                "killable": True,
                "faction": "monster",
                "role": _ROLE_ENEMY,
                "is_hostile": True,
                "is_ally": False,
                "team": 100,
                "x": int(row.get("x") or 0),
                "y": int(row.get("y") or 0),
                "z": int(row.get("z") or 0),
                "conditional_param0": int(row.get("conditional_param0") or 0),
                "conditional_param1": int(row.get("conditional_param1") or 0),
                "conditional_param2": int(row.get("conditional_param2") or 0),
                "wire_visible": True,
                "killed": False,
            }
        )
    return out


def select_task_controlled_passive_objects(
    rows: Sequence[dict[str, Any]],
    *,
    active_quest_ids: Sequence[int] = (),
    dungeon_id: int = 0,
    difficulty: int = -1,
) -> list[dict[str, Any]]:
    """Return only passive rows explicitly required by an active task.

    A map's passive-object sections also describe static collision/decoration.
    Sending every one as a type-9 actor duplicates client-owned map geometry and
    can block exits.  The task contract is exact: ``[hunt enemy]`` must match
    this dungeon, difficulty, object code, and enemy type ``3``.
    """
    quest_ids: list[int] = []
    for raw_quest_id in active_quest_ids or ():
        try:
            quest_id = int(raw_quest_id) & 0xFFFF
        except (TypeError, ValueError):
            continue
        if quest_id > 0:
            quest_ids.append(quest_id)
    if not rows or not quest_ids or int(dungeon_id) <= 0:
        return []
    try:
        from game.quest.quest_index import get_quest_index
        from game.quest.quest_residual import match_hunt_kill_channel

        quest_index = get_quest_index()
    except Exception:
        return []

    def is_target(code: int) -> bool:
        if int(code) <= 0:
            return False
        for quest_id in quest_ids:
            meta = quest_index.get_meta(quest_id)
            if meta is None:
                continue
            if match_hunt_kill_channel(
                meta,
                monster_code=int(code),
                dungeon_id=int(dungeon_id),
                difficulty=int(difficulty),
                enemy_type=_QUEST_ENEMY_PASSIVE,
            ) is not None:
                return True
        return False

    selected: list[dict[str, Any]] = []
    selected_special_indexes: set[int] = set()
    for row in rows:
        if str(row.get("kind") or "").lower() != "special_passive":
            continue
        if is_target(int(row.get("code") or 0)):
            selected.append(row)
            selected_special_indexes.add(int(row.get("packet_index") or 0))

    for row in rows:
        kind = str(row.get("kind") or "").lower()
        if kind == "passive_object" and is_target(int(row.get("code") or 0)):
            selected.append(row)
        elif bool(row.get("from_special_passive")) and int(
            row.get("template_order") or 0
        ) in selected_special_indexes:
            selected.append(row)
    return selected


def _try_parse_special_passive_with_spawns(
    toks: list[str],
) -> list[dict[str, Any]] | None:
    """S4A12 TryParseSpecialPassiveObjectsWithSpawns：整段必须吃完。"""
    object_rows: list[dict[str, Any]] = []
    template_rows: list[dict[str, Any]] = []
    i = 0
    object_index = 0
    while i < len(toks):
        if i + 4 >= len(toks):
            return None
        try:
            object_code = int(_strip_tick(toks[i]), 0)
            x = int(_strip_tick(toks[i + 1]), 0)
            y = int(_strip_tick(toks[i + 2]), 0)
            flags = int(_strip_tick(toks[i + 3]), 0)
            spawn_count = int(_strip_tick(toks[i + 4]), 0)
        except ValueError:
            return None
        if spawn_count < 0:
            return None
        i += 5
        object_row = None
        item_drop_refs: list[tuple[int, int, int, int, int]] = []
        if object_code > 0:
            object_row = _special_passive_object_row(
                object_code, x, y, flags, object_index
            )
            object_rows.append(object_row)
        if spawn_count > (len(toks) - i) // 6:
            return None
        for spawn_i in range(spawn_count):
            if i + 5 >= len(toks):
                return None
            kind = _strip_tick(toks[i])
            if not kind.startswith("["):
                return None
            try:
                scode = int(_strip_tick(toks[i + 1]), 0)
                slevel = int(_strip_tick(toks[i + 2]), 0)
                # p0 p1 p2 可负
                p0 = int(_strip_tick(toks[i + 3]), 0)
                p1 = int(_strip_tick(toks[i + 4]), 0)
                p2 = int(_strip_tick(toks[i + 5]), 0)
            except ValueError:
                return None
            i += 6
            kind_norm = kind.lower().replace(" ", "")
            if kind_norm == "[item]":
                # Zero is a valid DGN drop-group reference, not a missing code.
                # Keep the other authored fields without inventing semantics.
                if scode >= 0:
                    item_drop_refs.append((scode, slevel, p0, p1, p2))
                continue
            if scode <= 0:
                continue
            if "monster" not in kind_norm:
                continue
            template_rows.append(
                {
                    "kind": "monster",
                    "code": scode,
                    "lv": slevel,
                    "autolv": 0,
                    "type": 0,
                    "faction": "monster",
                    "role": _ROLE_ENEMY,
                    "is_hostile": True,
                    "is_ally": False,
                    "quest_enemy_type": _QUEST_ENEMY_MONSTER,
                    "is_blocking": False,
                    "grants_exp": True,
                    "killable": True,
                    "template_order": object_index,
                    "slot_index": spawn_i,
                    "packet_index": spawn_i,
                    "flag0": 1,
                    "flag1": object_index & 0xFF,
                    "from_special_passive": True,
                }
            )
        if object_row is not None:
            object_row["item_drop_refs"] = tuple(item_drop_refs)
        object_index += 1
    if not object_rows and not template_rows:
        return None
    if i != len(toks):
        return None
    return object_rows + template_rows


def parse_map_special_passive_tokens(raw: str) -> list[dict[str, Any]]:
    """Parse ``[special passive object]`` → type9 对象 + 可选 ``[monster]`` 子刷怪。

    对齐 S4A12 ``AppendSpecialPassiveObjects``：
    1) ObjectCode>0 → wire type=9，不挡路
    2) Spawns 里 kind=``[monster]`` → 普通怪行（不挡路，Flag0=1）
    """
    text = str(raw or "")
    m = _SPECIAL_BLOCK.search(text)
    body = m.group(1) if m else ""
    # Hell maps embed group/rate/order triples inside the special passive
    # object block. They are templates for the pillar, not additional type9
    # objects, so remove the nested block before parsing the pillar itself.
    body = _HELLPARTY_INLINE_BLOCK.sub(" ", body)
    toks = _tokenize(body)
    if not toks:
        return []
    extended = _try_parse_special_passive_with_spawns(toks)
    if extended is not None:
        return extended
    # 扁平 4 元组：code x y flags
    nums: list[int] = []
    for t in toks:
        try:
            nums.append(int(_strip_tick(t), 0))
        except ValueError:
            continue
    object_rows: list[dict[str, Any]] = []
    for oi, off in enumerate(range(0, len(nums) - 3, 4)):
        oc, ox, oy, of = nums[off : off + 4]
        if oc <= 0:
            continue
        object_rows.append(_special_passive_object_row(oc, ox, oy, of, oi))
    return object_rows


def parse_map_passive_object_tokens(raw: str) -> list[dict[str, Any]]:
    """Parse ``[passive object]`` → type9 可破坏物（卵 / 蚁穴 / 箱子等）。

    90qiyuan ``parsePassiveObjects``：扁平 ``code x y flags`` 四元组。
    与 ``[special passive object]`` 正交；任务 hunt enemy type=3 常指这些 code
    （例：蚁卵 18553、蚁穴 18568、剧情 dummy 13099）。
    """
    text = str(raw or "")
    m = _PASSIVE_BLOCK.search(text)
    body = m.group(1) if m else ""
    toks = _tokenize_body(body)
    if not toks:
        return []
    nums: list[int] = []
    for t in toks:
        try:
            nums.append(int(_strip_tick(t), 0))
        except ValueError:
            continue
    object_rows: list[dict[str, Any]] = []
    for oi, off in enumerate(range(0, len(nums) - 3, 4)):
        oc, ox, oy, of = nums[off : off + 4]
        if oc <= 0:
            continue
        object_rows.append(
            _passive_object_row(oc, ox, oy, of, oi, kind="passive_object")
        )
    return object_rows


def _tokenize_body(body: str) -> list[str]:
    """与 parse special 相同：`tick` 或空白切分。"""
    return _tokenize(body)


@lru_cache(maxsize=1)
def _ai_character_lst() -> dict[int, str]:
    for cand in ("AICharacter/AICharacter.lst", "aicharacter/aicharacter.lst"):
        try:
            text = pvf_runtime.read_text(cand)
        except Exception:
            continue
        out: dict[int, str] = {}
        for aid_s, rel in _AI_LST_PAIR.findall(text or ""):
            try:
                aid = int(aid_s, 0)
            except ValueError:
                continue
            rel = str(rel or "").strip().replace("\\", "/")
            if not rel:
                continue
            low = rel.lower()
            if not low.startswith("aicharacter/"):
                rel = "AICharacter/" + rel.lstrip("/")
            out[aid] = rel
        if out:
            return out
    return {}


@dataclass(frozen=True)
class AiCharacterDefinition:
    code: int
    level: int
    appearance_point: int
    death_tower_items: tuple[tuple[int, int], ...] = ()


@lru_cache(maxsize=2048)
def resolve_ai_character_definition(code: int) -> AiCharacterDefinition | None:
    """Read level, tower appearance cost and tower-only drops from one AIC."""
    cid = int(code or 0)
    if cid <= 0:
        return None
    path = _ai_character_lst().get(cid)
    if not path:
        return None
    try:
        text = pvf_runtime.read_text(path)
    except Exception:
        # 大小写回退
        try:
            text = pvf_runtime.read_text(path.replace("AICharacter/", "aicharacter/"))
        except Exception:
            return None
    m = _SECTION.search(text or "")
    body = m.group(1) if m else (text or "")
    ints: list[int] = []
    for tok in _tokenize(body):
        try:
            ints.append(int(_strip_tick(tok), 0))
        except ValueError:
            continue
    if len(ints) < 5:
        return None
    lv = int(ints[4])
    if lv <= 0 or lv > 255:
        return None
    appearance_match = _APPEARANCE_POINT_SECTION.search(text or "")
    appearance_values = (
        re.findall(r"-?\d+", appearance_match.group(1) or "")
        if appearance_match is not None
        else []
    )
    appearance_point = int(appearance_values[0]) if appearance_values else 0
    tower_item_match = _DEATH_TOWER_ITEM_SECTION.search(text or "")
    tower_item_values = (
        [int(value) for value in re.findall(r"-?\d+", tower_item_match.group(1) or "")]
        if tower_item_match is not None
        else []
    )
    tower_items: list[tuple[int, int]] = []
    for index in range(0, len(tower_item_values) - 1, 2):
        item_id = int(tower_item_values[index])
        drop_rate = int(tower_item_values[index + 1])
        if item_id <= 0 or drop_rate <= 0:
            continue
        tower_items.append((item_id, min(10000, drop_rate)))
    return AiCharacterDefinition(
        code=cid,
        level=lv,
        appearance_point=max(0, appearance_point),
        death_tower_items=tuple(tower_items),
    )


@lru_cache(maxsize=2048)
def resolve_ai_character_level(code: int) -> int | None:
    """从 AICharacter 脚本 ``[minimum info]`` 第 5 个整数读等级（90qiyuan）。"""
    definition = resolve_ai_character_definition(int(code or 0))
    return int(definition.level) if definition is not None else None


def _monster_level(row: dict[str, Any], basic: int) -> int:
    raw_lv = int(row.get("lv") or 0)
    autolv = int(row.get("autolv") or 0)
    if "level" in row and row.get("level") is not None and int(row.get("level") or -1) >= 0:
        # special passive type9 允许 level=0
        if row.get("kind") == "special_passive":
            return max(0, min(255, int(row.get("level") or 0)))
    if raw_lv != 0:
        return max(1, min(255, basic + autolv))
    return max(1, min(255, autolv if autolv > 0 else (basic if basic > 0 else 1)))


def _ai_level(row: dict[str, Any], basic: int) -> int:
    code = int(row.get("code") or 0)
    from_pvf = resolve_ai_character_level(code)
    if from_pvf is not None:
        return int(from_pvf) & 0xFF
    # 回退：副本基础等级
    return max(1, min(255, basic if basic > 0 else 1))


def load_map_monsters(
    map_index: int,
    dungeon_basic_lv: int = 0,
    *,
    first_seq: int = 0,
    active_quest_ids: Sequence[int] = (),
    dungeon_id: int = 0,
    difficulty: int = -1,
    template_state: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """加载地图 001D mon 表（怪 + AI + 任务明确要求的被动对象）。

    返回行字段（兼容旧 mon 行）::

      seq, code, level, type, slot_index, template_order, flag0/1,
      kind, quest_enemy_type, is_blocking, grants_exp, killable,
      faction, role, is_hostile, is_ally, team?
    """
    if not _env_bool("USDOF_DUNGEON_MONSTERS", True):
        if template_state is not None:
            template_state["source"] = "monsters_off"
        return []
    mid = int(map_index or 0)
    if mid <= 0:
        if template_state is not None:
            template_state["source"] = "invalid_map"
        return []
    template, template_source = _load_map_actor_template(mid)
    if template_state is not None:
        template_state["source"] = template_source
    if template is None:
        return []
    basic = max(0, int(dungeon_basic_lv or 0)) & 0xFF

    (
        monster_rows,
        ai_rows,
        passive_template_rows,
        _condition_rows,
        _conditional_summon_rows,
    ) = template
    rows = [dict(row) for row in monster_rows]
    if _env_bool("USDOF_DUNGEON_MAP_AI_CHARACTERS", True):
        rows.extend(dict(row) for row in ai_rows)
    passive_rows = [dict(row) for row in passive_template_rows]
    # Map passive sections are client-owned static geometry by default.  Only a
    # matching active hunt-enemy(type=3) task may turn one into a wire actor.
    rows.extend(
        select_task_controlled_passive_objects(
            passive_rows,
            active_quest_ids=active_quest_ids,
            dungeon_id=dungeon_id,
            difficulty=difficulty,
        )
    )

    limit = _env_int("USDOF_DUNGEON_MONSTER_LIMIT", 0)
    if limit > 0:
        rows = rows[:limit]

    extra_state = 100 if _env_bool("USDOF_DUNGEON_MON_EXTRA_100", False) else 0
    force_zero_order = _env_bool("USDOF_DUNGEON_MON_TEMPLATE_ORDER_ZERO", False)
    out: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        kind = str(row.get("kind") or "monster")
        is_passive_kind = kind in {
            "special_passive",
            "passive_object",
            "passive",
            "map_passive",
        }
        if kind == "ai_character":
            level = _ai_level(row, basic)
        elif is_passive_kind:
            level = int(row.get("level") or 0) & 0xFF
        else:
            level = _monster_level(row, basic)

        slot = 0 if force_zero_order else int(row.get("slot_index") if row.get("slot_index") is not None else i)
        if row.get("packet_index") is not None and is_passive_kind:
            slot = int(row.get("packet_index") or 0)

        tmpl = 0 if force_zero_order else int(row.get("template_order") if row.get("template_order") is not None else i)
        mon_type = int(row.get("type") or 0) & 0xFF
        # S4A12：APC type 必须 5..8
        if kind == "ai_character" and not (5 <= mon_type <= 8):
            mon_type = 5
        if is_passive_kind:
            mon_type = _SPECIAL_PASSIVE_WIRE_TYPE

        # 缺省：普通怪敌对；AI 由 parse 已填；passive 默认可交互不挡门
        role = str(row.get("role") or "")
        if not role:
            if kind == "ai_character":
                role = str(_apc_combat_flags(str(row.get("faction") or "character"))["role"])
            elif is_passive_kind:
                role = _ROLE_NEUTRAL
            else:
                role = _ROLE_ENEMY
        is_hostile = bool(
            row["is_hostile"] if "is_hostile" in row else (role == _ROLE_ENEMY)
        )
        is_ally = bool(row["is_ally"] if "is_ally" in row else (role == _ROLE_ALLY))
        quest_enemy = int(row.get("quest_enemy_type") or 0) & 0xFF
        if is_passive_kind and quest_enemy <= 0:
            quest_enemy = _QUEST_ENEMY_PASSIVE
        is_scene_dummy = bool(row.get("is_scene_dummy", False))
        if (
            kind == "monster"
            and not is_scene_dummy
            and _is_scene_dummy_script_candidate(row)
        ):
            is_scene_dummy = mon_has_scene_dummy_wait_action(
                int(row.get("code") or 0)
            )
        is_blocking = bool(
            row.get("is_blocking", kind == "monster" and is_hostile)
        ) and not is_scene_dummy

        out.append(
            {
                "kind": kind,
                "template_order": tmpl & 0xFFFF,
                "aux": 0,
                "aux2": 0,
                "slot_index": slot & 0xFFFFFFFF,
                "seq": (int(first_seq) + i + 1) & 0xFFFF,
                "code": int(row.get("code") or 0) & 0xFFFFFFFF,
                "level": level & 0xFF,
                "type": mon_type,
                "flag0": int(row.get("flag0") or 0) & 0xFF,
                "flag1": int(row.get("flag1") or 0) & 0xFF,
                "extra_state": int(extra_state) & 0xFFFFFFFF,
                "extra_flag": 0,
                "quest_enemy_type": quest_enemy,
                "is_scene_dummy": is_scene_dummy,
                "is_blocking": is_blocking,
                "grants_exp": bool(row.get("grants_exp", kind == "monster" and is_hostile)),
                "killable": bool(row.get("killable", True)),
                "faction": str(row.get("faction") or ("monster" if is_hostile else "character")),
                "role": role,
                "is_hostile": is_hostile,
                "is_ally": is_ally,
                "team": int(row.get("team") or 0) if row.get("team") is not None else None,
                "x": row.get("x"),
                "y": row.get("y"),
            }
        )
    return out


# ---------------------------------------------------------------------------
# 剧情对话 face 预装（全副本通用）
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _monster_lst_paths() -> dict[int, str]:
    """monster/monster.lst → code → 相对 path（可能无 monster/ 前缀）。"""
    try:
        from game.drop.pvf_section import parse_lst_entries

        text = pvf_runtime.read_text("monster/monster.lst")
    except Exception:
        return {}
    out: dict[int, str] = {}
    for code, rel in parse_lst_entries(text or ""):
        path = str(rel or "").replace("\\", "/").lstrip("/")
        if int(code) > 0 and path:
            out[int(code)] = path
    return out


def resolve_monster_script_path(code: int) -> str:
    """mon code → ``monster/...`` 脚本路径；找不到返回空串。"""
    mid = int(code or 0)
    if mid <= 0:
        return ""
    rel = _monster_lst_paths().get(mid, "")
    if not rel:
        return ""
    if not rel.lower().startswith("monster/"):
        rel = "monster/" + rel
    return rel


@lru_cache(maxsize=8192)
def mon_has_scene_dummy_wait_action(code: int) -> bool:
    """Return whether a monster script explicitly uses ``Action/dummy.act``.

    This is intentionally narrower than positional heuristics: PVF story maps
    use the dummy action for client-owned scene actors which must not hold the
    server's room-clear latch.
    """
    path = resolve_monster_script_path(int(code or 0))
    if not path:
        return False
    try:
        text = pvf_runtime.read_text(path)
    except Exception:
        return False
    match = _WAITING_ACTION_BLOCK.search(str(text or ""))
    if match is None:
        return False
    return any(
        _strip_tick(token).replace("\\", "/").strip().lower()
        == "action/dummy.act"
        for token in _tokenize(match.group(1) or "")
    )


@lru_cache(maxsize=8192)
def mon_has_face_image(code: int) -> bool:
    """``.mob`` 是否含 ``[face image]``（对话头像依赖该资源已加载）。"""
    path = resolve_monster_script_path(int(code or 0))
    if not path:
        return False
    try:
        text = pvf_runtime.read_text(path)
    except Exception:
        return False
    if not text:
        return False
    return _FACE_IMAGE_BLOCK.search(text) is not None


def clear_monster_script_cache() -> None:
    """Clear cached monster-script metadata after a PVF reload or test mock."""
    _monster_lst_paths.cache_clear()
    mon_has_face_image.cache_clear()
    mon_has_scene_dummy_wait_action.cache_clear()


def parse_event_monster_positions(raw: str) -> list[tuple[int, int]]:
    """``[event monster position]`` → (x,y) 列表（z 忽略）。

    常见 PVF 为 ``x y z`` 三元组；不足三列时按 ``x y`` 成对回退。
    """
    text = str(raw or "")
    m = _EVENT_MONSTER_POS_BLOCK.search(text)
    if not m:
        return []
    nums: list[int] = []
    for tok in _tokenize(m.group(1) or ""):
        try:
            nums.append(int(_strip_tick(tok), 0))
        except ValueError:
            continue
    out: list[tuple[int, int]] = []
    if len(nums) >= 3 and len(nums) % 3 == 0:
        for i in range(0, len(nums), 3):
            out.append((int(nums[i]), int(nums[i + 1])))
        return out
    for i in range(0, len(nums) - 1, 2):
        out.append((int(nums[i]), int(nums[i + 1])))
    return out


def list_maze_map_ids(
    dungeon_id: int,
    maze_index: int = 0,
) -> list[int]:
    """当前 maze 全部 map_id（map_specs + start/boss 格）。顺序：specs 原序。"""
    did = int(dungeon_id or 0)
    if did <= 0:
        return []
    try:
        from game.quest.quest_index import get_quest_index

        dgn = get_quest_index().get_dungeon(did)
    except Exception:
        return []
    mazes = list(getattr(dgn, "mazes", None) or [])
    if not mazes:
        return []
    mi = int(maze_index or 0)
    if mi < 0 or mi >= len(mazes):
        mi = 0
    maze = mazes[mi]
    # 尽量带 map_specs（start_room 会 enrich；索引侧也可能已有）
    specs = list(getattr(maze, "map_specs", None) or [])
    if not specs:
        try:
            from game.chain_loader import load_chain

            _sr = load_chain("helper-dungeon_start_room.py")
            maze = _sr._load_maze_details(did, mi)  # type: ignore[attr-defined]
            specs = list(getattr(maze, "map_specs", None) or [])
        except Exception:
            specs = list(getattr(maze, "map_specs", None) or [])
    out: list[int] = []
    seen: set[int] = set()
    for sp in specs:
        mid = int(sp.get("map_id") or 0)
        if mid > 0 and mid not in seen:
            seen.add(mid)
            out.append(mid)
        for cand in list(sp.get("candidates") or []):
            try:
                c = int(cand)
            except (TypeError, ValueError):
                continue
            if c > 0 and c not in seen:
                seen.add(c)
                out.append(c)
    return out


def collect_map_actor_codes(map_index: int) -> set[int]:
    """单图 mon + ai character 的 code 集合（不含 special passive 本体）。"""
    mid = int(map_index or 0)
    if mid <= 0:
        return set()
    path = resolve_map_path(mid)
    if not path:
        return set()
    try:
        text = pvf_runtime.read_text(path)
    except Exception:
        return set()
    codes: set[int] = set()
    for row in parse_map_monster_tokens(text):
        c = int(row.get("code") or 0)
        if c > 0:
            codes.add(c)
    for row in parse_map_ai_character_tokens(text):
        c = int(row.get("code") or 0)
        if c > 0:
            codes.add(c)
    return codes


def collect_maze_face_speaker_codes(
    dungeon_id: int,
    maze_index: int = 0,
    *,
    exclude_map_index: int = 0,
) -> list[int]:
    """同迷宫其它房中、带 ``[face image]`` 的 mon/APC code（去重、排序）。

    优先级（通用，非单本硬编码）：
      1. 出现在 boss 格 map 上的 face mon（剧情命名体常在 boss 房）
      2. 在迷宫内出现次数更少的 code（杂兵多房复用；发言人常独有）
      3. code 升序稳定排序
    """
    did = int(dungeon_id or 0)
    if did <= 0:
        return []
    map_ids = list_maze_map_ids(did, int(maze_index or 0))
    if not map_ids:
        return []
    exclude = int(exclude_map_index or 0)
    # boss map 集合
    boss_maps: set[int] = set()
    try:
        from game.quest.quest_index import get_quest_index

        dgn = get_quest_index().get_dungeon(did)
        mazes = list(getattr(dgn, "mazes", None) or [])
        mi = int(maze_index or 0)
        if mi < 0 or mi >= len(mazes):
            mi = 0
        maze = mazes[mi] if mazes else None
        if maze is not None:
            try:
                from game.chain_loader import load_chain

                _sr = load_chain("helper-dungeon_start_room.py")
                maze = _sr._load_maze_details(did, mi)  # type: ignore[attr-defined]
            except Exception:
                pass
            specs = list(getattr(maze, "map_specs", None) or [])
            boss_cells = {
                (int(x), int(y)) for x, y in list(getattr(maze, "boss_maps", None) or [])
            }
            for sp in specs:
                key = (int(sp.get("x", -1)), int(sp.get("y", -1)))
                typ = str(sp.get("type") or "").lower()
                mid = int(sp.get("map_id") or 0)
                if mid > 0 and (typ == "boss" or key in boss_cells):
                    boss_maps.add(mid)
    except Exception:
        boss_maps = set()

    # code → 出现的 map 数 / 是否在 boss 房
    code_maps: dict[int, set[int]] = {}
    code_on_boss: set[int] = set()
    for mid in map_ids:
        if mid == exclude:
            continue
        for code in collect_map_actor_codes(mid):
            code_maps.setdefault(int(code), set()).add(int(mid))
            if mid in boss_maps:
                code_on_boss.add(int(code))

    scored: list[tuple[int, int, int, int]] = []
    for code, maps in code_maps.items():
        if not mon_has_face_image(code):
            continue
        on_boss = 0 if code in code_on_boss else 1  # boss 优先 → 0 在前
        rarity = len(maps)  # 越少越优先
        scored.append((on_boss, rarity, int(code), int(code)))
    scored.sort()
    return [c for _ob, _r, _c, c in scored]


def _parse_xy_pair(x: Any, y: Any) -> tuple[int, int] | None:
    try:
        if x is None or y is None:
            return None
        return int(x), int(y)
    except (TypeError, ValueError):
        return None


def _pick_preload_xy(
    map_index: int,
    existing: Sequence[dict[str, Any]],
) -> tuple[int, int]:
    """预装坐标：优先 event monster position，其次现有 mon 旁，最后屏外。"""
    path = resolve_map_path(int(map_index or 0))
    if path:
        try:
            text = pvf_runtime.read_text(path)
            pos = parse_event_monster_positions(text)
            if pos:
                return int(pos[0][0]), int(pos[0][1])
        except Exception:
            pass
    for mon in list(existing or []):
        xy = _parse_xy_pair(mon.get("x"), mon.get("y"))
        if xy is not None:
            # 略偏移，避免与第一只怪完全叠点
            return int(xy[0]), int(xy[1])
    # 屏外：客户端仍会装 mon 模板；坐标不参与 wire 001D 行
    return -1000, -1000


def build_story_face_preload_row(
    code: int,
    *,
    level: int = 1,
    seq: int = 1,
    slot_index: int = 0,
    template_order: int = 0,
    x: Any = None,
    y: Any = None,
) -> dict[str, Any]:
    """构造 001D 剧情 face 预装行：team=0 友方，不挡门、不给经验。"""
    return {
        "kind": "monster",
        "template_order": int(template_order) & 0xFFFF,
        "aux": 0,
        "aux2": 0,
        "slot_index": int(slot_index) & 0xFFFFFFFF,
        "seq": int(seq) & 0xFFFF,
        "code": int(code) & 0xFFFFFFFF,
        "level": max(1, int(level) & 0xFF),
        "type": 0,
        "flag0": 0,
        "flag1": 0,
        "extra_state": 0,
        "extra_flag": 0,
        "quest_enemy_type": 0,
        "is_blocking": False,
        "grants_exp": False,
        "killable": False,
        "faction": "character",
        "role": _ROLE_ALLY,
        "is_hostile": False,
        "is_ally": True,
        "team": 0,
        "x": x,
        "y": y,
        "story_face_preload": True,
    }


def inject_story_face_preloads(
    mons: list[dict[str, Any]] | None,
    *,
    dungeon_id: int,
    map_index: int,
    maze_index: int = 0,
    dungeon_basic_lv: int = 0,
    first_seq: int = 0,
) -> list[dict[str, Any]]:
    """把同迷宫其它房的 face mon 以友方注入当前 001D（通用）。

    适用：任意地区地下城开场/过图剧情对话框需要 speaker face，但发言体
    不在当前 map 的 ``[monster]`` 表（例：遗忘之森 17096 开场要 69271）。

    规则：
      - 仅注入 **当前表未出现** 的 code
      - 候选来自 **同 dungeon + maze** 其它 map（boss 房优先）
      - 候选须 mon 脚本含 ``[face image]``
      - team=0 / ally / 不挡门 / 不给经验 / killable=False
      - 数量上限 ``USDOF_DUNGEON_STORY_FACE_PRELOAD_MAX``（默认 8）
      - ``USDOF_DUNGEON_STORY_FACE_PRELOAD=0`` 关闭

    不绑 dungeon_id / map_id 白名单；全副本统一走 maze map_specs。
    """
    base = list(mons or [])
    # 默认 OFF：方案 A 已证伪（多怪可见 + 头像仍无）
    if not _env_bool(_ENV_STORY_FACE_PRELOAD, False):
        return base
    did = int(dungeon_id or 0)
    mid = int(map_index or 0)
    if did <= 0 or mid <= 0:
        return base

    present: set[int] = set()
    for m in base:
        try:
            c = int(m.get("code") or 0)
        except (TypeError, ValueError):
            continue
        if c > 0:
            present.add(c)

    candidates = collect_maze_face_speaker_codes(
        did,
        int(maze_index or 0),
        exclude_map_index=mid,
    )
    # 再滤：当前房已有的不要
    need = [c for c in candidates if c not in present]
    if not need:
        return base

    max_n = max(0, _env_int(_ENV_STORY_FACE_PRELOAD_MAX, _DEFAULT_STORY_FACE_PRELOAD_MAX))
    if max_n <= 0:
        return base
    need = need[:max_n]

    level = max(1, min(255, int(dungeon_basic_lv or 0) if int(dungeon_basic_lv or 0) > 0 else 1))
    # seq：接在现有行之后；若 first_seq 给定则以其为基
    if base:
        try:
            last_seq = max(int(m.get("seq") or 0) for m in base)
        except Exception:
            last_seq = int(first_seq or 0)
    else:
        last_seq = int(first_seq or 0)
    next_slot = len(base)
    px, py = _pick_preload_xy(mid, base)

    for i, code in enumerate(need):
        row = build_story_face_preload_row(
            code,
            level=level,
            seq=(last_seq + i + 1) & 0xFFFF,
            slot_index=(next_slot + i) & 0xFFFFFFFF,
            template_order=(next_slot + i) & 0xFFFF,
            x=px,
            y=py,
        )
        base.append(row)
        present.add(int(code))
    return base


def actor_is_hostile(mon: dict[str, Any] | None) -> bool:
    """是否敌对（可计任务/经验/掉落的战斗目标）。"""
    if not mon:
        return False
    if "is_hostile" in mon:
        return bool(mon.get("is_hostile"))
    role = str(mon.get("role") or "").lower()
    if role:
        return role == _ROLE_ENEMY
    fac = str(mon.get("faction") or "").lower()
    if fac in {"character", "neutral", "friend", "ally"}:
        return False
    if fac == "monster":
        return True
    # 普通怪默认敌对；AI 缺省友方更安全
    kind = str(mon.get("kind") or "monster").lower()
    return kind != "ai_character"


def actor_is_ally(mon: dict[str, Any] | None) -> bool:
    if not mon:
        return False
    if "is_ally" in mon:
        return bool(mon.get("is_ally"))
    return str(mon.get("role") or "").lower() == _ROLE_ALLY


def actor_counts_for_combat(mon: dict[str, Any] | None) -> bool:
    """击杀是否走完整战斗链（经验/掉落/任务 residual）。

    队友 / 中立 APC：只消亡 0026，不给奖励、不计 hunt。
    可破坏建筑（type9 / passive object）：走 combat 链以计 hunt residual，
    但 grants_exp=false、is_hostile=false → 不给经验；物品掉落见 DIE 专用分支。
    """
    if not mon:
        return False
    if is_passive_actor(mon) and bool(mon.get("killable", True)):
        return True
    if not bool(mon.get("killable", True)) and not actor_is_hostile(mon):
        return False
    if actor_is_ally(mon):
        return False
    if not actor_is_hostile(mon) and str(mon.get("kind") or "") == "ai_character":
        return False
    return True


def room_blocking_alive(mons: list[dict[str, Any]] | None) -> int:
    """参与通关门闩的存活 actor 数（默认 mon is_blocking）。"""
    n = 0
    for m in list(mons or []):
        if m.get("killed"):
            continue
        if not bool(m.get("is_blocking", True)):
            continue
        n += 1
    return n


def room_has_blocking(mons: list[dict[str, Any]] | None) -> bool:
    return any(bool(m.get("is_blocking", True)) for m in list(mons or []))
