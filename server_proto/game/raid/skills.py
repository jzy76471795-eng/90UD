"""Owned native C02B7/N027A raid skills for the frozen US resource.

03095150 assigns time=0, attack=1, invincible=2, restore=3, coin=4.
00FEAB00 -> 0103E440/0103DFC0 applies the active records to the local actor;
N027A is not only an icon. 0119A370/0119CEB0 compare raid group numbers.
0103DCA0 coin case subtracts its PVF amount from ordinary used revives.
"""
from dataclasses import dataclass, replace
import re
import struct

from .party_board import party_snapshot
from .runtime import RaidError

C_RAID_SKILL = 0x02b7
N_RAID_SKILLS = 0x027a
_IDS = {'INCREASE TIME': 0, 'ATTACK BONUS': 1, 'INVINCIBLE': 2, 'RESTORE': 3, 'INCREASE COIN': 4}


@dataclass(frozen=True)
class SkillRule:
    skill: int
    scope: str
    cooldown: int
    duration: int
    value: int


@dataclass(frozen=True)
class SkillUse:
    phase: int
    skill: int
    group: int
    member: int
    effect_end: float
    cooldown_end: float


def parse_rules(text):
    rules = {}
    for block in re.findall(r'\[BUFF\](.*?)\[/BUFF\]', text, re.S):
        match = re.fullmatch(r'\s*\[([^\]]+)\]\s*\[(RAID|PARTY)\]\s*(\d+)\s+(\d+)\s+(\d+)\s*', block)
        if match is None or match[1] not in _IDS:
            raise ValueError('unsupported frozen raid skill rule')
        skill = _IDS[match[1]]
        if skill in rules:
            raise ValueError('duplicate raid skill')
        rule = SkillRule(skill, match[2], *(int(match[i]) for i in (3,4,5)))
        if not 0 < rule.duration <= rule.cooldown <= 86400:
            raise ValueError('invalid raid skill timing')
        rules[skill] = rule
    if set(rules) != set(range(5)) or any(rules[i].scope != ('RAID' if i in (0,1) else 'PARTY') for i in rules):
        raise ValueError('incomplete raid skill resource')
    return rules


def parse_request(body):
    # Native writer sends BHH; opcode%14=9 retains a 16-byte padded block.
    if not isinstance(body, bytes) or len(body) not in (5, 16):
        raise RaidError('raid skill requires its exact native request')
    skill, group, member = struct.unpack('<BHH', body[:5])
    if skill not in range(5) or not 1 <= group <= 10 or not 1 <= member < 65535:
        raise RaidError('invalid native raid skill target')
    return skill, group, member


def encode_skills(uses, deadline, rules):
    uses = tuple(sorted(uses, key=lambda use: use.skill))
    body = bytearray([len(uses)])
    for use in uses:
        rule = rules[use.skill]
        end = deadline(use.effect_end, rule.duration)
        cooldown = deadline(use.cooldown_end, rule.cooldown)
        if end is None or cooldown is None:
            raise RaidError('native skill clock has not been delivered')
        body += struct.pack('<BBHHII', use.skill, 1, use.group, use.member, end, cooldown)
    return bytes(body)


def projection(adapter, entry, battle, data, session, clock_ready):
    rules = (getattr(adapter.runtime, 'skill_rules', None) or {}).get(entry.raid_type)
    uses = tuple(use for use in data.skills.values() if use.phase == battle.phase
                 and use.cooldown_end > adapter.clock()) if battle.status == 'active' else ()
    if not uses or rules is None or not clock_ready:
        return b'\0'
    def deadline(end, duration):
        return (adapter.native_timer_deadline(session, entry, end, duration)
                if adapter.native_timer_deadline is not None
                else adapter.native_deadline(entry, end, duration))
    return encode_skills(uses, deadline, rules)


def activate(adapter, session, body):
    skill, target_group, target_member = parse_request(body)
    with adapter._lock:
        entry, owner = adapter._entry(session)
        raid = entry.domain
        raid._leader(owner.character_id)
        with raid._lock:
            now = adapter.clock()
            raid.advance_clock(now)
            battle = raid._active(now)
            if now < battle.enter_after:
                raise RaidError('raid skill requires the started phase')
            rules = (getattr(adapter.runtime, 'skill_rules', None) or {}).get(entry.raid_type)
            if rules is None:
                raise RaidError('raid skill resource is unavailable')
            rule = rules[skill]
            if adapter.native_clock_ready is not None and not adapter.native_clock_ready(session):
                raise RaidError('raid skill requires the delivered native clock')
            rows = [row for row in party_snapshot(adapter.service, entry)
                    if row.group == target_group and row.members[0] == target_member]
            if len(rows) != 1:
                raise RaidError('raid skill target is not a current published ordinary party')
            row = rows[0]
            data = adapter._runs[raid.raid_key]
            previous = data.skills.get(skill)
            if previous is not None and previous.phase == battle.phase and previous.cooldown_end > now:
                raise RaidError('raid skill cooldown is active')
            run = None
            if rule.scope == 'PARTY':
                run = adapter.bridge.skill_run(raid, row.sessions)
            use = SkillUse(battle.phase, skill,
                0xffff if rule.scope == 'RAID' else target_group,
                0xffff if rule.scope == 'RAID' else target_member,
                now + rule.duration, now + rule.cooldown)
            # Validate exact native seconds and codec before any mutation.
            def deadline(end, duration):
                return (adapter.native_timer_deadline(session, entry, end, duration)
                        if adapter.native_timer_deadline is not None
                        else adapter.native_deadline(entry, end, duration))
            adapter.codec(0, N_RAID_SKILLS, encode_skills((use,), deadline, rules))
            if skill == 0:
                raid._battle = replace(battle, deadline=battle.deadline + rule.value)
            elif skill == 4:
                # Ordinary per-run usage only. Luke's separate phase-wide
                # resource quota is not erased by a party restoration skill.
                data.run_revives[run] = max(0, data.run_revives.get(run, 0) - rule.value)
            data.skills[skill] = use
        return adapter._project(entry, session), 'raid skill applied id=%d group=%d' % (skill, target_group)
