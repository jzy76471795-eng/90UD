"""Bind the reviewed raid adapters to the existing local US transport."""
import os
from secrets import randbelow
from threading import RLock
from time import monotonic
from .protocol import LOCAL_NATIVE_NAMESPACE
from .native_connection import capture_login_send, confirm_login_send, invalidate_selection

_LOCK = RLock()


def enabled():
    return (os.environ.get('USDOF_RAID_LOCAL', '').strip().lower() in ('1', 'true', 'yes', 'on')
            or public_enabled())


def public_enabled():
    return (os.environ.get('USDOF_RAID_BATTLE', '').strip().lower() in ('1', 'true', 'yes', 'on')
            and os.environ.get('USDOF_RAID_LOBBY', '').strip().lower() in ('1', 'true', 'yes', 'on')
            and os.environ.get('USDOF_RAID_LOCAL', '').strip().lower() not in ('1', 'true', 'yes', 'on'))


def remember_session(service, session, entry, character_id, account_id):
    if not hasattr(session, '_raid_selected_epoch'):
        session._raid_selected_epoch = object()
    service._raid_sessions[id(session)] = (session, entry, character_id, account_id, session._raid_selected_epoch)


def detach_session(service, session, reason):
    def cleanup(label, callback):
        try:
            callback()
        except Exception as exc:
            log = getattr(service.server, 'log', None)
            if callable(log):
                try:log('[Raid] detached session cleanup deferred component=%s error=%s' % (label, exc))
                except Exception:pass
    with service._lock:
        service.forget_waiting(session)
        frozen = service._raid_sessions.pop(id(session), None)
        if frozen is not None and frozen[0] is session:
            _session, entry, cid, account, epoch = frozen
            if (service.current(cid) is entry and not any(
                    row[1] is entry and row[2] == cid for row in service._raid_sessions.values())):
                from .runtime import RaidError
                try:
                    retained = False
                    if entry.domain.member(cid).account_id == str(account):
                        try:
                            retained = service.suspend_session(frozen, reason)
                        except RaidError:
                            retained = False
                    if not retained and entry.domain.member(cid).account_id == str(account):
                        service.exit_refunds.depart(entry, cid)
                        entry.domain.leave(cid)
                        recovery=getattr(service.battle_adapter,'settlement_recovery',None)
                        if recovery is not None:recovery.departed(entry,cid)
                        service._membership.pop(cid, None)
                        service._disconnected.pop(cid, None)
                        entry.records.pop(cid, None)
                except RaidError:
                    pass
                if not entry.domain.members:
                    service.raids.pop(entry.domain.raid_key, None)
        # Freeze or remove membership before optional scene/profile cleanup.
        # A failure in any one observer must not strand an online raid seat.
        squad = getattr(service.server, 'raid_squad_runs', None)
        if squad is not None:cleanup('squad', lambda: squad.forget(session))
        cleanup('routing', lambda: invalidate_selection(service.server, session, reset_login=reason == 'channel_change'))
        if service.battle_adapter is not None:cleanup('battle', lambda: service.battle_adapter.forget_session(session))
        cleanup('inventory', lambda: service.forget_inventory_refresh(session))
        cleanup('lobby-refresh', lambda: service.forget_lobby_refresh(session))
        cleanup('native-lobby', lambda: service.native_lobby.forget_session(session))
        cleanup('native-join', lambda: service.native_join.forget_session(session))
        cleanup('native-party', lambda: service.native_party.forget_session(session))
        if hasattr(service, 'ordinary_party'):
            cleanup('ordinary-party', lambda: service.ordinary_party.forget_session(session))
        if hasattr(service, 'native_solo_party'):
            cleanup('solo-party', lambda: service.native_solo_party.forget_session(session))
        session._raid_selected_epoch = object()
        session.raid_id = None


