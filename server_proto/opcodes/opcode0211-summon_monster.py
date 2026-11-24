"""C1/0x0211 conditional monster summon for special dungeons.

Stage3 request (19 bytes)::

    u16 conditional_type
    u32 monster_code
    u32 state_id
    u32 map_id
    u16 conditional_param0
    u16 conditional_param1
    u8  match_count

The success response is flag 1 / opcode 0x0211 and contains ``result=1``, the
echoed state id, and one runtime actor record.  Both the DGN boss-entrance
declaration and the current MAP conditional-summon template authorize the
actor before it is added to the server-owned room state.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

from game.chain_loader import load_chain
from game.drop.room_drop_preload import preload_room_drops_before_visibility
from game.quest.quest_index import get_quest_index


_map_actors = load_chain("helper-dungeon_map_monsters.py")
_room_state = load_chain("helper-dungeon_room_state.py")

load_map_conditional_summon_monsters = (
    _map_actors.load_map_conditional_summon_monsters
)
allocate_runtime_actor_key = _room_state.allocate_runtime_actor_key
cache_current_room_state = _room_state.cache_current_room_state


@dataclass(frozen=True)
class SummonMonster0211Request:
    conditional_type: int
    monster_code: int
    state_id: int
    map_id: int
    conditional_param0: int
    conditional_param1: int
    match_count: int


def parse_summon_monster_0211(body: bytes) -> SummonMonster0211Request | None:
    raw = bytes(body or b"")
    if len(raw) != 19:
        return None
    (
        conditional_type,
        monster_code,
        state_id,
        map_id,
        conditional_param0,
        conditional_param1,
        match_count,
    ) = struct.unpack("<HIIIHHB", raw)
    if monster_code <= 0 or state_id <= 0 or map_id <= 0 or match_count <= 0:
        return None
    return SummonMonster0211Request(
        conditional_type=int(conditional_type),
        monster_code=int(monster_code),
        state_id=int(state_id),
        map_id=int(map_id),
        conditional_param0=int(conditional_param0),
        conditional_param1=int(conditional_param1),
        match_count=int(match_count),
    )


def build_summon_monster_response_0211(
    *,
    state_id: int,
    actor_key: int,
    monster_code: int,
    level: int,
) -> bytes:
    """Build the Stage3 command-success body for one created actor."""
    return struct.pack(
        "<BIBHIBH",
        1,
        int(state_id) & 0xFFFFFFFF,
        1,
        int(actor_key) & 0xFFFF,
        int(monster_code) & 0xFFFFFFFF,
        3,
        int(level) & 0xFFFF,
    )


def _active_dungeon_id(session: Any) -> int:
    if session is None:
        return 0
    return int(
        getattr(session, "selected_dungeon_id", 0)
        or getattr(session, "cur_dungeon_id", 0)
        or 0
    )


def _existing_summon(session: Any) -> dict[str, Any] | None:
    actor_lists = [list(getattr(session, "dungeon_room_mons", None) or [])]
    states = getattr(session, "dungeon_room_states", None)
    if isinstance(states, dict):
        actor_lists.extend(
            list(state.get("monsters") or [])
            for state in states.values()
            if isinstance(state, dict)
        )
    for actors in actor_lists:
        for actor in actors:
            if bool(actor.get("conditional_summon_actor")):
                return actor
    return None


def handle_summon_monster_0211(
    server: Any,
    body: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    request = parse_summon_monster_0211(body)
    raw = bytes(body or b"")
    base = "C1/0211 SUMMON_MONSTER len=%d plain=%s" % (
        len(raw),
        raw.hex(" ") or "<empty>",
    )
    if request is None:
        return [], base + "; reject=bad_19b_request"
    if session is None or not bool(getattr(session, "in_dungeon_run", False)):
        return [], base + "; reject=not_in_dungeon_run"
    if not bool(getattr(session, "dungeon_boss_condition_complete", False)):
        return [], base + "; reject=boss_condition_incomplete"

    current_map = int(getattr(session, "dungeon_map_index", 0) or 0)
    current_x = int(getattr(session, "dungeon_cell_x", 0) or 0) & 0xFFFF
    current_y = int(getattr(session, "dungeon_cell_y", 0) or 0) & 0xFFFF
    if int(request.map_id) != current_map:
        return [], "%s; reject=map_mismatch request=%d current=%d" % (
            base,
            request.map_id,
            current_map,
        )
    if (
        int(request.conditional_param0) != current_x
        or int(request.conditional_param1) != current_y
    ):
        return [], "%s; reject=cell_mismatch request=(%d,%d) current=(%d,%d)" % (
            base,
            request.conditional_param0,
            request.conditional_param1,
            current_x,
            current_y,
        )

    existing = _existing_summon(session)
    if existing is not None:
        return [], "%s; duplicate=state_%d actor=%d" % (
            base,
            request.state_id,
            int(existing.get("seq") or 0),
        )

    dungeon_id = _active_dungeon_id(session)
    try:
        dungeon = get_quest_index().get_dungeon(dungeon_id)
    except Exception as exc:
        return [], base + "; reject=dungeon_index_error err=%r" % (exc,)
    allowed_codes = {
        int(code)
        for code in tuple(
            getattr(dungeon, "boss_entrance_summon_monsters", ()) or ()
        )
        if int(code) > 0
    }
    if int(request.monster_code) not in allowed_codes:
        return [], "%s; reject=dgn_summon_not_declared dungeon=%d code=%d" % (
            base,
            dungeon_id,
            request.monster_code,
        )

    templates = load_map_conditional_summon_monsters(
        current_map,
        [request.monster_code],
        dungeon_basic_lv=int(getattr(session, "dungeon_basis_level", 0) or 0),
        first_seq=0,
    )
    if not templates:
        return [], "%s; reject=map_conditional_template_missing map=%d code=%d" % (
            base,
            current_map,
            request.monster_code,
        )

    actor_key = allocate_runtime_actor_key(session)
    if actor_key <= 0:
        return [], base + "; reject=actor_key_exhausted"

    actor = dict(templates[0])
    actor["seq"] = int(actor_key) & 0xFFFF
    actor["conditional_summon_actor"] = True
    actor["conditional_summon_state_id"] = int(request.state_id) & 0xFFFFFFFF
    actor["conditional_summon_type"] = int(request.conditional_type) & 0xFFFF
    actor["conditional_summon_match_count"] = int(request.match_count) & 0xFF
    actor["runtime_map_index"] = current_map & 0xFFFFFFFF
    actor["runtime_cell_x"] = current_x
    actor["runtime_cell_y"] = current_y
    actor["killed"] = False

    monsters = getattr(session, "dungeon_room_mons", None)
    if not isinstance(monsters, list):
        monsters = list(monsters or [])
        session.dungeon_room_mons = monsters
    monsters.append(actor)
    cached = cache_current_room_state(session)
    if cached is not None:
        session.dungeon_room_mons = cached["monsters"]

    preload_note = preload_room_drops_before_visibility(
        server,
        session,
        source="summon_0211",
    )

    response = build_summon_monster_response_0211(
        state_id=request.state_id,
        actor_key=actor_key,
        monster_code=request.monster_code,
        level=int(actor.get("level") or 1),
    )
    packet = server._codec_response(1, 0x0211, response)
    return [packet], (
        "%s; response=success state=%d actor=%d code=%d level=%d "
        "map=%d cell=(%d,%d) template_xy=(%d,%d); %s"
        % (
            base,
            request.state_id,
            actor_key,
            request.monster_code,
            int(actor.get("level") or 1),
            current_map,
            current_x,
            current_y,
            int(actor.get("x") or 0),
            int(actor.get("y") or 0),
            preload_note,
        )
    )
