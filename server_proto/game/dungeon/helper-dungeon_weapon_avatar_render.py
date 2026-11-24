"""Build the Stage3 S0/0321 initial render-state commit.

The dungeon map installer commits the actor render state before it creates the
per-slot equipment objects.  The registered 0321 reader reconstructs an
alternate USERINFO object, reads both the compact appearance rows and the full
equipment rows, and calls the same render-state owner after those rows exist.
This is the native path needed for a weapon-avatar model on the first dungeon
load; SkillInfo is unrelated to this state.
"""
from __future__ import annotations

import struct
from typing import Any

from game.character.character_stats import build_additional_info_for_character
from game.chain_loader import load_chain

WEAPON_AVATAR_SLOT = 10
WEAPON_AVATAR_COMPACT_AUX = b"\x00" * 4


def clear_pending_render(session: Any) -> None:
    if session is None:
        return
    session.dungeon_weapon_avatar_render_pending = None
    session.dungeon_weapon_avatar_render_character_id = None
    session.dungeon_weapon_avatar_render_dungeon_id = None
    session.dungeon_weapon_avatar_render_map_index = None
    session.dungeon_weapon_avatar_render_run_generation = 0
    session.dungeon_weapon_avatar_render_sent = False


def _entry_int(entry: Any, name: str, default: int = 0) -> int:
    if isinstance(entry, dict):
        value = entry.get(name, default)
    else:
        value = getattr(entry, name, default)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return int(default)


def _has_weapon_avatar(entries: Any) -> int:
    for entry in tuple(entries or ()):
        if _entry_int(entry, "slot", -1) == WEAPON_AVATAR_SLOT:
            item_id = _entry_int(entry, "item_id", 0)
            if item_id > 0:
                return item_id & 0xFFFFFFFF
    return 0


def _resolve_equipment(server: Any, character: Any) -> tuple[tuple[Any, ...], str]:
    helper = load_chain("helper-userinfo_equipment.py")
    try:
        return helper.resolve_enter_room_equipment(
            character,
            enabled=True,
            backfill_from_pvf=False,
            repo=server._repo(),
            prefer_live_db=True,
        )
    except Exception as exc:
        entries = tuple(getattr(character, "equipped_entries", ()) or ())
        return entries, "equipment_fallback=%s" % type(exc).__name__


def _guild_membership_active(server: Any, character: Any) -> bool:
    try:
        from game.guild.guild_benefits import has_active_guild_membership

        return bool(has_active_guild_membership(server._repo(), character))
    except Exception:
        return False


def _special_reward_ids(server: Any, character: Any) -> tuple[int, ...]:
    """Preserve the same personality/time-fragment IDs as USERINFO1."""
    try:
        snapshot = load_chain(
            "helper-quest_completion_snapshot.py"
        ).load_finished_quest_snapshot(
            server._repo(), _entry_int(character, "character_id", 0)
        )
        from game.quest.personality_rewards import finished_personality_reward_ids
        from game.quest.time_fragment_rewards import finished_time_fragment_reward_ids

        finished = tuple(getattr(snapshot, "finished_quest_ids", ()) or ())
        job = _entry_int(character, "job", 0)
        return tuple(
            sorted(
                set(finished_personality_reward_ids(finished)).union(
                    finished_time_fragment_reward_ids(finished, job_id=job)
                )
            )
        )
    except Exception:
        return ()


def encode_dungeon_compact_equipment_list(equipment: Any) -> bytes:
    """Encode the 0321 compact appearance table after the actor is present.

    The ordinary compact helper intentionally emits the persisted minimum
    19-byte row.  The alternate dungeon reader uses the same row reader, but
    a weapon-avatar row must carry a non-null four-byte auxiliary object just
    as it does after a real list1 -> worn move.  Keep this projection local to
    0321 so town/inventory packets retain their established wire shape.
    """
    compact_helper = load_chain("helper-userinfo_equipment.py")
    rows = compact_helper.normalize_equipment_entries(equipment)
    if len(rows) > 0xFF:
        raise ValueError("0321 compact equipment count exceeds u8")
    body = bytearray((len(rows) & 0xFF,))
    for slot, item_id, flag, aux1, aux2, unknown in rows:
        custom_blob = (
            WEAPON_AVATAR_COMPACT_AUX if slot == WEAPON_AVATAR_SLOT else b""
        )
        body += compact_helper.build_userinfo_equipment_row(
            slot,
            item_id,
            flag=flag,
            aux1=aux1,
            aux2=aux2,
            unknown=unknown,
            custom_blob=custom_blob,
        )
    return bytes(body)