def local_raid_hooks(server):
    if not enabled():
        return None
    hooks = getattr(server, 'raid_transport_hooks', None)
    if hooks is not None:
        return hooks
    with _LOCK:
        hooks = getattr(server, 'raid_transport_hooks', None)
        if hooks is not None:
            return hooks
        from .bootstrap import runtime_data
        from .battle_adapter import RaidBattleAdapter
        from .dungeon_adapter import OwnerIdentity
        from .ledger import RaidLedger
        from .router import service_for
        from .transport_hooks import RaidTransportHooks
        from game.game_server import _packet_key
        service, runtime = service_for(server), runtime_data()
        if public_enabled():
            from .native_solo_party import NativeSoloParty
            service.native_solo_party = NativeSoloParty(service)
            from .ordinary_party import install
            ordinary = install(service)
            def prepared(entry):
                return ordinary.all_ready(entry) if ordinary.uses(entry) else service.native_solo_party.ready(entry)
            def auto_prepare(entry):
                if not ordinary.uses(entry):
                    return service.native_solo_party.auto_prepare_allowed(entry)
                return all(ordinary.auto_prepare_allowed(row[0]) for row in service._raid_sessions.values() if row[1] is entry)
            def prepared_for(entry, session):
                return ordinary.ready(session) if ordinary.uses(entry) else service.native_solo_party.ready(entry)
            def auto_for(entry, session):
                return ordinary.auto_prepare_allowed(session) if ordinary.uses(entry) else service.native_solo_party.auto_prepare_allowed(entry)
            def participants(entry, session):
                return ordinary.participants(session) if ordinary.uses(entry) else (session.selected_character_id,)
        ledger = RaidLedger(server._repo())
        ledger.ensure_schema()
        from .ticket_refunds import recover
        from time import time
        recover(ledger, (entry.domain.battle.instance_id for entry in service.raids.values()
                         if entry.domain.battle is not None), time())
        def owner(session):
            # The bridge validates every group peer under the current sender's
            # TLS. Resolve each already-authenticated frozen session on its own;
            # adapter request entry still checks the actual caller through _actor.
            cid, aid, record = service._selected_actor(session)
            entry = service.current(cid)
            if entry is None:
                from .runtime import RaidError
                raise RaidError('selected owner has no frozen raid membership')
            epoch = service.require_session(entry, session, cid, aid)
            return OwnerIdentity(cid, str(aid), int(session.selected_channel_id), epoch)
        from game.quest.quest_index import daily_quest_day_key
        from .native_clock import clock_ready, native_deadline, native_timer_deadline
        adapter = RaidBattleAdapter(service, runtime, ledger,
            entry_period_key=('server-local-calendar' if public_enabled() else os.environ['USDOF_RAID_ENTRY_PERIOD_KEY']),
            entry_period_resolver=daily_quest_day_key if public_enabled() else None, owner_resolver=owner,
            native_prepared=prepared if public_enabled() else None,
            native_auto_prepare=auto_prepare if public_enabled() else None,
            native_session_prepared=prepared_for if public_enabled() else None,
            native_session_auto_prepare=auto_for if public_enabled() else None,
            native_participants=participants if public_enabled() else None,
            roll=randbelow,
            barrier_classifier=getattr(runtime, 'actor_classifier', None),
            native_deadline=native_deadline, native_clock_ready=clock_ready,
            native_timer_deadline=native_timer_deadline, clear_quota=True)
        if public_enabled():
            adapter.bridge._ordinary_authorizer = lambda raid, session, ids: (
                ordinary.authorize_run(raid, session, ids)
                if ordinary.uses(service.current(session.selected_character_id)) else None)
        adapter.on_selection_changed = lambda session, reason: detach_session(service, session, reason)
        service.battle_adapter = adapter
        hooks = RaidTransportHooks(adapter, packet_key=_packet_key)
        server.raid_transport_hooks = hooks
        if public_enabled():
            from .squad_runs import RaidSquadRuns
            server.raid_squad_runs = RaidSquadRuns(hooks)
        return hooks


class _PhaseResultPacket(bytes):
    """Wire-identical result notification carrying its originating phase's send policy."""


class PhaseResultDelivery:
    def __init__(self):
        self._lock = RLock()
        self._closing = False
        self._writing = 0

    def packet(self, value):
        if not isinstance(value, bytes):
            return value
        packet = _PhaseResultPacket(value)
        packet.delivery = self
        return packet

    def try_close(self):
        # Once the switch is requested, no later write may enter phase one.
        # The caller retries on its normal tick while previously started writes finish.
        with self._lock:
            self._closing = True
            return self._writing == 0

    def reopen(self):
        with self._lock:
            self._closing = False

    def write(self, client, packet):
        with self._lock:
            if self._closing:
                return False
            self._writing += 1
        try:
            client.sendall(packet)
            return True
        finally:
            with self._lock:
                self._writing -= 1


def _write_packet(client, packet):
    if isinstance(packet, _PhaseResultPacket):
        return packet.delivery.write(client, packet)
    client.sendall(packet)
    return True


