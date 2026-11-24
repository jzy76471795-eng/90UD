"""Original ordinary-party handlers behind authenticated relay preparation.

The controller performs HTTPS on its worker. Actor projection is warmed on the
original Game pump before taking the party lock; all original TCP messages are
still sent only by the owning Game connection thread.
"""
import struct,threading
from game.normal_party import NormalParties
from game.raid.runtime import RaidError
from login.party_relay_client import RelayUnavailable
from game.party_relay_controller import RelayController, RelayWarming
from game.normal_party_udp import UdpUnavailable
from game.normal_party_run import NormalPartyRuns

class RelayedRuns(NormalPartyRuns):
    def _start(self,body,session):
        dispatch=self.parties._dispatch
        previous=getattr(dispatch,'require_connected',False)
        dispatch.require_connected=True
        try:return super()._start(body,session)
        finally:dispatch.require_connected=previous

class RelayedParties(NormalParties):
    def __init__(self,server,controller):
        super().__init__(server)
        self.controller=controller;self._accepted={};self._profile_lock=threading.RLock();self._profiles={}
        self._dispatch=threading.local()
        self.runs=RelayedRuns(self)
        controller.add_provider(self.source_groups)

    def source_groups(self):
        with self._lock:
            groups=[]
            for party in {id(p):p for p in self._parties.values()}.values():
                members=tuple(party.members.values())
                if len(members)<2:continue
                try:enabled=self.controller.rollout.group(s[2] for s in members)
                except RelayUnavailable:continue # Invalid mixed membership gets no relay authority.
                if enabled:groups.append((('ordinary',id(party)),members))
            return tuple(groups)

    def _relay_mode(self,selections):
        try:return self.controller.rollout.group(s[2] for s in selections)
        except RelayUnavailable as exc:raise RaidError(str(exc)) from None

    def _owner(self,session,*,town):
        cached=getattr(self._dispatch,'owner',None)
        if town and cached is not None and cached[0].session is session:
            if not self._live(cached) or getattr(session,'in_dungeon_run',False) or getattr(session,'in_dungeon_select',False) or not getattr(session,'enter_seria_settled',False):
                raise RaidError('ordinary party town selection changed')
            return cached
        return super()._owner(session,town=town)

    def handle(self,opcode,body,session):
        # The existing ownership query runs before the global party lock.
        self._dispatch.owner=super()._owner(session,town=opcode!=13)
        try:return super().handle(opcode,body,session)
        finally:self._dispatch.owner=None

    def _warm_profile(self,owner):
        from game.town_social import service_for
        from game.character.native_profile_projection import userinfo0_projection
        now=self.clock()
        with self._profile_lock:
            row=self._profiles.get(owner)
            if row is not None and now-row[0]<.5:return
        projection=userinfo0_projection(service_for(self.server).build_body(owner))
        if projection is None or projection.character_id!=owner[4] or not 0<=projection.weakness<=100:
            raise RaidError('actual public actor projection unavailable')
        with self._profile_lock:
            self._profiles={key:value for key,value in self._profiles.items() if now-value[0]<3}
            if len(self._profiles)>=1024 and owner not in self._profiles:raise RaidError('relay profile cache capacity')
            self._profiles[owner]=(now,projection.weakness)

    def _peer_addresses(self,selections):
        from game.raid.native_network_protocol import PeerAddress,encode_peer_addresses
        from game.raid.native_udp import native_ipv4_word
        selected=tuple(selections)
        if not self._relay_mode(selected):return super()._peer_addresses(selected)
        try:
            if getattr(self._dispatch,'require_connected',False) and len(selected)>1:self.controller.group_proof(selected)
            endpoints=self.controller.endpoints(selected,invitation=getattr(self._dispatch,'invitation',False))
        except RelayWarming as exc:
            raise UdpUnavailable(selected, str(exc), relay=True) from None
        except RelayUnavailable as exc:raise RaidError(str(exc)) from None
        peers=[];now=self.clock()
        with self._profile_lock:
            for selection,(actor,ip,port) in zip(selected,endpoints):
                profile=self._profiles.get(selection)
                if profile is None or now-profile[0]>2:
                    if getattr(self._dispatch, 'invitation', False):
                        raise UdpUnavailable(selected, f'relay awaits fresh profile cid={selection[4]}', relay=True)
                    raise RaidError('relay awaits fresh public actor profile')
                address=native_ipv4_word(ip)
                # Conservative native payload budget. RLV2 separately fragments
                # opaque native datagrams up to8192 at the measured outer MTU.
                peers.append(PeerAddress(actor.actor,address,address,port,actor.key,3,512,profile[1]))
        return encode_peer_addresses(peers)

    def _invite_ready(self,owner,body):
        self._dispatch.invitation=True
        try:return super()._invite_ready(owner,body)
        finally:self._dispatch.invitation=False

    def _accept(self,owner,body):
        if not self.controller.rollout.contains(owner[2]):return super()._accept(owner,body)
        cid,mode,value=struct.unpack('<HBI',self._fixed(body,7))
        if value!=0:return super()._accept(owner,body)
        invite=self._invites.get(owner[0])
        if invite is None:
            for row in self._accepted.values():
                if row['owner']==owner and row['body']==body:return [],'relay accept already consumed'
            raise RaidError('relay accept has no delivered invitation')
        if invite.recipient!=owner or invite.sender[4]!=cid or invite.mode!=mode or not invite.delivered or self.clock()>=invite.expires:
            raise RaidError('relay accept has no delivered current invitation')
        self._peer(owner,cid)
        party=self.current(invite.leader[0].session)
        if party is not invite.party:raise RaidError('invited party changed before preparation')
        old=tuple(party.members.values()) if party is not None else (invite.leader,)
        selections=old+(invite.joiner,)
        try:identity=self.controller.begin(selections,old)
        except RelayUnavailable as exc:raise RaidError(str(exc)) from None
        self._invites.pop(owner[0],None)
        self._accepted[identity]={'owner':owner,'invite':invite,'body':bytes(body),'members':selections,'failed':False,'released':False}
        return [],'relay preparing before original native trigger'

    def _remove(self,selection,**kwargs):
        if self.controller.rollout.contains(selection[2]) and selection not in getattr(self._dispatch,'committing',()):self.controller.cancel_selection(selection,'party_leave_kick_or_selection_change')
        result=super()._remove(selection,**kwargs);self.controller._wake.set();return result

    def _advance(self,owner):
        packets=[]
        with self._lock:
            for identity,row in tuple(self._accepted.items()):
                state=self.controller.state(identity)
                if state['state']=='RETIRED' or (state['state']=='ABORTED' and row['failed']):
                    self._accepted.pop(identity,None);continue
                if state['state']=='ABORTED' and not any(self._live(s) for s in row['members']):
                    self.controller.cleanup_completed(identity);self._accepted.pop(identity,None);continue
                if state['state']=='ABORTED' and state['revoke_acked'] and not row['failed'] and owner in row['members']:
                    row['failed']=True;invite=row['invite']
                    if row['released']:
                        self._dispatch.committing=row['members']
                        targets=self.controller.cleanup_selections(identity) or (invite.joiner,)
                        try:
                            for target in targets:self._remove(target,refresh_peers=False)
                        finally:self._dispatch.committing=()
                    self.controller.cleanup_completed(identity)
                    # The existing system-message layout, not an acceptance
                    # notification with an ignored trailing error integer.
                    name=b'System';message=b'Party invitation cancelled. Please invite again.'
                    notice=bytes((0,0))+struct.pack('<I',len(name))+name+b'\0'+struct.pack('<I',len(message))+message
                    self._queue(invite.sender,0,0x41,notice)
                    self._queue(invite.recipient,1,11,b'\x00\x13'+bytes([invite.mode]))
                    self._accepted.pop(identity,None)
                elif row['owner']==owner and state['state']=='READY' and self.controller.release(identity,row['members']):
                    cid,mode,value=struct.unpack('<HBI',self._fixed(row['body'],7))
                    self._dispatch.committing=row['members']
                    try:
                        original,_=super()._accept_current(owner,cid,mode,value,row['invite'])
                        row['released']=True;packets.extend(original)
                    except Exception:
                        self.controller.cancel(identity,'original_accept_commit_failed');raise
                    finally:self._dispatch.committing=()
        return packets

    def forget(self,session):
        super().forget(session)
        with self._lock:
            for identity,row in tuple(self._accepted.items()):
                if all(s[0].session is session or not self._live(s) for s in row['members']):
                    self.controller.cleanup_completed(identity);self._accepted.pop(identity,None)

    def pump(self,session,client):
        try:owner=self._owner(session,town=False)
        except RaidError:return
        if owner[0].client is not client:raise RaidError('relay pump socket ownership mismatch')
        if self.controller.rollout.contains(owner[2]):
            try:self._warm_profile(owner)
            except Exception:pass # No readiness is granted from a failed projection.
            for packet in self._advance(owner):
                if not self._live(owner):return
                client.sendall(packet)
        return super().pump(session,client)

def install(server,control):
    if getattr(server,'normal_parties',None) is not None:raise RuntimeError('ordinary party service already owns active state')
    controller=RelayController(server,control)
    parties=RelayedParties(server,controller);server.normal_parties=parties
    server.party_relay_controller=controller;controller.start()
    return controller
