"""Hell Party room resolution and PVF wave templates.

Runtime uses the shared pvf_runtime reader only. The selected Hell room is
stored on the dungeon session; callers still build packets through the normal
001C/001D helpers.
"""
from __future__ import annotations

import difflib
import os
import random
import re
from functools import lru_cache
from typing import Any, Iterable

from game.chain_loader import load_chain
from game.quest.quest_index import get_quest_index
from pvf import pvf_runtime

_map_mons = load_chain("helper-dungeon_map_monsters.py")
_start_room = load_chain("helper-dungeon_start_room.py")

_BT = chr(96)
_HELLPARTY_BLOCK = re.compile(
    "(?is)" + re.escape(_BT) + r"?\[hellparty\]" + re.escape(_BT)
    + r"?(.*?)" + re.escape(_BT) + r"?\[/hellparty\]" + re.escape(_BT) + r"?"
)
_SPECIAL_BLOCK = re.compile(
    r"(?is)\[special passive object\](.*?)\[/special passive object\]"
)
_MAP_COORD_GROUP = re.compile(r"\(([^)]*)\)")
_MAP_COORD_PAIR = re.compile(r"(\d+)\s*[,.]\s*(\d+)")
_TOKEN_RE = re.compile(re.escape(_BT) + r"([^" + re.escape(_BT) + r"]*)" + re.escape(_BT) + r"|(\S+)")
_HELL_MONSTER_FLAG = re.compile(r"(?is)\[hell monster\]\s*`?\s*1\b")
_MAP_SIG_WS = re.compile(r"\s+")
_MAP_SIG_DIGIT = re.compile(r"\d+")
_MAP_SIG_STRIP_BLOCKS = (
    _HELLPARTY_BLOCK,
    _SPECIAL_BLOCK,
    re.compile(r"(?is)\[monster\].*?\[/monster\]"),
    re.compile(r"(?is)\[monster team\].*?\[/monster team\]"),
    re.compile(r"(?is)\[ai character\].*?\[/ai character\]"),
    re.compile(r"(?is)\[passive object\].*?\[/passive object\]"),
)

_HELL_HIDDEN_TEMPLATE_FLAG = 1
_HELL_ATTACH_ALL_WAVES = 0xFF
_HELL_WAVE_MODE_ALL = "all"
_HELL_WAVE_MODE_RANDOM_ONE = "random_one"


