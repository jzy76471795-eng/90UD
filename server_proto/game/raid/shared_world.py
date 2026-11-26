"""Server-owned raid spawn identities with separate per-recipient loot rows.

The ordinary handlers bind real PVF spawns before encoding START_MAP or
preloading drops. No client field or copied private row value creates identity.
"""
from copy import deepcopy
from dataclasses import dataclass, field
from functools import wraps
from threading import RLock
from uuid import uuid4

from .dungeon_adapter import BridgeError


def _locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


@dataclass(frozen=True)
class EntryPlan:
    start: dict
    seed: int
    actor_base: int


@dataclass(frozen=True, eq=False)
class SharedActorIdentity:
    _world: object = field(repr=False)
    room_key: tuple
    actor_code: int
    actor_type: int
    actor_sequence: object = None
    wire_visible: bool = True
    capability: str = field(default_factory=lambda: uuid4().hex)

    @property
    def run_id(self):
        return self._world.ticket.run_id if self._world.ticket is not None else None


@dataclass
class _Entity:
    identity: SharedActorIdentity
    dead: bool = False


@dataclass
class _Room:
    seed: int
    actor_base: int
    templates: object = None
    entities: tuple = ()
    personal_sequences: set = field(default_factory=set)


class SharedRunWorld:
    def __init__(self, dungeon_id, participants):
        ids = tuple(participants)
        if (type(dungeon_id) is not int or dungeon_id <= 0 or not 1 <= len(ids) <= 4
                or len(set(ids)) != len(ids) or any(type(cid) is not int or not 1 <= cid <= 0xfffe for cid in ids)):
            raise BridgeError('owned dungeon and distinct native party UIDs required')
        self.dungeon_id, self.participants = dungeon_id, ids
        self.ticket, self._plan, self._policy = None, None, None
        self._entries, self._rooms, self._views = {}, {}, {}
        self._cursor = 0x191
        # Bridge callbacks already own the bridge lock. Keep this lock local:
        # no world operation may call back into the bridge or transport.
        self._lock = RLock()

    def _entry(self, session, owner):
        selected = self._entries.get(owner.character_id)
        if selected is None or selected[0] is not session or selected[1] != owner:
            raise BridgeError('shared world requires its frozen selected-session owner')

    def _next_base(self):
        base = self._cursor
        while True:
            collisions = [cid for cid in self.participants if base < cid <= base + 255]
            if not collisions:
                break
            base = max(collisions)
        if base + 255 > 0xfffe:
            raise BridgeError('shared actor key space exhausted without wrapping')
        return base

    @_locked
    def entry_plan(self, session, owner, start, seed, difficulty, entry_option):
        if owner.character_id not in self.participants:
            raise BridgeError('selected actor is outside the frozen shared party')
        policy = (difficulty, entry_option)
        if self._plan is None:
            if owner.character_id != self.participants[0]:
                raise BridgeError('first party slot must choose the shared entry')
            if type(seed) is not int or not 0 <= seed <= 0xffffffff or not isinstance(start, dict):
                raise BridgeError('actual ordinary entry plan unavailable')
            actual = deepcopy(start)
            key = (int(actual.get('maze_index') or 0), int(actual.get('start_x') or 0),
                   int(actual.get('start_y') or 0), int(actual.get('map_index') or 0))
            if key[0] < 0 or not all(0 <= n <= 255 for n in key[1:3]) or key[3] <= 0:
                raise BridgeError('actual ordinary start room invalid')
            self._plan, self._policy = (actual, seed, self._next_base()), policy
            self._rooms[key] = _Room(seed, self._plan[2])
            self._cursor = self._plan[2] + 255
        if self._policy != policy:
            raise BridgeError('shared difficulty or entry option changed')
        existing = self._entries.get(owner.character_id)
        if existing is not None and (existing[0] is not session or existing[1] != owner):
            raise BridgeError('replacement session cannot inherit an active shared world')
        self._entries[owner.character_id] = (session, owner)
        start, seed, actor_base = self._plan
        return EntryPlan(deepcopy(start), seed, actor_base)

    @_locked
    def attach_ticket(self, ticket):
        if (ticket.dungeon_id != self.dungeon_id
                or {m.character_id for m in ticket.participants} != set(self.participants)
                or self.ticket is not None and self.ticket is not ticket):
            raise BridgeError('shared world ticket or frozen participants changed')
        self.ticket = ticket

    @_locked
    def room_plan(self, session, owner, key):
        self._entry(session, owner)
        if key[0] != int(self._plan[0].get('maze_index') or 0) or key[3] <= 0:
            raise BridgeError('room is outside the shared maze')
        current = self._rooms.get(key)
        if current is None:
            seed = (self._plan[1] ^ (key[1] << 16) ^ (key[2] << 24)) & 0xfffbffff
            current = self._rooms[key] = _Room(seed, self._next_base())
            self._cursor = current.actor_base + 255
        return current.seed, current.actor_base

    @staticmethod
    def room_key(session):
        return (int(session.dungeon_maze_index), int(session.dungeon_cell_x),
                int(session.dungeon_cell_y), int(session.dungeon_map_index))

    @_locked
    def bind_room(self, session, owner):
        self._entry(session, owner)
        key = self.room_key(session)
        actual = getattr(session, 'dungeon_room_states', {}).get(key)
        if not isinstance(actual, dict) or actual.get('key') != key or actual.get('map_index') != key[3]:
            raise BridgeError('actual cached ordinary room required before shared spawn binding')
        seed, base = self.room_plan(session, owner, key)
        if (actual.get('seed'), actual.get('first_seq')) != (seed, base):
            raise BridgeError('ordinary room was built with a different shared seed/actor range')
        room = self._rooms[key]
        view_key = (id(session), key)
        view = self._views.get(view_key)
        if view is None:
            rows = (list(getattr(session, 'dungeon_room_mons', ()) or ()),
                    list(getattr(session, 'dungeon_room_passive_objects', ()) or ()))
            personal = [row for row in rows[0] if row.get('personal_quest_actor')]
            rows = ([row for row in rows[0] if not row.get('personal_quest_actor')], rows[1])
            if room.templates is None:
                entities = []
                for group in rows:
                    for row in group:
                        code, kind = row.get('code'), row.get('type')
                        if type(code) is not int or code <= 0 or type(kind) is not int:
                            raise BridgeError('actual PVF spawn identity unavailable')
                        if group is rows[0] and row.get('wire_visible', True):
                            seq = row.get('seq')
                            if type(seq) is not int or not base < seq <= base + 255 or seq in self.participants:
                                raise BridgeError('shared spawn key collides or escapes its reserved range')
                            self._cursor = max(self._cursor, seq)
                        entities.append(_Entity(SharedActorIdentity(self, key, code, kind,
                            row.get('seq'), bool(row.get('wire_visible', True)))))
                room.templates, room.entities = deepcopy(rows), tuple(entities)
            else:
                rows = deepcopy(room.templates)
                for row, entity in zip(rows[0] + rows[1], room.entities):
                    row['killed'] = entity.dead
            bindings = {id(row): (row, entity) for row, entity in zip(rows[0] + rows[1], room.entities)}
            if personal:
                reserved = {row.get('seq') for row in rows[0]} | room.personal_sequences
                free = (seq for seq in range(base + 255, base, -1) if seq not in reserved)
                for row in personal:
                    seq = next(free, None)
                    if seq is None:
                        raise BridgeError('personal quest actor range exhausted')
                    row['seq'] = seq
                    room.personal_sequences.add(seq)
                    entity = _Entity(SharedActorIdentity(self, key, row['code'], row['type'],
                                                        seq, bool(row.get('wire_visible', True))))
                    rows[0].append(row)
                    bindings[id(row)] = (row, entity)
            view = self._views[view_key] = (rows, bindings)
        else:
            # Re-entry projects authoritative deaths into the existing view.
            # Active peers keep their own normal death/drop callbacks until
            # re-entry; never overwrite their per-account loot metadata.
            for row, entity in view[1].values():
                if entity.dead:
                    row['killed'] = True
        session.dungeon_room_mons, session.dungeon_room_passive_objects = view[0]
        actual['monsters'] = session.dungeon_room_mons
        session.dungeon_seed, session.dungeon_room_first_seq = seed, base
        session.dungeon_room_sequence_cursor = max(int(getattr(session, 'dungeon_room_sequence_cursor', 0) or 0), self._cursor + 1)
        return view[0]

    @_locked
    def actor_identity(self, session, owner, row, *, missing_ok=False):
        self._entry(session, owner)
        key = self.room_key(session)
        view = self._views.get((id(session), key))
        found = view[1].get(id(row)) if view else None
        if found is None or found[0] is not row:
            if missing_ok:
                return None
            raise BridgeError('actor has no authoritative shared spawn registration')
        identity = found[1].identity
        if (row.get('code'), row.get('type'), row.get('seq'), bool(row.get('wire_visible', True))) != (
                identity.actor_code, identity.actor_type, identity.actor_sequence, identity.wire_visible):
            raise BridgeError('shared PVF entity changed after spawn')
        if row.get('killed') is True:
            found[1].dead = True
        return identity

    @_locked
    def owned_actor_views(self, identity):
        """Registered rows, including non-wire clear shadows; no guessed keys.

        Ordinary-party typed death callbacks consume these on the actual
        recipient thread. Existing raid wire-only projection is unchanged.
        """
        if not isinstance(identity, SharedActorIdentity) or identity._world is not self:
            raise BridgeError('foreign shared entity')
        peers = []
        for session, owner in self._entries.values():
            if not getattr(session, 'in_dungeon_run', False) or self.room_key(session) != identity.room_key:
                continue
            view = self._views.get((id(session), identity.room_key))
            if view is None:
                continue
            for row, entity in view[1].values():
                if entity.identity is identity and not row.get('killed'):
                    peers.append((session, owner, row))
        return tuple(peers)

    @_locked
    def living_peer_views(self, identity):
        """Actual same-room monster views only; no guessed passive-object key."""
        if not isinstance(identity, SharedActorIdentity) or identity._world is not self:
            raise BridgeError('foreign shared entity')
        peers = []
        for session, owner in self._entries.values():
            if self.room_key(session) != identity.room_key:
                continue
            view = self._views.get((id(session), identity.room_key))
            if view is None:
                continue
            for row in view[0][0]:
                registered = view[1].get(id(row))
                seq = row.get('seq')
                if (registered is not None and registered[0] is row
                        and registered[1].identity is identity and not row.get('killed')
                        and row.get('wire_visible', True) and type(seq) is int and 1 <= seq <= 0xfffe):
                    peers.append((session, owner, row))
        return tuple(peers)
