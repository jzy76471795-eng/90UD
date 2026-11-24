"""Relay authority for actual Anton/Luke owned combat squads.

Retain the raid's selections, stable slots, sent native profiles, roster/socket
receipts and one-use run authority. Replace only its transport proof producer.
The worker publishes routes outside all raid/lobby locks; original recipient
pumps cannot send peer triggers until every DLL has installed the same view.
"""
from dataclasses import dataclass
import struct
from threading import RLock
from game.raid.ordinary_party import OrdinaryParties
from game.raid.native_party import NativePublicationPending
from game.raid.native_network_protocol import PeerAddress
from game.raid.native_udp import native_ipv4_word
from game.raid.runtime import RaidError,_OrdinaryPublicationProof,_OrdinaryRelayProof
from login.party_relay_client import RelayUnavailable


@dataclass(frozen=True)
class _TownView:
    actor: object
    scope: object
    scene: object
    party_key: object
    peers: tuple
    hidden: tuple = ()


@dataclass(eq=False)
class _TownPlan:
    view: _TownView
    packets: tuple
    sent: int = 0


class RaidTownPresence:
    """Project every authenticated actor in the current public raid-town scene.

    NativeLobby's USERINFO0 send receipt survives a map transition, while
    the native map removes actor objects. Recreate those objects and place
    them with the same N0017/N0016 contract used by ordinary town presence.
    Encoding and receipt commits remain on each recipient's account thread.
    """
    def __init__(self, parties):
        self.parties = parties
        self.service = parties.service
        self._views, self._pending = {}, {}
        self._lock = RLock()

    def visible(self, recipient, subject):
        """Consume both actual public-town send receipts for player actions."""
        lobby = self.service.native_lobby
        with self._lock:
            first, second = self._views.get(id(recipient)), self._views.get(id(subject))
            if (first is None or second is None or first.actor.session is not recipient
                    or second.actor.session is not subject or first.scope is None
                    or first.scope != second.scope
                    or first.scope != self._scope(recipient) or second.scope != self._scope(subject)
                    or first.scene != getattr(recipient, 'town_scene_generation', 0)
                    or second.scene != getattr(subject, 'town_scene_generation', 0)):
                return False
            if (not any(row[1] is second.actor for row in first.peers)
                    or not any(row[1] is first.actor for row in second.peers)):
                return False
        # No presence lock is held while validating the selected native bodies.
        return lobby.visible(recipient, subject) and lobby.visible(subject, recipient)

    @staticmethod
    def _scope(session):
        channel = getattr(session, 'selected_channel_id', None)
        town = getattr(session, 'cur_town_id', None)
        area = getattr(session, 'cur_area_id', None)
        if (type(channel) is not int or {23: 19, 32: 32}.get(channel) != town
                or type(town) is not int or type(area) is not int or not 1 <= area <= 254
                or not getattr(session, 'enter_seria_settled', False)
                or getattr(session, 'in_dungeon_run', False)
                or getattr(session, 'in_dungeon_select', False)):
            return None
        return channel, town, area

    @staticmethod
    def _position(session):
        # C0023 persists coordinates/direction, without a motion field.
        # Retain ordinary town presence's established neutral param=100.
        fields = (getattr(session, 'cur_pos_x', None), getattr(session, 'cur_pos_y', None),
                  getattr(session, 'cur_direction', None), getattr(session, 'town_motion_state', 100))
        if any(type(value) is not int for value in fields):
            raise RaidError('raid town position requires actual integer session fields')
        x, y, direction, motion = fields
        if not (0 <= x <= 65535 and 0 <= y <= 65535 and 0 <= direction <= 255 and 0 <= motion <= 65535):
            raise RaidError('raid town position is outside the native field bounds')
        return fields

    def _snapshot(self, session):
        lobby = self.service.native_lobby
        actor = lobby._own(session, town=False)
        if self.service.server.account_id != actor.account_id:
            raise RaidError('raid town packets require their owning recipient account')
        scope = self._scope(session)
        entry = self.service.current(actor.character_id)
        if entry is not None:
            self.service.require_session(entry, session, actor.character_id, actor.account_id)
        peers = []
        if scope is not None:
            with lobby._lock:
                candidates = tuple(lobby._actors.values())
            for subject in candidates:
                if subject is actor or self._scope(subject.session) != scope:
                    continue
                try:
                    if lobby._own(subject.session, town=False) is not subject:
                        continue
                    body = lobby.current_body(subject.session)
                except RaidError:
                    continue
                peers.append((subject.character_id, subject, body, self._position(subject.session)))
        peers.sort(key=lambda row: row[0])
        if len({row[0] for row in peers}) != len(peers):
            raise RaidError('raid town peer identity is ambiguous')
        visible = {row[1] for row in peers}
        # A different scene removes a model; raid membership does not.
        with lobby._lock:
            hidden = tuple(sorted(((delivery.subject.character_id, delivery.subject, delivery.body, id(delivery))
                for delivery in lobby._deliveries.values()
                if delivery.recipient is actor and delivery.subject not in visible), key=lambda row: row[0]))
        return _TownView(actor, scope, getattr(session, 'town_scene_generation', 0),
                         None if entry is None else (id(entry), entry.domain.raid_key), tuple(peers), hidden)

    def pending_packets(self, session):
        # NativeLobby calls this outside its lock: keep service->lobby order.
        with self.service._lock:
            if self.service.server.account_id != getattr(session, 'account_id', None):
                raise RaidError('raid town packets require their owning recipient account')
            try:
                view = self._snapshot(session)
            except RaidError:
                self.forget_session(session)
                return []
            with self._lock:
                key = id(session)
                pending = self._pending.get(key)
                if pending is not None and pending.view == view:
                    return list(pending.packets[pending.sent:])
                self._pending.pop(key, None)
                previous = self._views.get(key)
                if previous is not None and previous.actor is not view.actor:
                    previous = None
                    self._views.pop(key, None)
                # Dungeon packets own dungeon actors. Discard town cache on
                # entry without injecting town-removal frames into that run.
                if getattr(session, 'in_dungeon_run', False) or getattr(session, 'in_dungeon_select', False):
                    self._views.pop(key, None)
                    return []
                known = {} if previous is None else {row[0]: row for row in previous.peers}
                current = {row[0]: row for row in view.peers}
                frames = []
                encode = self.service.server._codec_response
                old_hidden = () if previous is None else previous.hidden
                removed = set()
                for row in view.hidden:
                    if row not in old_hidden:
                        frames.append(encode(0, 0x17, struct.pack('<HBBHHBB', row[0], 255, 255, 0, 0, 5, 3)))
                        removed.add(row[0])
                for cid, old in sorted(known.items()):
                    if cid not in removed and (cid not in current or old[1] is not current[cid][1]):
                        frames.append(encode(0, 0x17, struct.pack('<HBBHHBB', cid, 255, 255, 0, 0, 5, 3)))
                scene_changed = previous is None or (previous.scope, previous.scene) != (view.scope, view.scene)
                for cid, subject, body, position in view.peers:
                    old = known.get(cid)
                    x, y, direction, motion = position
                    if scene_changed or old is None or old[1] is not subject or old[2] != body:
                        # Public outsiders also need NativeLobby's exact
                        # owner-socket profile receipt for player actions.
                        delivery = self.service.native_lobby.queue_peer(session, subject.session)
                        frames.append(self.service.native_lobby._encode_delivery(delivery)
                            if not delivery.sent else encode(0, 2, body))
                        frames.append(encode(0, 0x17, struct.pack('<HBBHHBB', cid, view.scope[1], view.scope[2], x, y, direction, 3)))
                    elif old[3] != position:
                        frames.append(encode(0, 0x16, struct.pack('<HHHBH', cid, x, y, direction, motion)))
                if frames:
                    self._pending[key] = _TownPlan(view, tuple(frames))
                else:
                    self._views[key] = view
                return frames

    def note_send(self, session, packet):
        with self.service._lock:
            with self._lock:
                pending = self._pending.get(id(session))
                if (pending is None or pending.view.actor.session is not session
                        or pending.packets[pending.sent] is not packet):
                    return
                pending.sent += 1
                if pending.sent != len(pending.packets):
                    return
            try:
                current = self._snapshot(session)
            except RaidError:
                current = None
            with self._lock:
                if self._pending.get(id(session)) is not pending:
                    return
                self._pending.pop(id(session), None)
                if current == pending.view:
                    self._views[id(session)] = current

    def forget_session(self, session):
        with self._lock:
            for directory in (self._views, self._pending):
                value = directory.get(id(session))
                actor = value.view.actor if isinstance(value, _TownPlan) else value.actor if value is not None else None
                if actor is not None and actor.session is session:
                    directory.pop(id(session), None)