def _strip_tick(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == _BT and text[-1] == _BT:
        return text[1:-1].strip()
    return text


def _map_filename(path: str) -> str:
    return str(path or "").replace("\\", "/").rsplit("/", 1)[-1].lower()


def _is_hell_map_path(path: str) -> bool:
    return "hell" in _map_filename(path)


def _tokenize(text: str) -> list[str]:
    toks: list[str] = []
    for a, b in _TOKEN_RE.findall(str(text or "")):
        toks.append(a if a != "" else b)
    return toks


def parse_hellparty_entries_from_map(raw: str) -> list[dict[str, int]]:
    """Parse nested hellparty blocks as group/rate/order triples."""
    out: list[dict[str, int]] = []
    for block in _HELLPARTY_BLOCK.findall(str(raw or "")):
        vals: list[int] = []
        for tok in _tokenize(block):
            try:
                vals.append(int(_strip_tick(tok), 0))
            except ValueError:
                continue
        for i in range(0, len(vals) - 2, 3):
            group_id, rate, order = vals[i : i + 3]
            if group_id <= 0:
                continue
            out.append(
                {
                    "group_id": int(group_id),
                    "rate": max(0, int(rate)),
                    "order": int(order),
                }
            )
    return out


def parse_hellparty_pillar(raw: str) -> dict[str, int]:
    """Return the first special passive object that owns a hellparty block."""
    text = str(raw or "")
    m = _SPECIAL_BLOCK.search(text)
    body = m.group(1) if m else ""
    if not body:
        return {}
    hell_match = _HELLPARTY_BLOCK.search(body)
    has_hellparty = bool(hell_match)
    prefix = body[: hell_match.start()] if hell_match else body
    nums: list[int] = []
    for tok in _tokenize(prefix):
        try:
            nums.append(int(_strip_tick(tok), 0))
        except ValueError:
            continue
    if len(nums) < 5:
        return {}
    object_code, pos_x, pos_y, flags, _spawn_count = nums[-5:]
    return {
        "code": int(object_code),
        "x": int(pos_x),
        "y": int(pos_y),
        "flags": int(flags),
        "has_hellparty": int(has_hellparty),
    }


def _hell_mode_to_group_key(hell_party_mode: int) -> str:
    return "B" if int(hell_party_mode or 0) == 2 else "A"


def hell_party_reward_roll_count_for_mode(hell_party_mode: int) -> int:
    """Return the PVF equipment-roll count for a Hell Party mode.

    The same ``[difficulty]`` table applies to ordinary Hell Party groups and
    special-Hell final bosses.  The latter do not have a generated Hell room,
    so they must resolve the value directly instead of inheriting an actor
    field.
    """
    mode_key = _hell_mode_to_group_key(hell_party_mode)
    rule = _hellparty_difficulty_rules().get(mode_key) or {}
    return max(0, int(rule.get("reward_roll_count") or 0))


def hell_party_wave_mode(value: str | None = None) -> str:
    """Resolve the local Hell Party wave switch without changing PVF data.

    ``all`` keeps every valid PVF ``order`` wave. ``random_one`` picks one
    valid order after that order selected its weighted PVF group. Unknown
    values deliberately retain the existing all-wave behavior.
    """
    raw = (
        str(value)
        if value is not None
        else str(os.environ.get("USDOF_GAME_DUNGEON_HELL_WAVE_MODE") or "")
    )
    normalized = raw.strip().lower().replace("-", "_")
    if normalized in {"random_one", "one", "single", "1"}:
        return _HELL_WAVE_MODE_RANDOM_ONE
    return _HELL_WAVE_MODE_ALL


@lru_cache(maxsize=1)
def _hellparty_difficulty_rules() -> dict[str, dict[str, int]]:
    """Read ``hellparty.etc [difficulty]`` for the PVF reward roll count."""
    try:
        text = pvf_runtime.read_text("etc/hellparty.etc")
    except Exception:
        return {}
    match = re.search(r"(?is)\[difficulty\](.*?)\[/difficulty\]", text or "")
    if not match:
        return {}

    tokens = _tokenize(match.group(1) or "")
    out: dict[str, dict[str, int]] = {}
    pos = 0
    while pos < len(tokens):
        key = _strip_tick(tokens[pos]).upper()
        if len(key) != 1 or not key.isalpha():
            pos += 1
            continue
        pos += 1
        values: list[int] = []
        while pos < len(tokens) and len(values) < 5:
            token = _strip_tick(tokens[pos])
            pos += 1
            try:
                values.append(int(token, 0))
            except ValueError:
                continue
        if len(values) != 5:
            continue
        out[key] = {
            "reward_roll_count": max(0, int(values[0])),
            "unknown1": int(values[1]),
            "ratio_probability": int(values[2]),
            "probability": int(values[3]),
            "unknown4": int(values[4]),
        }
    return out


@lru_cache(maxsize=1)
def _hellparty_groups() -> dict[int, dict[str, list[dict[str, int]]]]:
    try:
        text = pvf_runtime.read_text("etc/hellparty.etc")
    except Exception:
        return {}

    out: dict[int, dict[str, list[dict[str, int]]]] = {}
    for match in re.finditer(
        r"(?is)\[group index\](.*?)\[group\](.*?)\[/group\]",
        text or "",
    ):
        idx_tokens = _tokenize(match.group(1) or "")
        group_id = 0
        for tok in idx_tokens:
            try:
                group_id = int(_strip_tick(tok), 0)
                break
            except ValueError:
                continue
        if group_id <= 0:
            continue

        mode = ""
        pending_code: int | None = None
        target = out.setdefault(group_id, {})
        for tok in _tokenize(match.group(2) or ""):
            s = _strip_tick(tok)
            if len(s) == 1 and s.isalpha():
                mode = s.upper()
                pending_code = None
                target.setdefault(mode, [])
                continue
            try:
                val = int(s, 0)
            except ValueError:
                continue
            if not mode:
                mode = "A"
            if pending_code is None:
                pending_code = val
                continue
            target.setdefault(mode, []).append(
                {
                    "code": int(pending_code),
                    "entity_type": 1 if int(val) == 1 else 0,
                }
            )
            pending_code = None
    return out


@lru_cache(maxsize=1)
def _hell_map_paths() -> tuple[tuple[int, str], ...]:
    return tuple(
        (int(map_id), str(path))
        for map_id, path in _map_mons._map_lst().items()
        if _is_hell_map_path(path)
    )


def warmup_hell_party_index() -> dict[str, int]:
    groups = _hellparty_groups()
    rules = _hellparty_difficulty_rules()
    entries = 0
    for by_mode in groups.values():
        for rows in by_mode.values():
            entries += len(rows)
    return {
        "hell_group_count": len(groups),
        "hell_group_entries": entries,
        "hell_map_count": len(_hell_map_paths()),
        "hell_difficulty_rule_count": len(rules),
    }


def _group_has_entries(group_id: int, mode_key: str) -> bool:
    rows = _hellparty_groups().get(int(group_id), {}).get(str(mode_key or "A"), [])
    return bool(rows)


@lru_cache(maxsize=8192)
def is_hell_monster_script(code: int, entity_type: int) -> bool:
    """Read the actor script's PVF ``[hell monster]`` flag.

    A Hell Party group still receives its normal independent-drop channel for
    this actor, but its final member must not create the group equipment
    reward when this flag is set.
    """
    monster_code = int(code or 0)
    if monster_code <= 0:
        return False
    try:
        if int(entity_type) == 1:
            path = str(_map_mons._ai_character_lst().get(monster_code) or "")
        else:
            path = str(_map_mons.resolve_monster_script_path(monster_code) or "")
        if not path:
            return False
        text = pvf_runtime.read_text(path.replace("\\", "/").lower())
    except Exception:
        return False
    return bool(_HELL_MONSTER_FLAG.search(text or ""))


def _pick_entry_for_order(
    entries: list[dict[str, int]],
    *,
    dungeon_id: int,
    map_index: int,
    hell_party_mode: int,
    wave_selection_seed: int | None = None,
) -> dict[str, int] | None:
    if not entries:
        return None
    total = sum(max(0, int(e.get("rate") or 0)) for e in entries)
    if total <= 0:
        return entries[0]
    seed = (
        (int(dungeon_id) * 0x45D9F3B)
        ^ (int(map_index) * 0x119DE1F3)
        ^ (int(hell_party_mode) * 0x9E37)
        ^ (int(entries[0].get("order") or 0) * 0x85EB)
    ) & 0xFFFFFFFF
    if wave_selection_seed is not None:
        seed ^= (int(wave_selection_seed) * 0x27D4EB2D) & 0xFFFFFFFF
        seed &= 0xFFFFFFFF
    roll = random.Random(seed).randrange(total)
    for entry in entries:
        rate = max(0, int(entry.get("rate") or 0))
        if rate <= 0:
            continue
        if roll < rate:
            return entry
        roll -= rate
    return entries[0]


def build_hellparty_waves(
    raw_map_text: str,
    *,
    dungeon_id: int,
    map_index: int,
    hell_party_mode: int,
    basis_level: int,
    wave_mode: str | None = None,
    wave_selection_seed: int | None = None,
) -> list[dict[str, Any]]:
    selected_wave_mode = hell_party_wave_mode(wave_mode)
    mode_key = _hell_mode_to_group_key(hell_party_mode)
    reward_roll_count = hell_party_reward_roll_count_for_mode(hell_party_mode)
    by_order: dict[int, list[dict[str, int]]] = {}
    for entry in parse_hellparty_entries_from_map(raw_map_text):
        group_id = int(entry.get("group_id") or 0)
        if group_id <= 0 or not _group_has_entries(group_id, mode_key):
            continue
        by_order.setdefault(int(entry.get("order") or 0), []).append(entry)

    waves: list[dict[str, Any]] = []
    for order in sorted(by_order):
        selected = _pick_entry_for_order(
            by_order[order],
            dungeon_id=int(dungeon_id),
            map_index=int(map_index),
            hell_party_mode=int(hell_party_mode),
            wave_selection_seed=(
                wave_selection_seed
                if selected_wave_mode == _HELL_WAVE_MODE_RANDOM_ONE
                else None
            ),
        )
        if not selected:
            continue
        group_id = int(selected.get("group_id") or 0)
        monsters = build_hellparty_monster_rows(
            group_id,
            order=order,
            hell_party_mode=hell_party_mode,
            basis_level=basis_level,
            reward_roll_count=reward_roll_count,
        )
        if monsters:
            waves.append(
                {
                    "order": int(order),
                    "group_id": group_id,
                    "rate": int(selected.get("rate") or 0),
                    "reward_roll_count": reward_roll_count,
                    "monsters": monsters,
                }
            )
    if selected_wave_mode == _HELL_WAVE_MODE_RANDOM_ONE and len(waves) > 1:
        # One run seed drives both group-rate and order choice.  The caller
        # stores it on the selected room so room reloads cannot re-roll APCs.
        pick_seed = (
            (int(wave_selection_seed or 0) * 0x9E3779B1)
            ^ (int(dungeon_id) * 0x85EBCA6B)
            ^ (int(map_index) * 0xC2B2AE35)
            ^ (int(hell_party_mode) * 0x27D4EB2D)
        ) & 0xFFFFFFFF
        waves = [waves[random.Random(pick_seed).randrange(len(waves))]]
    return waves


def build_hellparty_monster_rows(
    group_id: int,
    *,
    order: int,
    hell_party_mode: int,
    basis_level: int,
    reward_roll_count: int = 1,
) -> list[dict[str, Any]]:
    mode_key = _hell_mode_to_group_key(hell_party_mode)
    entries = _hellparty_groups().get(int(group_id), {}).get(mode_key, [])
    out: list[dict[str, Any]] = []
    for i, entry in enumerate(entries):
        code = int(entry.get("code") or 0)
        if code <= 0:
            continue
        entity_type = int(entry.get("entity_type") or 0)
        if entity_type == 1:
            mon_type = 5
            level = _map_mons.resolve_ai_character_level(code) or int(basis_level or 1)
            quest_enemy_type = 2
        else:
            mon_type = 0
            level = int(basis_level or 1)
            quest_enemy_type = 1
        is_hell_monster = is_hell_monster_script(code, entity_type)
        out.append(
            {
                "kind": "hell_party",
                "template_order": int(order) & 0xFFFF,
                "slot_index": i,
                "packet_index": None,
                "seq": 0,
                "code": code,
                "level": max(1, min(255, int(level))),
                "type": mon_type,
                "flag0": _HELL_HIDDEN_TEMPLATE_FLAG,
                "flag1": _HELL_ATTACH_ALL_WAVES,
                "extra_state": 0,
                "extra_flag": 0,
                "quest_enemy_type": quest_enemy_type,
                "is_blocking": True,
                "grants_exp": True,
                "killable": True,
                "faction": "monster",
                "role": "enemy",
                "is_hostile": True,
                "is_ally": False,
                "is_hell_party_actor": True,
                "hell_party_group_id": int(group_id),
                "hell_party_order": int(order),
                "hell_party_mode": int(hell_party_mode),
                "hell_reward_roll_count": max(0, int(reward_roll_count)),
                "is_hell_monster_script": is_hell_monster,
            }
        )
    return out


def hell_party_reward_roll_count_on_death(session: Any, actor: Any) -> int:
    """Return the PVF group reward rolls for this death, otherwise zero.

    ``hellparty.etc [difficulty]`` defines a reward count per group, not per
    actor. The final alive member of each group consumes that count, except
    for scripts carrying ``[hell monster] 1``.
    """
    if not isinstance(actor, dict) or not bool(actor.get("is_hell_party_actor")):
        return 0
    if bool(actor.get("is_hell_monster_script")):
        return 0
    group_id = int(actor.get("hell_party_group_id") or 0)
    if group_id <= 0:
        return 0
    order = int(actor.get("hell_party_order") or 0)
    try:
        actor_seq = int(actor.get("seq") or 0)
    except (TypeError, ValueError):
        actor_seq = 0
    for candidate in list(getattr(session, "dungeon_room_mons", None) or []):
        if not isinstance(candidate, dict) or not bool(candidate.get("is_hell_party_actor")):
            continue
        if int(candidate.get("hell_party_group_id") or 0) != group_id:
            continue
        if int(candidate.get("hell_party_order") or 0) != order:
            continue
        if candidate is actor:
            continue
        try:
            candidate_seq = int(candidate.get("seq") or 0)
        except (TypeError, ValueError):
            candidate_seq = 0
        if actor_seq > 0 and candidate_seq == actor_seq:
            continue
        if not bool(candidate.get("killed")):
            return 0
    return max(0, int(actor.get("hell_reward_roll_count") or 0))


def _candidate_dirs_for_maze(maze: Any, dgn_path: str) -> list[str]:
    dirs: list[str] = []

    def add(path: str) -> None:
        p = str(path or "").replace("\\", "/").strip("/")
        if not p:
            return
        if "/" in p:
            p = p.rsplit("/", 1)[0]
        p = p.lower().strip("/")
        if p and p not in dirs:
            dirs.append(p)

    for spec in list(getattr(maze, "map_specs", None) or []):
        mid = int(spec.get("map_id") or 0)
        if mid > 0:
            add(_map_mons.resolve_map_path(mid))
        for cand in list(spec.get("candidates") or []):
            try:
                c = int(cand)
            except (TypeError, ValueError):
                continue
            if c > 0:
                add(_map_mons.resolve_map_path(c))
    base = str(dgn_path or "").replace("\\", "/").rsplit("/", 1)[-1]
    if "." in base:
        base = base.rsplit(".", 1)[0]
    if base:
        add("map/" + base + "/_")
    return dirs


def _path_in_dirs(path: str, dirs: Iterable[str]) -> bool:
    low = str(path or "").replace("\\", "/").strip("/").lower()
    path_parts = [part for part in low.split("/") if part]
    for d in dirs:
        dd = str(d or "").strip("/").lower()
        if not dd:
            continue
        if low.startswith(dd + "/") or low == dd:
            return True
        dir_parts = [part for part in dd.split("/") if part]
        if len(path_parts) < len(dir_parts):
            continue
        prefix_len = len(dir_parts) - 1
        if prefix_len > 0 and path_parts[:prefix_len] != dir_parts[:prefix_len]:
            continue
        if _normalize_path_segment(path_parts[prefix_len]) == _normalize_path_segment(
            dir_parts[-1]
        ):
            return True
    return False


def _normalize_path_segment(value: str) -> str:
    text = str(value or "").strip().lower()
    if text.endswith("_r"):
        return text[:-2]
    return text


def _path_key(value: str) -> str:
    text = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    if "." in text:
        text = text.rsplit(".", 1)[0]
    text = _MAP_COORD_GROUP.sub(" ", text)
    text = text.replace("season", " ")
    text = text.replace("hell", " ")
    text = _normalize_path_segment(text)
    return re.sub(r"[^a-z0-9]+", "", text)


def _path_name_affinity(path: str, dgn_path: str) -> int:
    left = _path_key(path)
    right = _path_key(dgn_path)
    if not left or not right:
        return 0
    if left == right:
        return 80
    if left in right or right in left:
        return 60
    return int(difflib.SequenceMatcher(None, left, right).ratio() * 40)


def _parse_cells_from_path(path: str) -> list[tuple[int, int]]:
    cells: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for body in _MAP_COORD_GROUP.findall(str(path or "")):
        for left, right in _MAP_COORD_PAIR.findall(body):
            cell = (int(left), int(right))
            if cell in seen:
                continue
            seen.add(cell)
            cells.append(cell)
    return cells


def _infer_cells_from_map_paths(
    maze: Any,
    dirs: Iterable[str],
) -> dict[tuple[int, int], int]:
    """Use PVF map filenames only when a dungeon omitted map specifications."""
    start_cells = {
        (int(x), int(y)) for x, y in list(getattr(maze, "start_maps", None) or [])
    }
    boss_cells = {
        (int(x), int(y)) for x, y in list(getattr(maze, "boss_maps", None) or [])
    }
    choices: dict[tuple[int, int], tuple[int, int]] = {}
    for map_id, path in _map_mons._map_lst().items():
        if _is_hell_map_path(path) or not _path_in_dirs(path, dirs):
            continue
        filename = _map_filename(path)
        for cell in _parse_cells_from_path(path):
            priority = 20
            if cell in start_cells and "start" in filename:
                priority = 0
            elif cell in boss_cells and "boss" in filename:
                priority = 0
            elif "start" in filename or "boss" in filename:
                priority = 10
            rank = (priority, int(map_id))
            previous = choices.get(cell)
            if previous is None or rank < previous:
                choices[cell] = rank
    return {cell: map_id for cell, (_priority, map_id) in choices.items()}


def _parse_cell_from_path(path: str) -> tuple[int, int] | None:
    cells = _parse_cells_from_path(path)
    return cells[0] if cells else None


def _map_id_hints_from_path(path: str) -> list[int]:
    base = str(path or "").replace("\\", "/").rsplit("/", 1)[-1]
    if "." in base:
        base = base.rsplit(".", 1)[0]
    base = _MAP_COORD_GROUP.sub(" ", base)
    out: list[int] = []
    seen: set[int] = set()
    for token in re.findall(r"\d+", base):
        value = int(token)
        if value <= 0 or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _map_signature_from_text(raw_map_text: str) -> str:
    text = str(raw_map_text or "").lower()
    for pattern in _MAP_SIG_STRIP_BLOCKS:
        text = pattern.sub(" ", text)
    text = _MAP_SIG_DIGIT.sub("#", text)
    text = _MAP_SIG_WS.sub(" ", text).strip()
    return text[:8192]


@lru_cache(maxsize=8192)
def _map_signature_for_path(path: str) -> str:
    try:
        raw = pvf_runtime.read_text(str(path or "").replace("\\", "/"))
    except Exception:
        return ""
    return _map_signature_from_text(raw)


def _cell_similarity_ratio(
    hell_signature: str,
    *,
    normal_map_id: int,
) -> float:
    if not hell_signature:
        return 0.0
    normal_path = _map_mons.resolve_map_path(int(normal_map_id))
    if not normal_path:
        return 0.0
    normal_signature = _map_signature_for_path(normal_path)
    if not normal_signature:
        return 0.0
    return difflib.SequenceMatcher(None, hell_signature, normal_signature).ratio()


def _best_content_match_cell(
    hell_signature: str,
    *,
    cells: dict[tuple[int, int], int],
    boss_cells: set[tuple[int, int]],
) -> dict[str, Any] | None:
    ranked: list[tuple[int, float, tuple[int, int], int]] = []
    for cell, normal_map_id in cells.items():
        if int(normal_map_id or 0) <= 0:
            continue
        ratio = _cell_similarity_ratio(
            hell_signature,
            normal_map_id=int(normal_map_id),
        )
        if ratio <= 0.0:
            continue
        score = int(ratio * 1000)
        if cell in boss_cells:
            score += 40
        ranked.append((score, ratio, cell, int(normal_map_id)))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (-item[0], item[2][1], item[2][0], item[3]))
    best_score, best_ratio, best_cell, best_map_id = ranked[0]
    return {
        "cell": best_cell,
        "normal_map_id": best_map_id,
        "ratio": best_ratio,
        "score": best_score,
    }


