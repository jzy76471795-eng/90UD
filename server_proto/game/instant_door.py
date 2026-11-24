"""Authenticated Alt+arrow requests reuse the normal owned MOVE_MAP route.

A party shortcut requires a fresh, matching request from every living member.
It never manufactures native handshakes, peers, deaths, loot or scene ownership.
"""
from dataclasses import dataclass
from hmac import compare_digest
from time import monotonic
from uuid import uuid4
from control.contracts import ControlApiError
from game.raid.shared_world import SharedRunWorld

DIRECTIONS={'door-left':(-1,0),'door-right':(1,0),'door-up':(0,-1),'door-down':(0,1)}
ACTIONS=frozenset(DIRECTIONS)|{'door-cancel'}
LEASE_SECONDS=8.0
INTENT_SECONDS=8.0

def reject(code,message):raise ControlApiError(code,message,status=409)

@dataclass
class Lease:
    token:str
    fingerprint:tuple
    expires:float

class InstantDoor:
    def __init__(self,game,clock=monotonic):self.game,self.clock=game,clock

    def _run(self,session):
        raid=getattr(self.game,'raid_squad_runs',None)
        if raid is not None:
            run=raid._current(session)
            if run is not None:return raid,run
        party=getattr(self.game,'normal_parties',None)
        runs=getattr(party,'runs',None)
        if runs is not None:
            run=runs._current(session)
            if run is not None:return runs,run
        # An uncoordinated multi-player scene must never be treated as solo.
        if getattr(session,'selected_channel_id',None) in (23,32):
            service=getattr(self.game,'raid_service',None);directory=getattr(service,'ordinary_party',None)
            party=directory.current(session,auto=False) if directory is not None else None
        elif party is not None:party=party.current(session)
        if party is not None and len(party.members)>1:reject('DOOR_PARTY_NOT_READY','队伍房间尚未同步完成，请稍后再试')
        return None,None

    def _fingerprint(self,session,runs,run):
        keys=getattr(self.game,'native_game_connections',None)
        if keys is None:reject('DOOR_SESSION_NOT_READY','游戏连接尚未就绪')
        selection=keys.chat_selection(session)
        if selection is None or selection[4]!=session.selected_character_id:reject('DOOR_SESSION_NOT_READY','角色连接已变化')
        return (selection,getattr(session,'dungeon_run_generation',None),
                SharedRunWorld.room_key(session),getattr(session,'dungeon_seed',None),
                id(run) if run is not None else None,id(run.cycle) if run is not None else None)

    @staticmethod
    def _loaded(session):
        return bool(getattr(session,'in_dungeon_run',False) and getattr(session,'dungeon_finish_loading',False)
            and getattr(session,'selected_character_id',0) and int(getattr(session,'dungeon_clear_phase',0) or 0)==0
            and not getattr(session,'in_dungeon_select',False))

    def snapshot(self,session):
        if (not self._loaded(session) or getattr(session,'dungeon_death_pending',False)
                or self._unsupported(session)):
            session._instant_door_lease=None;return ''
        try:
            runs,run=self._run(session)
            if run is not None:
                with runs._metadata():
                    member=runs._member(run,session)
                    if (not runs._valid(run) or not runs._scene(run,member)
                            or run.cycle.projected!=runs._connections(run)):return ''
                    fingerprint=self._fingerprint(session,runs,run)
            else:fingerprint=self._fingerprint(session,None,None)
        except (ControlApiError,ValueError):return ''
        lease=getattr(session,'_instant_door_lease',None)
        if not isinstance(lease,Lease) or lease.fingerprint!=fingerprint:lease=Lease(uuid4().hex,fingerprint,0)
        lease.expires=self.clock()+LEASE_SECONDS;session._instant_door_lease=lease
        return lease.token

    @staticmethod
    def _clear(session):
        return all(bool(row.get('killed')) for row in getattr(session,'dungeon_room_mons',()) if row.get('is_blocking',True))

    @staticmethod
    def _unsupported(session):
        return bool(getattr(session,'altar_active',False) or getattr(session,'dungeon_tower_kind',None)
                    or getattr(session,'dungeon_layered_map_cell',None))

    def handle(self,params,session):
        action=params['action'];token=params['target'];now=self.clock()
        lease=getattr(session,'_instant_door_lease',None)
        if not isinstance(lease,Lease) or now>=lease.expires or not compare_digest(lease.token,token):
            reject('DOOR_TARGET_CHANGED','房间已变化，请松开方向键后重试')
        runs,run=self._run(session)
        if lease.fingerprint!=self._fingerprint(session,runs,run):reject('DOOR_TARGET_CHANGED','房间或队伍已变化，请松开方向键后重试')
        if action=='door-cancel':
            if run is not None:
                with runs._metadata():
                    row=getattr(run,'_instant_door_intents',None)
                    if row is not None:row.pop(lease.fingerprint[0][0],None)
            return [],{'state':'cancelled'},'instant door own intent cancelled'
        if not self._loaded(session) or getattr(session,'dungeon_death_pending',False):reject('DOOR_NOT_READY','请等待房间加载完成并保持存活')
        if not self._clear(session):reject('DOOR_ROOM_NOT_CLEAR','请先清理当前房间的怪物')
        if self._unsupported(session):reject('DOOR_UNSUPPORTED','当前副本请使用原来的过门方式')
        dx,dy=DIRECTIONS[action];room=SharedRunWorld.room_key(session);cell=(room[1]+dx,room[2]+dy)
        cells=getattr(session,'dungeon_required_cells',None)
        if (not 0<=cell[0]<=255 or not 0<=cell[1]<=255 or not cells or cell not in cells):
            reject('NO_ADJACENT_ROOM','这个方向没有相邻房间')
        if now<getattr(session,'_instant_door_next',0):reject('DOOR_COOLDOWN','过门请求过快，请松开方向键后重试')
        session._instant_door_next=now+0.35
        if run is not None:
            with runs._metadata():
                member=runs._member(run,session);cycle=run.cycle
                if (not runs._valid(run) or not runs._scene(run,member) or cycle.projected!=runs._connections(run)
                        or cycle.room!=room or lease.fingerprint!=self._fingerprint(session,runs,run)):
                    reject('DOOR_TARGET_CHANGED','队伍房间已变化，请稍后重试')
                if getattr(run,'_instant_door_cycle',None) is not cycle:
                    run._instant_door_cycle=cycle;run._instant_door_intents={};run._instant_door_consumed=False
                if run._instant_door_consumed:return [],{'state':'queued'},'instant door already queued'
                intents=run._instant_door_intents
                intents[member.selection[0]]=(cell,now+INTENT_SECONDS)
                alive=tuple(m for m in runs._members(run) if not getattr(m.session,'dungeon_death_pending',False))
                ready=sum(1 for m in alive if (value:=intents.get(m.selection[0])) is not None and value[0]==cell and value[1]>now)
                if ready!=len(alive) or not alive:
                    return [],{'state':'waiting','ready':ready,'required':len(alive)},'instant door awaits every living member'
                if not all(runs._scene(run,m) and self._clear(m.session) and SharedRunWorld.room_key(m.session)==room for m in runs._members(run)):
                    reject('DOOR_PARTY_NOT_READY','队伍房间尚未同步完成，请稍后再试')
                # Never overwrite or acknowledge a different native move.
                if run.move_intent is not None:
                    previous_member,previous_body,previous_cycle=run.move_intent
                    if previous_cycle is not cycle or bytes(previous_body)[:2]!=bytes(cell):
                        reject('DOOR_MOVE_REJECTED','已有过门请求正在处理，请稍后重试')
                run._instant_door_consumed=True
                # This existing coordinator route is metadata-only. Keep the
                # cycle check and intent submission under its own RLock; all
                # resource handlers and sends still run on recipient loops.
                try:
                    routed=runs.route(0x2d,bytes(cell),session)
                    if routed is None or run.move_intent is None:
                        reject('DOOR_MOVE_REJECTED','队伍过门未就绪，请重试')
                except BaseException:
                    run._instant_door_consumed=False;run._instant_door_intents.clear()
                    raise
                packets,note=routed
                return list(packets),{'state':'queued','direction':action[5:]},'instant door; '+note
        # Solo handlers use the same dynamic router as the Game connection.
        from game.packet_router import route_dynamic_game_packet
        result=route_dynamic_game_packet(self.game,flag=1,opcode=0x2d,plain=bytes(cell),session=session,
            create_character_userinfo_refresh_pending=False,charselect_0004_sent=True)
        if not result.handled:reject('DOOR_MOVE_REJECTED','本次过门未被游戏处理，请重试')
        if SharedRunWorld.room_key(session)[1:3]!=cell:reject('DOOR_MOVE_REJECTED','本次过门未完成，请使用原来的过门方式')
        session._instant_door_lease=None
        return list(result.response_packets),{'state':'moved','direction':action[5:]},'instant door; '+result.note