def send_game_packet(server, session, client, packet, *, on_sent=None):
    """Preserve exact socket receipts on immediate, delayed and control sends."""
    hooks = local_raid_hooks(server) if session is not None else None
    packets = [packet]
    for value in packets:
        receipt = hooks.capture_send(session, value) if hooks is not None else None
        profile_receipt = hooks.battle.service.native_lobby.capture_profile_send(session, value) if hooks is not None else None
        login_receipt = capture_login_send(server, session, client, value)
        if not _write_packet(client, value):
            continue
        # Recipient-owned progress records the actual socket write even if a
        # later observer raises. This never reports a queued/failed write.
        if on_sent is not None:
            on_sent(value)
        confirm_login_send(server, login_receipt)
        note_local_send(server, session, value, profile_receipt)
        if hooks is not None:
            packets.extend(hooks.after_send(receipt))


def close_game_session(server, session):
    hooks = getattr(server, 'raid_transport_hooks', None) if enabled() else None
    if hooks is not None:
        hooks.close(session)


def local_notifications(server, session):
    hooks = local_raid_hooks(server)
    if hooks is None:
        return []
    # Drain/validate even when selection reset has removed current membership.
    batches = hooks.pop_notifications(session)
    service = hooks.battle.service
    service.expire_disconnected()
    if getattr(session, '_raid_start_wait', None) is not None:
        started = service.retry_start(session)
        if started:
            batches.append(started)
    notices = service.recovery_notice_packets(session)
    if notices:batches.append(notices)
    if getattr(session, 'selected_character_id', 0):
        refunds = hooks.battle.refund_packets(session)
        if refunds:batches.append(refunds)
    actor_packets = service.native_lobby.pending_packets(session)
    if actor_packets:
        batches.append(actor_packets)
    join_packets = service.native_join.pending_packets(session)
    if join_packets:
        batches.append(join_packets)
    if (hooks.battle.raid_for_session(session) is None
            and getattr(session, 'selected_character_id', None) in service._disconnected
            and getattr(session, 'enter_seria_settled', False)
            and getattr(session, '_raid_native_namespace', None) == LOCAL_NATIVE_NAMESPACE):
        try:
            recovery, _note = service.reconnect_session(session)
            batches.append(recovery)
        except Exception as exc:
            server.log('[Raid] authenticated lobby recovery deferred: %s' % exc)
    if hasattr(server, 'gm_item_commands') and getattr(session,'account_id',None)==server.account_id:
        try:
            def encode(list_types):
                rows = []
                for list_type in sorted(list_types):
                    packets, _detail = server.gm_item_commands._full_container_packets(
                        server._repo(), session.selected_character_id, list_type)
                    rows.extend(packets)
                return rows
            rows = service.inventory_refresh_packets(session, encode)
            if rows is not None:
                batches.append(rows)
        except Exception as exc:
            server.log('[Raid] committed recipient inventory refresh deferred: %s' % exc)
    if hooks.battle.raid_for_session(session) is None:
        try:
            views = service.refresh_views(session)
            if views:
                batches.append(views)
        except Exception as exc:
            server.log('[Raid] recipient search refresh deferred: %s' % exc)
        # A selected actor leaving the raid still needs its old native party
        # cleared. A changed selection has already discarded that recipient.
        party = service.native_party.pending_packets(session)
        if party:batches.append(party)
        return batches
    try:
        lobby = service.lobby_refresh_packets(session)
        if lobby is not None:
            batches.append(lobby)
        views = service.refresh_views(session)
        if views:
            batches.append(views)
    except Exception as exc:
        server.log('[Raid] recipient lobby refresh deferred: %s' % exc)
    party = service.native_party.pending_packets(session)
    if party:
        batches.append(party)
    return batches