def _candidate_cells_for_hell_map(
    path: str,
    raw_map_text: str,
    *,
    cells: dict[tuple[int, int], int],
    boss_cells: set[tuple[int, int]],
    dgn_path: str,
    allow_foreign_path_cell_fallback: bool = False,
) -> list[dict[str, Any]]:
    hell_signature = _map_signature_from_text(raw_map_text)
    name_bonus = _path_name_affinity(path, dgn_path)
    seen: set[tuple[int, int]] = set()
    out: list[dict[str, Any]] = []

    def add(
        cell: tuple[int, int],
        source: str,
        base_score: int,
        ratio: float | None = None,
    ) -> None:
        if cell in seen or cell not in cells:
            return
        normal_map_id = int(cells.get(cell) or 0)
        if normal_map_id <= 0:
            return
        actual_ratio = (
            _cell_similarity_ratio(hell_signature, normal_map_id=normal_map_id)
            if ratio is None
            else float(ratio)
        )
        score = int(base_score) + name_bonus + int(max(0.0, actual_ratio) * 100)
        if cell in boss_cells:
            score += 25
        out.append(
            {
                "cell": cell,
                "normal_map_id": normal_map_id,
                "source": source,
                "ratio": actual_ratio,
                "score": score,
            }
        )
        seen.add(cell)

    path_cells = _parse_cells_from_path(path)
    for cell in path_cells:
        add(cell, "path_cell", 320)
    if path_cells and (out or not allow_foreign_path_cell_fallback):
        return out

    hinted_ids = set(_map_id_hints_from_path(path))
    if hinted_ids:
        for cell, normal_map_id in cells.items():
            if int(normal_map_id or 0) in hinted_ids:
                add(cell, "map_id_hint", 280)
    if out:
        return out

    best = _best_content_match_cell(
        hell_signature,
        cells=cells,
        boss_cells=boss_cells,
    )
    if best and float(best.get("ratio") or 0.0) >= 0.45:
        add(
            tuple(best["cell"]),
            (
                "foreign_path_content_match"
                if path_cells
                else "content_match"
            ),
            220,
            ratio=float(best.get("ratio") or 0.0),
        )
    if out:
        return out

    if len(boss_cells) == 1:
        add(
            next(iter(boss_cells)),
            "foreign_path_boss_fallback" if path_cells else "boss_fallback",
            140,
            ratio=0.0,
        )
    return out


