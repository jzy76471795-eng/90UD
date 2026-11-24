"""Mode0 invitations between current members of one raid, in town.

Raid admission remains NativeJoin mode10. This flow creates/joins an owned
ordinary squad only after the recipient receives and accepts its invitation.
"""
from dataclasses import dataclass
from collections import deque
from time import monotonic
import struct
from .runtime import RaidError

@dataclass(eq=False)
class Invitation:
    sender: object
    recipient: object
    entry: object
    party: object
    expires: float
    notice: bytes
    packet: object = None
    sent: bool = False
    application: bool = False

@dataclass(eq=False)
class Reply:
    body: bytes
    expires: float
    packet: object = None

class SquadInvites:
    def __init__(self, service):
        self.service = service
        self.clock = monotonic
        self.pending = {}
        self.replies = {}

    def directory(self):
        value = getattr(self.service, 'ordinary_party', None)
        if value is None:
            raise RaidError('owned raid squads are unavailable')
        return value

    def actor(self, session):
        return self.service.native_lobby.lookup(session, session.selected_character_id)

    def current(self, invite):
        lobby, entry = self.service.native_lobby, invite.entry
        if self.clock() >= invite.expires:
            raise RaidError('raid squad invitation expired')
        for actor in (invite.sender, invite.recipient):
            if (lobby.lookup(actor.session, actor.character_id) is not actor
                    or self.service.current(actor.character_id) is not entry):
                raise RaidError('raid squad invitation selection or membership changed')
            self.service.require_session(entry, actor.session, actor.character_id, actor.account_id)
            if entry.domain.battle is not None and entry.domain._admitted.get(actor.character_id) != entry.domain.member(actor.character_id):
                raise RaidError('post-start squad requires this raid\'s admitted members')
        if invite.sender.channel_id != invite.recipient.channel_id:
            raise RaidError('raid squad invitation channel changed')
        host = invite.recipient if invite.application else invite.sender
        joiner = invite.sender if invite.application else invite.recipient
        party = self.actual_party(host)
        if (not self.same_party(party, invite.party)
                or party is not None and (party.host_character_id != host.character_id or len(party.members) >= 4)
                or self.recipient_in_squad(joiner)):
            raise RaidError('raid squad invitation actual membership changed')
        return invite

    @staticmethod
    def same_party(first, second):
        return (first is second or first is not None and second is not None
            and first.entry is second.entry and (first.group, first.generation, first.party_id, first.host_character_id)
            == (second.group, second.generation, second.party_id, second.host_character_id))

    def actual_party(self, actor):
        party = self.directory().current(actor.session, auto=False)
        return party if party is not None and party.group > 0 else None

    def recipient_in_squad(self, actor):
        # A numbered one-member party is still alone. Only acceptance may
        # move that owner; a notice or decline retains its existing party.
        party = self.directory().current(actor.session, auto=False)
        return party is not None and len(party.members) > 1

    def prune(self):
        for actor, invite in tuple(self.pending.items()):
            try:self.current(invite)
            except RaidError:self.pending.pop(actor, None)
        for actor, replies in tuple(self.replies.items()):
            if (self.service.native_lobby._by_session.get(id(actor.session)) is not actor
                    or getattr(actor.session, '_raid_selected_epoch', None) is not actor.epoch):
                self.replies.pop(actor, None)
            else:
                retained = deque(reply for reply in replies if self.clock() < reply.expires)
                if retained:self.replies[actor] = retained
                else:self.replies.pop(actor, None)

    def request(self, session, body):
        from game.normal_party import NormalParties
        raw = NormalParties._fixed(body, 13)
        target, mode, value, aux, value2 = struct.unpack('<HBIHI', raw)
        if mode != 0:
            raise RaidError('raid squad invitation requires mode0')
        with self.service._lock:
            self.prune()
            sender = self.actor(session)
            recipient = self.service.native_lobby.lookup(session, target)
            entry = self.service.current(sender.character_id)
            if sender is recipient or entry is None or self.service.current(target) is not entry:
                raise RaidError('squad invitation requires two different members of the same raid')
            directory = self.directory()
            directory._prune()
            party = self.actual_party(sender)
            target_party = self.actual_party(recipient)
            application = (target_party is not None and
                (len(target_party.members) > 1 or party is None))
            if application:
                if (self.recipient_in_squad(sender) or target_party.host_character_id != recipient.character_id
                        or len(target_party.members) >= 4):
                    raise RaidError('join request requires an unassigned applicant and a host with a free slot')
                party = target_party
            if party is not None and (party.host_character_id != sender.character_id or len(party.members) >= 4):
                if not application:
                    raise RaidError('squad leader and a free four-member slot are required')
            if not application and self.recipient_in_squad(recipient):
                raise RaidError('invited raid member already belongs to a squad')
            directory._town(session, party, additional_sessions=(recipient.session,))
            old = self.pending.get(recipient)
            if old is not None:
                try:self.current(old)
                except RaidError:self.pending.pop(recipient, None)
                else:
                    if old.sender is sender:
                        return [self.service._packet(1, 10, b'\1')], 'raid squad invitation remains pending'
                    raise RaidError('raid member already has a pending squad invitation')
            if len(self.pending) >= 256:
                raise RaidError('raid squad invitation queue is full')
            notice = struct.pack('<HBIHHHI', sender.character_id, 0, value, aux, 0, 0, value2)
            invite = Invitation(sender, recipient, entry, party, self.clock()+30, notice, application=application)
            self.current(invite)
            self.service.native_lobby.queue_peer(recipient.session, sender.session)
            self.pending[recipient] = invite
            return [self.service._packet(1, 10, b'\1')], 'raid squad invitation queued for its owned recipient'

    def pending_packets(self, session):
        with self.service._lock:
            self.prune()
            packets = []
            for actor, invite in tuple(self.pending.items()):
                try:self.current(invite)
                except RaidError:self.pending.pop(actor, None);continue
                if actor.session is session and not invite.sent:
                    if not self.service.native_lobby.visible(session, invite.sender.session):
                        continue
                    self.service._actor(session)
                    if invite.packet is None:
                        invite.packet = self.service._packet(0, 7, invite.notice)
                    packets.append(invite.packet)
            for actor, replies in tuple(self.replies.items()):
                if actor.session is not session:
                    continue
                try:
                    if self.actor(session) is not actor:raise RaidError('expired reply actor')
                except RaidError:self.replies.pop(actor, None);continue
                self.service._actor(session)
                for reply in replies:
                    if reply.packet is None:
                        reply.packet = self.service._packet(0, 8, reply.body)
                    packets.append(reply.packet)
            return packets

    def note_send(self, session, packet):
        if packet is None:
            return
        for actor, invite in tuple(self.pending.items()):
            if actor.session is session and invite.packet is packet:
                try:self.current(invite)
                except RaidError:self.pending.pop(actor, None);continue
                self.service._actor(session)
                invite.sent = True
        for actor, replies in tuple(self.replies.items()):
            if actor.session is session and any(reply.packet is packet for reply in replies):
                self.service._actor(session)
                remaining = deque(reply for reply in replies if reply.packet is not packet)
                if remaining:self.replies[actor] = remaining
                else:self.replies.pop(actor, None)

    def owns_response(self, session):
        with self.service._lock:
            return any(actor.session is session for actor in self.pending)

    def accept(self, session, body):
        from game.normal_party import NormalParties
        sender_id, mode, reason = struct.unpack('<HBI', NormalParties._fixed(body, 7))
        with self.service._lock:
            self.prune()
            recipient = self.actor(session)
            invite = self.pending.get(recipient)
            if mode == 0 and invite is None and reason == 0:
                # A repeated accepted response may refresh an existing seat;
                # it must never create membership without a live invitation.
                sender = self.service.native_lobby.lookup(session, sender_id)
                directory = self.directory()
                party = self.actual_party(recipient)
                if (sender is recipient or party is None
                        or not self.same_party(party, self.actual_party(sender))):
                    raise RaidError('squad response has no matching delivered invitation')
                self.service._actor(session)
                return directory.join(session, struct.pack('<HBI', sender_id, 0, 0))
            if (mode != 0 or invite is None or not invite.sent
                    or invite.sender.character_id != sender_id):
                raise RaidError('squad response has no matching delivered invitation')
            self.current(invite)
            directory = self.directory()
            party = self.actual_party(invite.recipient if invite.application else invite.sender)
            if not self.same_party(party, invite.party):
                raise RaidError('invited squad or squad leader changed')
            directory._town(session, party, additional_sessions=(invite.sender.session,))
            if reason:
                if sum(len(replies) for replies in self.replies.values()) >= 256:
                    raise RaidError('raid squad reply queue is full')
                self.pending.pop(recipient, None)
                self.replies.setdefault(invite.sender, deque()).append(
                    Reply(struct.pack('<HBI', recipient.character_id, 0, reason), self.clock()+30))
                return [self.service._packet(1, 11, b'\1'+struct.pack('<HB', sender_id, 0))], 'raid squad invitation declined'
            if invite.application:
                ack = self.service._packet(1, 11, b'\1'+struct.pack('<HB', sender_id, 0))
                packets = directory.accept_application(session, invite.sender.session)
                self.pending.pop(recipient, None)
                return packets + [ack], 'raid squad join application accepted by its current host'
            with directory._transaction():
                if self.recipient_in_squad(recipient):
                    raise RaidError('recipient already joined another squad')
                if party is None:
                    old = directory.current(invite.sender.session, auto=False)
                    if old is not None:
                        if len(old.members) != 1:raise RaidError('sender already owns a squad')
                        directory._replace_members(old, ())
                    group = directory.free_group(invite.entry, creating_character_id=invite.sender.character_id)
                    party = directory._new(invite.entry, group, (invite.sender.session,), enforce_group=False)
                    directory._parties[(id(invite.entry), group)] = party
                    directory._history[id(invite.entry)] = invite.entry
                    directory._paused.pop(id(invite.sender.session), None)
                if len(party.members) >= 4:raise RaidError('raid squad is full')
                packets, note = directory.join(session, struct.pack('<HBI', sender_id, 0, 0))
            # Other invitations from this same not-yet-created squad bind to
            # its first accepted generation; later revisions keep that ID.
            for pending in self.pending.values():
                if pending.sender is invite.sender and pending.entry is invite.entry and pending.party is None:
                    pending.party = party
            self.pending.pop(recipient, None)
            return packets, 'raid squad invitation accepted; '+note
