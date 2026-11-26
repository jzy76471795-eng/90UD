"""Build the pre-dungeon complete Stage3 USERINFO1 snapshot.

The select-dungeon path already owns the town character object after 0x0017.
For a worn weapon avatar, this helper prepares the same complete subtype-1
snapshot used by the normal character-entry path.  It is intentionally a
builder only: no database mutation and no packet emission happen here.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from game.character.adventure_group import calculate_adventure_group_for_character
from game.character.initial_skills import get_initial_skills_for_job
from game.character.payloads import build_userinfo_subtype1_min
from game.chain_loader import load_chain
from game.guild.guild_benefits import has_active_guild_membership

WEAPON_AVATAR_SLOT = 10


def _entry_int(entry: Any, name: str, default: int = 0) -> int:
    try:
        value = entry.get(name, default) if isinstance(entry, dict) else getattr(entry, name, default)
        return int(value or 0)
    except (TypeError, ValueError):
        return int(default)


def _load_current_character(repo: Any, character_id: int, account_hint: int) -> Any:
    cid = int(character_id)
    if int(account_hint or 0) > 0:
        for row in repo.list_by_account(int(account_hint)):
            if _entry_int(row, "character_id") == cid:
                return row
    for row in repo.list_all_alive():
        if _entry_int(row, "character_id") == cid:
            return row
    return None


def _load_skill_page(repo: Any, character: Any) -> tuple[tuple[Any, ...], str]:
    """Read the live page-0 skill rows, falling back only for an empty page."""
    cid = _entry_int(character, "character_id")
    job = _entry_int(character, "job") & 0xFF
    skill_helper = load_chain("helper-character_skills.py")
    rows = repo.load_character_skills(cid, page=0, expected_job=job)
    skills = tuple(skill_helper.rows_to_initial_skills(rows))
    if skills:
        return skills, "skills=db_page0 n=%d" % len(skills)
    fallback = tuple(get_initial_skills_for_job(job))
    return fallback, "skills=json_fallback n=%d" % len(fallback)


def _weapon_avatar_id(equipment: Any) -> int:
    for entry in tuple(equipment or ()):
        if _entry_int(entry, "slot", -1) != WEAPON_AVATAR_SLOT:
            continue
        item_id = _entry_int(entry, "item_id")
        if item_id > 0:
            return item_id & 0xFFFFFFFF
    return 0


def build_preselect_weapon_avatar_userinfo1(
    server: Any,
    *,
    session: Any,
    character: Any = None,
) -> tuple[bytes | None, str]:
    """Return one complete subtype-1 body, or a reasoned skip note.

    The caller places the returned body between 0x0017 and 0x0003.  A live
    slot-10 row is the only activation condition; all other fields are read
    from the same repository snapshot before encoding.
    """
    if session is None:
        return None, "preselect_userinfo1=skip_no_session"
    character_id = _entry_int(session, "selected_character_id")
    if character_id <= 0:
        return None, "preselect_userinfo1=skip_no_character"

    try:
        repo = server._repo()
        account_id = int(repo.get_account_id_for_character(character_id) or 0)
        if account_id <= 0:
            account_id = int(getattr(server, "account_id", 1) or 1)
        current = _load_current_character(repo, character_id, account_id)
        if current is None:
            return None, "preselect_userinfo1=skip_character_missing"

        # CharacterRepository returns an immutable CharacterRecord.  Replace
        # progression fields with the same live row used for the other entry
        # packets before building combat stats and the exp store.
        level, exp, expertise_exp = repo.get_character_progression(character_id)
        try:
            current = replace(
                current,
                level=int(level),
                exp=int(exp),
                expertise_exp=int(expertise_exp),
            )
        except (TypeError, ValueError):
            pass

        equipment_helper = load_chain("helper-userinfo_equipment.py")
        equipment, equipment_note = equipment_helper.resolve_enter_room_equipment(
            current,
            enabled=True,
            backfill_from_pvf=False,
            repo=repo,
            prefer_live_db=True,
        )
        weapon_avatar_id = _weapon_avatar_id(equipment)
        if weapon_avatar_id <= 0:
            return None, (
                "preselect_userinfo1=skip_no_slot10 %s" % equipment_note
            )

        skills, skill_note = _load_skill_page(repo, current)
        quest_snapshot = load_chain(
            "helper-quest_completion_snapshot.py"
        ).load_finished_quest_snapshot(repo, character_id)
        adventure = calculate_adventure_group_for_character(repo, current)
        adventure_level = int(adventure.adventure_level)
        guild_active = has_active_guild_membership(repo, current)
        body = build_userinfo_subtype1_min(
            current,
            exp=int(exp),
            skills=skills,
            entry_flag=0,
            equipment=equipment,
            finished_quest_ids=quest_snapshot.finished_quest_ids,
            adventure_group_level=adventure_level,
            guild_membership_active=guild_active,
        )
        if not body or body[0] != 1:
            raise ValueError("USERINFO1 subtype1 body validation failed")
    except Exception as exc:
        return None, "preselect_userinfo1=skip_snapshot_error:%s" % type(exc).__name__

    return body, (
        "preselect_userinfo1=prepared key=%d weapon_avatar=%d exp=%d level=%d "
        "skills=%d equipment=%d adventure_level=%d guild_active=%d plain_len=%d "
        "%s %s %s"
        % (
            character_id & 0xFFFF,
            weapon_avatar_id,
            int(exp),
            int(level),
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


__all__ = [
    "WEAPON_AVATAR_SLOT",
    "build_preselect_weapon_avatar_userinfo1",
]
