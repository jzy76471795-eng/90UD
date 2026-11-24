"""Native player trade/friends, using authenticated recipient-owned delivery.

US DFO 14b85f7e: C000A/000B mode1, C0013 list4, C0018;
N0007/0008, N000F 108-byte item, N0010/0011/0012.
Friends C0122/0123 and N0111 use the US readers, not CN row layouts.
"""
from collections import deque
from dataclasses import dataclass,field
from datetime import datetime,timezone
from threading import RLock
from types import SimpleNamespace
import sqlite3
import struct
import time
import uuid

from game.player_exchange_store import ExchangeStore,ExchangeError
from game.raid.runtime import RaidError
from game.native_chat import _string,_end,dstring

_LOCK=RLock()


@dataclass(eq=False)
class Invitation:
    sender:tuple
    recipient:tuple
    expires:float
    delivered:bool=False


@dataclass(eq=False)
class Trade:
    owners:tuple
    trade_id:str=field(default_factory=lambda:uuid.uuid4().hex)
    offers:dict=field(default_factory=dict)
    gold:dict=field(default_factory=dict)
    states:dict=field(default_factory=dict)
    ready:set=field(default_factory=set)
    active:bool=True
    completed:bool=False
    expires:float=0


class PlayerExchange:
    def __init__(self,server):
        self.server=server
        self.store=ExchangeStore(server._repo().db_path)
        self.lock=RLock();self.clock=time.monotonic
        self.invites={};self.trades={};self.queues={};self.friend_views={}

    def directory(self):return self.server.native_game_connections

    def live(self,owner):
        try:return self.directory().chat_selection(owner[0].session)==owner
        except (RaidError,ValueError):return False

    def owned(self,session):
        owner=self.directory().chat_selection(session,sender=True)
        self.store.character(owner[2],owner[4])
        return owner

    def peers(self,first,second):
        if first==second or first[4]==second[4] or not self.live(first) or not self.live(second):
            return False
        if first[3] in (23,32) or second[3] in (23,32):
            raid=getattr(self.server,'raid_service',None)
            lobby=getattr(raid,'native_lobby',None)
            presence=getattr(lobby,'_town_presence',None)
            return bool(first[3]==second[3] and presence is not None
                and presence.visible(first[0].session,second[0].session))
        from game.town_social import scope,service_for
        social=service_for(self.server)
        return (first!=second and first[4]!=second[4] and self.live(first) and self.live(second)
                and scope(first[0].session) is not None
                and scope(first[0].session)==scope(second[0].session)
                and social.visible(first,second) and social.visible(second,first))

    def peer(self,owner,cid):
        rows=[s for s in self.directory().chat_selections() if s[3]==owner[3] and s[4]==cid]
        if len(rows)!=1 or not self.peers(owner,rows[0]):raise ExchangeError('trade peer is not a mutually visible town character')
        self.store.character(rows[0][2],cid)
        return rows[0]

    def queue(self,owner,opcode,body,*,context=None,refresh=False):
        self.queues.setdefault(owner[0],deque()).append((owner,opcode,body,context,refresh))

    def ack(self,opcode,body=b''):return self.server._codec_response(1,opcode,b'\1'+body)

    def busy(self,owner):
        return owner[0] in self.trades or any(owner in (i.sender,i.recipient) for i in self.invites.values())

    def character_values(self,owner):
        row=self.store.character(owner[2],owner[4]);created=datetime.fromisoformat(row['created_at'])
        if created.tzinfo is None:created=created.replace(tzinfo=timezone.utc)
        stamp=int(created.timestamp());gold=self.store.wallet(owner[2],owner[4])
        if not 0<stamp<0x7fffffff or not 0<=gold<0x7fffffff:raise ExchangeError('trade character values out of native range')
        return gold,stamp

    def invite(self,owner,body):
        from game.normal_party import NormalParties
        # The dedicated trade button writes eleven bytes. The general peer
        # action also includes a two-byte display field before its zero tail.
        raw = bytes(body)
        if len(raw) == 11 or len(raw) == 16 and not any(raw[7:]):
            cid, mode, _, tail = struct.unpack('<HBII', NormalParties._fixed(raw, 11))
        else:
            cid, mode, _, _, tail = struct.unpack('<HBIHI', NormalParties._fixed(raw, 13))
        if mode!=1:raise ExchangeError('not an item-trade invitation')
        if tail!=0:raise ExchangeError('unconfirmed item-trade invitation tail')
        target=self.peer(owner,cid)
        if self.busy(owner) or self.busy(target):raise ExchangeError('trade participant is busy')
        gold,created=self.character_values(owner)
        invite=Invitation(owner,target,self.clock()+30);self.invites[target[0]]=invite
        self.queue(target,7,struct.pack('<HBII',owner[4],1,gold,created),context=invite)
        return [self.ack(10)],'player trade invitation queued'

    def accept(self,owner,body):
        from game.normal_party import NormalParties
        cid,mode,reason=struct.unpack('<HBI',NormalParties._fixed(body,7))
        invite=self.invites.get(owner[0])
        if (mode!=1 or invite is None or invite.recipient!=owner or invite.sender[4]!=cid
                or not invite.delivered or self.clock()>=invite.expires):raise ExchangeError('no current delivered trade invitation')
        self.invites.pop(owner[0],None)
        if reason:
            self.queue(invite.sender,-10,b'\0\x13\1')
            return [],'player trade invitation declined'
        try:
            peer=self.peer(owner,cid)
            gold,created=self.character_values(owner)
        except (ExchangeError,RaidError,ValueError,sqlite3.Error):
            self.queue(invite.sender,-10,b'\0\x13\1')
            raise
        trade=Trade((peer,owner),expires=self.clock()+300)
        for target in trade.owners:
            if target[0] in self.trades:raise ExchangeError('trade participant became busy')
        for target in trade.owners:
            self.trades[target[0]]=trade;trade.offers[target[4]]={};trade.gold[target[4]]=0;trade.states[target[4]]=0
        self.queue(peer,8,struct.pack('<HBIBI',owner[4],1,gold,0,created),context=trade)
        return [self.ack(11,struct.pack('<HBB',cid,1,0))],'player trade opened'

    @staticmethod
    def cancel_body(trade,owner):
        rows=[];cid=owner[4]
        if trade.gold[cid]:rows.append(struct.pack('<HBH',0,0,0))
        for offer in trade.offers[cid].values():rows.append(struct.pack('<HBH',offer['slot'],0,offer['row']['slot_index']))
        return struct.pack('<H',len(rows))+b''.join(rows)

    def cancel(self,trade):
        if not trade.active:return
        trade.active=False
        for owner in trade.owners:
            if self.trades.get(owner[0]) is trade:self.trades.pop(owner[0],None)
            if self.live(owner):self.queue(owner,16,self.cancel_body(trade,owner),refresh=True)

    def prune(self):
        for key,invite in tuple(self.invites.items()):
            if not self.live(invite.sender) or not self.live(invite.recipient) or self.clock()>=invite.expires or not self.peers(invite.sender,invite.recipient):
                self.invites.pop(key,None)
                if self.live(invite.sender):self.queue(invite.sender,-10,b'\0\x13\1')
        for trade in set(self.trades.values()):
            if self.clock()>=trade.expires or not self.peers(*trade.owners):self.cancel(trade)

    def current(self,owner):
        trade=self.trades.get(owner[0])
        if trade is None or owner not in trade.owners or not trade.active or not self.peers(*trade.owners):raise ExchangeError('no current town trade')
        if len(trade.ready)!=2:raise ExchangeError('trade windows are still opening')
        trade.expires=self.clock()+300
        return trade

    def entry(self,owner,offer):
        from game.character.payloads import _pack_common_inventory_entry108
        items=self.server._repo().list_inventory_items(owner[4],list_type=0)
        item=next((x for x in items if int(x.slot_index)==offer['row']['slot_index']),None)
        if item is None:raise ExchangeError('offered inventory projection missing')
        body=bytearray(_pack_common_inventory_entry108(item));struct.pack_into('<H',body,0,offer['slot'])
        if offer['row']['item_kind']=='stackable':struct.pack_into('<I',body,6,offer['count'])
        return bytes(body)

    def move(self,owner,body):
        from game.normal_party import NormalParties
        # Same C0013 writer used by normal inventory: B h i i B h i.
        raw=NormalParties._fixed(body,18)
        src,slot,instance,count,dst,dest,_=struct.unpack('<Bhi i Bhi',raw)
        trade=self.current(owner);cid=owner[4];peer=next(p for p in trade.owners if p!=owner)
        if trade.states[cid] or any(v==3 for v in trade.states.values()):raise ExchangeError('offering is locked')
        if src==0 and dst==4 and slot==dest==0:
            total=trade.gold[cid]+count
            if not 0<count<=total<=self.store.wallet(owner[2],cid):raise ExchangeError('invalid gold offer')
            trade.gold[cid]=total;entry=struct.pack('<HII',0,0,total)+bytes(98)
        elif src==4 and dst==0 and slot==dest==0:
            if count!=trade.gold[cid] or count<=0:raise ExchangeError('invalid gold withdrawal')
            trade.gold[cid]=0;entry=struct.pack('<Hi',0,-1)+bytes(102)
        elif src==0 and dst==4 and 3<=dest<=11:
            if dest in trade.offers[cid]:raise ExchangeError('offer slot occupied')
            row=self.store.item(owner[2],cid,slot,count)
            if any(o['row']['item_uid']==row['item_uid'] for o in trade.offers[cid].values()):raise ExchangeError('item already offered')
            from game.character.payloads import wire_count_for_inventory_item
            expected=wire_count_for_inventory_item(SimpleNamespace(**row))
            if instance!=expected:raise ExchangeError('inventory instance mismatch')
            offer=dict(row=row,count=count,slot=dest);entry=self.entry(owner,offer);trade.offers[cid][dest]=offer
        elif src==4 and dst==0 and slot in trade.offers[cid]:
            offer=trade.offers[cid][slot]
            if dest!=offer['row']['slot_index'] or count!=offer['count']:raise ExchangeError('invalid offer withdrawal')
            trade.offers[cid].pop(slot);entry=struct.pack('<Hi',slot,-1)+bytes(102)
        else:raise ExchangeError('unsupported trade move')
        self.queue(peer,15,entry,context=trade)
        return [self.ack(19,struct.pack('<BhiBh',src,slot,count,dst,dest))],'player trade offer updated'

    def state(self,owner,body):
        from game.normal_party import NormalParties
        value=NormalParties._fixed(body,1)[0];trade=self.current(owner);cid=owner[4]
        peer=next(p for p in trade.owners if p!=owner);other=trade.states[peer[4]];own=trade.states[cid]
        # US 028481F0 sends 3 on final consent; 02848220/027E1B50 send 2
        # on cancel/close. 01B7BF50 sends 0/1; 01B7B2A0 sends adding-done 5.
        if value==2:
            self.cancel(trade);return [self.ack(24)],'player trade cancelled'
        if value in (0,1,5):
            if value==1 and (own not in (5,1) or other not in (5,1)):
                raise ExchangeError('both participants must finish adding before locking')
            if value==5 and own not in (0,1):raise ExchangeError('invalid adding-complete transition')
            if value==0:
                # Revoke both final consents whenever either participant unlocks.
                for target in trade.owners:
                    trade.states[target[4]]=0
                    for receiver in trade.owners:self.queue(receiver,17,struct.pack('<HB',target[4],0),context=trade)
            else:
                # C0018/5 is an adding-complete REQUEST, not a server state.
                # Approve it as N0011/1: 01B7AF80 opens the final consent UI
                # only when both sides are 1. Echoing 5 leaves a separate
                # client authorization gate unset and its next button disabled.
                if own==1:return [self.ack(24)],'player trade already locked'
                trade.states[cid]=1
                for target in trade.owners:self.queue(target,17,struct.pack('<HB',cid,1),context=trade)
        elif value==3:
            if own!=1 or other not in (1,3):raise ExchangeError('trade needs two locks and explicit final consent')
            trade.states[cid]=3
            if other==3:
                try:
                    result=self.store.commit(trade.trade_id,tuple((p[2],p[4]) for p in trade.owners),
                        {p[4]:list(trade.offers[p[4]].values()) for p in trade.owners},trade.gold)
                except Exception:
                    self.cancel(trade);raise
                trade.active=False;trade.completed=True
                for target in trade.owners:
                    tid=target[4];self.trades.pop(target[0],None)
                    rows=[struct.pack('<HH',x['slot'],x['destination']) for x in result['transfers'][tid]]
                    source=next(p[4] for p in trade.owners if p!=target)
                    if trade.gold[source]:rows.insert(0,struct.pack('<HH',0,0))
                    self.queue(target,18,struct.pack('<H',len(rows))+b''.join(rows),refresh=True)
        else:raise ExchangeError('unsupported trade confirmation state')
        return [self.ack(24)],'player trade confirmation processed'

    def friend_body(self,owner):
        rows=self.store.friends(owner[2],owner[4]);parts=[]
        online={p[4]:p for p in self.directory().chat_selections()}
        for row in rows:
            peer=online.get(row['character_id']);channel=peer[3] if peer is not None and peer[2]==row['account_id'] else 0
            name=bytes(row['name_bytes'] or row['name'].encode('utf8'))
            parts.append(struct.pack('<BH',0,channel)+dstring(name)+struct.pack('<BIBBB',row['level'],row['job'],row['grow_type']&255,0,0))
        return struct.pack('<II',0,len(parts))+b''.join(parts)

    def friend(self,owner,opcode,raw):
        raw=bytes(raw);start=2 if opcode==290 else 0
        if len(raw)<start+5 or raw[start]!=0:raise ExchangeError('unsupported friend server group')
        name,end=_string(raw,start+1,120);_end(raw,end)
        try:text=name.decode('utf8')
        except UnicodeDecodeError:text=name.decode('cp936')
        if opcode==290:
            cid=struct.unpack_from('<H',raw)[0];target=self.store.find_character(text)
            if cid not in (0,65535,target['character_id']):raise ExchangeError('friend name and actor disagree')
            self.store.add_friend(owner[2],owner[4],target['character_id'])
        else:self.store.remove_friend(owner[2],owner[4],text)
        self.friend_views.pop(owner[0],None)
        self.queue(owner,273,self.friend_body(owner))
        return [self.ack(opcode)],'player friend relationship saved'

    def refresh(self,owner):
        from game.character.payloads import build_item_list_mode0
        repo=self.server._repo();rows,param=repo.build_itemlist_mode0_rows(owner[4],list_type=0)
        return self.server._codec_response(0,13,build_item_list_mode0(rows,list_param16=param))

    def pump(self,session,client):
        try:owner=self.directory().chat_selection(session,sender=True)
        except RaidError:return
        if owner[0].client is not client or self.server.account_id!=owner[2]:return
        with self.lock:
            self.prune();packets=self.queues.pop(owner[0],deque())
        try:
            for selected,opcode,body,context,refresh in packets:
                with self.lock:
                    if selected!=owner or not self.live(owner):continue
                    if isinstance(context,Trade) and not (context.active or context.completed):continue
                    if isinstance(context,Invitation) and self.invites.get(context.recipient[0]) is not context:continue
                client.sendall(self.server._codec_response(1 if opcode<0 else 0,abs(opcode),body))
                with self.lock:
                    if isinstance(context,Invitation):context.delivered=True
                if refresh:client.sendall(self.refresh(owner))
            with self.lock:
                trade=self.trades.get(owner[0])
                if trade is not None and owner in trade.owners:trade.ready.add(owner[4])
                previous=self.friend_views.get(owner[0]);now=self.clock()
                if previous is not None and previous[0]==owner and now-previous[1]<3:return
                body=self.friend_body(owner)
                if previous is None or previous[0]!=owner or previous[2]!=body:
                    client.sendall(self.server._codec_response(0,273,body))
                self.friend_views[owner[0]]=(owner,now,body)
        except OSError:
            self.forget(session);raise

    def forget(self,session):
        with self.lock:
            for key,invite in tuple(self.invites.items()):
                if session in (invite.sender[0].session,invite.recipient[0].session):
                    self.invites.pop(key,None)
                    if invite.sender[0].session is not session:self.queue(invite.sender,-10,b'\0\x13\1')
            for trade in set(self.trades.values()):
                if any(p[0].session is session for p in trade.owners):self.cancel(trade)
            for key in tuple(self.friend_views):
                if key.session is session:self.friend_views.pop(key,None)
            for key in tuple(self.queues):
                if key.session is session:self.queues.pop(key,None)


