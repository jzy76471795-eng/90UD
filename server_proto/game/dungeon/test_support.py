"""GM-only use of the frozen client's actual thirty-second God potion.

DFO14b85f7e: item2600027 -> 01A7DE50 / 01F3ACE0 / 01F3AD30
-> appendageD8 / 010C3F70 / 01C389D0; removal010C3F20 reverses it.
The ordinary USE_STACKABLE ACK invokes item-use on the client game thread.
No client memory, native code, PVF, character stats or schema are changed.
"""
import struct
from time import monotonic
from control.contracts import ControlApiError

GOD_POTION=2600027


class DungeonTestCommands:
    def __init__(self,server):
        self.server=server
        self.clock=monotonic

    def invincibility(self,params,session):
        if params:
            raise ControlApiError('INVALID_PARAMS','test invincibility takes no parameters',status=400)
        cid=getattr(session,'selected_character_id',None)
        if type(cid) is not int or cid<=0 or not getattr(session,'in_dungeon_run',False):
            raise ControlApiError('NOT_IN_DUNGEON','current client is not in a dungeon')
        if not getattr(session,'dungeon_finish_loading',False):
            raise ControlApiError('DUNGEON_LOADING','dungeon is still loading')
        if getattr(session,'dungeon_death_pending',False):
            raise ControlApiError('CHARACTER_DEAD','revive before using test invincibility')
        now=self.clock()
        key=(cid,getattr(session,'account_id',None),getattr(session,'dungeon_run_generation',None),
             getattr(session,'dungeon_map_index',None),getattr(session,'dungeon_cell_x',None),getattr(session,'dungeon_cell_y',None))
        previous=getattr(session,'_test_god_potion_use',None)
        if previous and previous[0]==key and now-previous[1]<25:
            return [],dict(renewed=False,duration_seconds=30,remaining_seconds=max(0,30-int(now-previous[1]))),'test God potion renewal not due'
        repo=self.server._repo()
        def find():
            return next((row for row in repo.list_inventory_items(cid,list_type=0)
                         if int(row['item_template_id'])==GOD_POTION and int(row['stack_count'])>0),None)
        packets=[]
        row=find()
        if row is None:
            grant,_result,_note=self.server.gm_item_commands.grant_item(dict(item_id=GOD_POTION,count=10),session)
            packets.extend(grant)
            row=find()
        if row is None:
            raise ControlApiError('ITEM_GRANT_FAILED','test potion was not available after grant')
        slot=int(row['slot_index'])
        body=struct.pack('<hBiiI',slot,0,0,GOD_POTION,0)
        used,note=self.server._handle_use_stackable_002c(body,session=session)
        packets.extend(used)
        # Grant and consume retain their existing separate atomic transactions.
        # If use rejects, still deliver the already-committed grant projection.
        if 'OK removed=1' not in note:
            return packets,dict(renewed=False,duration_seconds=0,error='ordinary test potion use was rejected'),note
        session._test_god_potion_use=(key,now)
        return packets,dict(renewed=True,duration_seconds=30,remaining_seconds=30,item_id=GOD_POTION,
                            consumed=1,character_id=cid),'test native God potion used through ordinary inventory and ACK'
