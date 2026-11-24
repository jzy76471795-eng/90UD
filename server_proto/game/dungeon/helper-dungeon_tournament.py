"""PVF-backed Shonan tournament arena runtime.

Tournament DGN files have no ordinary MAP specification.  Their one arena
MAP declares the tournament candidates, and Stage3 consumes a dedicated
four-round bracket projection through S0/0183, S0/0184, and S0/0185.  The
normal dungeon clear flow is therefore not part of a tournament run.
"""
from __future__ import annotations

import random
import re
import secrets
import struct
import threading
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from game.chain_loader import load_chain
from game.quest.quest_index import get_quest_index
from pvf import pvf_runtime


_TOURNAMENT_ROUND_COUNT = 4
_TEAM_COUNT = 16
_NPC_TEAM_COUNT = _TEAM_COUNT - 1
_TOURNAMENT_REWARD_CARD_TYPE_COUNT = 2
_TOURNAMENT_REWARD_CARDS_PER_TYPE = 2
_TOURNAMENT_REWARD_STATE_AVAILABLE = "available"
_TOURNAMENT_REWARD_STATE_RESERVED = "reserved"
_TOURNAMENT_REWARD_STATE_DELIVERED = "delivered"
_TOURNAMENT_REWARD_MANUAL_REFRESH_SECONDS = 1.0
# Stage3 S1/01CB's per-card values are party slots, rather than booleans.
# The three supported arenas are solo-only, so slot zero denotes this player
# and 0xFF denotes a card that remains unselected.
_TOURNAMENT_REWARD_SELECTED_PARTY_SLOT = 0
_TOURNAMENT_REWARD_UNSELECTED_PARTY_SLOT = 0xFF
# This flow is intentionally closed to the three Shonan tournament arenas.
# Do not let an unrelated PVF `[tournament dungeon]` marker route a normal
# dungeon through the arena state machine.
_SHONAN_TOURNAMENT_MAP_BY_DUNGEON: Mapping[int, int] = MappingProxyType(
    {
        120: 17100,
        121: 17101,
        122: 17102,
    }
)
_SHONAN_TOURNAMENT_DUNGEON_IDS = frozenset(
    _SHONAN_TOURNAMENT_MAP_BY_DUNGEON
)
_INDEX_LOCK = threading.RLock()
_INDEX_READY = False
_SPECS_BY_DUNGEON: Mapping[int, "TournamentSpec"] = MappingProxyType({})
_INT_TOKEN = re.compile(r"-?\d+")


@dataclass(frozen=True)
class TournamentCandidate:
    """One member definition from MAP ``[tournament enemies]``."""

    source_index: int
    party_count: int
    kind: str
    code: int
    strength: int
    name: str
    native_level: int = 0

    @property
    def tier(self) -> int:
        """Compatibility name for older diagnostics that called it a tier."""
        return int(self.strength)


@dataclass(frozen=True)
class TournamentTeam:
    position: int
    members: Tuple[TournamentCandidate, ...]
    is_player: bool = False

    @property
    def total_strength(self) -> int:
        return sum(max(0, int(member.strength)) for member in self.members)


@dataclass(frozen=True)
class TournamentRound:
    number: int
    teams: Tuple[TournamentTeam, ...]


@dataclass(frozen=True)
class TournamentReward:
    item_id: int
    amount: int

    @property
    def is_gold(self) -> bool:
        return int(self.item_id) == 0


@dataclass(frozen=True)
class TournamentSpec:
    """One fully resolved DGN/MAP tournament pairing."""

    dungeon_id: int
    dungeon_path: str
    map_index: int
    map_path: str
    start_cell: Tuple[int, int]
    boss_cell: Tuple[int, int]
    start_areas: Tuple[Tuple[int, int, int, int], ...]
    candidates: Tuple[TournamentCandidate, ...]
    party_limit: int
    basis_level: int
    round_fatigue: int
    enter_without_fatigue: bool
    clear_reward_exp: Tuple[Tuple[int, int], ...]
    result_cards: Tuple[Tuple[int, int, int, int], ...]
    reward_item_rates: Tuple[Tuple[int, int, int], ...]
    clear_reward_gold_rate: int

    @property
    def round_count(self) -> int:
        return _TOURNAMENT_ROUND_COUNT

    @property
    def start_area(self) -> Tuple[int, ...]:
        """Legacy first-area view retained for callers that only need solo."""
        return tuple(self.start_areas[0]) if self.start_areas else ()

    def candidates_for_party(self) -> Tuple[TournamentCandidate, ...]:
        return tuple(
            row
            for row in self.candidates
            if int(row.party_count) == int(self.party_limit)
        )

    def reward_exp_for_round(self, completed_rounds: int) -> int:
        target = max(0, min(_TOURNAMENT_ROUND_COUNT, int(completed_rounds)))
        for round_number, experience in self.clear_reward_exp:
            if int(round_number) == target:
                return max(0, int(experience))
        return 0

    def result_card_for_key(self, key: int) -> Tuple[int, int, int]:
        for result_key, gold_weight, item_weight, empty_weight in self.result_cards:
            if int(result_key) == int(key):
                return (
                    max(0, int(gold_weight)),
                    max(0, int(item_weight)),
                    max(0, int(empty_weight)),
                )
        return 0, 0, 0


def _normalize_path(path: str) -> str:
    return str(path or "").replace("\\", "/").strip("/")


def _section_text(text: str, name: str) -> str:
    escaped = re.escape(str(name or ""))
    match = re.search(
        r"(?is)`?\[" + escaped + r"\]`?(.*?)(?=\r?\n\s*`?\[|\Z)",
        str(text or ""),
    )
    return (match.group(1) if match else "").strip()


def _all_section_texts(text: str, name: str) -> Tuple[str, ...]:
    escaped = re.escape(str(name or ""))
    pattern = re.compile(
        r"(?is)`?\[" + escaped + r"\]`?(.*?)(?=\r?\n\s*`?\[|\Z)"
    )
    return tuple(match.group(1).strip() for match in pattern.finditer(str(text or "")))


def _outer_section(text: str, name: str) -> str:
    escaped = re.escape(str(name or ""))
    match = re.search(
        r"(?is)`?\[" + escaped + r"\]`?(.*?)`?\[/" + escaped + r"\]`?",
        str(text or ""),
    )
    return (match.group(1) if match else "").strip()


def _int_values(text: str) -> List[int]:
    return [int(value) for value in _INT_TOKEN.findall(str(text or ""))]


def _numeric_lines(text: str) -> Tuple[Tuple[int, ...], ...]:
    rows: List[Tuple[int, ...]] = []
    for line in str(text or "").splitlines():
        values = tuple(int(value) for value in _INT_TOKEN.findall(line))
        if values:
            rows.append(values)
    return tuple(rows)