def _maze_block_for_hell_config(dgn_path: str, maze_index: int) -> str:
    """Return the selected dungeon maze block without relying on map filenames."""
    try:
        text = pvf_runtime.read_text(str(dgn_path or ""))
    except Exception:
        return ""
    blocks = re.findall(r"(?is)\[maze\](.*?)\[/maze\]", text or "")
    if not blocks:
        parts = re.split(r"(?is)\[maze\s*info\]", text or "")
        blocks = [part for part in parts[1:] if str(part).strip()]
    if not blocks:
        return str(text or "")
    index = max(0, min(int(maze_index or 0), len(blocks) - 1))
    return re.split(r"(?is)\[maze\s*info\]|\[maze\]", blocks[index], maxsplit=1)[0]


def _section_int_values(raw: str, section_name: str) -> list[int]:
    match = re.search(
        r"(?is)\[" + re.escape(section_name) + r"\](.*?)(?=\n\s*\[|\Z)",
        str(raw or ""),
    )
    if not match:
        return []
    values: list[int] = []
    for token in _tokenize(match.group(1) or ""):
        try:
            values.append(int(_strip_tick(token), 0))
        except ValueError:
            continue
    return values


def _declared_hell_maps_for_maze(
    dgn_path: str,
    maze_index: int,
) -> list[dict[str, Any]]:
    """Read a maze's authoritative Hell replacement-map declarations.

    The coordinate encoded in a Hell map filename is not necessarily a maze
    coordinate. ``[seal door pos]`` is the PVF field that binds its map index
    to a dungeon cell. Seasonal declarations remain a lower-priority fallback
    when the normal seal-door map is absent or has no ``[hellparty]`` block.
    """
    block = _maze_block_for_hell_config(dgn_path, maze_index)
    out: list[dict[str, Any]] = []
    for map_tag, pos_tag, source, score in (
        ("seal door map index", "seal door pos", "dgn_seal_door", 1200),
        (
            "season seal door map index",
            "season seal door pos",
            "dgn_season_seal_door",
            1100,
        ),
    ):
        map_values = _section_int_values(block, map_tag)
        pos_values = _section_int_values(block, pos_tag)
        if not map_values or len(pos_values) < 2:
            continue
        map_id = int(map_values[0])
        if map_id <= 0:
            continue
        out.append(
            {
                "map_id": map_id,
                "cell": (int(pos_values[0]), int(pos_values[1])),
                "source": source,
                "score": int(score),
            }
        )
    return out


