"""Character-owned auction escrow. Every asset change uses the repository transaction.

No account-wallet writes, remote backups, background actor mutation or client patch.
Expired rows are ineligible immediately; opening auction/mail materializes settlement.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid

FEE = 10000
DURATION = 86400
MAX_PRICE = 300000000
MAX_LISTINGS = 100


class AuctionError(ValueError):
    def __init__(self, message, code=0x99):
        super().__init__(message)
        self.code = code


def ensure_schema(conn):
    from game.player_mail import ensure_schema as mail_schema
    mail_schema(conn)
    conn.execute('''CREATE TABLE IF NOT EXISTS character_auction_listings(
      auction_id INTEGER PRIMARY KEY AUTOINCREMENT,
      seller_id INTEGER NOT NULL REFERENCES characters(character_id),
      item_id INTEGER NOT NULL, item_json TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity>0),
      start_price INTEGER NOT NULL, buyout_price INTEGER NOT NULL, unit_price INTEGER NOT NULL,
      bid INTEGER NOT NULL DEFAULT 0, bidder_id INTEGER REFERENCES characters(character_id),
      created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
      state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','sold','cancelled','expired')),
      revision INTEGER NOT NULL DEFAULT 1, fee INTEGER NOT NULL,
      variant TEXT NOT NULL, search_json TEXT NOT NULL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_auction_search ON character_auction_listings(state,item_id,expires_at,auction_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_auction_seller ON character_auction_listings(seller_id,state)')
    conn.execute('''CREATE TABLE IF NOT EXISTS character_auction_receipts(
      request_key TEXT PRIMARY KEY, character_id INTEGER NOT NULL,
      opcode INTEGER NOT NULL, auction_id INTEGER NOT NULL,
      action TEXT NOT NULL, created_at INTEGER NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS character_auction_sales(
      sale_id INTEGER PRIMARY KEY AUTOINCREMENT,
      auction_id INTEGER NOT NULL REFERENCES character_auction_listings(auction_id),
      buyer_id INTEGER NOT NULL, quantity INTEGER NOT NULL CHECK(quantity>0),
      gold INTEGER NOT NULL CHECK(gold>0), variant TEXT NOT NULL,
      created_at INTEGER NOT NULL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_auction_history ON character_auction_sales(variant,created_at)')


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def fingerprint(row):
    return hashlib.sha256(dumps(dict(row)).encode('utf8')).hexdigest()


def context(session):
    from game.player_mail import owner
    current = getattr(session, 'auction_context', None)
    if not current or current['owner'] != owner(session):
        current = dict(owner=owner(session), quote=None, last_list=None, offers={}, nonce=uuid.uuid4().hex, seen_at=0)
        session.auction_context = current
    return current


def request_key(nonce, cid, opcode, body):
    return hashlib.sha256((nonce + ':' + str(cid) + ':' + str(opcode)).encode() + bytes(body)).hexdigest()


def saved_part(row, count):
    saved = dict(row)
    if saved['item_kind'] == 'stackable':
        saved.update(stack_count=count, instance_value=count)
    return saved


def deliver(store, conn, cid, title, message, *, gold=0, item=None, quantity=0):
    """Use the existing exact-instance player-mail claim path, with a system sender."""
    from game.native_mailbox import metadata
    from game.player_mail import ITEM_ID_BASE
    mid = store.send_mail(dict(characterId=cid, title=title, body=message, gold=gold), conn)
    if mid >= ITEM_ID_BASE:
        raise AuctionError('邮件编号超出客户端范围。')
    conn.execute('INSERT INTO gmkit_player_mails VALUES(?,?,?,?,?)', (mid, 0, '拍卖行', 0, int(gold == 0)))
    if item is not None:
        saved = saved_part(item, quantity)
        iid = saved['item_template_id']
        aid = conn.execute('INSERT INTO gmkit_mail_items(mail_id,position,item_id,quantity,item_name) VALUES(?,?,?,?,?)',
                           (mid, 0, iid, quantity, str(metadata(iid).name or ''))).lastrowid
        if aid >= ITEM_ID_BASE:
            raise AuctionError('附件编号超出客户端范围。')
        conn.execute('INSERT INTO gmkit_player_mail_items VALUES(?,?)', (aid, dumps(saved)))
    return mid


def sale(store, conn, row, buyer, count, price, now):
    item = json.loads(row['item_json'])
    deliver(store, conn, buyer, '拍卖购买成功', f'拍卖 #{row["auction_id"]}：请领取购买的物品。', item=item, quantity=count)
    deliver(store, conn, row['seller_id'], '拍卖成交货款', f'拍卖 #{row["auction_id"]} 已成交 {count} 件，无成交手续费。', gold=price)
    conn.execute('INSERT INTO character_auction_sales(auction_id,buyer_id,quantity,gold,variant,created_at) VALUES(?,?,?,?,?,?)',
                 (row['auction_id'], buyer, count, price, row['variant'], now))
    left = row['quantity'] - count
    if left:
        conn.execute('UPDATE character_auction_listings SET quantity=?,item_json=?,buyout_price=?,revision=revision+1 WHERE auction_id=?',
                     (left, dumps(saved_part(item, left)), left * row['unit_price'], row['auction_id']))
    else:
        conn.execute("UPDATE character_auction_listings SET state='sold',revision=revision+1,bid=0,bidder_id=NULL WHERE auction_id=?", (row['auction_id'],))


def settle_due(server, cid=None):
    """Bounded global sweep plus all of the opening character's due rows (max 100)."""
    from game.native_mailbox import store_for
    store = store_for(server)
    with server._repo().inventory_transaction_scope() as conn:
        ensure_schema(conn)
        now = int(time.time())
        rows = conn.execute("SELECT * FROM character_auction_listings WHERE state='active' AND expires_at<=? AND (seller_id=? OR bidder_id=?) ORDER BY auction_id", (now, cid, cid)).fetchall() if cid else []
        ids = {r['auction_id'] for r in rows}
        rows += [r for r in conn.execute("SELECT * FROM character_auction_listings WHERE state='active' AND expires_at<=? ORDER BY expires_at,auction_id LIMIT 100", (now,)) if r['auction_id'] not in ids]
        for row in rows:
            row = dict(row)
            if row['bidder_id']:
                sale(store, conn, row, row['bidder_id'], row['quantity'], row['bid'], now)
            else:
                deliver(store, conn, row['seller_id'], '拍卖到期退回', f'拍卖 #{row["auction_id"]} 已满 24 小时，请领取退回物品。', item=json.loads(row['item_json']), quantity=row['quantity'])
                conn.execute("UPDATE character_auction_listings SET state='expired',revision=revision+1 WHERE auction_id=?", (row['auction_id'],))
            store.audit('auction.expire', f'拍卖#{row["auction_id"]}到期结算', 'auction', conn=conn)
        return len(rows)