class RelayedRaidParties(OrdinaryParties):
    def __init__(self,service,controller):
        super().__init__(service);self.controller=controller
        if getattr(service.native_lobby, '_town_presence', None) is not None:
            raise RaidError('raid town presence is already installed')
        self.town_presence = RaidTownPresence(self)
        service.native_lobby._town_presence = self.town_presence
        controller.add_provider(self.source_groups)

    def source_groups(self):
        with self.service._lock:
            parties=tuple(self._parties.values())
        directory=self.service.server.native_game_connections;rows=[]
        for party in parties:
            if len(party.members)<2:continue
            try:selections=tuple(directory.chat_selection(seat.session) for seat in party.members)
            except RaidError:continue
            if all(s is not None for s in selections):
                try:enabled=self.controller.rollout.group(s[2] for s in selections)
                except RelayUnavailable:continue
                if enabled:rows.append((('raid',*self.key(party)),selections))
        return tuple(rows)

    def _relay_mode(self,actors):
        try:return self.controller.rollout.group(actor.account_id for actor in actors)
        except RelayUnavailable as exc:raise RaidError(str(exc)) from None

    def _selections(self,actors):
        keys=self.service.server.native_game_connections
        selected=tuple(keys.chat_selection(actor.session) for actor in actors)
        if any(s is None or (s[2],s[3],s[4])!=(actor.account_id,actor.channel_id,actor.character_id) for s,actor in zip(selected,actors)):
            raise RaidError('raid relay requires actual delivered Game selection')
        return selected

    def _facts(self,actors):
        if not self._relay_mode(actors):return super()._facts(actors)
        selections=self._selections(actors)
        try:endpoints=self.controller.endpoints(selections,invitation=True)
        except RelayUnavailable as exc:raise RaidError(str(exc)) from None
        keys=self.service.server.native_game_connections;peers=[]
        for actor,(bound,ip,port) in zip(actors,endpoints):
            key=keys.key_for_actor(actor)
            try:proof=self.service.native_lobby.self_profile.proof(actor.session)
            except RaidError as exc:raise RaidError('member=%s self-profile: %s' % (actor.character_id,exc)) from exc
            if (key!=bound.key or proof.actor is not actor or type(proof.value) is not int or not 0<=proof.value<=100):
                raise RaidError('raid relay requires real delivered native key/profile')
            address=native_ipv4_word(ip)
            peers.append(PeerAddress(actor.character_id,address,address,port,key,3,512,proof.value))
        return tuple(peers)

    def resolve(self,entry,actors):
        if len(actors)>1 and self._relay_mode(actors):
            try:self.controller.endpoints(self._selections(actors))
            except RelayUnavailable as exc:raise NativePublicationPending(str(exc)) from None
        return super().resolve(entry,actors)

    def preparation_error(self,session):
        error=super().preparation_error(session)
        if error is not None:return error
        if not self.controller.rollout.contains(getattr(session,'account_id',None)):return None
        try:
            party=self.current(session,auto=False)
            if party is None:return None
            self.controller.group_proof(self._selections(self._actors(party)))
            return None
        except (RelayUnavailable,RaidError,AttributeError) as exc:
            return str(exc) or type(exc).__name__

    def _publication_proof(self,party,actors):
        if not self._relay_mode(actors):return super()._publication_proof(party,actors)
        service,server=self.service,self.service.server
        lobby,native,keys=service.native_lobby,service.native_party,server.native_game_connections
        try:proof_key,stamp=self.controller.group_proof(self._selections(actors))
        except RelayUnavailable as exc:raise RaidError(str(exc)) from None
        references=[(self,'_parties',(id(party.entry),party.group),party),(service,'raids',party.entry.domain.raid_key,party.entry)]
        absent,attributes,sockets=[],[],[]
        attributes.extend(((service,'native_lobby',lobby,True),(service,'native_party',native,True),
            (lobby,'self_profile',lobby.self_profile,True),(server,'native_game_connections',keys,True)))
        for actor in actors:
            session=actor.session;connection=keys._connections[id(session)]
            roster_key=(id(session),actor.epoch)
            references.extend(((service,'_raid_sessions',id(session),service._raid_sessions[id(session)]),
                (lobby,'_by_session',id(session),actor),(lobby,'_actors',(actor.channel_id,actor.character_id),actor),
                (lobby.self_profile,'_values',id(session),lobby.self_profile.proof(session)),
                (keys,'_connections',id(session),connection),(native,'_sent',actor,native._sent[actor]),
                (service,'_lobby_projections',roster_key,service._lobby_projections[roster_key])))
            absent.extend(((service,'_lobby_pending',roster_key),(native,'_pending',actor)))
            attributes.extend(((session,'selected_character_id',actor.character_id,False),
                (session,'account_id',actor.account_id,False),(session,'selected_channel_id',actor.channel_id,False),
                (session,'_raid_selected_epoch',actor.epoch,True),(session,'_raid_native_namespace',session._raid_native_namespace,False),
                (session,'login_session_id',connection.login_session_id,False),(connection,'selected',connection.selected,True),
                (connection,'sent',True,True),(connection,'sent_generation',connection.sent_generation,False),
                (connection,'channel_id',actor.channel_id,False),(connection,'identity',connection.identity,True)))
            sockets.append(connection.client)
            for subject in actors:
                if actor is subject:continue
                delivery=lobby._deliveries[(actor,subject)]
                references.append((lobby,'_deliveries',(actor,subject),delivery));attributes.append((delivery,'sent',True,True))
        transport=_OrdinaryRelayProof(server,self.controller,proof_key,stamp)
        proof=_OrdinaryPublicationProof(tuple(references),tuple(absent),tuple(attributes),tuple(sockets),(transport,))
        if not proof.current():raise RaidError('actual raid relay publication changed before run authorization')
        return proof
