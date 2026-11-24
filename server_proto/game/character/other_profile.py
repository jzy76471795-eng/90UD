"""C0008 target/kind3 -> native USERINFO0 and full USERINFO3 inspection.

018D27C0 reads the profile; 018D0010 opens window009E for a different UID.
The request does not change selection or return the account's roster.
"""
import struct
from dataclasses import replace

from game.chain_loader import load_chain
from game.raid.runtime import RaidError


def build_body(record, equipment, skills, *, exp, finished_quest_ids=(),
               adventure_group_level=0, guild=None):
    from game.character.character_stats import build_additional_info_for_character
    from game.character.payloads import _write_userinfo1_skill_page, project_adventure_group_effect_level
    from game.quest.personality_rewards import finished_personality_reward_ids
    from game.quest.time_fragment_rewards import finished_time_fragment_reward_ids
    job = int(record.job)
    _, combat = build_additional_info_for_character(job, int(record.level), int(record.grow_type),
        sub_grow_type=getattr(record, 'sub_grow_type', None), stat_level=100,
        guild_membership_active=guild is not None)
    if len(combat) != 83:
        raise ValueError('inspection combat profile must be the pinned 83-byte layout')
    equipment_blob, _, _ = load_chain('helper-userinfo1_equipment.py').encode_userinfo1_equipment_list(equipment)
    fragments = finished_time_fragment_reward_ids(finished_quest_ids, job_id=job)
    rewards = sorted(set(finished_personality_reward_ids(finished_quest_ids)).union(fragments))
    if len(fragments) > 255:
        raise ValueError('inspection time fragment count exceeds native width')
    body = bytearray(b'\x03\x01\x00\x00')
    body += struct.pack('<IBBHII', project_adventure_group_effect_level(adventure_group_level),
                        0, 0, int(record.character_id), exp, len(combat))
    body += combat
    body += bytes((int(getattr(record, 'ex_equip_slot_stat', 0) or 0) & 255,))
    body += equipment_blob + struct.pack('<I', 0) + b'\x00'
    _write_userinfo1_skill_page(body, skills)
    _write_userinfo1_skill_page(body, None)
    body += bytes((min(255, max(0, int(getattr(record, 'equipped_creature_level', 0) or 0))),))
    # 018D0010: three PvP counters, four flags, and a u8 title-id count.
    # This server has no PvP progression; the persisted public profile is empty.
    body += bytes(17)
    body += struct.pack('<I', 0)
    # 018CEAE0: guild level/id/name, membership grade/flag.
    name = (guild.display_name if guild else '').encode('utf8')[:64].decode('utf8', 'ignore').encode('utf8')
    body += struct.pack('<HII', int(getattr(guild, 'level', 0) or 0),
                        int(guild.guild_id) if guild else 0, len(name)) + name + b'\x00\x00'
    body += bytes((len(fragments),)) + struct.pack('<I', len(rewards))
    body += b''.join(struct.pack('<I', q) for q in rewards)
    body += b'\x00'  # 018D04C0 empty optional effect list
    return bytes(body)


def handle(server, session, target):
    try:
        directory = server.native_game_connections
        owner = directory.chat_selection(session, sender=True)
        matches = [s for s in directory.chat_selections() if s[3] == owner[3] and s[4] == target]
        if len(matches) != 1:
            raise RaidError('inspection target is not a unique online same-channel actor')
        selected = matches[0]
        repo = server._repo()
        record = next((r for r in repo.list_by_account(selected[2]) if int(r.character_id) == target), None)
        if record is None:
            raise RaidError('inspection target no longer belongs to its connection')
        helper = load_chain('helper-userinfo_equipment.py')
        equipment, _ = helper.resolve_enter_room_equipment(record, enabled=True,
            backfill_from_pvf=False, repo=repo, prefer_live_db=True)
        exp, level = helper._resolve_exp_level_for_view(record, repo=repo, character_id=target)
        record = replace(record, level=level, exp=exp)
        skills, _ = load_chain('helper-character_skills.py').resolve_skills_for_character(
            repo, target, int(record.job), seed_if_empty=False)
        quests = load_chain('helper-quest_completion_snapshot.py').load_finished_quest_snapshot(repo, target)
        from game.character.adventure_group import calculate_adventure_group_for_character
        from game.guild.guild_repository import GuildRepository
        from game.character.payloads import build_userinfo_subtype0_min
        guild = GuildRepository(repo.db_path).get_guild_for_character(target)
        adventure = calculate_adventure_group_for_character(repo, record)
        appearance = build_userinfo_subtype0_min(record, equipment,
            guild_id=int(guild.guild_id) if guild else 0, guild_name=guild.display_name if guild else '')
        profile = build_body(record, equipment, skills, exp=exp,
            finished_quest_ids=quests.finished_quest_ids,
            adventure_group_level=int(adventure.adventure_level), guild=guild)
        if directory.chat_selection(session, sender=True) != owner or directory.chat_selection(selected[0].session) != selected:
            raise RaidError('inspection selection changed while reading profile')
        return [server._codec_response(0, 2, appearance), server._codec_response(0, 2, profile)], 'GET_USERINFO inspection target=%s' % target
    except (RaidError, ValueError, AttributeError) as exc:
        return [], 'GET_USERINFO inspection rejected: ' + str(exc)
