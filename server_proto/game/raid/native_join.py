"""Native mode0A request/notice/approval correlated to owned live selections.

The legacy profile resolver accepts complete server profiles. The request-aware
production resolver uses server wallet/fatigue/recovery data and explicitly
relays the client's exorcism display hint, outside every authorization decision.
"""
from dataclasses import dataclass
from math import isfinite
from time import monotonic

from .ingress_protocol import (N_JOIN_REQUEST, C_JOIN_RAID_VIA_PARTY, C_JOIN_RAID_APPROVAL,
    encode_join_request_notice, join_approval_ack_extra, parse_join_request, parse_join_approval_request)
from .runtime import RaidError

PENDING_JOIN_SECONDS = 120  # Server timeout policy, not a recovered native enum.


@dataclass(frozen=True)
class JoinProfile:
    revive_coins: int
    fatigue: int
    weakness_percent: int
    exorcism: int

    def notice(self, actor_key):
        if type(self.weakness_percent) is not int or not 0 <= self.weakness_percent <= 100:
            raise RaidError('authoritative weakness percentage unavailable')
        return encode_join_request_notice(actor_key=actor_key, revive_coins=self.revive_coins,
            fatigue=self.fatigue, weakness_percent=self.weakness_percent, exorcism=self.exorcism)


@dataclass(eq=False)
class _Request:
    applicant: object
    leader: object
    entry: object
    expires: float
    notice_body: bytes
    request_frame: bytes
    packet: object = None
    sent: bool = False


@dataclass(frozen=True)
class _Approved:
    request: _Request
    packet: object
    approval_frame: bytes
    group_peers: tuple


