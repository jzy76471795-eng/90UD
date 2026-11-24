"""Owned raid preparation lobby, independent of the unfinished battle bridge."""
import os

from .protocol import C_CREATE_RAID, C_START_RAID, C_MODIFY_RAID
from .battle_protocol import C_RAID_ENTRY_COST_INFO, C_SELECT_RAID_REWARD_CARD
from .luke_mechanics import C_RAID_SET_SYMBOL
from .router import is_raid_command, route_raid_packet, service_for
from .runtime import RaidError


def enabled():
    return (os.environ.get('USDOF_RAID_LOBBY', '').strip().lower() in ('1', 'true', 'yes', 'on')
            and os.environ.get('USDOF_RAID_LOCAL', '').strip().lower() not in ('1', 'true', 'yes', 'on'))


def disconnect(server, session):
    if not enabled():
        return
    service = getattr(server, 'raid_service', None)
    if service is not None:
        with service._lock:
            _detach_owned(service, session)


def _detach_owned(service, session):
    from .local_bindings import detach_session
    frozen = service._raid_sessions.get(id(session))
    detach_session(service, session, 'lobby_selection_closed')
    if frozen is None or frozen[0] is not session:
        return
    _session, entry, cid, account, _epoch = frozen
    # The normal bridge intentionally ignores expired epochs. Preparation
    # has no recovery lease: remove its old owned member unless a newer
    # authenticated session has already rebound that exact membership.
    if (service.current(cid) is entry
            and not any(value[1] is entry and value[2] == cid
                        for value in service._raid_sessions.values())
            and entry.domain.member(cid).account_id == str(account)):
        entry.domain.leave(cid)
        service._membership.pop(cid, None)
        entry.records.pop(cid, None)
        if not entry.domain.members:
            service.raids.pop(entry.domain.raid_key, None)


def _prune(service):
    for frozen in tuple(service._raid_sessions.values()):
        session, entry, cid, account, epoch = frozen
        try:
            service.require_session(entry, session, cid, account)
        except RaidError:
            _detach_owned(service, session)