def _tokens(text: str) -> List[str]:
    values: List[str] = []
    for quoted, raw in re.findall(r"`([^`]*)`|(\S+)", str(text or "")):
        value = quoted if quoted != "" else raw
        value = str(value or "").strip()
        if value:
            values.append(value)
    return values


def _normalize_candidate_kind(token: str) -> str:
    value = str(token or "").strip().strip("`").strip().lower()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1].strip()
    if value in {"apc", "ai character", "ai_character"}:
        return "apc"
    if value == "monster":
        return "monster"
    return ""


def _parse_candidates(map_text: str) -> Tuple[TournamentCandidate, ...]:
    values = _tokens(_outer_section(map_text, "tournament enemies"))
    if not values:
        return ()

    cursor = 0
    party_count = 1
    if cursor < len(values):
        try:
            party_count = int(values[cursor], 0)
            cursor += 1
        except ValueError:
            pass
    if party_count <= 0 or cursor >= len(values):
        return ()
    kind = _normalize_candidate_kind(values[cursor])
    cursor += 1
    if not kind:
        return ()

    candidates: List[TournamentCandidate] = []
    while cursor + 2 < len(values):
        try:
            code = int(values[cursor], 0)
            strength = int(values[cursor + 1], 0)
        except ValueError:
            break
        name = str(values[cursor + 2] or "").strip()
        cursor += 3
        if code <= 0 or not name:
            continue
        candidates.append(
            TournamentCandidate(
                source_index=len(candidates),
                party_count=int(party_count),
                kind=kind,
                code=int(code),
                strength=max(0, int(strength)),
                name=name,
            )
        )
    return tuple(candidates)


def _dungeon_is_tournament(dgn_text: str) -> bool:
    values = _int_values(_section_text(dgn_text, "tournament dungeon"))
    return bool(values and int(values[0]) != 0)


def _has_dgn_flag(dgn_text: str, name: str) -> bool:
    escaped = re.escape(str(name or ""))
    return re.search(r"(?im)^\s*`?\[" + escaped + r"\]`?", dgn_text or "") is not None


def _first_maze_cell(maze: Any, attr: str) -> Tuple[int, int]:
    for value in list(getattr(maze, attr, None) or []):
        try:
            return int(value[0]), int(value[1])
        except (IndexError, TypeError, ValueError):
            continue
    return 0, 0


def _parse_start_areas(map_text: str) -> Tuple[Tuple[int, int, int, int], ...]:
    areas: List[Tuple[int, int, int, int]] = []
    for body in _all_section_texts(map_text, "tournament start area"):
        values = _int_values(body)
        if len(values) < 4:
            continue
        party_count, x, y, direction = values[:4]
        if party_count > 0:
            areas.append((int(party_count), int(x), int(y), int(direction)))
    return tuple(areas)


def _parse_clear_reward_exp(dgn_text: str) -> Tuple[Tuple[int, int], ...]:
    rows: Dict[int, int] = {}
    values = _int_values(_section_text(dgn_text, "tournament clear reward exp"))
    for cursor in range(0, len(values) - 1, 2):
        round_number = int(values[cursor])
        if 0 <= round_number <= _TOURNAMENT_ROUND_COUNT:
            rows[round_number] = max(0, int(values[cursor + 1]))
    return tuple(sorted(rows.items()))


def _parse_reward_definition(
    dgn_text: str,
) -> Tuple[Tuple[Tuple[int, int, int, int], ...], Tuple[Tuple[int, int, int], ...]]:
    item_rows: List[Tuple[int, int, int]] = []
    result_body = _outer_section(dgn_text, "result card")
    rate_body = _outer_section(result_body, "reward item rate")
    rate_values = _int_values(rate_body)
    cursor = 0
    while cursor + 2 < len(rate_values) and int(rate_values[cursor]) > 5:
        item_id, weight, count = rate_values[cursor:cursor + 3]
        if int(weight) > 0 and int(count) > 0:
            item_rows.append((int(item_id), int(weight), int(count)))
        cursor += 3

    def _card_rows(values: Sequence[int]) -> Dict[int, Tuple[int, int, int]]:
        rows: Dict[int, Tuple[int, int, int]] = {}
        offset = 0
        while offset + 3 < len(values):
            key, gold_weight, item_weight, empty_weight = values[offset:offset + 4]
            if not 0 <= int(key) <= 5:
                break
            rows[int(key)] = (
                max(0, int(gold_weight)),
                max(0, int(item_weight)),
                max(0, int(empty_weight)),
            )
            offset += 4
        return rows

    # The original US PVF closes [reward item rate] before the six result-card
    # rows. Some translated PVFs kept those rows inside the nested section.
    # Consume the two containers independently, then use the nested tail only
    # for keys absent from the original outer layout.
    escaped = re.escape("reward item rate")
    card_body = re.sub(
        r"(?is)`?\[" + escaped + r"\]`?.*?`?\[/" + escaped + r"\]`?",
        "",
        result_body,
        count=1,
    )
    card_rows = _card_rows(_int_values(card_body))
    for key, values in _card_rows(rate_values[cursor:]).items():
        card_rows.setdefault(int(key), values)
    cards = tuple(
        (key, values[0], values[1], values[2])
        for key, values in sorted(card_rows.items())
    )
    return cards, tuple(item_rows)


def _with_native_apc_levels(
    candidates: Sequence[TournamentCandidate],
) -> Tuple[TournamentCandidate, ...]:
    """Resolve APC levels during warmup so a round never cold-reads PVF."""
    try:
        map_actors = load_chain("helper-dungeon_map_monsters.py")
        resolve_level = map_actors.resolve_ai_character_level
    except Exception:
        resolve_level = None
    out: List[TournamentCandidate] = []
    for candidate in candidates:
        level = 0
        if candidate.kind == "apc" and resolve_level is not None:
            try:
                level = max(0, int(resolve_level(int(candidate.code)) or 0))
            except Exception:
                level = 0
        out.append(
            TournamentCandidate(
                source_index=int(candidate.source_index),
                party_count=int(candidate.party_count),
                kind=str(candidate.kind),
                code=int(candidate.code),
                strength=int(candidate.strength),
                name=str(candidate.name),
                native_level=level,
            )
        )
    return tuple(out)