def service_for(server):
    with _LOCK:
        service=getattr(server,'player_exchange',None)
        if service is None:
            service=PlayerExchange(server);server.player_exchange=service
        return service


def route(server,opcode,body,session):
    peer_mode=opcode in (10,11) and len(body)>=3 and body[2]==1
    move=opcode==19 and len(body)>=12 and (body[0]==4 or body[11]==4)
    if not(peer_mode or move or opcode in (24,290,291)):return None
    try:
        service=service_for(server)
        with service.lock:
            service.prune();owner=service.owned(session)
            if opcode==10:return service.invite(owner,body)
            if opcode==11:return service.accept(owner,body)
            if opcode==19:return service.move(owner,body)
            if opcode==24:return service.state(owner,body)
            return service.friend(owner,opcode,body)
    except (ExchangeError,RaidError,ValueError,sqlite3.Error) as exc:
        suffix=b'\1' if peer_mode else (bytes((body[0],body[11])) if move else b'')
        note='player exchange rejected: '+str(exc)
        logger=getattr(server,'log',None)
        if callable(logger):logger('[PlayerExchange] opcode=%04X cid=%s %s'%(opcode,getattr(session,'selected_character_id',None),note))
        code=0x15 if opcode in (290,291) else 0x13
        return [server._codec_response(1,opcode,bytes((0,code))+suffix)],note


def pump(server,session,client):
    if not getattr(session,'enter_seria_settled',False):return
    if not getattr(server._repo(),'db_path',None):return
    try:service_for(server).pump(session,client)
    except sqlite3.Error as exc:
        logger=getattr(server,'log',None)
        if callable(logger):logger('[PlayerExchange] database unavailable: '+str(exc))


def forget(server,session):
    service=getattr(server,'player_exchange',None)
    if service is not None:service.forget(session)