def _build_inner(
    character: Any,
    equipment: tuple[Any, ...],
    *,
    guild_active: bool,
    special_reward_ids: tuple[int, ...],
) -> bytes:
    """Encode the exact read order of sub_1A8C8C0(..., a5=1)."""
    job = _entry_int(character, "job", 0) & 0xFF
    grow = _entry_int(character, "grow_type", 0) & 0xFF
    raw_sub = getattr(character, "sub_grow_type", None)
    sub_grow = None if raw_sub is None else (_entry_int(character, "sub_grow_type") & 0xFF)
    level = max(1, _entry_int(character, "level", 1))
    _stats, combat_blob = build_additional_info_for_character(
        job,
        level,
        grow,
        sub_grow_type=sub_grow,
        stat_level=100,
        guild_membership_active=guild_active,
    )
    if len(combat_blob) != 83:
        raise ValueError("0321 combat blob must be 83B, got %d" % len(combat_blob))

    compact_blob = encode_dungeon_compact_equipment_list(equipment)
    full_helper = load_chain("helper-userinfo1_equipment.py")
    full_blob, _count, _note = full_helper.encode_userinfo1_equipment_list(equipment)

    body = bytearray()
    # sub_18CD370 / sub_18CC3D0 / sub_18CE5F0
    body += b"\x00\x00\x00\x00"
    # sub_18CC850: u32 length + 83-byte combat state
    body += struct.pack("<I", len(combat_blob))
    body += combat_blob
    # sub_18CC8C0: ex_equip state, then sub_18CC430 compact appearance rows
    body += bytes((_entry_int(character, "ex_equip_slot_stat", 0) & 0xFF,))
    body += compact_blob
    # The alternate parser consumes four scalar fields before the full
    # equipment table: u32, u8, u8, u8, followed by one additional u8.  The
    # reward list is read later by sub_18CCA50; placing it here shifts every
    # equipment row for characters that have finished reward quests.
    body += struct.pack("<I", 0)
    body += b"\x00\x00\x00\x00"
    # sub_166AED0 full equipment object creation.
    body += full_blob
    # sub_2C74A80 consumes a clone/title u16 before the final-row state.
    body += struct.pack("<H", 0)
    # sub_18CD0D0 starts with two u8 values (mode and row count).  An empty
    # state therefore occupies exactly two bytes; each non-empty row would
    # additionally carry its u8/u16/u8 fields.
    body += b"\x00\x00"
    # sub_18CCA50: special-reward count followed by reward IDs.
    body += struct.pack("<I", len(special_reward_ids))
    for quest_id in special_reward_ids:
        body += struct.pack("<I", int(quest_id) & 0xFFFFFFFF)
    # a5=1 branch reads five u32 values after sub_18CCA50.
    body += b"\x00" * 20
    return bytes(body)


