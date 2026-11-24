"""PVF daily account/character quotas and explicit Seria lifetime policy."""
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
import re,struct,time
from pvf import pvf_runtime

COMPLETION_TICKETS=frozenset((10157166,10157168,10157169,10157835,10157836,10158900,202690001))
SERIA_FATIGUE=10098032
SCHEMA='''CREATE TABLE IF NOT EXISTS shop_purchase_counts (
 scope TEXT NOT NULL,owner_id INTEGER NOT NULL,item_id INTEGER NOT NULL,
 period_kind TEXT NOT NULL,period_key TEXT NOT NULL,purchase_count INTEGER NOT NULL CHECK(purchase_count>=0),
 PRIMARY KEY(scope,owner_id,item_id,period_kind,period_key))'''

@dataclass(frozen=True)
class Rule:
 scope:str
 count:int
 period:str

class _AbortPurchase(Exception):pass

@dataclass
class Reservation:
 allowed:bool
 reason:str=''
 committed:bool=False
 def commit(self):self.committed=True

@lru_cache(maxsize=8192)
def parse_rule(item_id,path):
 if item_id==SERIA_FATIGUE:return ()
 if item_id in COMPLETION_TICKETS:return (Rule('character',2,'lifetime'),)
 if not path:return ()
 text=pvf_runtime.read_text(path)
 rules={}
 for tag in ('daily purchase limit','purchase limit'):
  sections=re.findall(r'\['+tag+r'\]([^\[]*)',text,re.I)
  for body in sections:
   pattern=r'\s*`([^`]+)`\s*(-?\d+)\s*' if tag.startswith('daily') else r'\s*`([^`]+)`\s*`([^`]+)`\s*(-?\d+)\s*'
   match=re.fullmatch(pattern,body)
   if not match:raise ValueError('malformed '+tag)
   scope=match[1].lower();scope='character' if scope=='charac' else scope
   period='daily' if tag.startswith('daily') else match[2].lower()
   period='lifetime' if period=='accumulate' else period
   count=int(match.groups()[-1])
   if scope not in ('account','character') or period not in ('daily','weekly','lifetime') or count<0:
    raise ValueError('unsupported '+tag)
   if count:
    key=(scope,period);rules[key]=min(count,rules.get(key,count))
 return tuple(Rule(scope,count,period) for (scope,period),count in sorted(rules.items()))

def rule_for(metadata):return parse_rule(int(metadata.item_id),metadata.source_path)

def _period_keys(now):
    day = int(now + 7200) // 86400
    return {'daily': str(day), 'weekly': str((day + 3) // 7), 'lifetime': 'all'}

def handle_purchase_count_request(server, body, session):
    """An NPC shop asks for its counter cache using a single u32 shop ID."""
    character_id = getattr(session, 'selected_character_id', None)
    if len(body) != 4 or not character_id:
        return [server._codec_response(1, 0x02CC, b'\x00')], 'SHOP_COUNT invalid request'
    repo = server._repo()
    account_id = repo.get_account_id_for_character(character_id)
    session_account = getattr(session, 'account_id', None)
    if not account_id or (session_account is not None and session_account != account_id):
        return [server._codec_response(1, 0x02CC, b'\x00')], 'SHOP_COUNT invalid owner'
    keys = _period_keys(time.time())
    with repo.open() as conn:
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shop_purchase_counts'"
        ).fetchone()
        rows = conn.execute(
            "SELECT item_id, MAX(purchase_count) FROM shop_purchase_counts "
            "WHERE ((scope='character' AND owner_id=?) OR (scope='account' AND owner_id=?)) "
            "AND ((period_kind='daily' AND period_key=?) OR (period_kind='weekly' AND period_key=?) "
            "OR (period_kind='lifetime' AND period_key='all')) AND purchase_count>0 "
            "GROUP BY item_id ORDER BY item_id",
            (character_id, account_id, keys['daily'], keys['weekly']),
        ).fetchall() if present else []
    # The native success reader clears its caches and consumes four counted lists.
    # Lists one and three update the item counter used by the NPC shop.
    payload = bytearray(struct.pack('<BI', 1, len(rows)))
    for item_id, count in rows:
        payload.extend(struct.pack('<II', item_id, count))
    payload.extend(struct.pack('<III', 0, 0, 0))
    shop_id, = struct.unpack('<I', body)
    return [server._codec_response(1, 0x02CC, bytes(payload))], f'SHOP_COUNT shop={shop_id} rows={len(rows)}'

@contextmanager
def reservation(repo,character_id,metadata,count,*,now=None):
 rules=rule_for(metadata)
 if not rules:
  yield Reservation(True)
  return
 if type(count) is not int or count<=0:raise ValueError('positive purchase quantity required')
 periods=_period_keys(time.time() if now is None else now)
 state=Reservation(False)
 try:
  with repo.inventory_transaction_scope() as conn:
   owner=conn.execute('SELECT account_id FROM characters WHERE character_id=? AND delete_flag=0',(character_id,)).fetchone()
   if owner is None:raise ValueError('purchase owner unavailable')
   conn.execute(SCHEMA)
   updates=[];state.allowed=True
   for rule in rules:
    # The project uses Monday 06:00 for weekly resets, alongside daily 06:00.
    period=periods[rule.period]
    owner_id=int(owner[0]) if rule.scope=='account' else int(character_id)
    key=(rule.scope,owner_id,int(metadata.item_id),rule.period,period)
    row=conn.execute('SELECT purchase_count FROM shop_purchase_counts WHERE scope=? AND owner_id=? AND item_id=? AND period_kind=? AND period_key=?',key).fetchone()
    previous=int(row[0]) if row else 0
    if previous+count>rule.count:
     state.allowed=False;state.reason='%s_%s_purchase_limit %d/%d'%(rule.scope,rule.period,previous,rule.count)
     break
    updates.append(key+(previous+count,))
   yield state
   if not state.allowed or not state.committed:raise _AbortPurchase()
   conn.executemany('INSERT INTO shop_purchase_counts VALUES(?,?,?,?,?,?) ON CONFLICT(scope,owner_id,item_id,period_kind,period_key) '
                'DO UPDATE SET purchase_count=excluded.purchase_count',updates)
 except _AbortPurchase:
  pass

def execute_purchase(repo,account_id,character_id,metadata,count,operation,*,now=None):
 if repo.get_account_id_for_character(character_id)!=account_id:
  return False,'character_account_mismatch',{}
 try:rule_for(metadata)
 except (ValueError,KeyError) as exc:return False,'purchase_limit_rule_invalid '+str(exc),{}
 with reservation(repo,character_id,metadata,count,now=now) as state:
  if not state.allowed:return False,state.reason,{}
  result=operation()
  if result[0]:state.commit()
  return result

def notice(server):
 message='该道具已达到购买次数限制。'.encode('utf-8')
 name='系统'.encode('utf-8')
 return server._codec_response(0,0x41,b'\0\0'+len(name).to_bytes(4,'little')+name+b'\0'+len(message).to_bytes(4,'little')+message)
