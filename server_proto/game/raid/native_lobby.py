"""Native actor identity from the actual selected actor producer and sends.

This directory does not turn a database CID or a client-supplied word into
join authority. It retains the exact server-built USERINFO0 body and freezes
the authenticated session, channel and selection epoch that received it.
"""
from dataclasses import dataclass
from threading import RLock
from time import monotonic

from .protocol import LOCAL_NATIVE_NAMESPACE
from .runtime import RaidError
from .native_self_profile import SentSelfProfile


@dataclass(frozen=True, eq=False)
class SelectedActor:
    session: object
    character_id: int
    account_id: int
    channel_id: int
    epoch: object
    body: bytes


@dataclass
class _Delivery:
    recipient: SelectedActor
    subject: SelectedActor
    body: bytes
    packet: object = None
    sent: bool = False


@dataclass(frozen=True, eq=False)
class _Appearance:
    actor: SelectedActor
    packet: object
    body: bytes


class NativeLobby:
    def __init__(self, service):
        self.service = service
        self._pending_self, self._actors, self._by_session = {}, {}, {}
        self._deliveries = {}
        self._appearance_bodies, self._appearance_pending = {}, {}
        self._profile_refresh_after = {}
        self._lock = RLock()
        self.self_profile = SentSelfProfile(self)

    def _valid(self, actor, *, town=False):
        session = actor.session
        cid, account, _record = self.service._selected_actor(session)
        if ((cid, account, getattr(session, 'selected_channel_id', None))
                != (actor.character_id, actor.account_id, actor.channel_id)
                or getattr(session, '_raid_selected_epoch', None) is not actor.epoch
                or getattr(session, '_raid_native_namespace', None) != LOCAL_NATIVE_NAMESPACE
                or town and (getattr(session, 'in_dungeon_run', False)
                             or not getattr(session, 'enter_seria_settled', False))):
            raise RaidError('native actor selection, namespace or town ownership expired')
        return actor

    def _own(self, session, *, town=True):
        actor = self._by_session.get(id(session))
        if actor is None or actor.session is not session:
            raise RaidError('actual selected USERINFO0 has not been sent to this connection')
        self._valid(actor, town=town)
        if self._actors.get((actor.channel_id, actor.character_id)) is not actor:
            raise RaidError('native actor UID is no longer owned by this selection')
        return actor

    def prepare_self(self, session, packet, body):
        """Only the real USERINFO0 producer calls this, before its send."""
        with self._lock:
            cid, account, _record = self.service._actor(session)
            channel = getattr(session, 'selected_channel_id', None)
            if channel not in (23, 32):
                return
            if (type(cid) is not int or not 1 <= cid <= 0xfffe or not isinstance(body, bytes)
                    or len(body) < 39 or body[:4] != b'\x00\x01\x00\x00'
                    or int.from_bytes(body[37:39], 'little') != cid):
                raise RaidError('actual native USERINFO0 does not identify the selected owned actor')
            if not hasattr(session, '_raid_selected_epoch'):
                session._raid_selected_epoch = object()
            actor = SelectedActor(session, cid, account, channel, session._raid_selected_epoch, body)
            previous = self._actors.get((channel, cid))
            if previous is not None and previous.session is not session:
                try:
                    self._valid(previous)
                except RaidError:
                    pass
                else:
                    raise RaidError('native UID is ambiguous between two live selected connections')
            self._pending_self[id(session)] = (actor, packet)
            self.self_profile.prepare(actor, packet, body, 'selected-userinfo0')

    def lookup(self, requester, actor_key, *, target_town=True):
        with self._lock:
            origin = self._own(requester)
            actor = self._actors.get((origin.channel_id, actor_key))
            if actor is None:
                raise RaidError('native actor key has no authenticated same-channel selected owner')
            if self._own(actor.session, town=target_town) is not actor or actor.channel_id != origin.channel_id:
                raise RaidError('native actor directory entry belongs to an expired channel selection')
            return actor

    def rebind_epoch(self, session):
        """Keep a real selected actor across a raid-lobby membership rebind.

        None is only for internal business callers with no native producer.
        A native producer's pending or expired proof must defer recovery.
        """
        with self._lock:
            pending = self._pending_self.get(id(session))
            if pending is not None and pending[0].session is session:
                raise RaidError('native reconnect awaits its exact selected actor body send')
            if id(session) not in self._by_session:
                return None
            return self._own(session).epoch

    def _body_for(self, actor):
        return self._appearance_bodies.get(actor, actor.body)

    def current_body(self, session):
        """Latest exact self-delivered model; SelectedActor.body is SELECT evidence."""
        with self._lock:
            return self._body_for(self._own(session, town=False))

    def prepare_appearance(self, session, packet, body):
        """Called by the actual town appearance producer, before its own send."""
        with self._lock:
            previous = self._by_session.get(id(session))
            if previous is None or previous.session is not session:
                return False
            self.service._actor(session)
            actor = self._own(session)
            if (packet is None or not isinstance(body, bytes) or len(body) < 39
                    or body[:4] != b'\x00\x01\x00\x00'
                    or int.from_bytes(body[37:39], 'little') != actor.character_id):
                raise RaidError('appearance must be the actual owned native USERINFO0')
            pending = self._appearance_pending.setdefault(id(session), [])
            for value in pending:
                if value.packet is packet:
                    if value.actor is not actor or value.body != body:
                        raise RaidError('ambiguous appearance packet identity')
                    return True
            pending.append(_Appearance(actor, packet, body))
            self.self_profile.prepare(actor, packet, body, 'appearance-userinfo0')
            return True

    def _commit_appearance(self, actor, body):
        if body == self._body_for(actor):
            return
        self._appearance_bodies[actor] = body
        for key, delivery in tuple(self._deliveries.items()):
            if delivery.subject is not actor:
                continue
            try:
                valid = self._own(delivery.recipient.session, town=False) is delivery.recipient
            except RaidError:
                valid = False
            if valid:
                self._deliveries[key] = _Delivery(delivery.recipient, actor, body)
            else:
                self._deliveries.pop(key, None)

    def queue_peer(self, recipient, subject):
        """Queue an intent without encoding inside another account's thread."""
        with self._lock:
            audience, actor = self._own(recipient), self._own(subject)
            if audience.channel_id != actor.channel_id:
                raise RaidError('native peer publication requires the same selected raid channel')
            if audience is actor:
                return None
            key = (audience, actor)
            delivery = self._deliveries.get(key)
            if delivery is None:
                delivery = self._deliveries[key] = _Delivery(audience, actor, self._body_for(actor))
            return delivery

    def _encode_delivery(self, delivery):
        cid, account, _record = self.service._actor(delivery.recipient.session)
        if (cid, account) != (delivery.recipient.character_id, delivery.recipient.account_id):
            raise RaidError('native peer encoding requires its own recipient account context')
        if delivery.packet is None:
            delivery.packet = self.service.server._codec_response(0, 2, delivery.body)
        return delivery.packet

    def publish_to(self, recipient, subject):
        """Owning recipient thread: queue and encode this full real body."""
        with self._lock:
            delivery = self.queue_peer(recipient, subject)
            return [] if delivery is None or delivery.sent else [self._encode_delivery(delivery)]

    def _delivery_valid(self, delivery, *, town=True):
        return (self._own(delivery.recipient.session, town=town) is delivery.recipient
                and self._own(delivery.subject.session, town=town) is delivery.subject
                and delivery.body is self._body_for(delivery.subject)
                and delivery.recipient.channel_id == delivery.subject.channel_id)

    def _recover_self_profile(self, session):
        """Recover a missing self receipt on its owning town connection."""
        if (getattr(session, 'selected_channel_id', None) not in (23, 32)
                or getattr(session, 'in_dungeon_run', False)
                or getattr(session, 'in_dungeon_select', False)):
            return []
        with self.service._lock, self._lock:
            key = id(session)
            actor = self._by_session.get(key)
            current = self.self_profile._values.get(key)
            if (actor is None or actor.session is not session
                    or (current is not None and current.actor is actor)
                    or key in self._pending_self or self._appearance_pending.get(key)):
                return []
            now = monotonic()
            previous = self._profile_refresh_after.get(key)
            if previous is not None and previous[0] is actor and now < previous[1]:
                return []
            try:
                self._own(session)
                cid, account, record = self.service._actor(session)
                entry = self.service.current(cid)
                if entry is None or (entry.domain.battle is None and entry.domain.member(cid).group == 0):
                    return []
                self.service.require_session(entry, session, cid, account)
            except RaidError:
                return []
            self._profile_refresh_after[key] = (actor, now + 2.0)

        from game.chain_loader import load_chain
        from game.character.native_profile_projection import encoded_profile, Userinfo0Projection, Userinfo1Projection
        server = self.service.server
        logger = getattr(server, 'log', None)
        # The town appearance producer supplies both exact packets from the
        # current character data. Attach its receipt only after the batch exists.
        try:
            packets, _ = load_chain('helper-userinfo_equipment.py').build_userinfo0_appearance_packets(
                server, character_id=cid, record=record, repo=server._repo(), include_visibility=False)
        except Exception as exc:
            if callable(logger):
                logger('[RaidProfile] cid=%s self model refresh deferred (%s)' % (cid, type(exc).__name__))
            return []
        models = [(packet, encoded_profile(packet)) for packet in packets]
        self_model = next(((packet, descriptor) for packet, descriptor in models
                           if descriptor is not None and type(descriptor.projection) is Userinfo0Projection
                           and descriptor.projection.character_id == cid), None)
        if self_model is None or not any(
                descriptor is not None and type(descriptor.projection) is Userinfo1Projection
                and descriptor.projection.character_id == cid for _, descriptor in models):
            return []
        with self.service._lock, self._lock:
            try:
                if (self._own(session) is not actor or self.service.current(cid) is not entry
                        or getattr(session, 'in_dungeon_select', False)):
                    return []
                current = self.self_profile._values.get(key)
                if current is not None and current.actor is actor:
                    return []
                self.service.require_session(entry, session, cid, account)
                packet, descriptor = self_model
                if not self.prepare_appearance(session, packet, descriptor.body):
                    return []
            except RaidError:
                return []
        if callable(logger):
            logger('[RaidProfile] cid=%s self model refresh queued on selected town connection' % cid)
        return packets

    def pending_packets(self, session):
        packets = self._recover_self_profile(session)
        with self._lock:
            for key, delivery in tuple(self._deliveries.items()):
                if delivery.recipient.session is not session:
                    continue
                try:
                    valid = self._delivery_valid(delivery, town=False)
                except RaidError:
                    valid = False
                if not valid:
                    self._deliveries.pop(key, None)
                elif not delivery.sent:
                    # Retain real selected-owner receipts across staggered
                    # dungeon entry. New town bodies still wait for both
                    # actual owners to be in town before encoding/sending.
                    try:
                        town_ready = self._delivery_valid(delivery)
                    except RaidError:
                        town_ready = False
                    if town_ready:
                        packets.append(self._encode_delivery(delivery))
        presence = getattr(self, '_town_presence', None)
        if presence is not None:
            packets.extend(presence.pending_packets(session))
        invites = getattr(self.service, 'squad_invites', None)
        if invites is not None:
            packets.extend(invites.pending_packets(session))
        return packets

    def visible(self, recipient, subject):
        with self._lock:
            try:
                audience, actor = self._own(recipient), self._own(subject)
                delivery = self._deliveries.get((audience, actor))
                return audience is actor or bool(delivery and delivery.sent and self._delivery_valid(delivery))
            except RaidError:
                return False

    def capture_profile_send(self, session, packet):
        return self.self_profile.capture_send(session, packet)

    def note_send(self, session, packet, profile_receipt=None):
        if packet is None:
            return
        with self._lock:
            pending = self._pending_self.get(id(session))
            if pending is not None and pending[0].session is session and pending[1] is packet:
                self.service._actor(session)
                actor = self._valid(pending[0])
                previous = self._actors.get((actor.channel_id, actor.character_id))
                if previous is not None and previous.session is not session:
                    try:
                        self._valid(previous)
                    except RaidError:
                        pass
                    else:
                        raise RaidError('native UID acquired a different live connection before send')
                self._actors[(actor.channel_id, actor.character_id)] = actor
                self._by_session[id(session)] = actor
                self._pending_self.pop(id(session), None)
                from .native_connection import bind_selected_actor
                bind_selected_actor(self.service.server, actor)
            appearances = self._appearance_pending.get(id(session), [])
            for value in tuple(appearances):
                if value.packet is not packet or value.actor.session is not session:
                    continue
                self.service._actor(session)
                try:
                    valid = self._own(session) is value.actor
                except RaidError:
                    valid = False
                if valid:
                    self._commit_appearance(value.actor, value.body)
                appearances.remove(value)
            if not appearances:
                self._appearance_pending.pop(id(session), None)
            for key, delivery in tuple(self._deliveries.items()):
                if delivery.recipient.session is session and delivery.packet is packet:
                    self.service._actor(session)
                    try:
                        valid = self._delivery_valid(delivery, town=False)
                    except RaidError:
                        valid = False
                    if valid:
                        delivery.sent = True
                    else:
                        self._deliveries.pop(key, None)
            self.self_profile.note_send(session, packet, profile_receipt)

        presence = getattr(self, '_town_presence', None)
        if presence is not None:
            presence.note_send(session, packet)
        returns = getattr(self, '_squad_returns', None)
        if returns is not None:
            returns.note_send(session, packet)

    def forget_session(self, session):
        from .native_connection import invalidate_selection
        invalidate_selection(self.service.server, session)
        with self._lock:
            self.self_profile.forget_session(session)
            self._appearance_pending.pop(id(session), None)
            self._profile_refresh_after.pop(id(session), None)
            for actor in tuple(self._appearance_bodies):
                if actor.session is session:
                    self._appearance_bodies.pop(actor, None)
            for key, actor in tuple(self._actors.items()):
                if actor.session is session:
                    self._actors.pop(key, None)
            actor = self._by_session.get(id(session))
            if actor is not None and actor.session is session:
                self._by_session.pop(id(session), None)
            pending = self._pending_self.get(id(session))
            if pending is not None and pending[0].session is session:
                self._pending_self.pop(id(session), None)
            for key, delivery in tuple(self._deliveries.items()):
                if session is delivery.recipient.session or session is delivery.subject.session:
                    self._deliveries.pop(key, None)
        presence = getattr(self, '_town_presence', None)
        if presence is not None:
            presence.forget_session(session)
        returns = getattr(self, '_squad_returns', None)
        if returns is not None:
            returns.forget(session)
