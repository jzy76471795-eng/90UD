"""Game-owned relay authority. HTTP never runs under a party/directory lock.

Only actual delivered LOGIN selections can become BoundActor values. Providers
copy their current membership while holding their own short lock; this worker
then validates those copies, binds identities and publishes the complete plan.
"""
from dataclasses import dataclass,field
import secrets,threading,time,uuid
from login.party_relay_client import RelayUnavailable,RelayBindingRejected,RelayRollout

class RelayWarming(RelayUnavailable):
    """The current authenticated selection may become ready on a later pump."""


@dataclass(frozen=True)
class BoundActor:
    selection:tuple=field(compare=False,repr=False)
    token:tuple
    member:int
    boot:str
    account:int
    login:str
    actor:int
    key:int
    channel:int

    def binding(self):
        return {'account_id':self.account,'login_instance':self.login,'character_id':self.actor,'native_key':self.key,'channel_id':self.channel}

@dataclass
class RelayAttempt:
    id:str
    actors:tuple
    old_actors:tuple
    scope:int
    epoch:int
    deadline:float
    state:str='PREPARING'
    activated:bool=False
    reason:str=''
    view:int=0
    contexts:dict=field(default_factory=dict)
    paths:dict=field(default_factory=dict)
    native_deadline:float=0
    released:bool=False
    cleanup_done:bool=False
    revoke_acked:bool=False
    failed_members:tuple=()
    terminal_at:object=None

@dataclass(frozen=True)
class RelayGroup:
    actors:tuple
    scope:int
    epoch:int

@dataclass(frozen=True)
class RelayEvidence:
    at:float
    stamp:tuple

