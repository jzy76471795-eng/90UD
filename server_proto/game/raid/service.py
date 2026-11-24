"""Current-US raid directory and authenticated session protocol adapter."""
from collections import deque
from dataclasses import dataclass, replace
from math import isfinite
from threading import RLock

from game.initial_equipment import get_userinfo_job_base_for_job
from .battle_protocol import (N_RAID_ENTRY_COST_INFO, C_RAID_INFO, C_RAID_ENTRY_COST_INFO, C_SELECT_RAID_REWARD_CARD, lobby_member, lobby_metadata,
                             parse_raid_info_request, raid_info_success_payload)
from .protocol import (C_CREATE_RAID, C_LEAVE_RAID, C_START_RAID, C_MODIFY_RAID, N_RAID_MODIFY, LOCAL_NATIVE_NAMESPACE,
                       create_success_payload, leave_success_payload,
                       encode_raid_modify, parse_create_request, parse_modify_request, parse_leave_request, parse_start_request)
from .ingress_protocol import (C_RAID_SEARCH, N_RAID_SEARCH_RESULT, N_RAID_WAITING_LIST,
    C_JOIN_RAID_VIA_PARTY, C_SET_RAID_WAITING, C_RAID_ACTION_294, C_RAID_MANAGER_WORK,
    C_JOIN_RAID_APPROVAL, C_JOIN_RAID_OTHER_NAMESPACE, C_RAID_MEMBER_LOOKUP,
    parse_search_request, encode_search_result,
    parse_action294_request, parse_member_raid_lookup_request, member_raid_lookup_success)
from .battle_projection import parse_group_request, parse_waiting_request
from .runtime import Raid, RaidError, Member, PREPARATION_GROUP_MAX
from .native_lobby import NativeLobby
from .native_join import NativeJoin
from .native_party import NativeParty


@dataclass
class DirectoryEntry:
    domain: Raid
    name_bytes: bytes
    raid_type: int
    records: dict
    native_actor_preparation: bool = False

    @property
    def family(self):
        return self.domain.family


@dataclass(frozen=True)
class DisconnectedMember:
    session: object
    entry: DirectoryEntry
    character_id: int
    account_id: int
    channel_id: int
    expires_at: float