def note_local_send(server, session, packet, profile_receipt=None):
    """Called only after the owning client.sendall returned successfully."""
    from .native_clock import note_send as note_clock_send
    note_clock_send(session, packet)
    hooks = local_raid_hooks(server)
    if hooks is None:
        return
    namespace = getattr(session, '_raid_namespace_pending', None)
    if namespace is not None and namespace[0] is packet:
        session._raid_native_namespace = namespace[1]
        session._raid_namespace_pending = None
    adapter = hooks.battle
    adapter.service.native_lobby.note_send(session, packet, profile_receipt)
    adapter.service.native_join.note_send(session, packet)
    adapter.service.view_sent(session, packet)
    adapter.service.lobby_refresh_packet_sent(session, packet)
    adapter.service.recovery_notice_sent(session, packet)
    adapter.service.native_party.note_send(session, packet)
    if hasattr(adapter.service, 'native_solo_party'):
        adapter.service.native_solo_party.note_send(session, packet)
    adapter.service.inventory_refresh_packet_sent(session, packet)
    raid = adapter.raid_for_session(session)
    if raid is None:
        return
    entry = adapter.service.current(session.selected_character_id)
    pending = adapter.pending_for(session)
    if pending is None or not any(value is packet for value in pending[0]):
        return
    batch = pending[0]
    previous = getattr(session, '_raid_projection_sent', None)
    if previous is None or previous[0] is not batch:
        previous = (batch, set())
        session._raid_projection_sent = previous
    for index, value in enumerate(batch):
        if value is packet and index not in previous[1]:
            previous[1].add(index)
            break
    projection = pending[1]
    projection.note_visible_write(packet)
    if entry.family == 'anton' and projection.active_state_packet is packet:
        # Retain the actual notice write across a scene change even when other
        # packets in this batch still need to be sent.
        projection.native_state = pending[2][0]
    if len(previous[1]) == len(batch):
        adapter.projection_sent(entry, batch, session)
        session._raid_projection_sent = None


def pump_local_raid(server, session, client):
    from .clear_quota import pump as pump_clear_quota
    pump_clear_quota(server, session, client)
    if not getattr(session, 'selected_character_id', None):
        return
    hooks = local_raid_hooks(server)
    if hooks is None:
        return
    # Durable settlement work must progress even when no raid channel is online.
    hooks.battle.settlement_recovery.retry()
    hooks.battle.service.exit_refunds.retry()
    if getattr(session,'selected_channel_id',None) not in (23,32):
        for batch in local_notifications(server,session):
            for packet in batch:
                send_game_packet(server,session,client,packet)
        return
    squad = getattr(server, 'raid_squad_runs', None)
    # Combat replication belongs to the recipient loop. Dispatch it ahead of
    # doors and the slower lobby/status refresh, on the recipient's account.
    if hasattr(server, 'die_monster_0027'):
        for packet in hooks.shared_death_packets(session, server.die_monster_0027.handle_die_monster_0027):
            send_game_packet(server, session, client, packet)
    if squad is not None:
        squad.pump(session, client)
    now = monotonic()
    if now - getattr(session, '_raid_last_tick', 0) < 0.25:
        return
    session._raid_last_tick = now
    tick = hooks.battle.tick(session) if hooks.battle.raid_for_session(session) is not None else []
    batches = [tick] if tick else []
    leave = getattr(server, '_handle_back_2_village_0084', None)
    if leave is not None and hooks.bridge.expired_run(session):
        # Run the original recipient-owned exit. This resets combat/room
        # state and returns to the raid waiting area without a clear/reward.
        forced = squad.expired_returns.prepare(session) if squad is not None else None
        packets, note = (forced if forced is not None else
            hooks.handle_opcode(0x0084, leave, b'', session=session))
        if packets:
            batches.append(packets)
        server.log('[Raid] expired owned run returned to waiting area: ' + note)
    for batch in local_notifications(server, session):
        # A retirement wake-up can reference the very same pending batch
        # returned by tick. Keep its identity until combining send intents;
        # packet bytes and repeated occurrences inside a batch are not keys.
        if batch and not any(batch is queued for queued in batches):
            batches.append(batch)
    packets = [packet for batch in batches for packet in batch]
    party_followup_queued = False
    for packet in packets:
        receipt = hooks.capture_send(session, packet)
        profile_receipt = hooks.battle.service.native_lobby.capture_profile_send(session, packet)
        if not _write_packet(client, packet):
            continue
        if squad is not None:
            squad.expired_returns.note_send(session, packet)
        note_local_send(server, session, packet, profile_receipt)
        followup = hooks.after_send(receipt)
        if followup:
            packets.extend(followup)
        if not party_followup_queued:
            # The final real actor/lobby send may unlock the ordered native
            # address/roster batch. At most one follow-up batch per pump keeps
            # a changing source from growing this send loop without a bound.
            party = hooks.battle.service.native_party.pending_packets(session)
            if party:
                party_followup_queued = True
                packets.extend(value for value in party if not any(value is queued for queued in packets))
        server.log('[Raid] timer/progression packet sent channel=%s bytes=%s' % (session.selected_channel_id, len(packet)))