def warmup_tournament_index() -> Dict[str, int]:
    """Load only the fixed three-map Shonan tournament runtime index."""
    global _INDEX_READY, _SPECS_BY_DUNGEON

    index = get_quest_index()
    index.ensure_all_dungeons_loaded()
    dungeon_cache = {
        int(dungeon_id): dungeon
        for dungeon_id, dungeon in list(
            getattr(index, "dungeon_cache", {}).items()
        )
    }
    tournament_dungeons: Dict[int, Tuple[Any, str]] = {}
    invalid_count = 0
    for dungeon_id in sorted(_SHONAN_TOURNAMENT_DUNGEON_IDS):
        dungeon = dungeon_cache.get(int(dungeon_id))
        if dungeon is None:
            invalid_count += 1
            continue
        path = _normalize_path(getattr(dungeon, "path", ""))
        if not path:
            invalid_count += 1
            continue
        try:
            dgn_text = pvf_runtime.read_text(path)
        except Exception:
            invalid_count += 1
            continue
        if not _dungeon_is_tournament(dgn_text):
            invalid_count += 1
            continue
        tournament_dungeons[int(dungeon_id)] = (dungeon, dgn_text)

    try:
        map_actors = load_chain("helper-dungeon_map_monsters.py")
        map_paths = dict(map_actors._map_lst())
    except Exception:
        map_paths = {}
    maps_by_dungeon: Dict[int, List[Tuple[int, str, str]]] = {}
    for dungeon_id, expected_map_index in _SHONAN_TOURNAMENT_MAP_BY_DUNGEON.items():
        if int(dungeon_id) not in tournament_dungeons:
            continue
        raw_path = map_paths.get(int(expected_map_index))
        if not raw_path:
            invalid_count += 1
            continue
        map_path = _normalize_path(raw_path)
        if not map_path:
            invalid_count += 1
            continue
        try:
            map_text = pvf_runtime.read_text(map_path)
        except Exception:
            invalid_count += 1
            continue
        declared_ids = set(_int_values(_section_text(map_text, "dungeon")))
        if (
            not _outer_section(map_text, "tournament enemies")
            or int(dungeon_id) not in declared_ids
        ):
            invalid_count += 1
            continue
        maps_by_dungeon[int(dungeon_id)] = [
            (int(expected_map_index), map_path, map_text)
        ]

    specs: Dict[int, TournamentSpec] = {}
    ambiguous_count = 0
    for dungeon_id in sorted(_SHONAN_TOURNAMENT_DUNGEON_IDS):
        pair = tournament_dungeons.get(int(dungeon_id))
        if pair is None:
            continue
        dungeon, dgn_text = pair
        rows = maps_by_dungeon.get(int(dungeon_id), [])
        if len(rows) != 1:
            if len(rows) > 1:
                ambiguous_count += 1
            continue
        map_index, map_path, map_text = rows[0]
        candidates = _with_native_apc_levels(_parse_candidates(map_text))
        party_values = _int_values(_section_text(dgn_text, "limit party count"))
        party_limit = int(party_values[0]) if party_values else 0
        basis_values = _int_values(_section_text(dgn_text, "basis level"))
        basis_level = int(basis_values[0]) if basis_values else 0
        start_areas = _parse_start_areas(map_text)
        result_cards, reward_item_rates = _parse_reward_definition(dgn_text)
        clear_reward_exp = _parse_clear_reward_exp(dgn_text)
        gold_values = _int_values(
            _section_text(dgn_text, "tournament clear reward gold rate")
        )
        valid_candidate_count = sum(
            1 for row in candidates if int(row.party_count) == party_limit
        )
        valid_area_count = sum(
            1 for row in start_areas if int(row[0]) == party_limit
        )
        if (
            party_limit < 1
            or party_limit > 4
            or basis_level < 1
            or valid_candidate_count < _NPC_TEAM_COUNT * party_limit
            or valid_area_count < 2
            or len(clear_reward_exp) < 5
            or len(result_cards) < 6
            or not reward_item_rates
        ):
            invalid_count += 1
            continue
        maze = (list(getattr(dungeon, "mazes", None) or []) or [None])[0]
        fatigue_values = _int_values(_section_text(dgn_text, "tournament round fatigue"))
        specs[int(dungeon_id)] = TournamentSpec(
            dungeon_id=int(dungeon_id),
            dungeon_path=_normalize_path(getattr(dungeon, "path", "")),
            map_index=int(map_index),
            map_path=map_path,
            start_cell=_first_maze_cell(maze, "start_maps"),
            boss_cell=_first_maze_cell(maze, "boss_maps"),
            start_areas=start_areas,
            candidates=candidates,
            party_limit=int(party_limit),
            basis_level=int(basis_level),
            round_fatigue=(int(fatigue_values[0]) if fatigue_values else 0),
            enter_without_fatigue=_has_dgn_flag(dgn_text, "enter without fatigue"),
            clear_reward_exp=clear_reward_exp,
            result_cards=result_cards,
            reward_item_rates=reward_item_rates,
            clear_reward_gold_rate=(int(gold_values[0]) if gold_values else 0),
        )

    with _INDEX_LOCK:
        _SPECS_BY_DUNGEON = MappingProxyType(dict(sorted(specs.items())))
        _INDEX_READY = True
    return {
        "tournament_dungeon_count": len(tournament_dungeons),
        "tournament_map_count": sum(len(rows) for rows in maps_by_dungeon.values()),
        "tournament_resolved_count": len(specs),
        "tournament_ambiguous_count": int(ambiguous_count),
        "tournament_invalid_candidate_count": int(invalid_count),
        "tournament_candidate_count": sum(len(spec.candidates) for spec in specs.values()),
    }


def tournament_spec_for_dungeon(dungeon_id: int) -> Optional[TournamentSpec]:
    if int(dungeon_id) not in _SHONAN_TOURNAMENT_DUNGEON_IDS:
        return None
    with _INDEX_LOCK:
        return _SPECS_BY_DUNGEON.get(int(dungeon_id))


def is_tournament_dungeon(dungeon_id: int) -> bool:
    return tournament_spec_for_dungeon(int(dungeon_id)) is not None


def resolve_tournament_map_for_cell(
    dungeon_id: int,
    cell_x: int,
    cell_y: int,
) -> int:
    spec = tournament_spec_for_dungeon(int(dungeon_id))
    if spec is None:
        return 0
    cell = int(cell_x) & 0xFF, int(cell_y) & 0xFF
    allowed = {
        (int(spec.start_cell[0]) & 0xFF, int(spec.start_cell[1]) & 0xFF),
        (int(spec.boss_cell[0]) & 0xFF, int(spec.boss_cell[1]) & 0xFF),
    }
    return int(spec.map_index) if cell in allowed else 0


def _rng_for_run(spec: TournamentSpec, run_seed: int) -> random.Random:
    seed = (
        int(run_seed)
        ^ (int(spec.dungeon_id) << 17)
        ^ (int(spec.map_index) << 1)
        ^ (int(spec.party_limit) << 41)
    ) & 0xFFFFFFFFFFFFFFFF
    return random.Random(seed)