def _logical_body(opcode, body):
    # The plain-wire hook yields logical bodies. Legacy codec7/8/4 decoding
    # retains its minimum block padding. Slice only a declared/fixed length;
    # trailing zero stripping would corrupt names and authenticated IDs.
    if not isinstance(body, bytes):
        return body
    logical, unit = None, None
    if opcode == 0x029A:
        logical, unit = 2, 8
    elif opcode == C_RAID_ENTRY_COST_INFO:
        logical, unit = 1, 8
    elif opcode == C_SELECT_RAID_REWARD_CARD:
        logical, unit = 2, 16
    elif opcode == 0x029c:
        from game.game_codec import unit_for_opcode
        logical, unit = 2, unit_for_opcode(opcode)
    elif opcode == 0x02b8 and len(body) >= 5:
        from game.game_codec import unit_for_opcode
        logical, unit = 5 + 4 * body[4], unit_for_opcode(opcode)
    elif opcode == C_RAID_SET_SYMBOL:
        logical, unit = 9, 8
    elif opcode in (C_CREATE_RAID, C_MODIFY_RAID) and len(body) >= 5:
        logical, unit = 5 + int.from_bytes(body[1:5], 'little'), 8
    elif opcode == 0x033E and body:
        unit = 16
        if body[0] == 1:
            logical = 1
        elif body[0] == 0 and len(body) >= 13:
            logical = 13 + int.from_bytes(body[9:13], 'little')
    if logical is not None and logical < len(body) == ((logical + unit - 1) // unit) * unit:
        return body[:logical]
    return body


def try_route(server, opcode, body, session):
    if not enabled():
        return None
    # GameServer parses its header from a bytearray peek before freezing
    # the packet. The retained reserved prefix makes the plain body mutable.
    # Freeze once at this transport boundary; business codecs own bytes only.
    if isinstance(body, (bytearray, memoryview)):
        body = bytes(body)
    if opcode == 7:
        disconnect(server, session)
        return None
    from .local_bindings import public_enabled, local_raid_hooks
    if (opcode in (10, 11) and public_enabled()
            and getattr(session, 'selected_channel_id', None) in (23, 32)
            and isinstance(body, bytes) and len(body) >= 3 and body[2] == 0):
        hooks = local_raid_hooks(server)
        service = hooks.battle.service
        entry = service.current(getattr(session, 'selected_character_id', None))
        if entry is not None:
            try:
                result = (service.squad_invites.request(session, body) if opcode == 10
                          else service.squad_invites.accept(session, body))
            except (RaidError, ValueError) as exc:
                result = ([server._codec_response(1, opcode, b'\x00\x13\x00')],
                          'raid squad invitation rejected: ' + str(exc))
            server.log('[RaidLobby] opcode=%04X mode0 cid=%s peer=%s %s' %
                       (opcode, session.selected_character_id, int.from_bytes(body[:2], 'little'), result[1]))
            return result
    if (opcode in (14, 121) and public_enabled()
            and getattr(session, 'selected_channel_id', None) in (23, 32)):
        hooks = local_raid_hooks(server)
        service = hooks.battle.service
        if service.current(getattr(session, 'selected_character_id', None)) is not None:
            try:
                result = service.ordinary_party.manage(session, opcode, body)
            except (RaidError, ValueError) as exc:
                result = ([server._codec_response(1, opcode, b'\x00\x13')],
                          'raid squad management rejected: ' + str(exc))
            server.log('[RaidLobby] opcode=%04X cid=%s %s' %
                       (opcode, session.selected_character_id, result[1]))
            return result
    if (opcode == 12 and public_enabled()
            and getattr(session, 'selected_channel_id', None) in (23, 32)):
        try:
            hooks = local_raid_hooks(server)
            service = hooks.battle.service
            entry = service.current(getattr(session, 'selected_character_id', None))
            if entry is None:
                return None  # An unadmitted raid-channel actor uses lobby parties.
            ordinary = getattr(service, 'ordinary_party', None)
            result = (ordinary.create(session, body) if entry is not None and ordinary is not None and ordinary.uses(entry)
                      else service.native_solo_party.create(session, body))
        except (RaidError, ValueError) as exc:
            result = ([], 'native self-party rejected: ' + str(exc))
        server.log('[RaidLobby] opcode=000C ' + result[1])
        return result
    if (opcode == 13 and public_enabled()
            and getattr(session, 'selected_channel_id', None) in (23, 32)):
        hooks = local_raid_hooks(server)
        service = hooks.battle.service
        if service.current(getattr(session, 'selected_character_id', None)) is None:
            return None
        town_packets = []
        try:
            entry = service.current(session.selected_character_id)
            ordinary = getattr(service, 'ordinary_party', None)
            returned = bool(getattr(session, 'in_dungeon_run', False))
            if returned:
                if body:
                    raise RaidError('individual dungeon leave requires an empty body')
                with service._lock:
                    cid, aid, _record = service._actor(session)
                    service.require_session(entry, session, cid, aid)
                # Use the individual native village path, even for the host.
                # The squad's result-menu path would send every member home.
                town_packets, _note = hooks.handle_opcode(0x84, server._handle_back_2_village_0084, b'', session=session, party_leave=True)
                if not town_packets or getattr(session, 'in_dungeon_run', False) or getattr(session, 'in_dungeon_select', False):
                    raise RaidError('owned individual village return did not complete')
                runs = getattr(server, 'raid_squad_runs', None)
                if runs is not None:
                    runs.confirm_town(session)
            party_packets, note = (ordinary.leave(session, body, returned=returned)
                if ordinary is not None and ordinary.uses(entry) else service.native_solo_party.leave(session, body))
            result = (list(town_packets) + party_packets, note)
        except (RaidError, ValueError) as exc:
            result = (list(town_packets) + [server._codec_response(1, opcode, b'\x00\x13')], 'native self-party leave rejected: ' + str(exc))
        server.log('[RaidLobby] opcode=000D ' + result[1])
        return result
    if (opcode == 11 and public_enabled() and getattr(session, 'selected_channel_id', None) in (23, 32)
            and not (isinstance(body, bytes) and len(body) >= 3 and body[2] == 10)):
        hooks = local_raid_hooks(server)
        service = hooks.battle.service
        entry = service.current(getattr(session, 'selected_character_id', None))
        if entry is None:
            return None
        try:
            ordinary = getattr(service, 'ordinary_party', None)
            if ordinary is None or not ordinary.uses(entry):
                raise RaidError('ordinary mode0 requires its owned current target party')
            result = ordinary.join(session, body)
        except (RaidError, ValueError) as exc:
            result = ([server._codec_response(1, opcode, b'\x00\x13\x00')], 'ordinary mode0 rejected: ' + str(exc))
        server.log('[RaidLobby] opcode=000B mode0 ' + result[1])
        return result
    if not is_raid_command(opcode, body, session):
        return None
    service = service_for(server)
    with service._lock:
        _prune(service)
        before_member = service.current(getattr(session, 'selected_character_id', None))
        if (opcode in (C_START_RAID, C_RAID_ENTRY_COST_INFO, C_SELECT_RAID_REWARD_CARD) and not public_enabled()
                or opcode == C_CREATE_RAID and not getattr(session, 'enter_seria_settled', False)):
            result = ([server._codec_response(1, opcode, b'\x00\x13')],
                      'raid lobby requires settled town; battle bridge is unavailable')
        else:
            if (opcode == C_START_RAID and not getattr(session, 'enter_seria_settled', False)):
                result = ([server._codec_response(1, opcode, b'\x00\x13')], 'raid start requires a settled selected actor')
            else:
                result = route_raid_packet(server, opcode, _logical_body(opcode, body), session)
        after_member = service.current(getattr(session, 'selected_character_id', None))
    if before_member is None and after_member is not None:
        from game.normal_party import retire_for_raid
        clear_packets = retire_for_raid(server, session)
        if clear_packets:
            result = (clear_packets + result[0], result[1])
    server.log('[RaidLobby] opcode=%04X channel=%s cid=%s %s' % (
        opcode, getattr(session, 'selected_channel_id', None),
        getattr(session, 'selected_character_id', None), result[1]))
    return result


def note_send(server, session, packet):
    if not enabled():
        return
    pending = getattr(session, '_raid_namespace_pending', None)
    if pending is not None and pending[0] is packet:
        session._raid_native_namespace = pending[1]
        session._raid_namespace_pending = None
        server.log('[RaidLobby] native namespace delivered channel=%s namespace=%s' % (
            getattr(session, 'selected_channel_id', None), pending[1]))


def guard_area(server, session, parsed):
    """Guard the actual raid TWN dungeon gates before changing town state."""
    if not enabled():
        return parsed, ''
    policy = {(19, 2): (0, 'Anton'),
              (32, 2): (1, 'Luke'), (32, 4): (1, 'Luke')}
    required = policy.get((parsed['town_id'], parsed['area_id']))
    if required is None:
        return parsed, ''
    raid_type, family = required
    if session is None:
        return None, 'raid gate denied: authenticated selected actor required'
    try:
        service = service_for(server)
        with service._lock:
            cid, account, _record = service._actor(session)
            entry = service.current(cid)
            if entry is None or entry.raid_type != raid_type:
                raise RaidError(f'{family} gate requires owned raid membership')
            service.require_session(entry, session, cid, account)
            if not getattr(session, 'enter_seria_settled', False):
                raise RaidError(f'{family} gate requires a settled selected actor')
        return parsed, ''
    except RaidError as exc:
        note = 'raid gate denied: ' + str(exc)
        server.log('[RaidLobby] ' + note)
        return None, note
