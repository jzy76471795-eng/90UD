"""Advance authored clear tasks at the final room of a licensed no-result run.

Uses warmed DGN flags/cells/MAP pools and existing quest progress packets.
Does not synthesize ordinary results, quest completion rewards, or a new wire.
"""
from game.quest.quest_index import get_quest_index
from game.quest.quest_clear import DungeonClearContext, apply_dungeon_clear_to_active_quests
from game.quest.quest_session import bind_session_quest_state, pack_set_trigger_ack_packets


def persist_licensed_no_result_clear(server, session):
    if session is None or not getattr(session, 'in_dungeon_run', False) or getattr(session, 'another_arad_active', False):
        return [], 'no_result_clear=skip_state'
    did=int(getattr(session,'cur_dungeon_id',0) or getattr(session,'selected_dungeon_id',0) or 0)
    ix=get_quest_index();dungeon=ix.get_dungeon(did)
    if not (dungeon.license_dungeon and dungeon.no_experience_monster_and_result):
        return [], 'no_result_clear=not_applicable'
    if int(getattr(session,'dungeon_clear_phase',0) or 0) not in (0,1):
        return [], 'no_result_clear=settled'
    mi=int(getattr(session,'dungeon_maze_index',0) or 0)
    if not 0<=mi<len(dungeon.mazes):return [], 'no_result_clear=invalid_maze'
    maze=dungeon.mazes[mi]
    cell=(int(getattr(session,'dungeon_cell_x',-1)),int(getattr(session,'dungeon_cell_y',-1)))
    if cell not in maze.boss_maps:return [], 'no_result_clear=not_final_cell'
    specs=[s for s in maze.map_specs if (int(s.get('x',-1)),int(s.get('y',-1)))==cell]
    boss_specs=[s for s in specs if str(s.get('type','')).lower()=='boss']
    selected=boss_specs or [s for s in specs if str(s.get('type','')).lower()=='map']
    allowed={int(mid) for s in selected for mid in (s.get('candidates') or (s.get('map_id'),)) if int(mid or 0)>0}
    if int(getattr(session,'dungeon_map_index',0) or 0) not in allowed:
        return [], 'no_result_clear=wrong_final_map'
    actors=list(getattr(session,'dungeon_room_mons',None) or ())
    if any(bool(a.get('is_blocking',True)) and not a.get('killed') for a in actors):
        return [], 'no_result_clear=room_alive'
    if not any(a.get('killed') and a.get('is_hostile',True) and a.get('killable',True)
               and int(a.get('type') or a.get('mon_type') or 0) in (3,8) for a in actors):
        return [], 'no_result_clear=no_final_boss_death'
    cid=int(getattr(session,'selected_character_id',0) or 0)
    if cid<=0:return [], 'no_result_clear=no_character'
    repo=server._repo()
    context=DungeonClearContext(dungeon_id=did,difficulty=int(getattr(session,'selected_difficulty',0) or 0),
                               map_id=int(session.dungeon_map_index))
    with repo.inventory_transaction_scope():
        active=[(int(q),int(t)) for _,q,t in repo.list_active_quests(cid)]
        # Only these tasks need a clear boolean. Score/timing/all-room tasks
        # still belong to the ordinary settlement path and its richer facts.
        eligible=[]
        for q,t in active:
            meta=ix.get_meta(q)
            if meta is not None and str(meta.quest_type).strip('[] ').lower()=='clear a dungeon' and int(meta.sub_type)==6:
                eligible.append((q,t))
        changed=apply_dungeon_clear_to_active_quests(eligible,context=context)
        pairs=[]
        for q,_old,new,_note in changed:
            ok,note=repo.update_active_quest_trigger(cid,q,new)
            if not ok:raise RuntimeError('no-result quest progress failed: '+str(note))
            pairs.append((q,new))
        if not pairs:return [], 'no_result_clear=n0'
        packets,_=pack_set_trigger_ack_packets(server,pairs)
    bind_session_quest_state(session,
        active_pairs=[(int(q),int(t)) for _,q,t in repo.list_active_quests(cid)],
        finished_ids=[int(q) for q,_ in repo.list_finished_quests(cid)])
    return packets,'no_result_clear=progress n=%d dungeon=%d'%(len(pairs),did)