def select_tournament_opponents(
    spec: TournamentSpec,
    *,
    run_seed: int,
) -> Tuple[TournamentCandidate, ...]:
    """Draw the 15 NPC teams' members without replacement for one run."""
    candidates = list(spec.candidates_for_party())
    required = _NPC_TEAM_COUNT * int(spec.party_limit)
    if len(candidates) < required:
        return ()
    rng = _rng_for_run(spec, run_seed)
    rng.shuffle(candidates)
    return tuple(candidates[:required])


def _placeholder_player_member(member_index: int) -> TournamentCandidate:
    return TournamentCandidate(
        source_index=-1 - int(member_index),
        party_count=1,
        kind="player",
        code=0,
        strength=0,
        name="",
        native_level=0,
    )


def _with_position(team: TournamentTeam, position: int) -> TournamentTeam:
    return TournamentTeam(
        position=int(position),
        members=tuple(team.members),
        is_player=bool(team.is_player),
    )


def build_tournament_bracket(
    spec: TournamentSpec,
    *,
    run_seed: int,
) -> Tuple[Tuple[TournamentRound, ...], Tuple[TournamentCandidate, ...]]:
    """Build a deterministic 16-team bracket exactly once per dungeon run."""
    selected = select_tournament_opponents(spec, run_seed=run_seed)
    party_limit = int(spec.party_limit)
    if len(selected) != _NPC_TEAM_COUNT * party_limit:
        return (), ()
    rng = _rng_for_run(spec, int(run_seed) ^ 0x7A0B4E7)
    npc_teams: List[TournamentTeam] = []
    for team_index in range(_NPC_TEAM_COUNT):
        start = team_index * party_limit
        npc_teams.append(
            TournamentTeam(
                position=0,
                members=tuple(selected[start:start + party_limit]),
                is_player=False,
            )
        )
    npc_teams.sort(key=lambda team: int(team.total_strength))

    player_position = rng.randrange(_TEAM_COUNT)
    opponent_position = player_position ^ 1
    first_round: List[Optional[TournamentTeam]] = [None] * _TEAM_COUNT
    first_round[player_position] = TournamentTeam(
        position=player_position,
        members=tuple(_placeholder_player_member(index) for index in range(party_limit)),
        is_player=True,
    )
    first_round[opponent_position] = _with_position(
        npc_teams[0], opponent_position
    )
    remaining = list(npc_teams[1:])
    rng.shuffle(remaining)
    remaining_index = 0
    for position in range(_TEAM_COUNT):
        if first_round[position] is None:
            first_round[position] = _with_position(
                remaining[remaining_index], position
            )
            remaining_index += 1
    if any(team is None for team in first_round):
        return (), ()

    rounds: List[TournamentRound] = [
        TournamentRound(1, tuple(team for team in first_round if team is not None))
    ]
    previous = rounds[0].teams
    for round_number in range(2, _TOURNAMENT_ROUND_COUNT + 1):
        next_teams: List[TournamentTeam] = []
        for match_index in range(len(previous) // 2):
            left = previous[match_index * 2]
            right = previous[match_index * 2 + 1]
            if left.is_player:
                winner = left
            elif right.is_player:
                winner = right
            elif left.total_strength == right.total_strength:
                winner = left if rng.randrange(2) == 0 else right
            else:
                winner = left if left.total_strength > right.total_strength else right
            next_teams.append(_with_position(winner, match_index))
        previous = tuple(next_teams)
        rounds.append(TournamentRound(round_number, previous))

    path: List[TournamentCandidate] = []
    for round_data in rounds:
        player_index = -1
        for index, team in enumerate(round_data.teams):
            if team.is_player:
                player_index = index
                break
        if player_index < 0:
            return (), ()
        path.extend(round_data.teams[player_index ^ 1].members)
    return tuple(rounds), tuple(path)


def _actor_from_candidate(
    candidate: TournamentCandidate,
    *,
    actor_seq: int,
    basis_level: int,
    round_number: int,
    member_index: int,
    path_index: int,
) -> Dict[str, Any]:
    is_apc = candidate.kind == "apc"
    level = int(candidate.native_level) if is_apc else 0
    if level <= 0:
        level = max(1, int(basis_level))
    is_final = int(round_number) == _TOURNAMENT_ROUND_COUNT
    actor_type = 5 if is_apc else 0
    if is_final:
        actor_type = 8 if is_apc else 3
    current = int(round_number) == 1
    return {
        "kind": "ai_character" if is_apc else "monster",
        "template_order": int(path_index) & 0xFFFF,
        "slot_index": int(path_index) & 0xFFFFFFFF,
        "seq": int(actor_seq) & 0xFFFF,
        "code": int(candidate.code) & 0xFFFFFFFF,
        "level": max(1, min(255, int(level))),
        "type": int(actor_type),
        "flag0": 0,
        "flag1": 0,
        "extra_state": 0,
        "extra_flag": 0,
        "quest_enemy_type": 2 if is_apc else 1,
        "faction": "monster",
        "role": "enemy",
        "is_hostile": True,
        "is_ally": False,
        "is_blocking": bool(current),
        "grants_exp": True,
        "killable": bool(current),
        "wire_visible": True,
        "tournament_actor": True,
        "tournament_round": int(round_number),
        "tournament_member": int(member_index),
        "tournament_path_index": int(path_index),
        "tournament_candidate_index": int(candidate.source_index),
        "tournament_candidate_kind": str(candidate.kind),
        "tournament_candidate_name": str(candidate.name),
        "tournament_strength": int(candidate.strength),
    }


def _session_values() -> Dict[str, Any]:
    return {
        "dungeon_tournament_active": False,
        "dungeon_tournament_completed": False,
        "dungeon_tournament_dungeon_id": 0,
        "dungeon_tournament_map_index": 0,
        "dungeon_tournament_seed": 0,
        "dungeon_tournament_round": 0,
        "dungeon_tournament_current_seq": 0,
        "dungeon_tournament_current_seqs": (),
        "dungeon_tournament_first_actor_seq": 0,
        "dungeon_tournament_party_limit": 0,
        "dungeon_tournament_rounds": (),
        "dungeon_tournament_path_actors": (),
        "dungeon_tournament_opponents": (),
        "dungeon_tournament_completed_rounds": 0,
        "dungeon_tournament_reward_exp": 0,
        "dungeon_tournament_rewards": (),
        "dungeon_tournament_reward_selection": None,
        "dungeon_tournament_round_fatigue": 0,
        "dungeon_tournament_enter_without_fatigue": False,
    }


def clear_tournament_session(session: Any) -> None:
    if session is None:
        return
    for name, value in _session_values().items():
        try:
            setattr(session, name, value)
        except Exception:
            pass


def initialize_tournament_run(
    session: Any,
    *,
    dungeon_id: int,
    basis_level: int,
    first_seq: Optional[int] = None,
    actor_seq: Optional[int] = None,
    run_seed: Optional[int] = None,
) -> Tuple[Tuple[Dict[str, Any], ...], str]:
    """Lock bracket state and return all four rounds' actors for the first 001D."""
    spec = tournament_spec_for_dungeon(int(dungeon_id))
    if spec is None:
        clear_tournament_session(session)
        return (), "tournament=skip_not_indexed dungeon=%d" % int(dungeon_id)
    if session is None:
        return (), "tournament=skip_no_session"
    if actor_seq is not None:
        first_actor_seq = int(actor_seq)
    else:
        first_actor_seq = int(first_seq or 0) + 1
    actor_count = _TOURNAMENT_ROUND_COUNT * int(spec.party_limit)
    if first_actor_seq <= 0 or first_actor_seq + actor_count - 1 > 0xFFFE:
        return (), "tournament=skip_actor_sequence_range"

    seed = int(run_seed) if run_seed is not None else secrets.randbits(64)
    rounds, path = build_tournament_bracket(spec, run_seed=seed)
    if len(rounds) != _TOURNAMENT_ROUND_COUNT or len(path) != actor_count:
        clear_tournament_session(session)
        return (), "tournament=skip_invalid_bracket dungeon=%d" % int(dungeon_id)

    actors: List[Dict[str, Any]] = []
    path_index = 0
    for round_data in rounds:
        for member_index in range(int(spec.party_limit)):
            candidate = path[path_index]
            actors.append(
                _actor_from_candidate(
                    candidate,
                    actor_seq=first_actor_seq + path_index,
                    basis_level=int(basis_level or spec.basis_level),
                    round_number=int(round_data.number),
                    member_index=member_index,
                    path_index=path_index,
                )
            )
            path_index += 1
    current_seqs = tuple(
        int(actor["seq"])
        for actor in actors
        if int(actor.get("tournament_round") or 0) == 1
    )
    values = _session_values()
    values.update(
        {
            "dungeon_tournament_active": True,
            "dungeon_tournament_dungeon_id": int(spec.dungeon_id),
            "dungeon_tournament_map_index": int(spec.map_index),
            "dungeon_tournament_seed": int(seed),
            "dungeon_tournament_round": 1,
            "dungeon_tournament_current_seq": int(current_seqs[0]),
            "dungeon_tournament_current_seqs": current_seqs,
            "dungeon_tournament_first_actor_seq": int(first_actor_seq),
            "dungeon_tournament_party_limit": int(spec.party_limit),
            "dungeon_tournament_rounds": tuple(rounds),
            "dungeon_tournament_path_actors": tuple(actors),
            "dungeon_tournament_opponents": tuple(path),
            "dungeon_tournament_round_fatigue": int(spec.round_fatigue),
            "dungeon_tournament_enter_without_fatigue": bool(
                spec.enter_without_fatigue
            ),
        }
    )
    for name, value in values.items():
        try:
            setattr(session, name, value)
        except Exception:
            pass
    return tuple(actors), (
        "tournament=start dungeon=%d map=%d party=%d bracket=16 first_actor=%d "
        "actors=%d fatigue=%d no_fatigue=%d"
        % (
            int(spec.dungeon_id),
            int(spec.map_index),
            int(spec.party_limit),
            int(first_actor_seq),
            len(actors),
            int(spec.round_fatigue),
            int(bool(spec.enter_without_fatigue)),
        )
    )


def _current_round_rows(session: Any, round_number: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for actor in list(getattr(session, "dungeon_room_mons", None) or []):
        if not isinstance(actor, dict) or not bool(actor.get("tournament_actor")):
            continue
        if int(actor.get("tournament_round") or 0) == int(round_number):
            rows.append(actor)
    return rows


def can_accept_tournament_actor_death(
    session: Any,
    *,
    actor: Mapping[str, Any],
) -> Tuple[bool, str]:
    """Validate that a DIE belongs to the bracket's active player path.

    All four path actors are published in the opening START_MAP so the
    client can render the complete bracket.  Only the active round may alter
    the server state; accepting a later actor here would permanently remove
    that opponent before its match begins.
    """
    if session is None or not bool(
        getattr(session, "dungeon_tournament_active", False)
    ):
        return False, "tournament=reject_inactive"
    try:
        current_round = int(getattr(session, "dungeon_tournament_round", 0))
        actor_round = int(actor.get("tournament_round", 0))
        actor_seq = int(actor.get("seq", 0)) & 0xFFFF
        current_seqs = tuple(
            int(value) & 0xFFFF
            for value in tuple(
                getattr(session, "dungeon_tournament_current_seqs", ()) or ()
            )
        )
    except (AttributeError, TypeError, ValueError):
        return False, "tournament=reject_bad_actor"
    if current_round < 1 or actor_round != current_round or actor_seq not in current_seqs:
        return False, "tournament=reject_not_current round=%d/%d seq=%d" % (
            actor_round,
            current_round,
            actor_seq,
        )
    return True, "tournament=accept_current round=%d seq=%d" % (
        current_round,
        actor_seq,
    )


def _activate_tournament_round(session: Any, round_number: int) -> Tuple[int, ...]:
    active: List[int] = []
    for actor in list(getattr(session, "dungeon_room_mons", None) or []):
        if not isinstance(actor, dict) or not bool(actor.get("tournament_actor")):
            continue
        is_current = int(actor.get("tournament_round") or 0) == int(round_number)
        actor["is_blocking"] = bool(is_current)
        actor["killable"] = bool(is_current)
        if is_current:
            active.append(int(actor.get("seq") or 0) & 0xFFFF)
    return tuple(active)


def _draw_reward_item(spec: TournamentSpec, rng: random.Random) -> TournamentReward:
    total = sum(max(0, int(row[1])) for row in spec.reward_item_rates)
    if total <= 0:
        return TournamentReward(-1, 0)
    roll = rng.randrange(total)
    cumulative = 0
    for item_id, weight, count in spec.reward_item_rates:
        cumulative += max(0, int(weight))
        if roll < cumulative:
            return TournamentReward(int(item_id), max(1, int(count)))
    return TournamentReward(-1, 0)


def _draw_result_reward(
    spec: TournamentSpec,
    *,
    result_key: int,
    rng: random.Random,
) -> TournamentReward:
    gold_weight, item_weight, empty_weight = spec.result_card_for_key(result_key)
    total = gold_weight + item_weight + empty_weight
    if total <= 0:
        return TournamentReward(-1, 0)
    roll = rng.randrange(total)
    if roll < gold_weight:
        gold = max(
            1,
            int(spec.basis_level) * max(1, int(spec.clear_reward_gold_rate)),
        )
        return TournamentReward(0, gold)
    if roll < gold_weight + item_weight:
        return _draw_reward_item(spec, rng)
    return TournamentReward(-1, 0)


def _generate_final_rewards(
    spec: TournamentSpec,
    *,
    run_seed: int,
) -> Tuple[Tuple[TournamentReward, ...], Tuple[TournamentReward, ...]]:
    rng = _rng_for_run(spec, int(run_seed) ^ 0x18C5A11)
    upper = tuple(
        _draw_result_reward(spec, result_key=4, rng=rng) for _ in range(2)
    )
    lower = tuple(
        _draw_result_reward(spec, result_key=5, rng=rng) for _ in range(2)
    )
    return upper, lower


def advance_tournament_round(
    session: Any,
    *,
    defeated_actor: Mapping[str, Any],
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Apply a terminal DIE only to the current bracket round.

    The client already owns every actor from the opening 001D.  Round changes
    therefore update only server gating; the native tournament UI consumes the
    prior S0/0183 state and the normal DIE notification.
    """
    if session is None or not bool(getattr(session, "dungeon_tournament_active", False)):
        return None, "tournament=skip_inactive"
    try:
        current_round = int(getattr(session, "dungeon_tournament_round", 0))
        actor_round = int(defeated_actor.get("tournament_round", 0))
        actor_seq = int(defeated_actor.get("seq", 0)) & 0xFFFF
        current_seqs = tuple(
            int(value) & 0xFFFF
            for value in tuple(
                getattr(session, "dungeon_tournament_current_seqs", ()) or ()
            )
        )
    except (AttributeError, TypeError, ValueError):
        return None, "tournament=skip_bad_actor"
    if current_round < 1 or actor_round != current_round or actor_seq not in current_seqs:
        return None, "tournament=skip_not_current round=%d/%d seq=%d" % (
            actor_round,
            current_round,
            actor_seq,
        )

    current_rows = _current_round_rows(session, current_round)
    if not current_rows:
        return None, "tournament=skip_missing_round_rows round=%d" % current_round
    if any(not bool(row.get("killed")) for row in current_rows):
        return {
            "accepted": True,
            "completed_round": 0,
            "next_round": current_round,
            "completed": False,
        }, "tournament=actor_dead round=%d awaiting_members=%d" % (
            current_round,
            sum(1 for row in current_rows if not bool(row.get("killed"))),
        )

    if current_round >= _TOURNAMENT_ROUND_COUNT:
        spec = tournament_spec_for_dungeon(
            int(getattr(session, "dungeon_tournament_dungeon_id", 0) or 0)
        )
        session.dungeon_tournament_active = False
        session.dungeon_tournament_completed = True
        session.dungeon_tournament_completed_rounds = _TOURNAMENT_ROUND_COUNT
        session.dungeon_tournament_round = _TOURNAMENT_ROUND_COUNT + 1
        session.dungeon_tournament_current_seq = 0
        session.dungeon_tournament_current_seqs = ()
        if spec is not None:
            session.dungeon_tournament_reward_exp = spec.reward_exp_for_round(4)
            session.dungeon_tournament_rewards = _generate_final_rewards(
                spec,
                run_seed=int(getattr(session, "dungeon_tournament_seed", 0) or 0),
            )
            session.dungeon_tournament_reward_selection = None
        return {
            "accepted": True,
            "completed_round": _TOURNAMENT_ROUND_COUNT,
            "next_round": 0,
            "completed": True,
        }, "tournament=complete rounds=4 final_reward=0185"

    next_round = current_round + 1
    next_seqs = _activate_tournament_round(session, next_round)
    if not next_seqs:
        return None, "tournament=skip_missing_next_rows round=%d" % next_round
    session.dungeon_tournament_completed_rounds = current_round
    session.dungeon_tournament_round = next_round
    session.dungeon_tournament_current_seq = int(next_seqs[0])
    session.dungeon_tournament_current_seqs = next_seqs
    return {
        "accepted": True,
        "completed_round": current_round,
        "next_round": next_round,
        "completed": False,
    }, "tournament=advance completed=%d next=%d seqs=%s" % (
        current_round,
        next_round,
        ",".join(str(value) for value in next_seqs),
    )


def build_tournament_info_0183(session: Any, *, difficulty: int) -> bytes:
    """Build S0/0183 using the exact Stage3 four-round reader layout."""
    if session is None:
        raise ValueError("tournament session is required")
    dungeon_id = int(getattr(session, "dungeon_tournament_dungeon_id", 0) or 0)
    party_limit = int(getattr(session, "dungeon_tournament_party_limit", 0) or 0)
    rounds = tuple(getattr(session, "dungeon_tournament_rounds", ()) or ())
    path_actors = tuple(
        getattr(session, "dungeon_tournament_path_actors", ()) or ()
    )
    if dungeon_id <= 0 or party_limit <= 0 or len(rounds) != _TOURNAMENT_ROUND_COUNT:
        raise ValueError("tournament bracket state is incomplete")
    if len(path_actors) != _TOURNAMENT_ROUND_COUNT * party_limit:
        raise ValueError("tournament path actor count is invalid")

    # Stage3 reader 0x0163FD70 consumes this header as u32, u8, u8.
    # A u16 dungeon id shifts the first round's number/team count into the
    # header, making the client parse every team as a 16-member team.
    body = bytearray(
        struct.pack(
            "<IBB",
            dungeon_id & 0xFFFFFFFF,
            int(difficulty) & 0xFF,
            party_limit & 0xFF,
        )
    )
    for round_data in rounds:
        if not isinstance(round_data, TournamentRound):
            raise ValueError("tournament round type is invalid")
        body += bytes([int(round_data.number) & 0xFF, len(round_data.teams) & 0xFF])
        for team in round_data.teams:
            if len(team.members) != party_limit:
                raise ValueError("tournament team member count is invalid")
            body += bytes([int(team.position) & 0xFF])
            for member in team.members:
                body += struct.pack(
                    "<IH",
                    int(member.code) & 0xFFFFFFFF,
                    max(0, min(0xFFFF, int(member.strength))),
                )

    actor_index = 0
    for round_data in rounds:
        body += bytes([int(round_data.number) & 0xFF])
        for _member_index in range(party_limit):
            actor = path_actors[actor_index]
            actor_index += 1
            body += struct.pack(
                "<HIBB",
                int(actor.get("seq") or 0) & 0xFFFF,
                int(actor.get("code") or 0) & 0xFFFFFFFF,
                int(actor.get("level") or 0) & 0xFF,
                int(actor.get("type") or 0) & 0xFF,
            )
    return bytes(body)


def build_tournament_map_info_0184(
    *,
    cell_x: int,
    cell_y: int,
    seed: int,
    map_index: int,
    revisit: bool = False,
) -> bytes:
    """Build S0/0184 with all Stage3 reader tail fields explicit."""
    body = bytearray(
        struct.pack(
            "<BBIBB",
            int(cell_x) & 0xFF,
            int(cell_y) & 0xFF,
            int(seed) & 0xFFFFFFFF,
            0,
            0 if revisit else 1,
        )
    )
    if not revisit:
        body += struct.pack("<IBBB", int(map_index) & 0xFFFFFFFF, 0, 0, 0)
    return bytes(body)


def build_tournament_clear_reward_0185(session: Any) -> bytes:
    """Build S0/0185 for the champion-only final card page."""
    if session is None or not bool(getattr(session, "dungeon_tournament_completed", False)):
        raise ValueError("tournament is not complete")
    completed_rounds = int(
        getattr(session, "dungeon_tournament_completed_rounds", 0) or 0
    )
    rewards = tuple(getattr(session, "dungeon_tournament_rewards", ()) or ())
    if len(rewards) != 2:
        raise ValueError("tournament reward groups are incomplete")
    body = bytearray(
        struct.pack(
            "<BI",
            max(0, min(_TOURNAMENT_ROUND_COUNT, completed_rounds)),
            int(getattr(session, "dungeon_tournament_reward_exp", 0) or 0)
            & 0xFFFFFFFF,
        )
    )
    body += b"\x00"  # ignored per-member reward list
    for group in rewards:
        cards = tuple(group or ())
        if len(cards) > 0xFF:
            raise ValueError("tournament card group is too large")
        body += bytes([len(cards) & 0xFF])
        for reward in cards:
            if not isinstance(reward, TournamentReward):
                raise ValueError("tournament reward type is invalid")
            body += struct.pack(
                "<iiH",
                int(reward.item_id),
                int(reward.amount),
                0,
            )
    return bytes(body)


def build_tournament_reward_selection_01cb(session: Any) -> bytes:
    """Build the Stage3 S1/01CB selected-card projection.

    The client consumes ``u8 enabled``, then one ``u8 card_count`` and one
    party-slot byte per card for each reward group.  This project has two
    solo-only groups of two cards, but derive the counts from the terminal
    reward table so the projection cannot disagree with S0/0185.
    """
    groups, note = _terminal_reward_groups(session)
    if groups is None:
        raise ValueError(note)
    state, state_note = _selection_state(session)
    if state is None:
        raise ValueError(state_note)
    if not bool(state.get("opened", False)):
        raise ValueError("tournament_reward=reject_not_open")

    states = state["delivery_states"]
    indexes = state["selected_indexes"]
    body = bytearray((1,))
    for card_type, cards in enumerate(groups):
        if len(cards) > 0xFF:
            raise ValueError("tournament reward card group is too large")
        state_name = str(states[card_type])
        selected_index = -1
        if state_name == _TOURNAMENT_REWARD_STATE_DELIVERED:
            selected_index = int(indexes[card_type])
            if selected_index < 0 or selected_index >= len(cards):
                raise ValueError("tournament reward delivered index is invalid")

        body.append(len(cards) & 0xFF)
        for card_index in range(len(cards)):
            party_slot = (
                _TOURNAMENT_REWARD_SELECTED_PARTY_SLOT
                if card_index == selected_index
                else _TOURNAMENT_REWARD_UNSELECTED_PARTY_SLOT
            )
            body.append(party_slot)
    return bytes(body)


def _terminal_reward_groups(
    session: Any,
) -> Tuple[Optional[Tuple[Tuple[TournamentReward, ...], ...]], str]:
    """Return the fixed two-by-two final-card table for a completed arena."""
    if session is None:
        return None, "tournament_reward=reject_no_session"
    dungeon_id = int(
        getattr(session, "dungeon_tournament_dungeon_id", 0) or 0
    )
    if dungeon_id not in _SHONAN_TOURNAMENT_DUNGEON_IDS:
        return None, "tournament_reward=reject_dungeon=%d" % dungeon_id
    if not bool(getattr(session, "dungeon_tournament_completed", False)):
        return None, "tournament_reward=reject_not_complete"
    if int(getattr(session, "dungeon_tournament_completed_rounds", 0) or 0) != (
        _TOURNAMENT_ROUND_COUNT
    ):
        return None, "tournament_reward=reject_rounds"

    raw_groups = tuple(getattr(session, "dungeon_tournament_rewards", ()) or ())
    if len(raw_groups) != _TOURNAMENT_REWARD_CARD_TYPE_COUNT:
        return None, "tournament_reward=reject_group_count"
    groups: List[Tuple[TournamentReward, ...]] = []
    for group in raw_groups:
        cards = tuple(group or ())
        if len(cards) != _TOURNAMENT_REWARD_CARDS_PER_TYPE:
            return None, "tournament_reward=reject_card_count"
        if any(not isinstance(card, TournamentReward) for card in cards):
            return None, "tournament_reward=reject_card_type"
        groups.append(cards)
    return tuple(groups), "tournament_reward=ready dungeon=%d" % dungeon_id


def _selection_state(
    session: Any,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Read or create the per-run, per-card-type reward ledger."""
    state = getattr(session, "dungeon_tournament_reward_selection", None)
    if state is None:
        state = {
            "opened": False,
            "manual_refresh_armed": False,
            "manual_refresh_next_monotonic": 0.0,
            "delivery_states": [
                _TOURNAMENT_REWARD_STATE_AVAILABLE
                for _ in range(_TOURNAMENT_REWARD_CARD_TYPE_COUNT)
            ],
            "selected_indexes": [
                -1 for _ in range(_TOURNAMENT_REWARD_CARD_TYPE_COUNT)
            ],
        }
        try:
            session.dungeon_tournament_reward_selection = state
        except Exception:
            return None, "tournament_reward=reject_state_store"

    if not isinstance(state, dict):
        return None, "tournament_reward=reject_state_type"
    states = state.get("delivery_states")
    indexes = state.get("selected_indexes")
    valid_states = {
        _TOURNAMENT_REWARD_STATE_AVAILABLE,
        _TOURNAMENT_REWARD_STATE_RESERVED,
        _TOURNAMENT_REWARD_STATE_DELIVERED,
    }
    if (
        not isinstance(states, list)
        or not isinstance(indexes, list)
        or len(states) != _TOURNAMENT_REWARD_CARD_TYPE_COUNT
        or len(indexes) != _TOURNAMENT_REWARD_CARD_TYPE_COUNT
        or any(value not in valid_states for value in states)
        or not isinstance(state.get("manual_refresh_armed"), bool)
    ):
        return None, "tournament_reward=reject_state_shape"
    try:
        next_refresh = float(
            state.get("manual_refresh_next_monotonic", 0.0) or 0.0
        )
    except (TypeError, ValueError):
        return None, "tournament_reward=reject_manual_refresh_deadline"
    if next_refresh < 0.0:
        return None, "tournament_reward=reject_manual_refresh_deadline"
    state["manual_refresh_next_monotonic"] = next_refresh
    return state, "tournament_reward=state_ready"


def open_tournament_reward_selection(
    session: Any,
    *,
    now_monotonic: Optional[float] = None,
) -> Tuple[bool, str]:
    """Open the Stage3 final-card interaction after C1/01CA."""
    _groups, note = _terminal_reward_groups(session)
    if _groups is None:
        return False, note
    state, state_note = _selection_state(session)
    if state is None:
        return False, state_note
    state["opened"] = True
    states = state["delivery_states"]
    if all(value == _TOURNAMENT_REWARD_STATE_DELIVERED for value in states):
        state["manual_refresh_armed"] = False
        state["manual_refresh_next_monotonic"] = 0.0
        refresh_note = "manual_refresh=complete"
    elif bool(state.get("manual_refresh_armed", False)):
        refresh_note = "manual_refresh=already_armed"
    else:
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        state["manual_refresh_armed"] = True
        state["manual_refresh_next_monotonic"] = (
            now + _TOURNAMENT_REWARD_MANUAL_REFRESH_SECONDS
        )
        refresh_note = "manual_refresh=armed interval_ms=%d" % int(
            _TOURNAMENT_REWARD_MANUAL_REFRESH_SECONDS * 1000.0
        )
    return True, "tournament_reward=opened manual_select_only %s %s" % (
        refresh_note,
        note,
    )


def take_due_tournament_reward_manual_refresh(
    session: Any,
    *,
    now_monotonic: Optional[float] = None,
) -> Tuple[bool, str]:
    """Claim one state-6 refresh without selecting or delivering any card."""
    _groups, note = _terminal_reward_groups(session)
    if _groups is None:
        return False, note
    state, state_note = _selection_state(session)
    if state is None:
        return False, state_note
    if not bool(state.get("opened", False)):
        return False, "tournament_reward=manual_refresh_not_open"
    if not bool(state.get("manual_refresh_armed", False)):
        return False, "tournament_reward=manual_refresh_inactive"

    states = state["delivery_states"]
    if all(value == _TOURNAMENT_REWARD_STATE_DELIVERED for value in states):
        state["manual_refresh_armed"] = False
        state["manual_refresh_next_monotonic"] = 0.0
        return False, "tournament_reward=manual_refresh_complete"

    now = time.monotonic() if now_monotonic is None else float(now_monotonic)
    deadline = float(state.get("manual_refresh_next_monotonic", 0.0) or 0.0)
    if now < deadline:
        remaining_ms = max(1, int((deadline - now) * 1000.0))
        return False, "tournament_reward=manual_refresh_pending remaining_ms=%d" % (
            remaining_ms,
        )

    state["manual_refresh_next_monotonic"] = (
        now + _TOURNAMENT_REWARD_MANUAL_REFRESH_SECONDS
    )
    pending_types = tuple(
        card_type
        for card_type, delivery_state in enumerate(states)
        if delivery_state != _TOURNAMENT_REWARD_STATE_DELIVERED
    )
    return True, (
        "tournament_reward=manual_refresh_due pending_types=%s %s"
        % (",".join(str(value) for value in pending_types), note)
    )


def reserve_tournament_reward_selection(
    session: Any,
    *,
    card_type: int,
    card_index: int,
) -> Tuple[Optional[TournamentReward], str]:
    """Reserve one card from each group before the inventory mutation."""
    groups, note = _terminal_reward_groups(session)
    if groups is None:
        return None, note
    state, state_note = _selection_state(session)
    if state is None:
        return None, state_note
    if not bool(state.get("opened", False)):
        return None, "tournament_reward=reject_not_open"
    if card_type < 0 or card_type >= len(groups):
        return None, "tournament_reward=reject_card_type=%d" % card_type
    cards = groups[card_type]
    if card_index < 0 or card_index >= len(cards):
        return None, "tournament_reward=reject_card_index=%d" % card_index

    states = state["delivery_states"]
    indexes = state["selected_indexes"]
    current = str(states[card_type])
    if current != _TOURNAMENT_REWARD_STATE_AVAILABLE:
        return None, "tournament_reward=reject_%s type=%d index=%d" % (
            current,
            card_type,
            int(indexes[card_type]),
        )

    states[card_type] = _TOURNAMENT_REWARD_STATE_RESERVED
    indexes[card_type] = int(card_index)
    return cards[card_index], "tournament_reward=reserved type=%d index=%d" % (
        card_type,
        card_index,
    )


def commit_tournament_reward_selection(
    session: Any,
    *,
    card_type: int,
    card_index: int,
) -> bool:
    """Commit a reservation only after the reward has been persisted."""
    state, _note = _selection_state(session)
    if state is None or card_type < 0 or card_type >= _TOURNAMENT_REWARD_CARD_TYPE_COUNT:
        return False
    states = state["delivery_states"]
    indexes = state["selected_indexes"]
    if (
        states[card_type] != _TOURNAMENT_REWARD_STATE_RESERVED
        or int(indexes[card_type]) != int(card_index)
    ):
        return False
    states[card_type] = _TOURNAMENT_REWARD_STATE_DELIVERED
    if all(value == _TOURNAMENT_REWARD_STATE_DELIVERED for value in states):
        state["manual_refresh_armed"] = False
        state["manual_refresh_next_monotonic"] = 0.0
    return True


def rollback_tournament_reward_selection(
    session: Any,
    *,
    card_type: int,
    card_index: int,
) -> None:
    """Release a reservation when its database mutation did not complete."""
    state, _note = _selection_state(session)
    if state is None or card_type < 0 or card_type >= _TOURNAMENT_REWARD_CARD_TYPE_COUNT:
        return
    states = state["delivery_states"]
    indexes = state["selected_indexes"]
    if (
        states[card_type] == _TOURNAMENT_REWARD_STATE_RESERVED
        and int(indexes[card_type]) == int(card_index)
    ):
        states[card_type] = _TOURNAMENT_REWARD_STATE_AVAILABLE
        indexes[card_type] = -1
