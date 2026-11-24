"""Bounded waits for real native UDP refreshes, owned by each caller's pump."""
from dataclasses import dataclass
import struct

from game.raid.runtime import RaidError

WAIT_SECONDS = 18
POLL_SECONDS = .25
MAX_OWNER_PENDING = 16


class UdpUnavailable(RaidError):
    def __init__(self, selections, message, *, relay=False):
        super().__init__(message)
        self.selections = tuple(selections)
        self.relay = relay


@dataclass
class PendingUdp:
    owner: tuple
    opcode: int
    body: bytes
    selections: tuple
    parties: tuple
    invite: object
    expires: float
    next_poll: float
    relay: bool = False
    last_reason: str = ""


class UdpWaits:
    def __init__(self, parties):
        self.parties = parties
        self.pending = {}

    @staticmethod
    def _key(owner, opcode, body):
        # C000A/C000B identify their independent target and mode in H B.
        # A second target must never replace an earlier unanswered request.
        return owner[0], opcode, bytes(body[:3])

    def repeat(self, owner, opcode, body):
        key = self._key(owner, opcode, body)
        pending = self.pending.get(key)
        if pending is None:
            return False
        if pending.owner == owner and pending.opcode == opcode and pending.body == body:
            return True  # A duplicate never extends the deadline.
        self.pending.pop(key, None)
        # An explicit decline must still reach the original invitation.
        declining = (opcode == 11 and len(body) >= 7 and pending.invite is not None
                     and body[:3] == pending.body[:3] and struct.unpack_from('<I', body, 3)[0] != 0)
        if pending.invite is not None and not declining:
            self.parties._cancel_invite(pending.invite)
        return False

    def reserved(self, recipient, owner):
        for pending in self.pending.values():
            if pending.opcode != 10 or pending.owner == owner:
                continue
            cid = struct.unpack_from('<H', pending.body)[0]
            if any(s == recipient and s[4] == cid for s in pending.selections):
                return True
        return False

    def defer(self, owner, opcode, body, unavailable, invite=None):
        key = self._key(owner, opcode, body)
        if len(self.pending) >= 128 or sum(p.owner[0] is owner[0] for p in self.pending.values()) >= MAX_OWNER_PENDING:
            raise RaidError('ordinary UDP refresh wait queue full')
        now = self.parties.clock()
        selections = unavailable.selections
        contexts = []
        for selection in selections:
            party = self.parties._parties.get(id(selection[0].session))
            contexts.append((selection, party, party.revision if party is not None else None))
        deadline = min(now + WAIT_SECONDS, invite.expires) if invite is not None else now + WAIT_SECONDS
        self.pending[key] = PendingUdp(owner, opcode, bytes(body), selections,
            tuple(contexts), invite, deadline, now + POLL_SECONDS, unavailable.relay, str(unavailable))
        logger = getattr(self.parties.server, "log", None)
        if unavailable.relay and callable(logger):
            logger(f"[PartyUdp] invite cid={owner[4]} deferred: {unavailable}")
        return [], 'ordinary party awaits real UDP refresh; request retained'

    def joined(self, leader, party, *, created=False):
        """Rebase only successful joins into this exact party, never leaves/replacements."""
        for pending in self.pending.values():
            contexts = []
            for selection, previous, revision in pending.parties:
                if previous is party or (created and previous is None and selection == leader):
                    previous, revision = party, party.revision
                contexts.append((selection, previous, revision))
            pending.parties = tuple(contexts)

    def _valid(self, pending):
        p = self.parties
        for selection, party, revision in pending.parties:
            session = selection[0].session
            if (not p._live(selection) or getattr(session, 'in_dungeon_run', False)
                    or getattr(session, 'in_dungeon_select', False)
                    or not getattr(session, 'enter_seria_settled', False)
                    or p._parties.get(id(session)) is not party
                    or (party is not None and party.revision != revision)):
                return False
        return pending.invite is None or p._invites.get(pending.owner[0]) is pending.invite

    def _fail(self, pending, reason):
        p = self.parties
        key = self._key(pending.owner, pending.opcode, pending.body)
        if self.pending.get(key) is not pending:
            return
        self.pending.pop(key, None)
        if pending.invite is not None:
            p._cancel_invite(pending.invite)
        if p._live(pending.owner) and len(p._queues.get(pending.owner[0], ())) < 128:
            mode = pending.body[2]
            p._queue(pending.owner, 1, pending.opcode, b'\x00\x13' + bytes([mode]))
        logger = getattr(p.server, 'log', None)
        if callable(logger):
            logger('[PartyUdp] opcode=%04X cid=%s refresh_wait=%s' % (
                pending.opcode, pending.owner[4], reason + (" " + pending.last_reason if pending.relay else "")))

    def prune(self):
        for pending in tuple(self.pending.values()):
            if self.parties.clock() >= pending.expires:
                self._fail(pending, 'timeout')
            elif not self._valid(pending):
                self._fail(pending, 'actor_or_party_changed')

    def forget(self, selection):
        for pending in tuple(self.pending.values()):
            if selection in pending.selections:
                self._fail(pending, 'member_left')

    def resume(self, owner):
        packets = []
        for pending in tuple(self.pending.values()):
            if pending.owner == owner:
                packets.extend(self._resume_one(pending))
        return packets

    def _resume_one(self, pending):
        owner = pending.owner
        key = self._key(owner, pending.opcode, pending.body)
        if self.pending.get(key) is not pending or self.parties.clock() < pending.next_poll:
            return []
        if not self._valid(pending) or self.parties.clock() >= pending.expires:
            self._fail(pending, 'expired_or_changed')
            return []
        p = self.parties
        # Pump runs on the authenticated caller's own connection thread. No
        # socket send, blocking wait, resource load or worker runs under here.
        # Completion can replace the joiner's singleton and call _remove.
        # Remove this wait first so that legitimate completion cannot cancel
        # its own request while changing membership under the same lock.
        self.pending.pop(key, None)
        try:
            # Poll only transport metadata while a refresh is missing. Do not
            # rebuild actor profiles for every quarter-second retry.
            if not pending.relay:
                p._udp_matches(pending.selections)
            cid = struct.unpack_from('<H', pending.body)[0]
            recipient = p._peer(owner, cid)
            if recipient not in pending.selections:
                raise RaidError('ordinary UDP refresh target connection replaced')
            if pending.opcode == 10:
                packets, note = p._invite_ready(owner, pending.body)
            else:
                cid, mode, value = struct.unpack('<HBI', p._fixed(pending.body, 7))
                packets, note = p._accept_current(owner, cid, mode, value, pending.invite)
        except UdpUnavailable as exc:
            pending.last_reason = str(exc)
            pending.next_poll = p.clock() + POLL_SECONDS
            self.pending[key] = pending
            return []
        except (RaidError, ValueError):
            self.pending[key] = pending
            self._fail(pending, 'revalidation_failed')
            return []
        self.pending.pop(key, None)
        logger = getattr(p.server, 'log', None)
        if callable(logger):
            logger('[PartyUdp] opcode=%04X cid=%s refresh_wait=ready' % (pending.opcode, owner[4]))
        return packets
