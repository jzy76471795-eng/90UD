"""Retire an ordinary mid-run leaver after an accepted town-side request.

The existing native return and town handlers run on this account's socket
thread with no party lock held. Sending a town transition is only an intent;
the same live selection must later report an accepted town position or area.
Completion returns and selector cancellation retain their existing handlers.
"""
from dataclasses import dataclass

@dataclass(frozen=True)
class ReturnTicket:
    owner: tuple
    party: object
    generation: int
    keep_party: bool = False


class PartyReturns:
    def __init__(self, parties):
        self.parties = parties
        self.pending = {}
        self.jobs = {}
        self.waiting = {}

    def _current(self, ticket):
        session = ticket.owner[0].session
        return (self.parties._live(ticket.owner)
                and self.parties._parties.get(id(session)) is ticket.party
                and ticket.owner in ticket.party.members.values()
                and int(getattr(session, 'dungeon_run_generation', 0) or 0) == ticket.generation)

    def forget(self, session):
        self.pending.pop(id(session), None)
        self.jobs.pop(id(session), None)
        for key, (_, _, roster, _, _) in tuple(self.waiting.items()):
            if any(ticket.owner[0].session is session for ticket in roster):
                self.waiting.pop(key, None)

    def _handler(self, opcode):
        if opcode == 0x48:
            return self.parties.server._handle_opcode_0048
        return (self.parties.server._handle_giveup_game_002a if opcode == 42
                else self.parties.server._handle_back_2_village_0084)

    def leave(self, opcode, body, session, *, explicit_party_leave=False, expected=None):
        parties = self.parties
        if opcode == 0x48:
            from game.chain_loader import load_chain
            eplp = load_chain('opcode0048-eplp_command.py')
            state, option = eplp._parse_eplp(body)
            # A focus notification or linked retry must keep its native path.
            if (state != 1 or option != 2
                    or getattr(session, 'altar_final_clear_pending', False)
                    or getattr(session, 'dungeon_tournament_completed', False)
                    or int(getattr(session, 'dungeon_clear_phase', 0) or 0) < 2):
                return None
        with parties._lock:
            if not getattr(session, 'in_dungeon_run', False):
                return None
            owner = parties._owner(session, town=False)
            party = parties._parties.get(id(session))
            generation = int(getattr(session, 'dungeon_run_generation', 0) or 0)
            if party is None or owner not in party.members.values() or generation <= 0:
                return None
            if expected is not None:
                revision, members, roster, _, _ = expected
                if (party.owner != owner or party.revision != revision or tuple(party.members.values()) != members
                        or not all(self._current(t) and getattr(t.owner[0].session,'in_dungeon_run',False)
                                   and int(getattr(t.owner[0].session,'dungeon_clear_phase',0) or 0) >= 2 for t in roster)):
                    return None
            completed = int(getattr(session, 'dungeon_clear_phase', 0) or 0) >= 2
            keep_party = completed and party.owner == owner and not explicit_party_leave
            followers = []
            if keep_party:
                for member in party.members.values():
                    peer = member[0].session
                    if member != owner and getattr(peer, 'in_dungeon_run', False):
                        followers.append(ReturnTicket(member, party,
                            int(getattr(peer, 'dungeon_run_generation', 0) or 0), True))
            ticket = ReturnTicket(owner, party, generation, keep_party)
            if keep_party and any(int(getattr(f.owner[0].session, 'dungeon_clear_phase', 0) or 0) < 2 for f in followers):
                self.waiting.setdefault(id(session), (party.revision, tuple(party.members.values()),
                    (ticket, *followers), opcode, bytes(body)))
                return [], 'completed group return queued until every member result'
            self.waiting.pop(id(session), None)
            self.pending[id(session)] = ticket
        handler = self._handler(opcode)
        try:
            # Existing account-owned handler may read resources, save or send.
            result = handler(body, session=session)
        finally:
            with parties._lock:
                if (self.pending.get(id(session)) is ticket
                        and (not self._current(ticket) or getattr(session, 'in_dungeon_run', False)
                             or getattr(session, 'in_dungeon_select', False))):
                    self.pending.pop(id(session), None)
        with parties._lock:
            if self.pending.get(id(session)) is ticket and self._current(ticket):
                for follower in followers:
                    if self._current(follower):
                        # Queue only the operation, never another account's raw
                        # payload. Each owner executes its own native handler.
                        self.jobs[id(follower.owner[0].session)] = (follower, opcode, b'\1\2' if opcode == 0x48 else b'')
        return result

    def pump(self, owner, client):
        """Run each completed return on its authenticated account socket thread."""
        parties = self.parties
        session = owner[0].session
        resume = False
        with parties._lock:
            wait = self.waiting.get(id(session))
            if wait is not None:
                revision, members, roster, opcode, body = wait
                ticket = roster[0]
                if owner[0].client is not client or parties.server.account_id != owner[2]:
                    return
                if (ticket.owner != owner or ticket.party.owner != owner or ticket.party.revision != revision
                        or tuple(ticket.party.members.values()) != members
                        or not all(self._current(t) and getattr(t.owner[0].session,'in_dungeon_run',False) for t in roster)):
                    self.waiting.pop(id(session), None)
                elif all(int(getattr(t.owner[0].session, 'dungeon_clear_phase', 0) or 0) >= 2 for t in roster):
                    self.waiting.pop(id(session), None)
                    resume = True
        if resume:
            result = self.leave(opcode, body, session, expected=wait)
            if result:
                for packet in result[0]:
                    client.sendall(packet)
            return
        with parties._lock:
            job = self.jobs.pop(id(owner[0].session), None)
            if job is None:
                return
            ticket, opcode, body = job
            session = owner[0].session
            if (ticket.owner != owner or owner[0].client is not client
                    or parties.server.account_id != owner[2] or not self._current(ticket)
                    or not getattr(session, 'in_dungeon_run', False)
                    or int(getattr(session, 'dungeon_clear_phase', 0) or 0) < 2):
                return
            self.pending[id(session)] = ticket
        handler = self._handler(opcode)
        try:
            packets, _ = handler(body, session=session)
            for packet in packets:
                client.sendall(packet)
        finally:
            with parties._lock:
                if (not self._current(ticket) or getattr(session, 'in_dungeon_run', False)
                        or getattr(session, 'in_dungeon_select', False)):
                    if self.pending.get(id(session)) is ticket:
                        self.pending.pop(id(session), None)

    def confirm_town(self, opcode, session):
        """Called only by an accepted native town position/area handler."""
        parties = self.parties
        with parties._lock:
            ticket = self.pending.get(id(session))
            if ticket is None:
                return None
            if not self._current(ticket):
                self.pending.pop(id(session), None)
                return None
            if (getattr(session, 'in_dungeon_run', False) or getattr(session, 'in_dungeon_select', False)
                    or not 1 <= int(getattr(session, 'cur_town_id', 0) or 0) <= 254
                    or not 0 <= int(getattr(session, 'cur_area_id', 255)) <= 254):
                return None
            self.pending.pop(id(session), None)
            # Survivors retain their run and native slots. Replaying op11
            # would reset a healthy peer transport during this roster update.
            if ticket.keep_party:
                parties.runs.detach(session)
                cards = getattr(parties, 'cards', None)
                if cards is not None:
                    cards.forget(session)
            else:
                parties._remove(ticket.owner, refresh_peers=False)
        from game.normal_party import _log_route
        _log_route(parties.server, session, opcode,
            'ordinary completed group return confirmed; membership preserved' if ticket.keep_party
            else 'ordinary individual return confirmed; own membership retired')
