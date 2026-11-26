"""Atomic native character renaming; a failed submission never consumes a card."""
from __future__ import annotations
import os,sqlite3,struct,time
from pathlib import Path
from game.character.character_db import CHARACTER_NAME_MAX_BYTES, is_valid_character_name, is_valid_new_character_name

DAY=86400
CN_CARD=2682290
SCHEMA=(
 '''CREATE TABLE IF NOT EXISTS character_rename_pending(
 character_id INTEGER PRIMARY KEY REFERENCES characters(character_id),
 account_id INTEGER NOT NULL, item_uid INTEGER NOT NULL UNIQUE,
 item_id INTEGER NOT NULL, source_slot INTEGER NOT NULL, created_at INTEGER NOT NULL)''',
 '''CREATE TABLE IF NOT EXISTS character_rename_audit(
 id INTEGER PRIMARY KEY AUTOINCREMENT,character_id INTEGER NOT NULL,account_id INTEGER NOT NULL,
 old_name TEXT NOT NULL,new_name TEXT NOT NULL,card_item_id INTEGER NOT NULL,
 card_item_uid INTEGER NOT NULL,renamed_at INTEGER NOT NULL)''',
 '''CREATE TABLE IF NOT EXISTS character_name_reservations(
 name TEXT PRIMARY KEY,character_id INTEGER NOT NULL,expires_at INTEGER NOT NULL)''',
 '''CREATE TRIGGER IF NOT EXISTS guard_reserved_character_name_insert BEFORE INSERT ON characters
 WHEN EXISTS(SELECT 1 FROM character_name_reservations r WHERE r.name=NEW.name
 AND r.expires_at>CAST(strftime('%s','now') AS INTEGER))
 BEGIN SELECT RAISE(ABORT,'character_name_reserved'); END''',
 '''CREATE TRIGGER IF NOT EXISTS guard_reserved_character_name_update BEFORE UPDATE OF name ON characters
 WHEN NEW.name<>OLD.name AND EXISTS(SELECT 1 FROM character_name_reservations r WHERE r.name=NEW.name
 AND r.expires_at>CAST(strftime('%s','now') AS INTEGER))
 BEGIN SELECT RAISE(ABORT,'character_name_reserved'); END''')

class Reject(Exception):pass
def ensure_schema(conn):
 for sql in SCHEMA:conn.execute(sql)

def pending_ids(db_path=None):
 value=str(db_path or os.environ.get('USDOF_SQLITE_DB',''))
 if not value or not Path(value).is_file():return frozenset()
 try:
  with sqlite3.connect(Path(value).resolve().as_uri()+'?mode=ro',uri=True) as conn:
   return frozenset(int(r[0]) for r in conn.execute('''SELECT p.character_id FROM character_rename_pending p
    JOIN characters c ON c.character_id=p.character_id AND c.account_id=p.account_id AND c.delete_flag=0
    JOIN character_items i ON i.item_uid=p.item_uid AND i.item_template_id=p.item_id
    AND i.owner_scope='character' AND i.owner_id=p.character_id AND i.character_id=p.character_id
    AND i.list_type=0 AND i.stack_count>0'''))
 except sqlite3.OperationalError as error:
  if 'no such table' in str(error):return frozenset()
  raise

def parse_activation(body):
 raw=bytes(body)
 if not 2<=len(raw)<=16 or any(raw[2:]):return None
 return struct.unpack_from('<H',raw)[0]

def parse_change(body):
 raw=bytes(body)
 if len(raw)<11 or raw[0]>=128:return None
 values=[];pos=1
 try:
  for _ in range(2):
   size=struct.unpack_from('<I',raw,pos)[0];pos+=4
   if not 1<=size<=CHARACTER_NAME_MAX_BYTES or pos+size>len(raw):return None
   data=raw[pos:pos+size];pos+=size;text=data.decode('utf-8')
   if not is_valid_character_name(text) or '\0' in text:return None
   values.append(text)
  if pos>=len(raw) or len(raw)>pos+16 or any(raw[pos+1:]):return None
  if not is_valid_new_character_name(values[1]):return None
  return dict(slot=raw[0],old_name=values[0],new_name=values[1],client_reason=raw[pos])
 except (UnicodeError,struct.error):return None

def is_card(metadata):
 return metadata.item_id==CN_CARD and metadata.action_type=='[character name change]' and metadata.is_stackable

def activate(repo,account_id,character_id,slot,*,metadata_resolver,now=None):
 now=int(time.time()) if now is None else int(now)
 if min(account_id,character_id)<=0:return False,'owner_missing',{}
 try:
  with repo.inventory_transaction_scope() as conn:
   ensure_schema(conn)
   char=conn.execute('SELECT * FROM characters WHERE character_id=? AND account_id=? AND delete_flag=0',(character_id,account_id)).fetchone()
   if char is None:raise Reject('character_account_mismatch')
   row=conn.execute("SELECT * FROM character_items WHERE owner_scope='character' AND owner_id=? AND character_id=? AND list_type=0 AND slot_index=?",(character_id,character_id,slot)).fetchone()
   if row is None:raise Reject('card_slot_empty')
   meta=metadata_resolver(int(row['item_template_id']))
   if not is_card(meta):raise Reject('not_rename_card')
   if int(char['level'])<meta.minimum_level:raise Reject('level_low')
   if int(row['stack_count'])<1:raise Reject('card_empty')
   old=conn.execute('SELECT * FROM character_rename_pending WHERE character_id=?',(character_id,)).fetchone()
   if old and not conn.execute("SELECT 1 FROM character_items WHERE item_uid=? AND item_template_id=? AND owner_scope='character' AND owner_id=? AND character_id=? AND list_type=0 AND stack_count>0",(old['item_uid'],old['item_id'],character_id,character_id)).fetchone():
    conn.execute('DELETE FROM character_rename_pending WHERE character_id=?',(character_id,));old=None
   if old:
    if int(old['item_uid'])!=int(row['item_uid']):raise Reject('another_card_pending')
    return True,'pending_replay',dict(character_id=character_id,item_uid=int(row['item_uid']),replay=True)
   if meta.item_id==CN_CARD:
    prior=conn.execute('SELECT MAX(renamed_at) FROM character_rename_audit WHERE character_id=?',(character_id,)).fetchone()[0]
    if prior is not None and now-int(prior)<90*DAY:raise Reject('rename_cooldown_90_days')
   conn.execute('INSERT INTO character_rename_pending VALUES(?,?,?,?,?,?)',(character_id,account_id,int(row['item_uid']),meta.item_id,slot,now))
   return True,'rename_pending',dict(character_id=character_id,item_uid=int(row['item_uid']),item_id=meta.item_id,replay=False)
 except (Reject,sqlite3.IntegrityError) as error:return False,str(error),{}