def resolve_hell_party_room(
    dungeon_id: int,
    *,
    difficulty: int = 0,
    maze_index: int = 0,
    hell_party_mode: int = 1,
    wave_mode: str | None = None,
    wave_selection_seed: int | None = None,
) -> dict[str, Any]:
    """Find the current maze Hell replacement map and prebuilt wave templates."""
    did = int(dungeon_id or 0)
    if did <= 0:
        return {"found": False, "reason": "bad_dungeon_id"}
    idx = get_quest_index()
    dgn = idx.get_dungeon(did)
    maze = _start_room._load_maze_details(did, int(maze_index or 0))
    if maze is None:
        return {"found": False, "reason": "no_maze"}
    dirs = _candidate_dirs_for_maze(maze, str(getattr(dgn, "path", "") or ""))
    if not dirs:
        return {"found": False, "reason": "no_candidate_dirs"}

    boss_cells = {
        (int(x), int(y)) for x, y in list(getattr(maze, "boss_maps", None) or [])
    }
    specs = list(getattr(maze, "map_specs", None) or [])
    cells = {
        (int(sp.get("x", -1)), int(sp.get("y", -1))): int(sp.get("map_id") or 0)
        for sp in specs
    }
    if not cells:
        cells = _infer_cells_from_map_paths(maze, dirs)
    candidates: list[
        tuple[int, int, int, int, str, int, dict[str, Any]]
    ] = []
    deferred_path_cell_maps: list[tuple[int, str, str, str]] = []

    def add_candidate(
        map_id: int,
        path: str,
        low: str,
        cell_info: dict[str, Any],
    ) -> None:
        cell = tuple(cell_info.get("cell") or ())
        if len(cell) != 2 or cell not in cells:
            return
        cx, cy = int(cell[0]), int(cell[1])
        score = int(cell_info.get("score") or 0)
        if "season" not in low:
            score += 200
        score -= int(map_id) // 100000
        candidates.append(
            (
                score,
                int(map_id),
                cx,
                cy,
                path,
                int(cell_info.get("normal_map_id") or 0),
                dict(cell_info),
            )
        )

    # A dungeon maze's [seal door map index]/[seal door pos] is the PVF
    # authority. In Grandine 104, for example, Hell_15414's filename says
    # (3,1) while its seal-door position is the non-boss cell (2,1).
    for declared in _declared_hell_maps_for_maze(
        str(getattr(dgn, "path", "") or ""),
        int(maze_index or 0),
    ):
        map_id = int(declared.get("map_id") or 0)
        cell = tuple(declared.get("cell") or ())
        if map_id <= 0 or len(cell) != 2 or cell not in cells:
            continue
        path = str(_map_mons.resolve_map_path(map_id) or "")
        if not path:
            continue
        try:
            raw = pvf_runtime.read_text(path)
        except Exception:
            continue
        if not parse_hellparty_entries_from_map(raw):
            continue
        normal_map_id = int(cells.get(cell) or 0)
        ratio = _cell_similarity_ratio(
            _map_signature_from_text(raw),
            normal_map_id=normal_map_id,
        )
        add_candidate(
            map_id,
            path,
            path.lower(),
            {
                "cell": cell,
                "normal_map_id": normal_map_id,
                "source": str(declared.get("source") or "dgn_seal_door"),
                "ratio": ratio,
                "score": int(declared.get("score") or 0),
            },
        )

    # Filename coordinates and map-content affinity are compatibility
    # fallbacks for PVF maps without a usable seal-door declaration.
    if not candidates:
        for map_id, path in _hell_map_paths():
            low = str(path or "").replace("\\", "/").strip("/").lower()
            if not _is_hell_map_path(path):
                continue
            if not _path_in_dirs(low, dirs):
                continue
            try:
                raw = pvf_runtime.read_text(path)
            except Exception:
                continue
            if not parse_hellparty_entries_from_map(raw):
                continue
            cell_infos = _candidate_cells_for_hell_map(
                path,
                raw,
                cells=cells,
                boss_cells=boss_cells,
                dgn_path=str(getattr(dgn, "path", "") or ""),
            )
            if not cell_infos and _parse_cells_from_path(path):
                deferred_path_cell_maps.append((int(map_id), path, low, raw))
            for cell_info in cell_infos:
                add_candidate(int(map_id), path, low, cell_info)

    # A maze variant may not contain the coordinates encoded by its area's
    # Hell map. Only after no direct PVF coordinate matched do we map that
    # same Hell map back to the closest normal room in the current maze.
    if not candidates:
        for map_id, path, low, raw in deferred_path_cell_maps:
            for cell_info in _candidate_cells_for_hell_map(
                path,
                raw,
                cells=cells,
                boss_cells=boss_cells,
                dgn_path=str(getattr(dgn, "path", "") or ""),
                allow_foreign_path_cell_fallback=True,
            ):
                add_candidate(map_id, path, low, cell_info)

    if not candidates:
        return {"found": False, "reason": "no_hell_map"}
    candidates.sort(key=lambda item: (-item[0], item[1]))
    _score, hell_map_id, hx, hy, path, normal_map_id, selected_cell = candidates[0]
    try:
        raw = pvf_runtime.read_text(path)
    except Exception:
        raw = ""
    basis = int(getattr(dgn, "basis_level", 0) or 0)
    pillar = parse_hellparty_pillar(raw)
    selected_wave_mode = hell_party_wave_mode(wave_mode)
    if (
        selected_wave_mode == _HELL_WAVE_MODE_RANDOM_ONE
        and wave_selection_seed is None
    ):
        wave_selection_seed = random.SystemRandom().getrandbits(32)
    waves = build_hellparty_waves(
        raw,
        dungeon_id=did,
        map_index=hell_map_id,
        hell_party_mode=int(hell_party_mode or 1),
        basis_level=basis,
        wave_mode=selected_wave_mode,
        wave_selection_seed=wave_selection_seed,
    )
    if not waves:
        return {"found": False, "reason": "no_waves", "map_index": hell_map_id}
    return {
        "found": True,
        "dungeon_id": did,
        "difficulty": int(difficulty) & 0xFF,
        "maze_index": int(maze_index or 0),
        "hell_party_mode": int(hell_party_mode or 1),
        "wave_mode": selected_wave_mode,
        "wave_selection_seed": int(wave_selection_seed or 0) & 0xFFFFFFFF,
        "x": hx & 0xFF,
        "y": hy & 0xFF,
        "map_index": int(hell_map_id),
        "normal_map_index": int(normal_map_id),
        "path": path,
        "cell_source": str(selected_cell.get("source") or ""),
        "cell_match_ratio": float(selected_cell.get("ratio") or 0.0),
        "pillar": pillar,
        "waves": waves,
        "wave_count": len(waves),
        "hidden_count": sum(len(w.get("monsters") or []) for w in waves),
        "basis_level": basis,
    }


