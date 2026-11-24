"""Narrow routing for confirmed current-US raid command numbers."""
from threading import Lock

from .battle_protocol import C_RAID_INFO, C_RAID_ENTRY_COST_INFO, C_SELECT_RAID_REWARD_CARD, parse_raid_info_request, raid_info_failure_payload
from .protocol import C_CREATE_RAID, C_LEAVE_RAID, C_START_RAID, C_MODIFY_RAID, LOCAL_NATIVE_NAMESPACE
from .runtime import RaidError, EntryMaterialsMissing
from .service import RaidService
from .luke_mechanics import C_RAID_SET_SYMBOL
from .party_board import C_RAID_MONSTER_HP
from .skills import C_RAID_SKILL
from .ingress_protocol import (C_RAID_SEARCH, N_RAID_SEARCH_RESULT,
    C_JOIN_RAID_VIA_PARTY, C_SET_RAID_WAITING, C_RAID_ACTION_294, C_RAID_MANAGER_WORK,
    C_JOIN_RAID_APPROVAL, C_JOIN_RAID_OTHER_NAMESPACE, C_RAID_MEMBER_LOOKUP,
    encode_search_result, unsupported_join_failure_body, RAID_JOIN_KIND)

RAID_OPCODES = frozenset((C_RAID_SKILL, C_RAID_SET_SYMBOL, C_CREATE_RAID, C_LEAVE_RAID, C_START_RAID, C_MODIFY_RAID, C_RAID_INFO,
                          C_RAID_ENTRY_COST_INFO, C_SELECT_RAID_REWARD_CARD,
                          C_RAID_SEARCH, C_SET_RAID_WAITING, C_RAID_ACTION_294, C_RAID_MANAGER_WORK))
_SERVICE_LOCK = Lock()


def is_raid_command(opcode, body, session):
    if opcode == C_RAID_MONSTER_HP:
        return getattr(session, 'selected_channel_id', None) in (23, 32)
    if opcode in RAID_OPCODES:
        return True
    if getattr(session, 'selected_channel_id', None) not in (23, 32):
        return False
    if opcode in (C_JOIN_RAID_OTHER_NAMESPACE, C_RAID_MEMBER_LOOKUP):
        return True
    return (opcode in (C_JOIN_RAID_VIA_PARTY, C_JOIN_RAID_APPROVAL) and isinstance(body, bytes) and len(body) >= 3
            and body[2] == RAID_JOIN_KIND)


def service_for(server):
    with _SERVICE_LOCK:
        service = getattr(server, 'raid_service', None)
        if service is None:
            service = RaidService(server)
            from .join_profile import SelectedJoinProfileResolver
            service.native_join.request_profile_resolver = SelectedJoinProfileResolver(service)
            server.raid_service = service
        return service


def route_raid_packet(server, opcode, body, session):
    try:
        if opcode == C_RAID_MONSTER_HP:
            from .local_bindings import local_raid_hooks
            hooks = local_raid_hooks(server)
            if hooks is None:
                raise RaidError('party board battle bridge unavailable')
            return hooks.battle.party_hp_report(session, body)
        if opcode == C_RAID_SKILL:
            from .local_bindings import local_raid_hooks
            from .skills import activate
            hooks = local_raid_hooks(server)
            if hooks is None:
                raise RaidError('raid battle is not enabled')
            return activate(hooks.battle, session, body)
        if opcode == C_RAID_SET_SYMBOL:
            from .local_bindings import local_raid_hooks
            hooks = local_raid_hooks(server)
            if hooks is None:
                raise RaidError('Luke battle is not enabled')
            return hooks.battle.luke_machine_report(session, body)
        if opcode == C_CREATE_RAID and (getattr(session, '_raid_native_namespace', None)
                                        != LOCAL_NATIVE_NAMESPACE):
            raise RaidError('raid namespace has not been announced successfully to this connection')
        return service_for(server).handle(opcode, body, session)
    except (RaidError, ValueError) as exc:
        if opcode == C_RAID_MONSTER_HP:
            return [], "party board HP ignored: " + str(exc)
        if opcode == C_RAID_SKILL:
            # No S1/02B7 consumer is registered in the frozen native client.
            return [], 'raid skill rejected: ' + str(exc)
        if opcode == C_RAID_SET_SYMBOL:
            # This native ACT notification has no established S1 ACK consumer.
            return [], 'Luke machine report rejected: ' + str(exc)
        if opcode == C_START_RAID and isinstance(exc, EntryMaterialsMissing):
            # 01605E10 error11 displays BFBD. It consumes and discards an
            # optional u32 vector; its element semantics remain unproved.
            return [server._codec_response(1, opcode, b'\x00\x11\x00\x00\x00\x00')], 'raid start rejected: ' + str(exc)
        # 0x13 has a verified simple error reader for CREATE/LEAVE/START.
        # Its localized wording has not been established here.
        extra = b''
        if opcode == C_RAID_SEARCH:
            # There is no S1/033E registration. An empty N2F2 also releases
            # native search pending state after malformed/unauthorized input.
            return [server._codec_response(0, N_RAID_SEARCH_RESULT, encode_search_result(0, 0, []))], 'raid search rejected: ' + str(exc)
        if opcode in (C_JOIN_RAID_VIA_PARTY, C_JOIN_RAID_APPROVAL):
            return [server._codec_response(1, opcode, unsupported_join_failure_body())], 'raid join rejected: ' + str(exc)
        if opcode == C_RAID_INFO:
            try:
                action, raid_id = parse_raid_info_request(body)
                extra = raid_info_failure_payload(action, raid_id)
            except ValueError:
                return [], 'raid malformed info request: ' + str(exc)
        packet = server._codec_response(1, opcode, b'\x00\x13' + extra)
        return [packet], 'raid rejected: ' + str(exc)