class RaidService:
    def __init__(self, server, *, join_profile_resolver=None, join_clock=None, party_state_resolver=None):
        self.server = server
        self.raids = {}
        self._membership = {}
        self._sequence = 0
        self._waiting = {}
        self._lock = RLock()
        self.battle_adapter = None
        self._raid_sessions = {}
        self._inventory_refresh = {}
        self._inventory_refresh_owners = {}
        self._inventory_refresh_batches = {}
        self._disconnected = {}
        self._lobby_projections = {}
        self._lobby_pending = {}
        self._recovery_notices = {}
        self._recovery_notice_pending = {}
        self.native_lobby = NativeLobby(self)
        self.native_join = NativeJoin(self, join_profile_resolver, join_clock)
        self.native_party = NativeParty(self, party_state_resolver)
        from .squad_invites import SquadInvites
        self.squad_invites = SquadInvites(self)
        from .exit_refunds import ExitRefunds
        self.exit_refunds = ExitRefunds(self)

    def _recovery_clock(self):
        now = self.battle_adapter.clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not isfinite(now) or now <= 0:
            raise RaidError('trusted raid recovery clock unavailable')
        return now

    def suspend_session(self, frozen, reason):
        """Keep an admitted member after TCP loss; never preserve a run binding."""
        session, entry, cid, account, epoch = frozen
        if reason != 'tcp_close' or self.battle_adapter is None or entry.domain.battle is None:
            return False
        config = self.battle_adapter._configs.get(entry.raid_type)
        seconds = getattr(config, 'disconnected_member_wait_seconds', None)
        if type(seconds) is not int or seconds <= 0 or entry.domain.battle.status == 'timed_out':
            return False
        member = entry.domain.member(cid)
        if member != entry.domain._admitted.get(cid) or member.account_id != str(account):
            return False
        self._disconnected[cid] = DisconnectedMember(session, entry, cid, account,
                                                    {0: 23, 1: 32}[entry.raid_type], self._recovery_clock() + seconds)
        name = self._recovery_name(entry, cid)
        self._queue_recovery_notice(entry, '%s已掉线，攻坚队席位保留%d分钟，期间重进原团本频道可自动归队。' % (name, seconds // 60))
        return True

    def expire_disconnected(self):
        self._reconcile_closed_sessions()
        with self._lock:
            self.exit_refunds.retry()
            recovery=getattr(self.battle_adapter,'settlement_recovery',None)
            if recovery is not None:recovery.retry()
            if not self._disconnected:
                return
            try:
                now = self._recovery_clock()
            except RaidError:
                # Retain the owned lease until a trustworthy clock returns.
                return
            for cid, lease in tuple(self._disconnected.items()):
                if now < lease.expires_at:
                    continue
                if self.current(cid) is not lease.entry:
                    self._disconnected.pop(cid, None)
                    continue
                if self._live_selected_member(lease.entry, cid):
                    self._disconnected.pop(cid, None)
                    continue
                try:
                    member = lease.entry.domain.member(cid)
                    if member.account_id != str(lease.account_id):
                        continue
                    self.exit_refunds.depart(lease.entry, cid)
                    lease.entry.domain.leave(cid)
                except RaidError:
                    # Preserve the lease so a transient cleanup failure retries.
                    continue
                self._disconnected.pop(cid, None)
                notice = '%s重连等待已超时，已移出攻坚队。' % self._recovery_name(lease.entry, cid)
                for key, frozen in tuple(self._raid_sessions.items()):
                    if frozen[1] is lease.entry and frozen[2] == cid:
                        self._raid_sessions.pop(key, None)
                        recipient = (id(frozen[0]), frozen[4])
                        self._recovery_notices.pop(recipient, None)
                        self._recovery_notice_pending.pop(recipient, None)
                self._queue_recovery_notice(lease.entry, notice)
                self._membership.pop(cid, None)
                lease.entry.records.pop(cid, None)
                if not lease.entry.domain.members:
                    self.raids.pop(lease.entry.domain.raid_key, None)
                if recovery is not None:recovery.departed(lease.entry,cid)

    @staticmethod
    def _recovery_name(entry, cid):
        record = entry.records.get(cid)
        raw = bytes(getattr(record, 'name_bytes', b''))
        try:return raw.decode('utf-8')
        except UnicodeDecodeError:return raw.decode('cp936', 'replace')

    def _notice(self, text):
        from game.native_chat import dstring
        return self._packet(0, 0x41, b'\0\0'+dstring(b'')+b'\0'+dstring(text.encode('utf-8')))

    def _queue_recovery_notice(self, entry, text):
        for frozen in self._raid_sessions.values():
            if frozen[1] is entry:
                key = (id(frozen[0]), frozen[4])
                rows = self._recovery_notices.setdefault(key, [])
                if len(rows) < 16:rows.append((frozen, text.encode('utf-8')))

    def recovery_notice_packets(self, session):
        """Build only on the authenticated recipient's account thread."""
        with self._lock:
            key = (id(session), getattr(session, '_raid_selected_epoch', None))
            pending = self._recovery_notice_pending.get(key)
            rows = self._recovery_notices.get(key)
            if not rows and pending is None:return []
            cid, account, _record = self._actor(session)
            if pending is not None:
                frozen = pending[0][0][0]
                self.require_session(frozen[1], session, cid, account)
                return pending[1]
            frozen, _text = rows[0]
            self.require_session(frozen[1], session, cid, account)
            from game.native_chat import dstring
            # N0041 kind0 is the existing native system-chat presentation.
            packets = [self._packet(0, 0x41, b'\0\0'+dstring(b'')+b'\0'+dstring(text)) for _owner, text in rows]
            self._recovery_notice_pending[key] = (tuple(rows), packets, set())
            return packets

    def recovery_notice_sent(self, session, packet):
        with self._lock:
            key = (id(session), getattr(session, '_raid_selected_epoch', None))
            pending = self._recovery_notice_pending.get(key)
            if pending is None:return
            rows, packets, sent = pending
            try:
                cid, account, _record = self._actor(session)
                self.require_session(rows[0][0][1], session, cid, account)
            except RaidError:return
            for index, value in enumerate(packets):
                if value is packet:sent.add(index);break
            if len(sent) == len(packets):
                queued = self._recovery_notices.get(key, [])
                if tuple(queued[:len(rows)]) == rows:del queued[:len(rows)]
                if not queued:self._recovery_notices.pop(key, None)
                self._recovery_notice_pending.pop(key, None)

    def _live_selected_member(self, entry, cid):
        for frozen in self._raid_sessions.values():
            if frozen[1] is not entry or frozen[2] != cid:
                continue
            try:
                self.require_session(entry, frozen[0], cid, frozen[3])
                directory = getattr(self.server, 'native_game_connections', None)
                if directory is not None:
                    connection = directory._connections.get(id(frozen[0]))
                    if (connection is None or connection.session is not frozen[0]
                            or connection.client.fileno() < 0):continue
                return True
            except (RaidError, OSError, AttributeError):
                continue
        return False

    def _reconcile_closed_sessions(self):
        """Audit the actual Game socket, not a retained Python session object."""
        directory = getattr(self.server, 'native_game_connections', None)
        if directory is None:return
        from .local_bindings import detach_session
        with self._lock:
            frozen_sessions = tuple(self._raid_sessions.values())
        for frozen in frozen_sessions:
            session, entry, cid, account, epoch = frozen
            with self._lock:
                if self._raid_sessions.get(id(session)) is not frozen:continue
                connection = directory._connections.get(id(session))
                try:closed = connection is None or connection.session is not session or connection.client.fileno() < 0
                except (OSError, AttributeError):closed = True
                reason = 'tcp_close' if closed else None
                if reason is None:
                    try:self.require_session(entry, session, cid, account)
                    except RaidError:reason = 'character_change'
                if reason is not None:detach_session(self, session, reason)

    def reconnect_session(self, session):
        """Trusted authenticated selected-session rebind to the raid lobby.

        Ordinary login must rebuild its own native actor/inventory. This does
        not resurrect an old dungeon scene, bridge ticket or successful send.
        """
        with self._lock:
            cid, account, record = self._actor(session)
            self._recovery_clock()
            self.expire_disconnected()
            lease = self._disconnected.get(cid)
            if (lease is None or lease.account_id != account or lease.session is session
                    or getattr(session, 'selected_channel_id', None) != lease.channel_id
                    or getattr(session, 'in_dungeon_run', False)
                    or id(session) in self._raid_sessions or self.current(cid) is not lease.entry):
                raise RaidError('no owned unexpired disconnected raid membership for this replacement session')
            entry = lease.entry
            if entry.domain.member(cid) != entry.domain._admitted.get(cid):
                raise RaidError('disconnected member differs from the frozen admission')
            selected_epoch = self.native_lobby.rebind_epoch(session)
            if selected_epoch is None:
                selected_epoch = object()
            projected = replace(entry, records={**entry.records, cid: record})
            projected._reconnecting_character_id = cid
            roster = tuple(self._member(projected if member.character_id == cid else entry, member)
                           for member in entry.domain.members)
            notification = encode_raid_modify(entry.domain.raid_key, 0,
                                              metadata=self.metadata(projected), members=roster)
            packets = [self._packet(0, N_RAID_MODIFY, notification)]
            # Encoding must succeed before the lease or live-session registry changes.
            session._raid_selected_epoch = selected_epoch
            session.raid_id = entry.domain.raid_key
            entry.records[cid] = record
            from .local_bindings import remember_session
            remember_session(self, session, entry, cid, account)
            ordinary = getattr(self, 'ordinary_party', None)
            if ordinary is not None:
                ordinary.reconnect_solo(session, entry, cid, account, selected_epoch)
            self._disconnected.pop(cid, None)
            self._queue_recovery_notice(entry, '%s已重连并返回攻坚队。' % self._recovery_name(entry, cid))
            return packets, 'authenticated disconnected raid member rebound to lobby without another admission debit'

    def current(self, character_id):
        return self.raids.get(self._membership.get(character_id))

    def _actor(self, session):
        actor = self._selected_actor(session)
        if actor[1] != int(self.server.account_id):
            raise RaidError('authenticated selected raid actor unavailable')
        return actor

    def _selected_actor(self, session):
        # Internal callers may inspect two already-authenticated sessions; do
        # not resolve the joining account from the leader thread's TLS.
        cid = getattr(session, 'selected_character_id', None)
        account = getattr(session, 'account_id', None)
        if type(cid) is not int or type(account) is not int or not 1 <= cid <= 65534 or account <= 0:
            raise RaidError('selected raid actor unavailable; native UID mapping required for large CIDs')
        # Ownership is a hot-path read. Roster enumeration repairs pets and
        # expires items for every character, so it cannot run recursively
        # under the raid lock while relay delivery leases are being checked.
        repo = self.server._repo()
        reader = getattr(repo, 'get_character_combat_projection', None)
        record = (reader(cid) if callable(reader) else
                  next((r for r in repo.list_by_account(account) if int(r.character_id) == cid), None))
        if (record is None or int(record.character_id) != cid
                or callable(reader) and int(record.account_id) != account):
            raise RaidError('raid actor does not belong to this account')
        return cid, account, record

    def require_session(self, entry, session, cid, account):
        frozen = self._raid_sessions.get(id(session))
        if (frozen is None or frozen[0] is not session or frozen[1] is not entry
                or frozen[2:4] != (cid, account)
                or frozen[4] is not getattr(session, '_raid_selected_epoch', None)
                or (getattr(session, 'selected_character_id', None), getattr(session, 'account_id', None)) != (cid, account)
                or getattr(session, 'selected_channel_id', None) != {0: 23, 1: 32}[entry.raid_type]
                or self.current(cid) is not entry
                or entry.domain.member(cid).account_id != str(account)):
            raise RaidError('stale or foreign selected raid session')
        return frozen[4]

    def join_session(self, leader_session, joining_session, *, group=0):
        """Trusted business mutation, using live authenticated session objects.

        NativeJoin may call this only after its frozen request, actor visibility
        and exact notice-send proof pass. This method emits no ACK or invented
        actor notification; complete production profiles remain mandatory.
        """
        with self._lock:
            leader, aid, _record = self._selected_actor(leader_session)
            entry = self.current(leader)
            if entry is None:
                raise RaidError('joining requires an existing owned leader')
            self.require_session(entry, leader_session, leader, aid)
            cid, account, record = self._selected_actor(joining_session)
            channel = {0: 23, 1: 32}[entry.raid_type]
            if (leader_session.selected_channel_id != channel
                    or joining_session.selected_channel_id != channel
                    or getattr(leader_session, 'in_dungeon_run', False)
                    or getattr(joining_session, 'in_dungeon_run', False)
                    or self.current(cid) is not None
                    or type(group) is not int or not 0 <= group <= PREPARATION_GROUP_MAX):
                raise RaidError('join requires unassigned selected actor, owned channel and preparation group')
            member = Member(cid, str(account), group)
            projected = replace(entry, records={**entry.records, cid: record})
            self._member(projected, member)  # Resolve display prerequisites before mutation.
            entry.domain.join(leader, member, commit_admission=(
                lambda admitted: self.battle_adapter.admit_during_break(entry, admitted)))
            entry.records[cid] = record
            self.forget_waiting(joining_session, viewer=False)
            self._membership[cid] = entry.domain.raid_key
            joining_session.raid_id = entry.domain.raid_key
            from .local_bindings import remember_session
            remember_session(self, joining_session, entry, cid, account)
            return entry

    def queue_inventory_refresh(self, character_id, account_id, list_types):
        """Keep an owner intent independently of raid membership or a TCP socket."""
        list_types=frozenset(list_types)
        if not list_types:return
        with self._lock:
            owner=(int(character_id),int(account_id))
            previous=self._inventory_refresh_owners.get(owner,(frozenset(),None))
            self._inventory_refresh_owners[owner]=(previous[0]|list_types,object())

    def _inventory_audience_current(self,frozen):
        session,entry,cid,aid,epoch=frozen
        return (getattr(session,'_raid_selected_epoch',None) is epoch
                and self._selected_actor(session)[:2]==(cid,aid))

    def inventory_refresh_for(self, session):
        with self._lock:
            try:cid,aid,_record=self._selected_actor(session)
            except RaidError:
                self.forget_inventory_refresh(session);return None
            epoch=getattr(session,'_raid_selected_epoch',None)
            if epoch is None:session._raid_selected_epoch=epoch=object()
            key=(id(session),epoch)
            pending=self._inventory_refresh.get(key)
            if pending is not None and not self._inventory_audience_current(pending[0]):
                self.forget_inventory_refresh(session);pending=None
            intent=self._inventory_refresh_owners.get((cid,aid))
            if intent is None:return pending
            frozen=(session,None,cid,aid,epoch)
            if pending is None or pending[2] is not intent[1]:
                pending=(frozen,intent[0],intent[1])
                self._inventory_refresh[key]=pending
            return pending

    def inventory_refresh_packets(self, session, encode):
        """Retain one exact encoded batch until every real send succeeds."""
        with self._lock:
            pending = self.inventory_refresh_for(session)
            if pending is None:
                return None
            key = (id(session), pending[0][4])
            delivery = self._inventory_refresh_batches.get(key)
            if delivery is None:
                packets = list(encode(pending[1]))
                if not packets:
                    raise RaidError('inventory refresh encoded no packets')
                delivery = (pending, packets, set())
                self._inventory_refresh_batches[key] = delivery
            return delivery[1]

    def inventory_refresh_packet_sent(self, session, packet):
        """Acknowledge only exact packet objects from the frozen audience."""
        with self._lock:
            if self.inventory_refresh_for(session) is None:
                return
            key = (id(session), getattr(session, '_raid_selected_epoch', None))
            delivery = self._inventory_refresh_batches.get(key)
            if delivery is None:
                return
            intent, packets, sent = delivery
            for index, value in enumerate(packets):
                if value is packet and index not in sent:
                    sent.add(index)
                    break
            if len(sent) == len(packets):
                self._inventory_refresh_batches.pop(key, None)
                # A newer merged intent still needs its own inventory snapshot.
                if self._inventory_refresh.get(key) is intent:
                    self._inventory_refresh.pop(key, None)
                    owner=(intent[0][2],intent[0][3])
                    latest=self._inventory_refresh_owners.get(owner)
                    if latest is not None and latest[1] is intent[2]:
                        self._inventory_refresh_owners.pop(owner,None)

    def forget_inventory_refresh(self, session):
        with self._lock:
            for mapping in (self._inventory_refresh, self._inventory_refresh_batches):
                for key in tuple(mapping):
                    if key[0] == id(session):
                        mapping.pop(key, None)

    @staticmethod
    def _waiting_owner(session):
        return (session.selected_channel_id, session.selected_character_id,
                session.account_id, getattr(session, '_raid_selected_epoch', None))

    def forget_waiting(self, session, *, viewer=True):
        self._waiting.pop(id(session), None)
        if viewer:
            session._raid_waiting_view = None

    def waiting_packets(self, session, *, query=False):
        import struct
        from time import monotonic
        owner = self._waiting_owner(session)
        view = getattr(session, '_raid_waiting_view', None)
        if view is None or view['owner'] != owner:
            if not query:
                return []
            view = session._raid_waiting_view = dict(owner=owner, shown={}, pending=deque(), query=False, poll=0)
        if view['pending']:
            view['query'] |= query
            return [item[0] for item in view['pending']]
        query = query or view['query']
        view['query'] = False
        if not query and monotonic() < view['poll']:
            return []
        view['poll'] = monotonic() + 1.0
        rows = {}
        for key, (actor, registered, family) in tuple(self._waiting.items()):
            if (self._waiting_owner(actor) != registered or self.current(registered[1]) is not None
                    or getattr(actor, 'in_dungeon_run', False)):
                self._waiting.pop(key, None)
            elif registered[0] == owner[0]:
                rows[registered[1]] = family
        rows = dict(sorted(rows.items()))
        shown = view['shown']
        pending = view['pending']
        def emit(opcode, body, cid, value):
            pending.append((self._packet(0, opcode, body), cid, value))
        for cid in sorted(set(shown) - set(rows)):
            emit(0x0252, struct.pack('<BH', 1, cid), cid, None)
        if query:
            # N0251 merges rows. The incremental handler also invalidates the native view.
            emit(N_RAID_WAITING_LIST, struct.pack('<H', len(rows)) +
                 b''.join(struct.pack('<HB', cid, family) for cid, family in rows.items()), None, rows)
            if rows:
                cid = next(iter(rows))
                emit(0x0252, struct.pack('<BHB', 0, cid, rows[cid]), cid, rows[cid])
        else:
            for cid, family in rows.items():
                if cid not in shown:
                    emit(0x0252, struct.pack('<BHB', 0, cid, family), cid, family)
        return [item[0] for item in pending]

    def search_page(self, expected, request):
        # Filter bytes have no proved business labels. For the local
        # fixture, expose only this channel's owned directory/name.
        entries = [entry for _, entry in sorted(self.raids.items())
                   if entry.raid_type == expected and request.search_bytes in entry.name_bytes]
        total = len(entries)
        cursor = min(request.cursor, total)
        # Native page caller multiplies its page index by 33.
        visible = [(self.metadata(entry), len(entry.domain.members))
                   for entry in entries[cursor:cursor + 33]]
        return encode_search_result(total, cursor, visible)

    def view_owner(self, session, entry=None):
        return (session.selected_character_id, session.account_id, session.selected_channel_id,
                getattr(session, '_raid_selected_epoch', None), getattr(session, 'town_scene_generation', 0),
                entry.domain.raid_key if entry is not None else None)

    def view_packet(self, session, name, owner, opcode, body, *, force=False):
        views = getattr(session, '_raid_view_delivery', None)
        if views is None:
            views = session._raid_view_delivery = {}
        last = views.get(name)
        if not force and last and last[:2] == (owner, body):
            return last[2]
        packet = self._packet(0, opcode, body)
        views[name] = (owner, body, packet)
        return packet

    def view_sent(self, session, packet):
        with self._lock:
            wait = getattr(session, '_raid_waiting_view', None)
            if wait and wait['pending'] and wait['pending'][0][0] is packet:
                _packet, cid, value = wait['pending'].popleft()
                if cid is None:
                    wait['shown'] = value
                elif value is None:
                    wait['shown'].pop(cid, None)
                else:
                    wait['shown'][cid] = value
                return
            for name, (owner, body, pending) in getattr(session, '_raid_view_delivery', {}).items():
                if pending is packet:
                    session._raid_view_delivery[name] = (owner, body, None)
                    break

    def refresh_views(self, session):
        from time import monotonic
        with self._lock:
            channel = getattr(session, 'selected_channel_id', None)
            family = {23: 0, 32: 1}.get(channel)
            search = getattr(session, '_raid_public_search', None)
            entry = self.current(getattr(session, 'selected_character_id', None))
            if family is None or getattr(session, 'in_dungeon_run', False):
                session._raid_public_search = None
                return []
            packets = self.waiting_packets(session) if getattr(session, '_raid_waiting_view', None) else []
            if search:
                owner = self.view_owner(session)
                if owner != search[0] or entry is not None:
                    session._raid_public_search = None
                elif monotonic() >= getattr(session, '_raid_next_public_poll', 0):
                    self._actor(session)
                    page = self.search_page(family, search[1])
                    packet = self.view_packet(session, 'public', owner, N_RAID_SEARCH_RESULT, page)
                    session._raid_next_public_poll = monotonic() + 1.0
                    if packet is not None:
                        packets.append(packet)
            if (entry is not None and entry.raid_type == family and self.battle_adapter is not None
                    and entry.domain.battle is None and session.account_id == self.server.account_id):
                self.require_session(entry, session, session.selected_character_id, session.account_id)
                body = self.battle_adapter.material_snapshot(entry)
                packet = self.view_packet(session, 'material', self.view_owner(session, entry),
                                          N_RAID_ENTRY_COST_INFO, body)
                if packet is not None:
                    packets.append(packet)
            return packets

    def lobby_refresh_packets(self, session):
        """Project actual leader/roster changes separately to every recipient.

        Polling the server snapshot also covers disconnect expiry and leader
        transfer; delivery does not depend on which account issued a command.
        Keep one exact batch until all its sends succeed, then compare the
        latest snapshot again. Battle state/symbols have their own projector.
        """
        with self._lock:
            cid, account, _record = self._actor(session)
            entry = self.current(cid)
            if entry is None:
                self.forget_lobby_refresh(session)
                return None
            epoch = self.require_session(entry, session, cid, account)
            key = (id(session), epoch)
            pending = self._lobby_pending.get(key)
            if pending is not None:
                return pending[2]
            roster = encode_raid_modify(entry.domain.raid_key, 3, members=self.members(entry))
            identity = (entry.domain.leader_id, entry.name_bytes, roster)
            if self._lobby_projections.get(key) == identity:
                return None
            metadata = encode_raid_modify(entry.domain.raid_key, 2, metadata=self.metadata(entry))
            # Native op3 can create a missing record. Op2 then fills its full
            # metadata; reversing these loses metadata for a fresh recipient.
            packets = [self._packet(0, N_RAID_MODIFY, body) for body in (roster, metadata)]
            self._lobby_pending[key] = (self._raid_sessions[id(session)], identity, packets, set())
            return packets

    def lobby_refresh_packet_sent(self, session, packet):
        with self._lock:
            self.squad_invites.note_send(session, packet)
            if packet is None:
                return
            key = (id(session), getattr(session, '_raid_selected_epoch', None))
            pending = self._lobby_pending.get(key)
            if pending is None:
                return
            frozen, identity, packets, sent = pending
            try:
                self.require_session(frozen[1], session, frozen[2], frozen[3])
            except RaidError:
                self.forget_lobby_refresh(session)
                return
            self._actor(session)  # The actual recipient's account TLS owns this send.
            for index, value in enumerate(packets):
                if value is packet and index not in sent:
                    sent.add(index)
                    break
            if len(sent) == len(packets):
                entry = frozen[1]
                current = (entry.domain.leader_id, entry.name_bytes, encode_raid_modify(
                    entry.domain.raid_key, 3, members=self.members(entry)))
                self._lobby_projections[key] = identity
                self._lobby_pending.pop(key, None)
                if identity == current:
                    self.native_join.member_lobby_delivered(session, entry)

    def forget_lobby_refresh(self, session):
        with self._lock:
            for mapping in (self._lobby_projections, self._lobby_pending,
                            self._recovery_notices, self._recovery_notice_pending):
                for key in tuple(mapping):
                    if key[0] == id(session):
                        mapping.pop(key, None)

    def _member(self, entry, member, *, group_override=None):
        record = entry.records[member.character_id]
        _, base = get_userinfo_job_base_for_job(int(record.job))
        group = member.group
        if member.character_id == getattr(entry, '_reconnecting_character_id', None):
            group = 0
        elif group_override is not None:
            group = group_override
        elif hasattr(self, 'ordinary_party') and self.ordinary_party.uses(entry):
            group = self.ordinary_party.display_group(entry, member.character_id, group)
        elif hasattr(self, 'native_solo_party'):
            group = self.native_solo_party.display_group(entry, member.character_id, group)
        value = lobby_member(member_id=member.character_id, name_bytes=bytes(record.name_bytes),
                             base_job=int(base), packed_grow=int(record.grow_type) & 0x7f,
                             party_number=int(group), level=int(record.level))
        battle = entry.domain.battle
        clears = 0 if battle is None else dict(battle.member_clears).get(member.character_id, 0)
        lease = self._disconnected.get(member.character_id)
        if (lease is not None and lease.entry.domain is entry.domain
                and member.character_id != getattr(entry, '_reconnecting_character_id', None)
                and member.account_id == str(lease.account_id)):
            seconds = self.battle_adapter._configs[entry.raid_type].disconnected_member_wait_seconds
            # 01467ECD: ID0 selects the offline display; +30 is the Unix
            # disconnect start, not a duration. Domain membership keeps its
            # real character ID, account and group for authenticated recovery.
            started = max(0, int(lease.expires_at - seconds))
            value = replace(value, member_id=0, field30=started)
        # Native 0119E570 reads tail u8 into member+38. 01468200
        # renders that member+38 against the real phase minimum (string7AFA).
        return replace(value, field38=min(255, max(0, int(clears))))

    def metadata(self, entry):
        if self.battle_adapter is not None and entry.domain.battle is not None:
            return self.battle_adapter.metadata(entry)
        leader = entry.domain.member(entry.domain.leader_id)
        return lobby_metadata(raid_id=entry.domain.raid_key, name_bytes=entry.name_bytes,
                              raid_type=entry.raid_type, leader=self._member(entry, leader))

    def members(self, entry):
        return tuple(self._member(entry, member) for member in entry.domain.members)

    def _start_roster(self, entry):
        return tuple((m, tuple(row[4] for row in self._raid_sessions.values()
                               if row[1] is entry and row[2] == m.character_id)) for m in entry.domain.members)

    def retry_start(self, session):
        from time import monotonic
        with self._lock:
            wait = getattr(session, '_raid_start_wait', None)
            if wait is None:
                return []
            entry, epoch, roster, deadline = wait
            if (self.current(getattr(session, 'selected_character_id', None)) is not entry
                    or getattr(session, '_raid_selected_epoch', None) is not epoch
                    or entry.domain.battle is not None):
                session._raid_start_wait = None
                return []
            if (monotonic() >= deadline or entry.domain.leader_id != session.selected_character_id
                    or not getattr(session, 'enter_seria_settled', False)
                    or getattr(session, 'in_dungeon_select', False) or getattr(session, 'in_dungeon_run', False)
                    or self._start_roster(entry) != roster):
                session._raid_start_wait = None
                return [self._packet(1, C_START_RAID, b'\0\x13'),
                        self._notice('队伍连接或编成发生变化，请确认队员状态后重新开战。')]
            from .router import route_raid_packet
            packets, _note = route_raid_packet(self.server, C_START_RAID, b'', session)
            if packets:
                session._raid_start_wait = None
            return packets

    def _packet(self, flag, opcode, body):
        return self.server._codec_response(flag, opcode, body)

    def handle(self, opcode, body, session):
        with self._lock:
            cid, account, record = self._actor(session)
            self.expire_disconnected()
            expected = {23: 0, 32: 1}.get(getattr(session, 'selected_channel_id', None))
            if opcode == C_RAID_SEARCH:
                if expected is None or getattr(session, 'in_dungeon_run', False):
                    raise RaidError('raid search requires the selected raid channel and town')
                request = parse_search_request(body)
                if request.action == 1:
                    session._raid_public_search = None
                    return self.waiting_packets(session, query=True), 'raid waiting list from current channel'
                owner = self.view_owner(session)
                session._raid_public_search = (owner, request)
                extra = self.search_page(expected, request)
                packet = self.view_packet(session, 'public', owner, N_RAID_SEARCH_RESULT, extra, force=True)
                return [packet], 'raid search from local channel-owned directory'
            if opcode == C_JOIN_RAID_VIA_PARTY:
                return self.native_join.request(session, body)
            if opcode == C_JOIN_RAID_APPROVAL:
                return self.native_join.approve(session, body)
            if opcode == C_RAID_MEMBER_LOOKUP:
                request = parse_member_raid_lookup_request(body)
                if request.namespace_u8 != LOCAL_NATIVE_NAMESPACE:
                    raise RaidError('member lookup requires the selected native actor namespace')
                target = self.native_lobby.lookup(session, request.target_key_u16, target_town=False)
                entry = self.current(target.character_id)
                if entry is None or entry.raid_type != expected:
                    raise RaidError('owned native actor is not a member of this channel raid')
                self.require_session(entry, target.session, target.character_id, target.account_id)
                extra = member_raid_lookup_success(entry.domain.raid_key, entry.domain.leader_id,
                                                   len(entry.domain.members))
                return [self._packet(1, opcode, b'\x01' + extra)], 'owned native member lookup: actual raid key, leader UID and roster count; no join'
            if opcode == C_JOIN_RAID_OTHER_NAMESPACE:
                parse_member_raid_lookup_request(body)
                raise RaidError('native other-namespace join awaits its authenticated handoff contract')
            if opcode == C_SET_RAID_WAITING:
                _action, family = parse_waiting_request(body)
                if (expected != family or self.current(cid) is not None or getattr(session, 'in_dungeon_run', False)
                        or not getattr(session, 'enter_seria_settled', False)):
                    raise RaidError('waiting registration requires an unassigned town actor in this raid channel')
                actor = self.native_lobby.lookup(session, cid, target_town=True)
                if actor.session is not session:
                    raise RaidError('waiting UID no longer belongs to this selected actor')
                owner = self._waiting_owner(session)
                already = self._waiting.get(id(session))
                if already and already[1] == owner:
                    return [self._packet(1, opcode, bytes((0, 0x12)))], 'waiting registration already active'
                self._waiting[id(session)] = (session, owner, family)
                # The registration button closes its window. Keep this owner subscribed
                # so its native model also receives subsequent membership removal.
                packets = self.waiting_packets(session, query=True)
                return [self._packet(1, opcode, bytes((1,)))] + packets, 'waiting registration accepted'
            if opcode == C_RAID_ACTION_294:
                parse_action294_request(body)
                raise RaidError('raid action294 business mutation has not been verified')
            if opcode == C_CREATE_RAID:
                raid_type, name = parse_create_request(body)
                if expected != raid_type or getattr(session, 'in_dungeon_run', False):
                    raise RaidError('raid type does not match the selected local channel')
                if self.current(cid) is not None:
                    raise RaidError('actor is already in a raid')
                # Client N0250 and the Join sender compare the high word with
                # CHANNELINFO field8, not the leader's actor/database identity.
                namespace = LOCAL_NATIVE_NAMESPACE
                for _ in range(65535):
                    self._sequence = self._sequence % 65535 + 1
                    raid_id = (namespace << 16) | self._sequence
                    if raid_id not in self.raids:
                        break
                else:
                    raise RaidError('raid directory namespace has no free key')
                family = 'anton' if raid_type == 0 else 'luke'
                domain = Raid(raid_id, name.hex(), family, False, Member(cid, str(account), 0))
                entry = DirectoryEntry(domain, name, raid_type, {cid: record})
                # Resolve every display dependency before committing membership.
                metadata, members = self.metadata(entry), self.members(entry)
                notification = encode_raid_modify(raid_id, 0, metadata=metadata, members=members)
                packets = [self._packet(0, N_RAID_MODIFY, notification),
                           self._packet(1, opcode, b'\x01' + create_success_payload(raid_id))]
                self.forget_waiting(session, viewer=False)
                self.raids[raid_id], self._membership[cid] = entry, raid_id
                session.raid_id = raid_id
                from .local_bindings import remember_session
                remember_session(self, session, entry, cid, account)
                return packets, f'raid created family={family} channel={session.selected_channel_id} raid_id={raid_id}'
            if opcode == C_MODIFY_RAID:
                raid_type, name = parse_modify_request(body)
                entry = self.current(cid)
                if entry is None or raid_type != expected or raid_type != entry.raid_type:
                    raise RaidError('modification requires this owned raid and unchanged family')
                self.require_session(entry, session, cid, account)
                entry.domain._leader(cid)
                projected = replace(entry, name_bytes=name)
                notification = encode_raid_modify(entry.domain.raid_key, 2, metadata=self.metadata(projected))
                packets = [self._packet(0, N_RAID_MODIFY, notification), self._packet(1, opcode, b'\x01')]
                entry.domain.rename(cid, name.hex())
                entry.name_bytes = name
                return packets, 'owned raid metadata name updated by leader'
            if opcode == C_RAID_INFO:
                action, raid_id = parse_raid_info_request(body)
                entry = self.raids.get(raid_id)
                if entry is None or entry.raid_type != expected:
                    raise RaidError('requested raid is unavailable')
                metadata = self.metadata(entry) if action in (0, 2) else None
                members = self.members(entry) if action in (1, 2) else None
                extra = raid_info_success_payload(action, raid_id, metadata=metadata, members=members)
                return [self._packet(1, opcode, b'\x01' + extra)], 'raid info from server-owned directory'
            entry = self.current(cid)
            if entry is None:
                raise RaidError('actor has no current raid')
            self.require_session(entry, session, cid, account)
            if opcode == C_RAID_MANAGER_WORK:
                # Popup menu 145 (委任攻坚队队长) writes three u32s at 02A0C236.
                if body[:4] == b'\x01\x00\x00\x00':
                    if len(body) != 12 or body[8:] != bytes(4):
                        raise RaidError('invalid raid leader transfer request')
                    successor = int.from_bytes(body[4:8], 'little')
                    entry.domain._leader(cid)
                    if expected != entry.raid_type or getattr(session, 'in_dungeon_run', False):
                        raise RaidError('raid leader transfer requires the owned channel and town')
                    if not self._live_selected_member(entry, successor):
                        raise RaidError('successor must be an online member of this raid')
                    member = self._member(entry, entry.domain.member(successor))
                    metadata = replace(self.metadata(entry), leader=member)
                    notification = encode_raid_modify(entry.domain.raid_key, 2, metadata=metadata)
                    packets = [self._packet(0, N_RAID_MODIFY, notification), self._packet(1, opcode, b'\x01')]
                    entry.domain.transfer_leadership(cid, successor)
                    return packets, f'raid leader transferred to {successor}'
                if (cid == entry.domain.leader_id and expected == entry.raid_type
                        and not getattr(session, 'in_dungeon_run', False)
                        and len(body) == 12 and body[:4] == bytes(4)
                        and not 1 <= int.from_bytes(body[4:8], 'little') <= 65535):
                    roster = encode_raid_modify(entry.domain.raid_key, 3, members=self.members(entry))
                    return [self._packet(0, N_RAID_MODIFY, roster),
                            self._packet(1, opcode, b'\0\x13'),
                            self._notice('成员列表已刷新，请重新选择在线队员后编队。')], 'invalid squad target; current roster refreshed'
                member_id, group = parse_group_request(body)
                if expected != entry.raid_type or getattr(session, 'in_dungeon_run', False):
                    raise RaidError('raid group change requires the owned channel and town')
                if cid != entry.domain.leader_id or not 0 <= group <= PREPARATION_GROUP_MAX:
                    raise RaidError('owned preparation leader and supported group required')
                entry.domain.member(member_id)
                ordinary = getattr(self, 'ordinary_party', None)
                battle = entry.domain.battle
                if battle is not None:
                    if (not (battle.status == 'active' or entry.domain.preparation_open())
                            or ordinary is None or not ordinary.uses(entry)):
                        raise RaidError('raid group change requires preparation or an active battle')
                    ack = self._packet(1, opcode, b'\x01')
                    packets = ordinary.regroup(entry, session, member_id, group)
                    return packets + [ack], (
                        'owned raid squad updated by leader target=%s group=%s' % (member_id, group))
                if ordinary is not None and group > 0 and sum(
                        member.character_id != member_id and
                        ordinary.display_group(entry, member.character_id, member.group) == group
                        for member in entry.domain.members) >= 4:
                    raise RaidError('native four-member group is full')
                projected = tuple(self._member(entry, member, group_override=group)
                                  if member.character_id == member_id else self._member(entry, member)
                                  for member in entry.domain.members)
                notification = encode_raid_modify(entry.domain.raid_key, 3, members=projected)
                packets = [self._packet(0, N_RAID_MODIFY, notification), self._packet(1, opcode, b'\x01')]
                entry.domain.move_member(cid, member_id, group)
                if ordinary is not None:
                    ordinary.manager_assigned(entry, member_id, group)
                return packets, 'raid preparation group updated by owned leader target=%s group=%s' % (member_id, group)
            if opcode == C_LEAVE_RAID:
                if parse_leave_request(body) != cid:
                    raise RaidError('leave member id does not match the authenticated actor')
                if getattr(session, 'in_dungeon_run', False):
                    raise RaidError('leave the owned dungeon run before leaving the raid')
                remaining = tuple(m for m in entry.domain.members if m.character_id != cid)
                if not remaining:
                    notification = encode_raid_modify(entry.domain.raid_key, 1)
                else:
                    notification = encode_raid_modify(entry.domain.raid_key, 3,
                        members=tuple(self._member(entry, m) for m in remaining))
                packets = [self._packet(0, N_RAID_MODIFY, notification),
                           self._packet(1, opcode, b'\x01' + leave_success_payload(1))]
                if remaining and cid == entry.domain.leader_id:
                    new_leader = min(remaining, key=lambda member: member.character_id)
                    metadata = replace(self.metadata(entry), leader=self._member(entry, new_leader))
                    packets.insert(0, self._packet(0, N_RAID_MODIFY,
                        encode_raid_modify(entry.domain.raid_key, 2, metadata=metadata)))
                ordinary = getattr(self, 'ordinary_party', None)
                if ordinary is not None and ordinary.uses(entry):
                    packets[:0] = ordinary.leave_packets(session)
                elif hasattr(self, 'native_solo_party'):
                    packets[:0] = self.native_solo_party.leave_packets(session)
                if self.battle_adapter is not None:
                    self.exit_refunds.depart(entry, cid)
                entry.domain.leave(cid)
                recovery=getattr(self.battle_adapter,'settlement_recovery',None)
                if recovery is not None:recovery.departed(entry,cid)
                self._membership.pop(cid)
                entry.records.pop(cid, None)
                if not remaining:
                    self.raids.pop(entry.domain.raid_key)
                if self.battle_adapter is not None:
                    self.battle_adapter.forget_session(session)
                session.raid_id = None
                self._raid_sessions.pop(id(session), None)
                self.forget_inventory_refresh(session)
                self.forget_lobby_refresh(session)
                # Leaving a raid does not change the authenticated selected
                # town actor. Invalidate raid approvals, retain its real body.
                self.native_join.forget_session(session)
                return packets, 'raid membership left'
            if opcode in (C_START_RAID, C_RAID_ENTRY_COST_INFO, C_SELECT_RAID_REWARD_CARD) and self.battle_adapter is None:
                from .local_bindings import local_raid_hooks
                local_raid_hooks(self.server)
            if opcode == C_RAID_ENTRY_COST_INFO and self.battle_adapter is not None:
                return self.battle_adapter.entry_cost_info(entry, body, session)
            if opcode == C_SELECT_RAID_REWARD_CARD and self.battle_adapter is not None:
                return self.battle_adapter.select_reward(entry, body, session)
            if opcode == C_START_RAID and self.battle_adapter is not None:
                if entry.domain.battle is None and entry.native_actor_preparation and len(entry.domain.members) > 1:
                    parse_start_request(body)
                    if cid != entry.domain.leader_id:
                        raise RaidError('current leader required to prepare native raid peers')
                    self.native_join.require_prepared_peers(entry)
                    try:
                        self.native_party.require_prepared(entry)
                    except RaidError as exc:
                        ordinary = getattr(self, 'ordinary_party', None)
                        failure = getattr(ordinary, '_preparation_failure', None)
                        if failure is not None and failure[0] is entry:
                            cause = failure[2]
                            if cause == 'current bilateral native relay proof unavailable':
                                from time import monotonic
                                if getattr(session, '_raid_start_wait', None) is None:
                                    session._raid_start_wait = (entry, session._raid_selected_epoch,
                                        self._start_roster(entry), monotonic() + 10)
                                return [], 'raid start waiting for current bilateral preparation'
                            if 'native character mismatch' in cause:
                                import re
                                match = re.search(r'cid=(\d+)', cause)
                                target = int(match[1]) if match else failure[1]
                                name = self._recovery_name(entry, target)
                                return [self._packet(1, opcode, b'\0\x13'),
                                    self._notice('%s的组队连接仍是旧角色，请重新选角后再开战。' % name)], 'raid start blocked by stale native character: '+cause
                            raise RaidError('%s; squad_representative=%s cause=%s' % (exc, failure[1], failure[2])) from exc
                        raise
                result = self.battle_adapter.start(entry, body, session)
                session._raid_start_wait = None
                return result
            raise RaidError('raid battle adapter has not passed its dependency gate')
