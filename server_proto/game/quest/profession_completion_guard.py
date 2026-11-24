"""Project first-job task flags using the character's existing profession."""
from functools import lru_cache
from game.quest.quest_index import get_quest_index, CHARACTER_JOB_TAGS
from game.quest.quest_rewards import resolve_quest_reward

@lru_cache(maxsize=15)
def _branches(job):
    index=get_quest_index();index.ensure_all_metas_loaded()
    tag=CHARACTER_JOB_TAGS.get(int(job));out={}
    for qid,meta in index.metas.items():
        if meta.jcq not in (1,10,20) or meta.job not in (tag,'[all]'):continue
        branch=int(getattr(meta,'grow_type',-1))
        if branch<=0:
            reward=resolve_quest_reward(qid,player_level=90,player_job=job,player_grow_type=0)
            if reward.chain_type==1:branch=int(reward.grow_number or 0)
        if branch>0:out[int(qid)]=branch
    return out

def filter_profession_completion_rows(repo,character_id,rows):
    """Hide erroneous other-branch flags without rewriting player history.

    This deliberately preserves the currently saved job, awakening, skills,
    and every ordinary quest. It cannot infer a player's former profession.
    """
    getter=getattr(repo,'get_character_combat_projection',None)
    if not callable(getter):return rows
    character=getter(int(character_id))
    if character is None:return rows
    branch=int(character.grow_type)&0x0f
    branches=_branches(int(character.job))
    return [(qid,flag) for qid,flag in rows if int(qid) not in branches or branches[int(qid)]==branch]