def quote(server, session, iid, variant):
    from game.native_mailbox import store_for, identity
    state = context(session)
    state.update(quote=None, last_list=None)
    with store_for(server).connect() as conn:
        cid = identity(server, session, conn)
        rows = conn.execute("SELECT * FROM character_items WHERE owner_scope='character' AND owner_id=? AND character_id=? AND list_type=0 AND item_template_id=?", (cid, cid, iid)).fetchall()
        state['quote'] = dict(iid=iid, rows={r['item_uid']: fingerprint(r) for r in rows}, at=time.monotonic(), nonce=uuid.uuid4().hex)
        return history(conn, variant)


def history(conn, variant):
    now = int(time.time())
    rows = conn.execute('SELECT quantity,gold,created_at FROM character_auction_sales WHERE variant=? AND created_at>=?', (variant, now - 6 * 86400)).fetchall()
    qty = sum(r['quantity'] for r in rows)
    average = sum(r['gold'] for r in rows) // qty if qty else 0
    prices, counts = [], []
    for day in range(5, -1, -1):
        dayrows = [r for r in rows if now - (day + 1) * 86400 <= r['created_at'] < now - day * 86400]
        n = sum(r['quantity'] for r in dayrows)
        prices.append(sum(r['gold'] for r in dayrows) // n if n else 0)
        counts.append(min(n, 0xffffffff))
    return [average, 0] + prices + counts


def mutate(server, session, opcode, body, values):
    from game.native_mailbox import store_for, identity
    from game.player_mail import validate_item, inventory_packets
    from game.native_auction import fixed_name, item_projection, search_metadata
    repo, store, state = server._repo(), store_for(server), context(session)
    now = int(time.time())
    with repo.inventory_transaction_scope() as conn:
        ensure_schema(conn)
        cid = identity(server, session, conn)
        if opcode == 0xb7:
            previous = state['last_list']
            q = state['quote']
            nonce = previous[1] if previous and previous[0] == bytes(body) else (q['nonce'] if q else '')
        else:
            nonce = state['nonce']
        if not nonce:
            raise AuctionError('请重新放入上架物品。', 0x94)
        key = request_key(nonce, cid, opcode, body)
        receipt = conn.execute('SELECT * FROM character_auction_receipts WHERE request_key=?', (key,)).fetchone()
        if receipt:
            packets = [server._codec_response(1, opcode, b'\1')] + inventory_packets(server, session)
            return packets, f'auction replay id={receipt["auction_id"]}'
        if opcode == 0xb7:
            lt, slot, iid, count, start, buyout, unit = values
            if lt != 0 or not 1 <= count <= 99999:
                raise AuctionError('上架物品栏或数量无效。', 0x94)
            if start != -1 and not 1 <= start <= MAX_PRICE or buyout != -1 and not 1 <= buyout <= MAX_PRICE:
                raise AuctionError('上架价格超出范围。')
            if (start == -1 and buyout == -1) or (buyout > 0 and (unit <= 0 or unit * count != buyout)) or (buyout == -1 and unit != 0) or (start > 0 and buyout > 0 and start > buyout):
                raise AuctionError('上架总价、单价或竞拍价不一致。')
            if not q or q['iid'] != iid or not 0 <= time.monotonic() - q['at'] <= 300:
                raise AuctionError('上架确认已过期，请重新放入物品。', 0x94)
            item, meta, count = validate_item(conn, cid, (lt, slot, iid, count))
            if q['rows'].get(item['item_uid']) != fingerprint(item):
                raise AuctionError('物品已改变，请重新放入。', 0x94)
            if conn.execute("SELECT COUNT(*) FROM character_auction_listings WHERE seller_id=? AND state='active'", (cid,)).fetchone()[0] >= MAX_LISTINGS:
                raise AuctionError('已达同时上架数量上限。', 0x97)
            saved = saved_part(item, count)
            raw = item_projection(saved)
            variant = (iid.to_bytes(4, 'little') + bytes([raw[5] & 31, raw[16], raw[43] & 31])).hex()
            indexed = search_metadata(meta, saved, raw)
            ok, note, _ = repo.spend_wallet_gold(cid, FEE)
            if not ok:
                raise AuctionError('上架费金币不足：' + note, 0x90)
            aid = conn.execute('''INSERT INTO character_auction_listings(seller_id,item_id,item_json,quantity,start_price,buyout_price,unit_price,created_at,expires_at,fee,variant,search_json)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''', (cid, iid, dumps(saved), count, start, buyout, unit, now, now + DURATION, FEE, variant, dumps(indexed))).lastrowid
            if item['item_kind'] == 'stackable' and item['stack_count'] > count:
                left = item['stack_count'] - count
                conn.execute('UPDATE character_items SET stack_count=?,instance_value=?,updated_at=CURRENT_TIMESTAMP WHERE item_uid=?', (left, left, item['item_uid']))
            else:
                conn.execute('DELETE FROM character_items WHERE item_uid=?', (item['item_uid'],))
            action = 'list'
        else:
            aid = values['auction_id']
            if not 0 < aid <= 0x7fffffffffffffff:
                raise AuctionError('拍卖编号无效。', 0x96)
            row = conn.execute('SELECT a.*,c.name seller_name FROM character_auction_listings a JOIN characters c ON c.character_id=a.seller_id WHERE auction_id=?', (aid,)).fetchone()
            if not row or row['state'] != 'active' or row['expires_at'] <= now:
                raise AuctionError('物品已成交、撤回或到期，请刷新。', 0x96)
            row = dict(row)
            if opcode == 0xb8:
                if row['seller_id'] != cid:
                    raise AuctionError('无权撤回他人的拍卖。', 0x9a)
                if row['bidder_id']:
                    raise AuctionError('已有竞价，不能撤回。', 0x98)
                deliver(store, conn, cid, '拍卖撤回', f'拍卖 #{aid} 已撤回，请领取物品。上架费不退。', item=json.loads(row['item_json']), quantity=row['quantity'])
                conn.execute("UPDATE character_auction_listings SET state='cancelled',revision=revision+1 WHERE auction_id=?", (aid,))
                action = 'cancel'
            else:
                if row['seller_id'] == cid:
                    raise AuctionError('不能购买自己的拍卖。', 0x9b)
                offered = state['offers'].get(aid)
                if offered != row['revision'] or not 0 <= time.monotonic() - state['seen_at'] <= 300:
                    raise AuctionError('搜索结果已变化，请刷新后购买。', 0x98)
                if values['item_id'] != row['item_id'] or values['name'] != fixed_name(row['seller_name'], 21)[:13]:
                    raise AuctionError('购买物品与搜索结果不符。', 0x96)
                price = values['price']
                if not 1 <= price <= MAX_PRICE:
                    raise AuctionError('购买金额无效。', 0x9d)
                count = values.get('quantity', row['quantity'])
                partial = opcode == 0x14e
                buy = row['buyout_price'] > 0 and price == row['buyout_price'] if not partial else True
                if partial:
                    if row['start_price'] != -1 or row['bidder_id'] or not 1 <= count <= min(row['quantity'], 10000) or row['buyout_price'] <= 0 or price != row['unit_price'] * count:
                        raise AuctionError('部分购买数量或金额已变化。', 0x98)
                elif not buy:
                    if row['start_price'] < 1 or price < row['start_price'] or price <= row['bid'] or (row['buyout_price'] > 0 and price >= row['buyout_price']):
                        raise AuctionError('竞价已变化，请刷新。', 0x9d)
                ok, note, _ = repo.spend_wallet_gold(cid, price)
                if not ok:
                    raise AuctionError('购买金币不足：' + note, 0x93)
                if row['bidder_id']:
                    deliver(store, conn, row['bidder_id'], '竞拍保证金退回', f'拍卖 #{aid} 的原竞价已被替代，请领取原保证金。', gold=row['bid'])
                if buy:
                    sale(store, conn, row, cid, count, price, now)
                    action = 'buy'
                else:
                    conn.execute('UPDATE character_auction_listings SET bidder_id=?,bid=?,revision=revision+1 WHERE auction_id=?', (cid, price, aid))
                    action = 'bid'
        conn.execute('INSERT INTO character_auction_receipts VALUES(?,?,?,?,?,?)', (key, cid, opcode, aid, action, now))
        store.audit('auction.' + action, f'角色#{cid} 拍卖#{aid} {action}', str(cid), conn=conn)
        # Native successful B7/B9/14E handlers subtract locally. Refresh AFTER ACK.
        packets = [server._codec_response(1, opcode, b'\1')] + inventory_packets(server, session)
    if opcode == 0xb7:
        state.update(last_list=(bytes(body), nonce), quote=None)
    return packets, f'auction {action} id={aid}'
