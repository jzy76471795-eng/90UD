"""Complete public peer snapshots built by the existing frozen-US factories."""
from dataclasses import replace
from game.chain_loader import load_chain
from game.character.payloads import build_userinfo_subtype1_min
from game.character.native_profile_projection import userinfo0_projection, _stamp_userinfo0
from game.character.adventure_group import calculate_adventure_group_for_character
from game.guild.guild_benefits import has_active_guild_membership
from game.raid.runtime import RaidError


def build_profiles(server, member):
    """Run on the profile owner's account thread, outside coordinator locks."""
    from game.town_social import service_for
    selection = member.selection
    if server.account_id != member.account_id:
        raise RaidError('peer snapshot requires its actual account thread')
    town = service_for(server).build_body(selection)
    projection = userinfo0_projection(town)
    if projection is None or projection.character_id != member.character_id:
        raise RaidError('peer public snapshot factory ownership mismatch')
    raw = bytearray(town)
    if len(raw) < 43 or raw[:4] != b'\0\1\0\0':
        raise RaidError('peer public snapshot has an unsupported namespace')
    state_offset = 43 + int.from_bytes(raw[39:43], 'little') + 5
    if state_offset >= len(raw):
        raise RaidError('peer public snapshot state is truncated')
    # Frozen018D0780 ->018D0AD3 ->00A94114: town state would overwrite
    # actor+34. Keep the original stamped factory body with dungeon state1.
    raw[state_offset] = 1
    public = _stamp_userinfo0(bytes(raw), character_id=member.character_id,
                             weakness_offset=projection.weakness_offset)
    repo = server._repo()
    record = next((r for r in repo.list_by_account(member.account_id)
                   if int(r.character_id) == member.character_id), None)
    if record is None:
        raise RaidError('peer public snapshot is not account owned')
    level, exp, expertise = repo.get_character_progression(member.character_id)
    record = replace(record, level=int(level), exp=int(exp), expertise_exp=int(expertise))
    equipment, _ = load_chain('helper-userinfo_equipment.py').resolve_enter_room_equipment(
        record, enabled=True, backfill_from_pvf=False, repo=repo, prefer_live_db=True)
    helper = load_chain('helper-dungeon_preselect_weapon_avatar_userinfo1.py')
    skills, _ = helper._load_skill_page(repo, record)
    quests = load_chain('helper-quest_completion_snapshot.py').load_finished_quest_snapshot(
        repo, member.character_id)
    adventure = calculate_adventure_group_for_character(repo, record)
    combat = build_userinfo_subtype1_min(record, exp=int(exp), skills=skills,
        entry_flag=0, equipment=equipment, finished_quest_ids=quests.finished_quest_ids,
        adventure_group_level=int(adventure.adventure_level),
        guild_membership_active=has_active_guild_membership(repo, record))
    # Frozen018D3A70 reads the target UID here; it is not an own-only message.
    if combat[:4] != b'\1\1\0\0' or int.from_bytes(combat[22:24], 'little') != member.character_id:
        raise RaidError('peer combat snapshot factory ownership mismatch')
    return public, combat
