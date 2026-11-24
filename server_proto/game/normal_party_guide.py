"""Guide and ordinary select share the existing per-owner entry coordinator.

Only synthetic-runtime candidate uses this file until separately deployed.
"""
from dataclasses import dataclass
from game.raid.runtime import RaidError
@dataclass
class Guide:
 party: object
 revision: int
 owner: tuple
 body: bytes
 selection: object
 expires: float

class PartyGuides:
 def __init__(self,parties):self.parties=parties;self.pending={}
 def start(self,body,session):
  p=self.parties
  with p._lock:
   party=p.current(session)
   if party is None or len(party.members)<2:return None
   owner=p._owner(session,town=False)
   if owner!=party.owner:raise RaidError('only the actual ordinary leader can guide a group dungeon')
   old=self.pending.get(id(party))
   if old:
    if old.revision==party.revision and old.owner==owner and old.body==bytes(body):return [],'ordinary guide already queued'
    raise RaidError('ordinary guide or membership changed while loading')
   if p.runs._current(session) is not None:return [],'ordinary shared entry already queued or active'
  # The selector validates actual town visibility and freezes participants.
  parsed=__import__('game.chain_loader',fromlist=['load_chain']).load_chain('flow-dungeon_enter.py').parse_0010_plain(body)
  result=p.selectors.route(15,int(parsed['dungeon_id']).to_bytes(4,'little'),session)
  if result is None:raise RaidError('ordinary guide selector unavailable')
  with p._lock:
   selection=p.selectors.rounds.get(id(party))
   if selection is None or not p.selectors._valid(selection):raise RaidError('ordinary guide lost its actual selector')
   self.pending[id(party)]=Guide(party,party.revision,owner,bytes(body),selection,p.clock()+30)
  return [],'ordinary guide queued through shared selector and entry'
 def pump(self,owner,client):
  p=self.parties;job=None
  with p._lock:
   for key,g in tuple(self.pending.items()):
    if not p.selectors._valid(g.selection) or p.clock()>=g.expires or g.party.revision!=g.revision:
     self.pending.pop(key,None);p.selectors.abort(g.party);continue
    if owner!=g.owner or g.selection.entered!={s[0] for s in g.selection.members}:continue
    self.pending.pop(key,None);job=g;break
  if job:
   try:
    result=p.runs._start(job.body,owner[0].session)
    if result is None:raise RaidError('ordinary guide lost its shared group')
    for packet in result[0]:client.sendall(packet)
   except Exception:
    with p._lock:p.selectors.abort(job.party)
    raise