class NativeJoin:
    def __init__(self, service, profile_resolver=None, clock=None):
        self.service = service
        self.profile_resolver = profile_resolver
        self.request_profile_resolver = None
        self.clock = clock or monotonic
        self._requests, self._approved = {}, {}

    def _now(self):
        now = self.clock()
        if type(now) not in (float, int) or not isfinite(now) or now <= 0:
            raise RaidError('trusted native join clock unavailable')
        return now

    def _actor_current(self, actor):
        return self.service.native_lobby.lookup(actor.session, actor.character_id) is actor

    def _valid(self, pending, *, joined=False):
        a, leader, entry = pending.applicant, pending.leader, pending.entry
        if (not self._actor_current(a) or not self._actor_current(leader)
                or (not joined and self._now() >= pending.expires)
                or a.channel_id != leader.channel_id
                or self.service.current(leader.character_id) is not entry
                or entry.domain.leader_id != leader.character_id
                or (not joined and not entry.domain.preparation_open()) or entry.domain.single
                or leader.channel_id != {0: 23, 1: 32}[entry.raid_type]):
            raise RaidError('native request selection, preparation raid or approval lease expired')
        self.service.require_session(entry, leader.session, leader.character_id, leader.account_id)
        if joined:
            if self.service.current(a.character_id) is not entry:
                raise RaidError('approval replay no longer owns its committed membership')
            self.service.require_session(entry, a.session, a.character_id, a.account_id)
        elif self.service.current(a.character_id) is not None:
            raise RaidError('native applicant is already assigned to a raid')
        return pending

    def _group(self, entry):
        adapter = self.service.battle_adapter
        config = getattr(adapter, '_configs', {}).get(entry.raid_type)
        maximum = getattr(config, 'member_max', None)
        if type(maximum) is not int or not 1 <= maximum <= 20:
            raise RaidError('actual PVF native raid member capacity unavailable')
        if len(entry.domain.members) >= maximum:
            raise RaidError('native raid has no free member slot')
        # Application approval admits a member, not a prepared combat squad.
        # The leader's explicit manager command assigns a numbered group.
        return 0

    def request(self, session, body):
        with self.service._lock:
            request = parse_join_request(body)
            directory = self.service.native_lobby
            a = directory.lookup(session, session.selected_character_id)
            leader = directory.lookup(session, request.target_key_u16)
            entry = self.service.current(leader.character_id)
            if a is leader or entry is None:
                raise RaidError('native request requires another owned preparation leader')
            key = a.character_id
            # A repeated application is also an opportunity for the applicant's
            # own account thread to refresh the trusted profile. Missing/invalid
            # input must revoke an earlier notice, never leave it approvable.
            try:
                if self.request_profile_resolver is not None:
                    profile = self.request_profile_resolver(session, request)
                else:
                    profile = self.profile_resolver(session) if self.profile_resolver is not None else None
                if type(profile) is not JoinProfile:
                    raise RaidError('complete authoritative native join profile producer unavailable')
                notice = profile.notice(a.character_id)
            except Exception as exc:
                self._requests.pop(key, None)
                if isinstance(exc, (RaidError, ValueError)):
                    raise
                raise RaidError('authoritative native join profile refresh failed') from exc
            old = self._requests.get(key)
            if old is not None:
                try:
                    self._valid(old)
                except RaidError:
                    self._requests.pop(key, None)
                else:
                    same_target = old.leader is leader and old.entry is entry
                    if old.applicant is not a or (not same_target and not old.sent):
                        raise RaidError('another frozen native request is still pending')
                    if same_target and old.notice_body == notice and not old.sent:
                        return [self.service._packet(1, C_JOIN_RAID_VIA_PARTY, b'\x01')], 'native request remains pending'
                    # Native mode10 reject and X only dismiss the leader's
                    # local window. A new explicit application must re-notify;
                    # polling alone still never resends a delivered notice.
                    # Replace the delivery token so a late ACK for the previous
                    # packet cannot mark this fresh application as delivered.
                    # An explicit new target also supersedes a delivered
                    # notice. Retain the old request until all checks on the
                    # new target succeed; an invalid target cannot revoke it.
            # No client revive/exorcism numeric field supplies missing values.
            pending = _Request(a, leader, entry, self._now() + PENDING_JOIN_SECONDS,
                               notice, request.frame)
            self._valid(pending)
            self._group(entry)
            ack = self.service._packet(1, C_JOIN_RAID_VIA_PARTY, b'\x01')
            directory.queue_peer(leader.session, session)
            self._requests[key] = pending
            self._approved.pop((leader, key), None)
            return [ack], 'native application pending actual peer visibility and notice delivery'

    def pending_packets(self, session):
        """Encode notices in the approver's thread after its peer body arrived."""
        with self.service._lock:
            packets = []
            for key, pending in tuple(self._requests.items()):
                if pending.leader.session is not session:
                    continue
                try:
                    self._valid(pending)
                    self._group(pending.entry)
                except RaidError:
                    self._requests.pop(key, None)
                    continue
                self._recipient(pending, session)
                if pending.sent or not self.service.native_lobby.visible(session, pending.applicant.session):
                    continue
                if pending.packet is None:
                    pending.packet = self.service._packet(0, N_JOIN_REQUEST, pending.notice_body)
                packets.append(pending.packet)
            return packets

    def _recipient(self, pending, session):
        cid, account, _record = self.service._actor(session)
        if (session is not pending.leader.session
                or (cid, account) != (pending.leader.character_id, pending.leader.account_id)):
            raise RaidError('native join notice requires the approver recipient account context')

    def note_send(self, session, packet):
        if packet is None:
            return
        with self.service._lock:
            for key, pending in tuple(self._requests.items()):
                if pending.leader.session is session and pending.packet is packet:
                    try:
                        self._valid(pending)
                    except RaidError:
                        self._requests.pop(key, None)
                        continue
                    self._recipient(pending, session)
                    if self.service.native_lobby.visible(session, pending.applicant.session):
                        pending.sent = True
            for key, approved in tuple(self._approved.items()):
                pending = approved.request
                if pending.leader.session is not session or approved.packet is not packet:
                    continue
                try:
                    self._valid(pending, joined=True)
                except RaidError:
                    self._approved.pop(key, None)
                    continue
                self._recipient(pending, session)
                # Approval ACK delivery is separate from applicant visibility.
                if getattr(pending.applicant.session, '_raid_native_approval_pending', None) is approved:
                    pending.applicant.session._raid_native_approval_pending = None
                # Queue only; its own account thread must encode and confirm
                # the leader's full selected body before visibility is true.
                directory = self.service.native_lobby
                directory.queue_peer(pending.applicant.session, session)
                for peer in approved.group_peers:
                    if peer is pending.leader:
                        continue
                    try:
                        if directory.lookup(peer.session, peer.character_id) is not peer:
                            continue
                        self.service.require_session(pending.entry, peer.session, peer.character_id, peer.account_id)
                        if (pending.entry.domain.member(peer.character_id).group
                                != pending.entry.domain.member(pending.applicant.character_id).group):
                            continue
                    except RaidError:
                        continue
                    directory.queue_peer(pending.applicant.session, peer.session)
                    directory.queue_peer(peer.session, pending.applicant.session)

    def approve(self, session, body):
        with self.service._lock:
            request = parse_join_approval_request(body)
            if request.actor_value_u32 != 0:
                raise RaidError('unconfirmed nonzero native approval branch')
            leader = self.service.native_lobby.lookup(session, session.selected_character_id)
            key = request.target_key_u16
            pending = self._requests.get(key)
            if pending is None:
                approved = self._approved.get((leader, key))
                if approved is None:
                    raise RaidError('native approval has no frozen owned application')
                self._valid(approved.request, joined=True)
                return [approved.packet], 'native approval replay; membership unchanged'
            self._valid(pending)
            if (pending.leader is not leader or not pending.sent
                    or not self.service.native_lobby.visible(session, pending.applicant.session)):
                raise RaidError('approval requires this approver and its exact successfully sent notice')
            group = self._group(pending.entry)
            # Freeze actual owned same-group peers before committing membership.
            # A later actor replacement must not become the recipient/subject
            # of an old approval, even when it reuses the same numeric UID.
            peers = tuple(self.service.native_lobby.lookup(session, member.character_id)
                          for member in pending.entry.domain.members if member.group == group)
            for peer in peers:
                self.service.require_session(pending.entry, peer.session, peer.character_id, peer.account_id)
            # Encode everything that can fail before changing directory/domain
            # membership. join_session resolves display prerequisites likewise.
            ack = self.service._packet(1, C_JOIN_RAID_APPROVAL,
                b'\x01' + join_approval_ack_extra(True, actor_key=pending.applicant.character_id))
            entry = self.service.join_session(session, pending.applicant.session, group=group)
            if entry is not pending.entry:
                raise RaidError('native approval joined a different raid')
            self._requests.pop(key, None)
            approved = _Approved(pending, ack, request.frame, peers)
            self._approved[(leader, key)] = approved
            entry.native_actor_preparation = True
            pending.applicant.session._raid_native_approval_pending = approved
            return [ack], 'native approval committed owned selected member; recipient lobby refresh queued normally'

    def member_lobby_delivered(self, session, entry):
        """Recover a committed join whose original approver is unavailable.

        Only RaidService's complete, exact and current roster/metadata send
        batch may call this. It confirms the applicant's existing membership;
        it does not approve a new request or skip peer-body delivery.
        """
        with self.service._lock:
            cid, account, _record = self.service._actor(session)
            approved = getattr(session, '_raid_native_approval_pending', None)
            if type(approved) is not _Approved:
                return False
            pending = approved.request
            if (pending.entry is not entry or pending.applicant.session is not session
                    or pending.applicant.character_id != cid or pending.applicant.account_id != account
                    or entry.domain.battle is not None or entry.domain.single):
                return False
            try:
                self.service.require_session(entry, session, cid, account)
                if not self._actor_current(pending.applicant):
                    return False
            except RaidError:
                return False
            try:
                self._valid(pending, joined=True)
            except RaidError:
                # Applicant is still the exact owned committed member, but
                # its old approver no longer owns the preparation leadership.
                session._raid_native_approval_pending = None
                return True
            return False

    def require_prepared_peers(self, entry):
        """Network START prerequisite; no admission or cross-account encoding.

        Native approvals require their actual ACK (or orphan recovery roster)
        delivery and every current same-group actor body. Internal callers without native joins
        do not use this gate. This does not prove PARTY_INFO/UDP readiness.
        """
        with self.service._lock:
            groups = {}
            for member in entry.domain.members:
                frozen = [value for value in self.service._raid_sessions.values()
                          if value[1] is entry and value[2] == member.character_id]
                if len(frozen) != 1:
                    raise RaidError('native preparation requires one owned selected member session')
                session, _entry, cid, account, _epoch = frozen[0]
                self.service.require_session(entry, session, cid, account)
                actor = self.service.native_lobby.lookup(session, cid)
                if getattr(session, '_raid_native_approval_pending', None) is not None:
                    raise RaidError('native approval success ACK has not been delivered')
                groups.setdefault(member.group if member.group else ('unassigned', cid), []).append(actor)
            waiting = False
            for actors in groups.values():
                for recipient in actors:
                    for subject in actors:
                        if recipient is subject:
                            continue
                        if not self.service.native_lobby.visible(recipient.session, subject.session):
                            self.service.native_lobby.queue_peer(recipient.session, subject.session)
                            waiting = True
            if waiting:
                raise RaidError('native same-group selected actor bodies are awaiting recipient delivery')

    def forget_session(self, session):
        with self.service._lock:
            session._raid_native_approval_pending = None
            for key, pending in tuple(self._requests.items()):
                if session is pending.applicant.session or session is pending.leader.session:
                    self._requests.pop(key, None)
            for key, approved in tuple(self._approved.items()):
                pending = approved.request
                if session is pending.applicant.session or session is pending.leader.session:
                    self._approved.pop(key, None)
