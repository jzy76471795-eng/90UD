"""Ordinary-town presence, encoded and sent only by each recipient's Game thread.

The selected account and connection generation come from native_connection.
N0002 builds the real peer actor before N0017 places it. Native 0167A160
removes a peer when its town/area differs from the recipient's current area.
Private area 0 and raid channels keep their existing ownership rules.
"""
from dataclasses import dataclass
import struct
from threading import RLock

from game.raid.runtime import RaidError

_LOCK = RLock()


def scope(session):
    channel = getattr(session, 'selected_channel_id', None)
    town, area = getattr(session, 'cur_town_id', 0), getattr(session, 'cur_area_id', 0)
    if (type(channel) is not int or channel in (23, 32) or channel <= 0
            or type(town) is not int or not 1 <= town <= 254
            or type(area) is not int or not 1 <= area <= 254
            or not getattr(session, 'enter_seria_settled', False)
            or getattr(session, 'in_dungeon_run', False)
            or getattr(session, 'in_dungeon_select', False)):
        return None
    return channel, town, area


def position(session):
    return (int(getattr(session, 'cur_pos_x', 0)) & 65535,
            int(getattr(session, 'cur_pos_y', 0)) & 65535,
            int(getattr(session, 'cur_direction', 5)) & 255,
            int(getattr(session, 'town_motion_state', 100)) & 65535)


@dataclass
class Actor:
    selection: tuple
    area: tuple
    position: tuple
    body: bytes
    revision: int


class TownSocial:
    def __init__(self, server):
        self.server = server
        self._actors, self._views = {}, {}
        self._lock = RLock()

    def directory(self):
        return self.server.native_game_connections

    def build_body(self, selection):
        from game.character.payloads import build_userinfo_subtype0_min
        from game.chain_loader import load_chain
        from game.guild.guild_repository import GuildRepository
        repo = self.server._repo()
        record = next((r for r in repo.list_by_account(selection[2])
                       if int(r.character_id) == selection[4]), None)
        if record is None:
            raise RaidError('town actor is not owned by its authenticated account')
        equipment, _ = load_chain('helper-userinfo_equipment.py').resolve_enter_room_equipment(
            record, enabled=True, backfill_from_pvf=False, repo=repo, prefer_live_db=True)
        guild = GuildRepository(repo.db_path).get_guild_for_character(selection[4])
        return build_userinfo_subtype0_min(record, equipment,
            guild_id=int(guild.guild_id) if guild else 0, guild_name=guild.display_name if guild else '')

    def _valid(self, actor):
        s = actor.selection[0].session
        try:
            return self.directory().chat_selection(s) == actor.selection and scope(s) == actor.area
        except RaidError:
            return False

    def visible(self, recipient, subject):
        with self._lock:
            a, b = self._actors.get(recipient[0]), self._actors.get(subject[0])
            if (a is None or b is None or a.selection != recipient or b.selection != subject
                    or a.area != b.area or not self._valid(a) or not self._valid(b)):
                return False
            view = self._views.get(recipient[0])
            return bool(view and view[0] == recipient and subject[4] in view[2]
                        and view[2][subject[4]][0] == subject)

    def pump(self, session, client):
        directory = self.directory()
        try:
            selected = directory.chat_selection(session, sender=True)
        except RaidError:
            self.forget(session)
            return
        if selected[0].client is not client:
            raise RaidError('town recipient socket ownership mismatch')
        area = scope(session)
        if area is None:
            if (getattr(session, 'cur_area_id', None) == 0
                    and not getattr(session, 'in_dungeon_run', False)
                    and not getattr(session, 'in_dungeon_select', False)):
                with self._lock:
                    previous = self._views.get(selected[0])
                    seen = tuple(previous[2]) if previous and previous[0] == selected else ()
                # 0167A160 removes these town actors for an out-of-area
                # position. Dropping the server view alone leaves stale
                # public actors visible after entering private Seria area 0.
                for cid in seen:
                    client.sendall(self.server._codec_response(0, 0x17,
                        struct.pack('<HBBHHBB',cid,255,255,0,0,5,3)))
            self.forget(session)
            return
        revision = int(getattr(session, 'town_appearance_revision', 0))
        with self._lock:
            old = self._actors.get(selected[0])
        body = old.body if old and old.selection == selected and old.revision == revision else self.build_body(selected)
        if (len(body) < 39 or body[:4] != b'\x00\x01\x00\x00'
                or int.from_bytes(body[37:39], 'little') != selected[4]):
            raise RaidError('town USERINFO0 does not identify its selected owner')
        with self._lock:
            # A selection can change while equipment was being read.
            if directory.chat_selection(session, sender=True) != selected or scope(session) != area:
                return
            self._actors[selected[0]] = Actor(selected, area, position(session), body, revision)
            for key, actor in tuple(self._actors.items()):
                if not self._valid(actor):
                    self._actors.pop(key, None)
                    self._views.pop(key, None)
            scene = (area, getattr(session, 'town_scene_generation', 0))
            previous = self._views.get(selected[0])
            seen = previous[2] if previous and previous[:2] == (selected, scene) else {}
            # Duplicate selected UIDs are ambiguous; never publish either socket.
            candidates = [a for a in self._actors.values() if a.area == area and a.selection != selected]
            counts = {}
            for a in candidates:
                counts[a.selection[4]] = counts.get(a.selection[4], 0) + 1
            peers = {a.selection[4]: a for a in candidates if counts[a.selection[4]] == 1}
            for cid, known in tuple(seen.items()):
                if cid not in peers or known[0] != peers[cid].selection:
                    # 255/255 is deliberately outside every admitted public area.
                    client.sendall(self.server._codec_response(0, 0x17, struct.pack('<HBBHHBB',cid,255,255,0,0,5,3)))
                    seen.pop(cid, None)
            for cid, actor in sorted(peers.items()):
                x, y, direction, motion = actor.position
                known = seen.get(cid)
                stamp = (actor.selection, actor.body, actor.position)
                if known is None or known[1] != actor.body:
                    client.sendall(self.server._codec_response(0, 2, actor.body))
                    client.sendall(self.server._codec_response(0, 0x17,
                        struct.pack('<HBBHHBB',cid,area[1],area[2],x,y,direction,3)))
                elif known[2] != actor.position:
                    client.sendall(self.server._codec_response(0, 0x16,
                        struct.pack('<HHHBH',cid,x,y,direction,motion)))
                seen[cid] = stamp
            self._views[selected[0]] = (selected, scene, seen)

    def forget(self, session):
        with self._lock:
            for key in tuple(self._actors):
                if key.session is session:
                    self._actors.pop(key, None)
                    self._views.pop(key, None)


def service_for(server):
    with _LOCK:
        value = getattr(server, 'town_social', None)
        if value is None:
            value = server.town_social = TownSocial(server)
        return value


def pump(server, session, client):
    if getattr(server, 'native_game_connections', None) is not None:
        service_for(server).pump(session, client)


def forget(server, session):
    value = getattr(server, 'town_social', None)
    if value is not None:
        value.forget(session)


def appearance_changed(session):
    if session is not None:
        session.town_appearance_revision = getattr(session, 'town_appearance_revision', 0) + 1


def scene_changed(session):
    if session is not None:
        session.town_scene_generation = getattr(session, 'town_scene_generation', 0) + 1
