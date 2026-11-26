"""Build the Stage3 subtype-1 follow-up for an in-dungeon level-up.

The native 0x0025 reader changes the character level before the server has
projected the new level's combat attributes.  Until the next room rebuilds
them, that leaves max HP/MP and attack-related values on different level
snapshots.  This helper sends one complete USERINFO subtype 1 snapshot for the
existing primary character object.  It never sends subtype 0, rebuilds a room,
removes a status, or performs a database mutation.
"""
from __future__ import annotations

from typing import Any

from game.character.adventure_group import calculate_adventure_group_for_character
from game.character.initial_skills import get_initial_skills_for_job
from game.character.payloads import build_userinfo_subtype1_min
from game.chain_loader import load_chain
from game.guild.guild_benefits import has_active_guild_membership


def _entry_int(entry: Any, name: str, default: int = 0) -> int:
    try:
        value = (
            entry.get(name, default)
            if isinstance(entry, dict)
            else getattr(entry, name, default)
        )
        return int(value or 0)
    except (TypeError, ValueError):
        return int(default)


def _load_skill_page(repo: Any, character: Any) -> tuple[tuple[Any, ...], str]:
    cid = _entry_int(character, "character_id")
    job = _entry_int(character, "job") & 0xFF
    skill_helper = load_chain("helper-character_skills.py")
    rows = repo.load_character_skills(cid, page=0, expected_job=job)
    skills = tuple(skill_helper.rows_to_initial_skills(rows))
    if skills:
        return skills, "skills=db_page0 n=%d" % len(skills)
    fallback = tuple(get_initial_skills_for_job(job))
    return fallback, "skills=json_fallback n=%d" % len(fallback)


def pack_dungeon_levelup_userinfo1_refresh(
    server: Any,
    *,
    session: Any,
    character_id: int,
    expected_level: int,
) -> tuple[list[bytes], str]:
    """Return one codec USERINFO1 packet after a successful level-up 0x0025.

    The packet is deliberately a same-actor combat projection: it closes the
    current room's level-dependent damage and HP/MP values without the room
    re-entry side effects of a full dungeon refresh.
    """
    cid = int(character_id or 0)
    expected = int(expected_level or 0)
    if session is None or not bool(getattr(session, "in_dungeon_run", False)):
        return [], "levelup_userinfo1 skip=not_in_dungeon"
    if cid <= 0 or expected <= 0:
        return [], "levelup_userinfo1 skip=invalid_identity"
    selected = _entry_int(session, "selected_character_id")
    if selected != cid:
        return [], (
            "levelup_userinfo1 skip=character_mismatch cid=%d selected=%d"
            % (cid, selected)
        )

    try:
        repo = server._repo()
        character = repo.get_character_combat_projection(cid)
        if character is None:
            return [], "levelup_userinfo1 skip=character_missing"

        level = _entry_int(character, "level")
        if level != expected:
            return [], (
                "levelup_userinfo1 skip=level_mismatch expected=%d db=%d"
                % (expected, level)
            )
        exp = _entry_int(character, "exp")

        equipment_helper = load_chain("helper-userinfo_equipment.py")
        equipment, equipment_note = equipment_helper.resolve_enter_room_equipment(
            character,
            enabled=True,
            backfill_from_pvf=False,
            repo=repo,
            prefer_live_db=True,
        )
        skills, skill_note = _load_skill_page(repo, character)
        quest_snapshot = load_chain(
            "helper-quest_completion_snapshot.py"
        ).load_finished_quest_snapshot(repo, cid)
        adventure = calculate_adventure_group_for_character(repo, character)
        adventure_level = int(adventure.adventure_level)
        guild_active = has_active_guild_membership(repo, character)
        body = build_userinfo_subtype1_min(
            character,
            exp=exp,
            skills=skills,
            entry_flag=0,
            equipment=equipment,
            finished_quest_ids=quest_snapshot.finished_quest_ids,
            adventure_group_level=adventure_level,
            guild_membership_active=guild_active,
        )
        if not body or body[0] != 1:
            raise ValueError("USERINFO1 subtype1 body validation failed")
        packet = server._codec_response(0, 0x0002, body)
    except Exception as exc:
        return [], (
            "levelup_userinfo1 skip=snapshot_error:%s" % type(exc).__name__
        )

    return [packet], (
        "levelup_userinfo1 S0/0002 level=%d exp=%d skills=%d equipment=%d "
        "adventure_level=%d guild_active=%d plain_len=%d %s %s %s"
        % (
            level,
            int(exp),
            len(skills),
            len(equipment),
            adventure_level,
            int(bool(guild_active)),
            len(body),
            skill_note,
            equipment_note,
            quest_snapshot.describe(),
        )
    )


__all__ = ["pack_dungeon_levelup_userinfo1_refresh"]
