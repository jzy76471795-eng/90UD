"""Frozen 01A88A50 writer: C1/01FE u16 Main inventory slot; S1 success byte."""
from game.character import character_rename as rename
from game.character.item_metadata import resolve_item_metadata

def handle(server,body,*,session=None):
 slot=rename.parse_activation(body)
 if slot is None:return [server._codec_response(1,0x1fe,b'\0\x12')],'C1/01FE RENAME_CARD reject=malformed'
 if session is None or bool(getattr(session,'in_dungeon_run',False)) or bool(getattr(session,'raid_id',None)):
  return [server._codec_response(1,0x1fe,b'\0\x13')],'C1/01FE RENAME_CARD reject=not_town'
 repo=server._repo();account=int(getattr(server,'account_id',0) or 0);cid=int(getattr(session,'selected_character_id',0) or 0)
 ok,note,result=rename.activate(repo,account,cid,slot,metadata_resolver=resolve_item_metadata)
 if not ok:return [server._codec_response(1,0x1fe,b'\0\x13')],'C1/01FE RENAME_CARD reject='+note
 from game.raid.local_bindings import local_raid_hooks
 hooks=local_raid_hooks(server)
 if hooks is not None:hooks.selection_changed(session,reason='character_change')
 ack=server._codec_response(1,0x1fe,b'\1')
 # Reuse the established teardown/roster chain and its PIV-0007 gate.
 packets,returned=server._handle_return_select_character(b'',session=session)
 state=server._session_state(session);state.rename_return_select_requested=True
 return [ack]+packets,'C1/01FE RENAME_CARD success; '+returned