def bind_hell_party_to_session(session: Any, hell_room: dict[str, Any] | None) -> None:
    if session is None:
        return
    found = bool(hell_room and hell_room.get("found"))
    session.dungeon_hell_enabled = found
    session.dungeon_hell_room = dict(hell_room or {}) if found else None
    session.dungeon_hell_party_mode = int((hell_room or {}).get("hell_party_mode") or 0)
    session.dungeon_hell_wave_mode = str((hell_room or {}).get("wave_mode") or "")
    session.dungeon_hell_wave_selection_seed = int(
        (hell_room or {}).get("wave_selection_seed") or 0
    ) & 0xFFFFFFFF
    session.dungeon_hell_x = int((hell_room or {}).get("x") if found else 0xFF) & 0xFF
    session.dungeon_hell_y = int((hell_room or {}).get("y") if found else 0xFF) & 0xFF
    session.dungeon_hell_map_index = int((hell_room or {}).get("map_index") or 0)
    session.dungeon_hell_normal_map_index = int(
        (hell_room or {}).get("normal_map_index") or 0
    )
    session.dungeon_hell_phase = "waiting_start" if found else ""


def get_session_hell_room(session: Any) -> dict[str, Any] | None:
    if session is None or not bool(getattr(session, "dungeon_hell_enabled", False)):
        return None
    room = getattr(session, "dungeon_hell_room", None)
    return dict(room) if isinstance(room, dict) else None


def is_session_hell_room(session: Any, cell_x: int, cell_y: int) -> bool:
    room = get_session_hell_room(session)
    if not room:
        return False
    return (
        int(room.get("x", -1)) == (int(cell_x) & 0xFF)
        and int(room.get("y", -1)) == (int(cell_y) & 0xFF)
    )


def append_hellparty_templates(
    monsters: Iterable[dict[str, Any]],
    hell_room: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    rows = [dict(m) for m in list(monsters or [])]
    if not hell_room or not hell_room.get("found"):
        return rows
    for wave in list(hell_room.get("waves") or []):
        for mon in list(wave.get("monsters") or []):
            row = dict(mon)
            row["slot_index"] = len(rows)
            rows.append(row)
    return rows