def complete(repo,account_id,parsed,*,metadata_resolver,now=None):
 now=int(time.time()) if now is None else int(now)
 if account_id<=0 or parsed is None or not is_valid_new_character_name(parsed['new_name']):return False,'invalid_request',{}
 records=repo.list_by_account(account_id)
 record=records[parsed['slot']] if 0<=parsed['slot']<len(records) else None
 if record is None:return False,'character_account_mismatch',{}
 try:
  with repo.inventory_transaction_scope() as conn:
   ensure_schema(conn)
   char=conn.execute('SELECT * FROM characters WHERE account_id=? AND character_id=? AND delete_flag=0',(account_id,int(record.character_id))).fetchone()
   if char is None:raise Reject('character_account_mismatch')
   cid=int(char['character_id'])
   pending=conn.execute('SELECT * FROM character_rename_pending WHERE account_id=? AND character_id=?',(account_id,cid)).fetchone()
   if pending is None:raise Reject('rename_not_pending')
   if str(char['name'])!=parsed['old_name']:raise Reject('old_name_mismatch')
   name=parsed['new_name']
   if name==parsed['old_name']:raise Reject('name_unchanged')
   if conn.execute('SELECT 1 FROM characters WHERE delete_flag=0 AND name=?',(name,)).fetchone():raise Reject('name_taken')
   if conn.execute('SELECT 1 FROM character_name_reservations WHERE name=? AND expires_at>?',(name,now)).fetchone():raise Reject('name_reserved')
   row=conn.execute("SELECT * FROM character_items WHERE item_uid=? AND owner_scope='character' AND owner_id=? AND character_id=? AND list_type=0",(pending['item_uid'],cid,cid)).fetchone()
   if row is None or int(row['item_template_id'])!=int(pending['item_id']) or int(row['stack_count'])<1:raise Reject('card_changed_or_missing')
   meta=metadata_resolver(int(row['item_template_id']))
   if not is_card(meta):raise Reject('not_rename_card')
   if meta.item_id==CN_CARD:
    prior=conn.execute('SELECT MAX(renamed_at) FROM character_rename_audit WHERE character_id=?',(cid,)).fetchone()[0]
    if prior is not None and now-int(prior)<90*DAY:raise Reject('rename_cooldown_90_days')
   changed=conn.execute('UPDATE characters SET name=?,name_bytes=?,updated_at=CURRENT_TIMESTAMP WHERE character_id=? AND account_id=? AND name=?',(name,name.encode('utf-8'),cid,account_id,parsed['old_name']))
   if changed.rowcount!=1:raise Reject('character_update_lost')
   old_raw=parsed['old_name'].encode('utf-8')
   conn.execute('UPDATE account_character_entries SET name=?,name_bytes=? WHERE name_bytes=? OR name=? OR CAST(name AS BLOB)=?',(name,name.encode('utf-8'),old_raw,parsed['old_name'],old_raw))
   count=int(row['stack_count'])
   if count==1:conn.execute('DELETE FROM character_items WHERE item_uid=?',(row['item_uid'],))
   else:conn.execute('UPDATE character_items SET stack_count=?,instance_value=?,updated_at=CURRENT_TIMESTAMP WHERE item_uid=?',(count-1,count-1,row['item_uid']))
   conn.execute('DELETE FROM character_rename_pending WHERE character_id=?',(cid,))
   conn.execute('INSERT INTO character_rename_audit(character_id,account_id,old_name,new_name,card_item_id,card_item_uid,renamed_at) VALUES(?,?,?,?,?,?,?)',(cid,account_id,parsed['old_name'],name,meta.item_id,int(row['item_uid']),now))
   if meta.item_id==CN_CARD:
    conn.execute('INSERT INTO character_name_reservations VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET character_id=excluded.character_id,expires_at=excluded.expires_at',(parsed['old_name'],cid,now+180*DAY))
   return True,'character_renamed',dict(character_id=cid,old_name=parsed['old_name'],new_name=name,item_id=meta.item_id,remaining=count-1)
 except (Reject,sqlite3.IntegrityError) as error:return False,str(error),{}

def is_name_reserved(db_path, name, *, now=None):
 value=Path(db_path)
 if not value.is_file():return False
 now=int(time.time()) if now is None else int(now)
 try:
  with sqlite3.connect(value.resolve().as_uri()+'?mode=ro',uri=True) as conn:
   return conn.execute('SELECT 1 FROM character_name_reservations WHERE name=? AND expires_at>?',(name,now)).fetchone() is not None
 except sqlite3.OperationalError as error:
  if 'no such table' in str(error):return False
  raise
