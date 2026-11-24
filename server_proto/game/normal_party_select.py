"""Ordinary-party selector delivery on each authenticated recipient thread.

Reuse the existing Stage3 001A/001B and 0084 town builders. This owns the
selector phase only; it does not grant a shared dungeon run or its rewards.
"""
from dataclasses import dataclass, field
from game.raid.runtime import RaidError


@dataclass(eq=False)
class Selection:
    party: object
    revision: int
    members: tuple
    expires: float
    origins: dict
    entered: set = field(default_factory=set)
    active: bool = True
    cancel_attempts: dict = field(default_factory=dict)


class PartySelectors:
    def __init__(self, parties):
        self.parties = parties
        self.rounds, self.jobs = {}, {}

    def forget(self, session):
        for connection in tuple(self.jobs):
            if connection.session is session:
                self.jobs.pop(connection, None)

    def _queue(self, target, kind, selection, body=b''):
        rows = self.jobs.setdefault(target[0], [])
        if kind == 'update':
            rows[:] = [r for r in rows if r[0] != 'update' or r[1] is not selection]
        if len(rows) >= 16:
            raise RaidError('ordinary selector delivery queue full')
        rows.append((kind, selection, body))

    def _valid(self, selection):
        p = selection.party
        return (selection.active and p.revision == selection.revision
                and tuple(s for _, s in sorted(p.members.items())) == selection.members
                and all(self.parties._live(s) for s in selection.members))

    def abort(self, party):
        selection = self.rounds.pop(id(party), None)
        if selection is None:
            return
        selection.active = False
        for target in selection.members:
            rows = self.jobs.setdefault(target[0], [])
            rows[:] = [r for r in rows if r[1] is not selection and
                       (r[0] != 'cancel' or getattr(target[0].session, '_normal_party_selector', None) is r[1])]
            if getattr(target[0].session, '_normal_party_selector', None) is selection:
                self._queue(target, 'cancel', selection)

    def retire(self, selection):
        """The existing dungeon-run path takes ownership beyond the selector."""
        self.rounds.pop(id(selection.party), None)
        selection.active = False
        for target in selection.members:
            rows = self.jobs.get(target[0], [])
            rows[:] = [r for r in rows if r[1] is not selection]

    def _leave(self, target, selection, opcode=132, requested=False):
        session = target[0].session
        if not requested and getattr(session, '_normal_party_selector', None) is not selection:
            return [], 'ordinary selector cancellation already settled'
        if getattr(session, 'in_dungeon_run', False):
            return [], 'ordinary selector cancellation does not leave a dungeon run'
        packets, note = ([], 'ordinary selector already in town')
        if requested or getattr(session, 'in_dungeon_select', False):
            handler = (self.parties.server._handle_giveup_game_002a if opcode == 42 else
                       self.parties.server._handle_back_2_village_0084)
            packets, note = handler(b'', session=session)
        if not getattr(session, 'in_dungeon_select', False):
            session._normal_party_selector = None
        return packets, note

    def route(self, opcode, body, session):
        if opcode in (42, 132):
            with self.parties._lock:
                owner = self.parties._owner(session, town=False)
                party = self.parties.current(session)
                selection = self.rounds.get(id(party)) if party is not None else None
                if selection is None or any(getattr(s[0].session, 'in_dungeon_run', False)
                                            for s in selection.members):
                    return None
                self.abort(party)
            return self._leave(owner, selection, opcode, requested=True)
        with self.parties._lock:
            return self._route(opcode, body, session)

    def _route(self, opcode, body, session):
        owner = self.parties._owner(session, town=False)
        party = self.parties.current(session)
        if party is None:
            return None
        selection = self.rounds.get(id(party))
        if opcode in (42, 132):
            if selection is None:
                return None
            if any(getattr(s[0].session, 'in_dungeon_run', False) for s in selection.members):
                return None
            self.abort(party)
            return self._leave(owner, selection, opcode, requested=True)
        if len(party.members) < 2:
            return None
        if party.owner != owner:
            raise RaidError('only the current party leader can open the group selector')
        raw = self.parties._fixed(body, 4)
        if selection is not None and not self._valid(selection):
            self.abort(party)
            raise RaidError('ordinary selector membership changed')
        if selection is None:
            members = tuple(s for _, s in sorted(party.members.items()))
            for target in members:
                peer = target[0].session
                if (getattr(peer, 'in_dungeon_run', False) or getattr(peer, 'in_dungeon_select', False)
                        or not getattr(peer, 'enter_seria_settled', False)):
                    raise RaidError('ordinary selector requires all members in settled town')
                if target != owner:
                    self.parties._peer(owner, target[4])
                if len(self.jobs.get(target[0], ())) >= 15:
                    raise RaidError('ordinary selector delivery queue full')
            origins = {s[0]:(getattr(s[0].session, 'cur_town_id', None),
                             getattr(s[0].session, 'cur_area_id', None)) for s in members}
            selection = Selection(party, party.revision, members, self.parties.clock() + 30, origins)
            self.rounds[id(party)] = selection
            for target in members:
                self._queue(target, 'enter', selection, raw)
            return [], 'ordinary party selector queued for every actual member'
        if int.from_bytes(raw, 'little') in (0, 0xffffffff):
            return [], 'ordinary party selector already queued or active'
        for target in selection.members:
            self._queue(target, 'update', selection, raw)
        return [], 'ordinary party preselect queued for every actual member'

    def pump(self, owner, client):
        if self.parties.server.account_id != owner[2]:
            return
        with self.parties._lock:
            for selection in tuple(self.rounds.values()):
                if any(getattr(s[0].session, 'in_dungeon_run', False) for s in selection.members):
                    self.retire(selection)
                elif (not self._valid(selection) or
                        (len(selection.entered) < len(selection.members) and self.parties.clock() >= selection.expires)):
                    self.abort(selection.party)
            jobs = self.jobs.pop(owner[0], ())
        for kind, selection, body in jobs:
            with self.parties._lock:
                if owner not in selection.members or not self.parties._live(owner):
                    continue
                if kind != 'cancel' and not self._valid(selection):
                    continue
            session = owner[0].session
            if kind == 'cancel':
                packets, _ = self._leave(owner, selection)
                with self.parties._lock:
                    if (getattr(session, '_normal_party_selector', None) is selection
                            and getattr(session, 'in_dungeon_select', False)):
                        count = selection.cancel_attempts.get(owner[0], 0) + 1
                        selection.cancel_attempts[owner[0]] = count
                        if count < 3:
                            self._queue(owner, 'cancel', selection)
            else:
                with self.parties._lock:
                    if kind == 'enter' and (getattr(session, 'in_dungeon_run', False)
                            or getattr(session, 'in_dungeon_select', False)
                            or (getattr(session, 'cur_town_id', None),getattr(session, 'cur_area_id', None))
                                != selection.origins[owner[0]]):
                        self.abort(selection.party)
                        continue
                    session._normal_party_selector = selection
                try:
                    packets, _ = self.parties.server._handle_enter_select_dungeon_000f(body, session=session)
                    if not getattr(session, 'in_dungeon_select', False):
                        raise RaidError('ordinary selector did not enter its actual member')
                except Exception:
                    with self.parties._lock:
                        self.abort(selection.party)
                    continue
                with self.parties._lock:
                    valid = self._valid(selection)
                    if valid:
                        selection.entered.add(owner[0])
                if not valid:
                    # A membership change can occur during native preparation.
                    # Restore only this same selected owner on its own thread.
                    if self.parties._live(owner):
                        packets, _ = self._leave(owner, selection)
                    else:
                        packets = []
            for packet in packets:
                client.sendall(packet)
        if (getattr(owner[0].session, '_normal_party_selector', None) is not None
                and getattr(owner[0].session, 'in_dungeon_run', False)):
            owner[0].session._normal_party_selector = None