def build_weapon_avatar_render_packet(
    server: Any,
    *,
    character: Any,
) -> tuple[bytes | None, str]:
    entries, equipment_note = _resolve_equipment(server, character)
    weapon_avatar_id = _has_weapon_avatar(entries)
    if weapon_avatar_id <= 0:
        return None, "weapon_avatar_render=skip_no_slot10 %s" % equipment_note
    key = _entry_int(character, "character_id", 0) & 0xFFFF
    if key <= 0:
        return None, "weapon_avatar_render=skip_no_character_key"
    inner = _build_inner(
        character,
        entries,
        guild_active=_guild_membership_active(server, character),
        special_reward_ids=_special_reward_ids(server, character),
    )
    body = bytearray()
    body += b"\x01"  # character_count
    body += struct.pack("<H", key)
    body += b"\x01"  # conversion_entry_count
    body += b"\x00"  # conversion index passed to sub_A93FC0
    # sub_16627A0 does not read a wire DSTR here.  It constructs the
    # conversion-name string locally and sub_2C6A2A0 copies that object into
    # the alternate USERINFO parser before sub_1A8C8C0 consumes the body.
    body += inner
    compact_len = len(encode_dungeon_compact_equipment_list(entries))
    full_len = len(
        load_chain("helper-userinfo1_equipment.py")
        .encode_userinfo1_equipment_list(entries)[0]
    )
    return bytes(body), (
        "weapon_avatar_render=prepared weapon_avatar=%d key=%d compact_len=%d "
        "full_len=%d plain_len=%d %s"
        % (
            weapon_avatar_id,
            key,
            compact_len,
            full_len,
            len(body),
            equipment_note,
        )
    )


def prepare_pending_render(
    server: Any,
    *,
    session: Any,
    character: Any,
    dungeon_id: int,
    map_index: int,
) -> str:
    clear_pending_render(session)
    if session is None:
        return "weapon_avatar_render=skip_no_session"
    character_id = _entry_int(session, "selected_character_id", 0)
    if character_id <= 0 or character is None:
        return "weapon_avatar_render=skip_no_character"
    if _entry_int(character, "character_id", 0) != character_id:
        return "weapon_avatar_render=skip_character_snapshot_mismatch"
    body, note = build_weapon_avatar_render_packet(server, character=character)
    if body is None:
        return note
    session.dungeon_weapon_avatar_render_pending = body
    session.dungeon_weapon_avatar_render_character_id = character_id
    session.dungeon_weapon_avatar_render_dungeon_id = int(dungeon_id) & 0xFFFFFFFF
    session.dungeon_weapon_avatar_render_map_index = int(map_index) & 0xFFFFFFFF
    session.dungeon_weapon_avatar_render_run_generation = int(
        getattr(session, "dungeon_run_generation", 0) or 0
    )
    session.dungeon_weapon_avatar_render_sent = False
    return note


def take_pending_render(*, session: Any) -> tuple[bytes | None, str]:
    if session is None:
        return None, "weapon_avatar_render=skip_no_session"
    pending = getattr(session, "dungeon_weapon_avatar_render_pending", None)
    if not pending:
        if bool(getattr(session, "dungeon_weapon_avatar_render_sent", False)):
            return None, "weapon_avatar_render=skip_already_sent"
        return None, "weapon_avatar_render=skip_no_pending"
    if not bool(getattr(session, "in_dungeon_run", False)):
        return None, "weapon_avatar_render=skip_not_in_run"
    if _entry_int(session, "selected_character_id", 0) != _entry_int(
        session, "dungeon_weapon_avatar_render_character_id", 0
    ):
        return None, "weapon_avatar_render=skip_character_mismatch"
    if _entry_int(session, "selected_dungeon_id", 0) != _entry_int(
        session, "dungeon_weapon_avatar_render_dungeon_id", 0
    ):
        return None, "weapon_avatar_render=skip_dungeon_mismatch"
    if _entry_int(session, "dungeon_map_index", 0) != _entry_int(
        session, "dungeon_weapon_avatar_render_map_index", 0
    ):
        return None, "weapon_avatar_render=skip_map_mismatch"
    if _entry_int(session, "dungeon_run_generation", 0) != _entry_int(
        session, "dungeon_weapon_avatar_render_run_generation", 0
    ):
        return None, "weapon_avatar_render=skip_generation_mismatch"
    body = bytes(pending)
    session.dungeon_weapon_avatar_render_pending = None
    session.dungeon_weapon_avatar_render_sent = True
    return body, "weapon_avatar_render=send_once plain_len=%d" % len(body)


__all__ = [
    "WEAPON_AVATAR_SLOT",
    "WEAPON_AVATAR_COMPACT_AUX",
    "build_weapon_avatar_render_packet",
    "clear_pending_render",
    "encode_dungeon_compact_equipment_list",
    "prepare_pending_render",
    "take_pending_render",
]
