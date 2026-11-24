"""Frozen 02B75F80: u8 roster slot, DSTR old/new UTF-8 names, u8 reason."""
from game.character import character_rename as rename
from game.character.item_metadata import resolve_item_metadata

def handle(server,body,*,session=None):
 parsed=rename.parse_change(body)
 if session is not None and int(getattr(session,'selected_character_id',0) or 0)>0:
  return [server._codec_response(1,0x87,b'\0\x02')],'C1/0087 RENAME reject=still_in_game'
 ok,note,result=rename.complete(server._repo(),int(getattr(server,'account_id',0) or 0),parsed,metadata_resolver=resolve_item_metadata)
 error=0x14 if note in ('name_taken','name_reserved') else 0x18 if note in ('invalid_request','name_unchanged') else 2
 return [server._codec_response(1,0x87,b'\1' if ok else bytes((0,error)))],'C1/0087 RENAME '+('success' if ok else 'reject')+'='+note