class RelayController:
    def __init__(self,server,control,*,clock=time.monotonic):
        self.server,self.control,self.clock=server,control,clock
        self.rollout=RelayRollout(getattr(getattr(control,'config',None),'rollout_accounts',None))
        self._lock=threading.RLock();self._providers=[];self._attempts={};self._groups={}
        self._bound={};self._rejected={};self._boot='';self._version=0;self._epoch=0
        self._status={};self._status_at=0;self._published={};self._published_at=0;self._view=0
        self._relay_evidence={}
        self._stopped=threading.Event();self._wake=threading.Event();self._thread=None
        self.last_error='';self.control_failures=0
        self.conflicted_members=();self.source_errors=0
        self.history_limit=512;self.history_ttl=60.;self.history_pruned=0

    def _prune_attempts(self,now):
        """Metadata only, caller holds _lock. Never discard required cleanup."""
        safe=[]
        for a in self._attempts.values():
            if a.state not in ('ABORTED','RETIRED'):continue
            if a.terminal_at is None:a.terminal_at=now
            if a.state=='RETIRED' or (a.state=='ABORTED' and (not a.released or (a.cleanup_done and a.revoke_acked))):safe.append(a)
        safe.sort(key=lambda a:a.terminal_at)
        excess=max(0,len(safe)-self.history_limit)
        for index,a in enumerate(safe):
            if index<excess or now-a.terminal_at>=self.history_ttl:
                self._attempts.pop(a.id,None);self.history_pruned+=1

    def add_provider(self,provider):
        with self._lock:self._providers.append(provider)

    def capture(self,selection):
        if not isinstance(selection,tuple) or len(selection)!=7:raise RelayUnavailable('invalid delivered LOGIN selection')
        directory=getattr(self.server,'native_game_connections',None)
        if directory is None:
            raise RelayUnavailable('current delivered LOGIN selection unavailable')
        try:
            if directory.chat_selection(selection[0].session)!=selection:raise RelayUnavailable('native selection changed')
            key=directory.key_for_selection(selection)
        except Exception:raise RelayUnavailable('current delivered LOGIN selection unavailable') from None
        connection=selection[0];identity=getattr(connection,'identity',None)
        account,channel,actor=selection[2:5]
        if not self.rollout.contains(account):raise RelayUnavailable('account outside party relay pilot')
        login=getattr(connection,'login_session_id',None)
        session=connection.session
        if identity is None or identity.account_id!=account or identity.login_session_id!=login or getattr(session,'login_session_id',None)!=login or not getattr(identity,'device_id',''):
            raise RelayUnavailable('authenticated native Game login required')
        mapped=self.control.binding_for(account,login)
        if mapped is None:raise RelayUnavailable('fresh launcher relay authorization required')
        if not all(type(v) is int and v>0 for v in (account,channel,actor,key)) or actor>=65535:
            raise RelayUnavailable('invalid actual native actor binding')
        token=(id(connection),login,selection[1],channel,actor,selection[5],id(selection[6]))
        return BoundActor(selection,token,mapped[0],mapped[1],account,login,actor,key,channel)

    def current(self,actor):
        try:return self.capture(actor.selection)==actor
        except Exception:return False

    def begin(self,selections,old_selections):
        actors=tuple(self.capture(s) for s in selections)
        old=tuple(self.capture(s) for s in old_selections)
        if not 2<=len(actors)<=4 or len({a.member for a in actors})!=len(actors) or len({a.channel for a in actors})!=1:
            raise RelayUnavailable('invalid same-channel relay preparation')
        with self._lock:
            if not self._boot or self.clock()-self._published_at>1.25:raise RelayUnavailable('relay authority not initialized')
            self._prune_attempts(self.clock())
            if len(self._attempts)>=4096:raise RelayUnavailable('relay attempt history bound')
            members={a.member for a in actors}
            if any(a.state in ('PREPARING','READY','CONNECTING') and members.intersection(x.member for x in a.actors) for a in self._attempts.values()):
                raise RelayUnavailable('a member is already preparing')
            self._epoch+=1
            attempt=RelayAttempt(uuid.uuid4().hex,actors,old,secrets.randbits(63)+1,self._epoch,self.clock()+6)
            self._attempts[attempt.id]=attempt
        self._wake.set();return attempt.id

    def cancel(self,attempt_id,reason):
        with self._lock:
            a=self._attempts.get(attempt_id)
            if a is not None and a.state!='ABORTED':a.state='ABORTED';a.reason=reason
        self._wake.set()

    def cancel_selection(self,selection,reason):
        with self._lock:
            ids=[a.id for a in self._attempts.values() if a.state in ('PREPARING','READY','CONNECTING') and any(x.selection==selection for x in a.actors)]
        for identity in ids:self.cancel(identity,reason)

    def state(self,attempt_id):
        with self._lock:
            a=self._attempts.get(attempt_id)
            if a is None:return {'id':attempt_id,'state':'RETIRED','reason':'history_expired','activated':False,'route_view':0,'deadline':0,'released':False,'revoke_acked':True,'failed_members':()}
            return {'id':a.id,'state':a.state,'reason':a.reason,'activated':a.activated,'route_view':a.view,'deadline':a.deadline,'released':a.released,'revoke_acked':a.revoke_acked,'failed_members':a.failed_members}

    def healthy(self):
        with self._lock:return bool(self._boot) and self.clock()-self._published_at<1.25

    def cleanup_completed(self,attempt_id):
        with self._lock:
            a=self._attempts.get(attempt_id)
            if a is not None:a.cleanup_done=True

    def cleanup_selections(self,attempt_id):
        with self._lock:
            a=self._attempts.get(attempt_id)
            return tuple(x.selection for x in a.actors if x.member in a.failed_members) if a is not None else ()

    def _game_selected(self,actor):
        try:return self.server.native_game_connections.chat_selection(actor.selection[0].session)==actor.selection
        except Exception:return False

    def group_proof(self,selections):
        """Cached bilateral native proof; never perform HTTP on a Game pump."""
        actors=tuple(sorted((self.capture(s) for s in selections),key=lambda a:a.member))
        key=tuple(a.member for a in actors)
        with self._lock:
            row=self._relay_evidence.get(key)
            if (row is None or self.clock()-row.at>1.25 or self.clock()-self._published_at>1.25
                    or row.stamp[0]!=self._boot or row.stamp[3]!=tuple((a.member,a.token) for a in actors)):
                raise RelayUnavailable('current bilateral native relay proof unavailable')
            return key,row.stamp

    def _evidence_snapshot(self,current,members,sessions):
        grouped={};active={r['id'] for r in members};result={}
        for row in members:grouped.setdefault((row['scope'],row['epoch']),[]).append(current[row['id']])
        for actor in current.values():
            if actor.member not in active:grouped[('solo',actor.member)]=[actor]
        now=self.clock()
        for route,values in grouped.items():
            actors=tuple(sorted(values,key=lambda a:a.member))
            if any(not self._native_ready(a,sessions.get(a.member,{}),installed=self._view) for a in actors):continue
            if len(actors)>1:
                connected=True
                for actor in actors:
                    peers={p['key']:p for p in (sessions[actor.member]['native'].get('peers') or ())}
                    if any(other is not actor and not (peers.get(other.key,{}).get('state')==0 and peers[other.key].get('incoming_confirmed') is True) for other in actors):connected=False
                if not connected:continue
            scope,epoch=route if route[0]!='solo' else (0,0)
            stamp=(self._boot,scope,epoch,tuple((a.member,a.token) for a in actors))
            result[tuple(a.member for a in actors)]=RelayEvidence(now,stamp)
        return result

    def _native_ready(self,actor,status,*,installed=None,context=None):
        if status.get('transport',{}).get('failed'):return False
        if status.get('closed') or not status.get('connected') or status.get('binding')!=actor.binding():return False
        now=int(time.time()*1000);at=status.get('native_report_at_ms',0)
        n=status.get('native',{})
        if not 0<=now-at<=1250 or n.get('character_id')!=actor.actor or not 0<n.get('socket',0)<0xffffffff or not 0<n.get('native_port',0)<=65535 or n.get('context_generation',0)<=0:return False
        if installed is not None and n.get('installed_route_view')!=installed:return False
        if context is not None and n.get('context_generation')!=context:return False
        verified=n.get('verified_sizes_age_ms') or {}
        return all(type(verified.get(str(size),verified.get(size))) is int and 0<=verified.get(str(size),verified.get(size))<=2000 for size in (64,512,8192))

    def endpoints(self,selections,*,invitation=False):
        actors=tuple(self.capture(s) for s in selections)
        with self._lock:
            if self.clock()-self._status_at>1.25 or self.clock()-self._published_at>1.25:raise RelayUnavailable('relay authority evidence expired')
            status=self._status;published=self._published;view=self._view
            for actor in actors:
                row = status.get(actor.member, {})
                if actor.boot != self._boot:
                    raise RelayUnavailable(f'relay authority changed for cid={actor.actor}')
                if self._native_ready(actor, row, installed=None if invitation else view):
                    continue
                native = row.get('native') or {}
                if row.get('closed') or row.get('transport', {}).get('failed'):
                    reason = 'closed or failed transport'
                elif row.get('binding') is not None and row['binding'] != actor.binding():
                    reason = 'selection binding mismatch'
                elif native.get('character_id') not in (None, 0, actor.actor):
                    reason = 'native character mismatch'
                else:
                    ages = native.get('verified_sizes_age_ms') or {}
                    missing = [str(size) for size in (64,512,8192)
                               if type(ages.get(str(size), ages.get(size))) is not int
                               or not 0 <= ages.get(str(size), ages.get(size)) <= 2000]
                    age = int(time.time()*1000) - (row.get('native_report_at_ms') or 0)
                    if not row.get('connected') or not row.get('binding'):
                        reason = 'awaiting connection or binding'
                    elif not 0 <= age <= 1250:
                        reason = 'native report missing or expired'
                    elif missing:
                        reason = 'awaiting probe sizes ' + ','.join(missing)
                    else:
                        reason = 'awaiting native socket, context or route'
                    if invitation:
                        raise RelayWarming(f'relay native capability not ready cid={actor.actor}: {reason}')
                raise RelayUnavailable(f'relay native capability not ready cid={actor.actor}: {reason}')
            if not invitation:
                rows=[published.get(a.member) for a in actors]
                if any(r is None or not r['active'] for r in rows) or len({(r['scope'],r['epoch']) for r in rows})!=1:raise RelayUnavailable('same-generation relay routes not installed')
                route=(rows[0]['scope'],rows[0]['epoch'])
                if {member for member,row in published.items() if row['active'] and (row['scope'],row['epoch'])==route}!={a.member for a in actors}:
                    raise RelayUnavailable('relay route membership does not match exact native party')
        return tuple((a,'198.%d.%d.%d'%(18+(a.member-1)//64516,1+(a.member-1)//254%254,1+(a.member-1)%254),31000) for a in actors)

    def release(self,attempt_id,selections):
        actors=tuple(self.capture(s) for s in selections)
        with self._lock:
            a=self._attempts.get(attempt_id)
            if a is None or a.state!='READY' or a.actors!=actors or self.clock()>=a.deadline:return False
            if self.clock()-self._status_at>1.25 or self.clock()-self._published_at>1.25:return False
            if any(not self._native_ready(x,self._status.get(x.member,{}),installed=self._view,context=a.contexts.get(x.member)) for x in actors):return False
            if any(a.paths.get(x.member)!=self._status.get(x.member,{}).get('transport',{}) for x in actors):return False
            a.state='CONNECTING';a.native_deadline=self.clock()+18;a.released=True
            return True

    def _snapshot_sources(self):
        with self._lock:providers=tuple(self._providers)
        # Providers must return only copied selections; no callback under our lock.
        rows=[];errors=0
        for provider in providers:
            try:rows.extend(tuple(provider()))
            except Exception:
                # Revoke the unavailable provider's scopes in this complete
                # snapshot. Other providers must retain their heartbeat.
                errors+=1
        desired={};owners={};conflicts=set()
        for row in rows:
            try:key,selections=row;hash(key);selections=tuple(selections)
            except Exception:errors+=1;continue
            try:
                actors=tuple(self.capture(selected) for selected in selections)
            except RelayUnavailable:
                # Keep the entire squad unpublished until every selected
                # owner has a current binding. A subset is a different party.
                continue
            if len(actors)<2:continue
            ids={a.member for a in actors}
            if len(actors)>4 or len(ids)!=len(actors) or len({a.channel for a in actors})!=1:
                conflicts.update(ids);continue
            if key in desired and desired[key]!=actors:
                conflicts.update(ids);conflicts.update(a.member for a in desired[key])
            for member in ids:
                if member in owners and owners[member]!=key:conflicts.add(member)
                owners[member]=key
            desired[key]=actors
        desired={key:actors for key,actors in desired.items() if not any(a.member in conflicts for a in actors)}
        with self._lock:self.source_errors+=errors
        return desired,conflicts

    def _read_status(self,members):
        # A bounded page prevents many logged-in solo users from making a
        # single status document exceed the private client's response cap.
        result=self.control.request('POST','/v1/control/status',{'member_ids':[]})
        result['sessions']=[]
        ordered=sorted(members)
        for offset in range(0,len(ordered),16):
            page=self.control.request('POST','/v1/control/status',{'member_ids':ordered[offset:offset+16]})
            if page['boot']!=result['boot'] or page['authority']['version']!=result['authority']['version']:
                raise RelayUnavailable('authority changed while reading status pages')
            result['sessions'].extend(page['sessions'])
        return result

    def cycle(self):
        desired,conflicts=self._snapshot_sources()
        with self._lock:attempts=tuple(self._attempts.values())
        candidates=[a for rows in desired.values() for a in rows]
        directory=getattr(self.server,'native_game_connections',None)
        for selection in directory.chat_selections() if directory is not None else ():
            try:
                actor=self.capture(selection)
                candidates.append(actor)
            except RelayUnavailable:continue
        for attempt in attempts:
            if attempt.state in ('ABORTED','RETIRED'):continue
            for actor in attempt.actors:
                if self.current(actor):candidates.append(actor)
        current={}
        for actor in candidates:
            if actor.member in current and current[actor.member]!=actor:conflicts.add(actor.member)
            else:current[actor.member]=actor
        current={member:actor for member,actor in current.items() if member not in conflicts}
        desired={key:actors for key,actors in desired.items() if not any(a.member in conflicts for a in actors)}
        with self._lock:self.conflicted_members=tuple(sorted(conflicts))
        status=self._read_status(current)
        boot=status['boot'];authority=status['authority']
        sessions={row['member_id']:row for row in status['sessions']}
        with self._lock:
            if self._boot and boot!=self._boot:
                for a in self._attempts.values():a.state='ABORTED';a.reason='relay_restarted'
                self._groups={};self._bound={};self._rejected={};self._published={}
            self._boot=boot;self._epoch=max(self._epoch,authority.get('max_epoch',0));self._version=max(self._version,authority['version'])
        # A dead/replaced edge cannot bind. Omit only that session instead of
        # repeatedly failing the global heartbeat for unrelated live teams.
        admissible={member for member,a in current.items() if a.boot==boot
            and not sessions.get(member,{}).get('closed',True)
            and not sessions.get(member,{}).get('transport',{}).get('failed',False)
            and sessions.get(member,{}).get('connected') is True
            and sessions[member].get('binding',{}).get('account_id')==a.account
            and sessions[member].get('binding',{}).get('login_instance')==a.login}
        self._rejected={member:token for member,token in self._rejected.items()
            if member in current and token==(boot,current[member].token)
            and sessions.get(member,{}).get('binding')!=current[member].binding()}
        changed=[a for a in current.values() if a.member in admissible
            and a.member not in self._rejected and self._bound.get(a.member)!=a.binding()]
        if changed:
            # Revoke the previous actor routes and obtain ACK before rebinding.
            removed={a.member for a in changed}
            with self._lock:
                retained=[r for member,r in self._published.items() if member not in removed]
                self._version+=1;version=self._version
            self.control.publish(boot,version,retained,[])
            for actor in changed:
                try:self.control.request('POST','/v1/control/bind',{'member_id':actor.member,'binding':actor.binding()})
                except RelayBindingRejected:
                    self._bound.pop(actor.member,None);self._rejected[actor.member]=(boot,actor.token)
                else:self._bound[actor.member]=actor.binding()
            status=self._read_status(current);sessions={r['member_id']:r for r in status['sessions']}
        now=self.clock()
        valid={member for member,a in current.items() if member in admissible
            and member not in self._rejected and self._bound.get(member)==a.binding()
            and sessions.get(member,{}).get('connected') is True
            and not sessions.get(member,{}).get('closed',True)
            and sessions[member].get('binding')==a.binding() and self.current(a)}
        game_selected={a.member for a in candidates+[x for attempt in attempts for x in attempt.actors]
            if self._game_selected(a)}
        with self._lock:
            for key,actors in tuple(desired.items()):
                actors=tuple(a for a in actors if a.member in valid)
                if len(actors)<2:desired.pop(key);continue
                prior=self._groups.get(key)
                if prior is None or prior.actors!=actors:
                    self._epoch+=1;prior=RelayGroup(actors,secrets.randbits(63)+1,self._epoch)
                desired[key]=prior
            self._groups=desired
            groups=list(desired.values());pending=[]
            for a in self._attempts.values():
                if a.state=='CONNECTED':
                    failed=tuple(x.member for x in a.actors if x.member not in valid and x.member in game_selected)
                    if failed:
                        a.state='ABORTED';a.reason='authoritative_source_conflict' if conflicts.intersection(failed) else 'authenticated_relay_session_lost';a.failed_members=failed
                if a.state=='CONNECTED' and not any(set(g.actors)==set(a.actors) for g in groups):
                    a.state='RETIRED'
                if a.state=='RETIRED':continue
                if a.state in ('PREPARING','READY') and now>=a.deadline:a.state='ABORTED';a.reason='prepare_timeout'
                if a.state=='CONNECTING' and now>=a.native_deadline:a.state='ABORTED';a.reason='native_cleanup_watchdog'
                if a.state in ('PREPARING','READY','CONNECTING') and any(x.member not in valid for x in a.actors):a.state='ABORTED';a.reason='actual_game_selection_changed'
                if a.state=='ABORTED':
                    if not any(x.member in game_selected for x in a.actors):a.cleanup_done=True
                    # Post-release cleanup removes the joiner on the owning pump;
                    # until then it must not remain authorized by the old roster.
                    if a.released and not a.cleanup_done:
                        new=set(a.failed_members) or ({x.member for x in a.actors}-{x.member for x in a.old_actors})
                        groups=[RelayGroup(tuple(x for x in g.actors if x.member not in new),g.scope,g.epoch) for g in groups]
                    continue
                ids={x.member for x in a.actors}
                overlap=[g for g in groups if ids.intersection(x.member for x in g.actors)]
                if any(not {x.member for x in g.actors}.issubset(ids) for g in overlap):a.state='ABORTED';a.reason='party_membership_changed';continue
                ready=all(self._native_ready(x,sessions.get(x.member,{})) for x in a.actors)
                if a.state=='PREPARING' and not a.activated and ready:
                    # Adopt a fresh epoch after reading relay tombstones.
                    self._epoch+=1;a.epoch=self._epoch;a.activated=True
                    a.contexts={x.member:sessions[x.member]['native']['context_generation'] for x in a.actors}
                if a.activated and a.state in ('PREPARING','READY','CONNECTING','CONNECTED'):
                    if a.state in ('READY','CONNECTING') and any(a.paths.get(x.member)!=sessions.get(x.member,{}).get('transport',{}) for x in a.actors):
                        a.state='ABORTED';a.reason='attempt_transport_changed';continue
                    if a.state!='CONNECTED' and any(not self._native_ready(x,sessions.get(x.member,{}),context=a.contexts.get(x.member)) for x in a.actors):
                        a.state='ABORTED';a.reason='native_context_or_capability_changed';continue
                    groups=[g for g in groups if not ids.intersection(x.member for x in g.actors)]
                    groups.append(RelayGroup(a.actors,a.scope,a.epoch))
                elif a.state=='PREPARING':
                    pending.extend({'id':x.member,'scope':a.scope,'epoch':a.epoch,'active':False} for x in a.actors)
            members=[]
            for group in groups:
                if len(group.actors)<2:continue
                members.extend({'id':x.member,'scope':group.scope,'epoch':group.epoch,'active':True} for x in group.actors)
            members.sort(key=lambda r:r['id']);pending.sort(key=lambda r:r['id'])
            self._version+=1;version=self._version
        ack=self.control.publish(boot,version,members,pending)
        with self._lock:
            self._published={r['id']:r for r in members};self._published_at=self.clock();self._view=ack['route_view']
            self._status=sessions;self._status_at=self.clock()
            self._relay_evidence=self._evidence_snapshot({member:a for member,a in current.items() if member in valid},members,sessions)
            for a in self._attempts.values():
                if a.state=='ABORTED':
                    a.revoke_acked=not any(r['scope']==a.scope and r['epoch']==a.epoch for r in members)
                if not a.activated or a.state not in ('PREPARING','READY','CONNECTING'):continue
                installed=all(self._native_ready(x,sessions.get(x.member,{}),installed=self._view,context=a.contexts.get(x.member)) for x in a.actors)
                if a.state=='PREPARING' and installed:
                    a.state='READY';a.view=self._view
                    a.paths={x.member:dict(sessions[x.member].get('transport',{})) for x in a.actors}
                if a.state=='CONNECTING' and installed:
                    connected=True
                    for actor in a.actors:
                        peers={p['key']:p for p in (sessions[actor.member]['native'].get('peers') or ())}
                        for other in a.actors:
                            if other is not actor and not (peers.get(other.key,{}).get('state')==0 and peers[other.key].get('incoming_confirmed') is True):connected=False
                    if connected:a.state='CONNECTED'
            self._prune_attempts(self.clock())

    def start(self):
        if self._thread is not None:raise RuntimeError('relay authority already started')
        def run():
            while not self._stopped.is_set():
                try:self.cycle()
                except Exception as exc:
                    # Do not renew cached authority on any source/control failure.
                    # DLL lease and relay heartbeat expire independently.
                    with self._lock:self.last_error=type(exc).__name__;self.control_failures+=1
                self._wake.wait(.2);self._wake.clear()
        self._thread=threading.Thread(target=run,name='party-relay-authority',daemon=True);self._thread.start()

    def stop(self):
        self._stopped.set();self._wake.set()
        if self._thread is not None:self._thread.join(2)
