"""Frozen DFO 14b85f7e native auction wire (B6..BD, 14E).

Readers 016102d0/01610420/01610550; request writers 027b4a50,
027c0220/027c03b0/027ab150/027ab4b0. Search category labels are from
027c0660 -> 017fe320 in the same executable, not another server's protocol.
"""
from __future__ import annotations

import json
import math
import struct
import time
import uuid

from game import auction_store as store

OPCODES = frozenset((0xb6, 0xb7, 0xb8, 0xb9, 0xba, 0xbb, 0xbc, 0xbd, 0x14e))


def fixed_name(name, size, character_id=0):
    from game.native_mailbox import display_name
    raw = display_name(name, size - 1, character_id)
    return raw.ljust(size, b'\0')


def item_projection(saved):
    from game.character.payloads import _pack_common_inventory_entry108
    from game.character.inventory_item_view import InventoryItemView
    inv = _pack_common_inventory_entry108(InventoryItemView(saved))
    raw = bytearray(73)
    raw[0] = inv[13]
    raw[1:5] = inv[2:6]
    raw[5] = inv[10]
    raw[6:12] = inv[6:10] + inv[11:13]
    raw[12:16] = inv[14:18]
    raw[16:19] = inv[19:22]
    raw[44:48] = inv[56:60]
    raw[43] = inv[76] & 31
    raw[51] = inv[18]
    raw[52] = inv[71]
    raw[53] = inv[77]
    raw[55:59] = inv[86:88] + inv[90:92]
    raw[59:64] = inv[103:108]
    # 027b3019 passes raw+29 to 015712b0: three interleaved RO triples,
    # state at +9, hidden option at +10..12, selected index at +13.
    for i in range(3):
        raw[29+3*i:32+3*i] = bytes([inv[61+i], inv[64+i], inv[67+i]])
    raw[38] = inv[70]
    raw[42] = 3
    return bytes(raw)


# The category tree is a wire/UI contract. Metadata remains the existing shared
# PVF ItemMetadata; no independent PVF reader or repeated directory scan.
WEAPONS = {
    'ssword': (10101,10102), 'katana': (10101,10103), 'club': (10101,10104),
    'hsword': (10101,10105), 'beamsword': (10101,10106),
    'knuckle': (10201,10202), 'gauntlet': (10201,10203), 'claw': (10201,10204),
    'boxglove': (10201,10205), 'tonfa': (10201,10206),
    'revolver': (10301,10302), 'automatic': (10301,10303), 'musket': (10301,10304),
    'hcannon': (10301,10305), 'bowgun': (10301,10306),
    'spear': (10401,10402), 'pole': (10401,10403), 'rod': (10401,10404),
    'staff': (10401,10405), 'broom': (10401,10406),
    'cross': (10501,10502), 'rosary': (10501,10503), 'totem': (10501,10504),
    'scythe': (10501,10505), 'axe': (10501,10506),
    'dagger': (10601,10602), 'twinsword': (10601,10603), 'wand': (10601,10604),
    'chakraweapon': (10601,10605), 'pike': (10801,10802), 'halberd': (10801,10803),
}


def categories(meta):
    eq = meta.equipment_type.strip().strip('[]').lower()
    parts = meta.source_path.lower().replace('\\', '/').split('/')
    path = set(parts)
    if meta.item_kind == 'equipment':
        if eq == 'weapon':
            family = parts[parts.index('weapon') + 1] if 'weapon' in path and parts.index('weapon') + 1 < len(parts) else ''
            return [10000] + list(WEAPONS.get(family, ()))
        if eq in ('coat', 'shoulder', 'pants', 'shoes', 'waist'):
            material = next((i for i, p in enumerate(('cloth','leather','larmor','harmor','plate')) if p in path), None)
            ids = [11000]
            if material is not None:
                base = 11001 if material == 0 else 11000 + material * 100
                ids += [base, base + 1 + ('coat','shoulder','pants','shoes','waist').index(eq)]
            return ids
        jewel = {'amulet':12001, 'ring':12002, 'wrist':12003, 'title':12004, 'title name':12004}
        if eq in jewel:
            return [12000, jewel[eq]]
        special = {'support':32001,'magic stone':32100,'earring':32200}
        if eq in special:
            ids = [32000, special[eq]]
            jobs = ('swordman','at swordman','demonic swordman','fighter','gunner','mage','creator mage','at mage','priest','at gunner','thief','at fighter','knight','demonic lancer','at priest')
            if eq != 'earring':
                for i, job in enumerate(jobs, 1):
                    if not meta.usable_jobs or 'all' in meta.usable_jobs or job in meta.usable_jobs:ids.append(special[eq] + i)
            return ids
        return [34000]
    st = meta.stackable_type.strip().lower()
    if st.startswith('[recipe]'):return [31000]
    if st.startswith('[material expert job]'):return [33000]
    if st.startswith('[avatar emblem]'):return [30000]
    if st.startswith('[material]'):return [13000,13002]
    if st.startswith('[quest'):return [13000,13005]
    if st.startswith(('[throw]', '[set item]')):return [13000,13003]
    if st.startswith('[extract]'):return [13000,13004]
    if st.startswith(('[booster]', '[legacy]', '[upgradable legacy]')):return [13000,13007]
    return [13000,13001]


def search_metadata(meta, saved, raw):
    from game.character.inventory_item_view import InventoryItemView
    view = InventoryItemView(saved)
    return dict(categories=categories(meta), rarity=meta.rarity, level=meta.minimum_level,
                enhance=raw[5] & 31, forging=raw[43] & 31,
                magic=[MAGIC_CATEGORIES.get(int(e['type']), 0) for e in view.random_options])


# Generated from the pinned client's PVF auctionrandomcategory.etc and validated
# against B7/BA/BB's 027bf970 -> 027baff0 lookup. This is a category ID mapping,
# never item attribute values or item eligibility.
MAGIC_CATEGORIES = {1: 301, 2: 302, 3: 303, 4: 304, 5: 305, 6: 306, 7: 307, 8: 308, 9: 309, 10: 310, 11: 311, 12: 312, 13: 313, 14: 604, 15: 1, 16: 2, 17: 3, 18: 4, 19: 5, 20: 6, 21: 7, 22: 8, 25: 201, 26: 201, 27: 202, 28: 401, 29: 402, 30: 403, 31: 404, 32: 405, 33: 406, 34: 407, 35: 408, 36: 411, 37: 412, 38: 413, 39: 414, 40: 415, 41: 101, 42: 102, 43: 103, 44: 314, 45: 206, 46: 207, 51: 212, 52: 213, 53: 214, 54: 215, 55: 216, 56: 217, 57: 218, 58: 219, 60: 104, 61: 601, 62: 602, 63: 603, 64: 203, 65: 204, 66: 205, 67: 220, 70: 1, 71: 2, 72: 3, 73: 4, 74: 5, 75: 6, 76: 7, 77: 8, 80: 201, 81: 202, 82: 401, 83: 402, 84: 409, 85: 405, 86: 406, 87: 407, 88: 408, 89: 411, 90: 412, 96: 7, 97: 8, 100: 1, 101: 2, 102: 3, 103: 4, 104: 5, 105: 6, 106: 201, 107: 202, 108: 401, 109: 402, 110: 410, 111: 405, 112: 406, 113: 407, 114: 408, 115: 411, 116: 412, 117: 606, 118: 607, 119: 608, 120: 609, 121: 229, 122: 230, 123: 220, 124: 229, 125: 230, 126: 220, 130: 101, 131: 102, 132: 103, 133: 314, 134: 605, 140: 1, 141: 2, 142: 3, 143: 4, 144: 5, 145: 6, 150: 201, 151: 202, 152: 7, 153: 8, 154: 604, 155: 405, 156: 406, 157: 411, 158: 412, 159: 611, 160: 612, 161: 613, 162: 614, 163: 619, 164: 203, 165: 405, 166: 406, 167: 411, 168: 412, 169: 407, 170: 408, 171: 101, 172: 102, 173: 103, 174: 405, 175: 406, 176: 411, 177: 412, 180: 501, 181: 501, 182: 501, 183: 501, 184: 501, 185: 502, 186: 502, 187: 502, 188: 503, 189: 503, 190: 503, 191: 503, 192: 504, 193: 504, 194: 504, 195: 505, 196: 505, 197: 506, 198: 506, 199: 506, 200: 221, 201: 222, 202: 223, 203: 224, 204: 225, 205: 226, 206: 227, 228: 611, 229: 612, 230: 613, 231: 614, 232: 619, 233: 611, 234: 612, 235: 613, 236: 614, 237: 619, 238: 203, 239: 228, 240: 611, 241: 612, 242: 613, 243: 614, 244: 615, 245: 616, 246: 617, 247: 618}


def record(row, mode=0, now=None, average=0):
    now = int(time.time()) if now is None else now
    item = item_projection(json.loads(row['item_json']))
    hours = min(24, max(1, math.ceil((row['expires_at'] - now) / 3600)))
    bid = row['bid'] or row['start_price']
    header = struct.pack('<Q', row['auction_id'])
    prices = struct.pack('<ii', bid, row['buyout_price'])
    # +116 five socket entries, +146 extension and auxiliary-grade fields are
    # zero for ordinary equipment; avatar/pet/customability are rejected on list.
    if mode == 0:
        out = header + b'\0' + prices + struct.pack('<I', average) + fixed_name(row['seller_name'], 21, row['seller_id']) + bytes([hours]) + item + bytes(42)
        assert len(out) == 158
    else:
        name = row.get('bidder_name', '') if mode == 1 else row['seller_name']
        out = header + prices + fixed_name(name, 13 if mode == 1 else 21, (row.get('bidder_id') or 0) if mode == 1 else row['seller_id']) + bytes([hours]) + item + bytes(34) + struct.pack('<I', average)
        assert len(out) == (141 if mode == 1 else 149)
    return out


def parse_search(opcode, body):
    if opcode == 0xba:
        if len(body) < 17:raise store.AuctionError('搜索请求不完整。')
        offset, lo, hi, n, category = struct.unpack_from('<IBBBH', body)
        if not 1 <= n <= 20 or len(body) != 17 + 4 * n:raise store.AuctionError('搜索物品数量无效。')
        ids = struct.unpack_from('<' + 'I' * n, body, 9)
        magic = struct.unpack_from('<HHH', body, 9 + 4 * n)
        rarity, low_level, high_level = 7, 0, 0
    else:
        if len(body) != 19:raise store.AuctionError('分类搜索请求长度无效。')
        offset, category, rarity, lo, hi, low_level, high_level, *magic = struct.unpack('<IHBBBBBHHHBB', body)
        magic = magic[:3]
        ids = ()
    low_forge, high_forge = body[-2:]
    if offset > 1000000 or lo > hi or low_forge > high_forge or rarity > 7 or (high_level and low_level > high_level):
        raise store.AuctionError('搜索筛选范围无效。')
    return dict(offset=offset, ids=ids, category=category, rarity=rarity, lo=lo, hi=hi,
                level_lo=low_level, level_hi=high_level, magic=magic, forge_lo=low_forge, forge_hi=high_forge)


def matches(row, query):
    x = json.loads(row['search_json'])
    if query['category'] not in (0, 0x84d2, 0xffff) and query['category'] not in x['categories']:return False
    if query['rarity'] != 7 and query['rarity'] != x['rarity']:return False
    if not query['lo'] <= x['enhance'] <= query['hi'] or not query['forge_lo'] <= x['forging'] <= query['forge_hi']:return False
    if x['level'] < query['level_lo'] or query['level_hi'] and x['level'] > query['level_hi']:return False
    required = sorted(query['magic'], key=lambda key: key == 30000)
    available = list(x['magic'])
    for key in required:
        if key == 0:continue
        if key == 30000:
            if not available:return False
            available.pop()
        elif key in available:available.remove(key)
        else:return False
    return True


def search(server, session, opcode, body):
    from game.native_mailbox import store_for, identity
    state, now = store.context(session), int(time.time())
    query = parse_search(opcode, body) if opcode in (0xba,0xbb) else None
    if query is None and body:raise store.AuctionError('我的拍卖请求必须为空。')
    with store_for(server).connect() as conn:
        cid = identity(server, session, conn)
        sql = "SELECT a.*,c.name seller_name,b.name bidder_name FROM character_auction_listings a JOIN characters c ON c.character_id=a.seller_id LEFT JOIN characters b ON b.character_id=a.bidder_id WHERE a.state='active' AND a.expires_at>?"
        args = [now]
        if opcode == 0xbc:sql += ' AND a.seller_id=?';args.append(cid)
        elif opcode == 0xbd:sql += ' AND a.bidder_id=?';args.append(cid)
        elif query['ids']:
            sql += ' AND a.item_id IN (' + ','.join('?' for _ in query['ids']) + ')';args.extend(query['ids'])
        rows = [dict(r) for r in conn.execute(sql + ' ORDER BY a.auction_id DESC', args)]
        if query:rows = [r for r in rows if matches(r, query)]
        total = len(rows)
        if query:
            rows = rows[query['offset']:query['offset']+100]
            if query['offset'] == 0:state.update(nonce=uuid.uuid4().hex, offers={})
            if len(state['offers']) + len(rows) > 10000:state['offers'].clear()
        else:rows = rows[:100 if opcode == 0xbc else 255]
        mode = 0 if query else (1 if opcode == 0xbc else 2)
        records = b''.join(record(r, mode, now, history_average(conn, r['variant'])) for r in rows)
        if query:
            response_opcode, prefix = 0xba, struct.pack('<IH', total, len(rows))
        else:
            response_opcode = opcode
            prefix = struct.pack('<B' if opcode == 0xbc else '<H', len(rows))
        packet = server._codec_response(1, response_opcode, b'\1' + prefix + records)
    state['offers'].update({r['auction_id']:r['revision'] for r in rows})
    state['seen_at'] = time.monotonic()
    return [packet], f'auction results={len(rows)} total={total}'


def history_average(conn, variant):
    return store.history(conn, variant)[0]


def route(server, opcode, body, session):
    response_opcode = 0xba if opcode == 0xbb else opcode
    try:
        from game.native_mailbox import store_for, identity
        with store_for(server).connect() as conn:cid = identity(server, session, conn)
        store.settle_due(server, cid)
        body = bytes(body)
        if opcode == 0xb6:
            if len(body) != 7:raise store.AuctionError('价格查询长度无效。')
            iid = struct.unpack_from('<I', body)[0]
            numbers = store.quote(server, session, iid, body.hex())
            result = ([server._codec_response(1, opcode, b'\1' + struct.pack('<14I', *numbers))], 'auction quote')
        elif opcode in (0xba,0xbb,0xbc,0xbd):result = search(server, session, opcode, body)
        else:
            if opcode == 0xb7:
                if len(body) != 32:raise store.AuctionError('上架请求长度无效。')
                values = struct.unpack_from('<BHIIiiI', body)
            elif opcode == 0xb8:
                if len(body) != 8:raise store.AuctionError('撤回请求长度无效。')
                values = dict(auction_id=struct.unpack('<Q', body)[0])
            elif opcode == 0xb9:
                if len(body) != 29:raise store.AuctionError('购买请求长度无效。')
                price, aid, name, iid = struct.unpack('<IQ13sI', body)
                values = dict(price=price, auction_id=aid, name=name, item_id=iid)
            elif opcode == 0x14e:
                if len(body) != 33:raise store.AuctionError('部分购买请求长度无效。')
                price, count, aid, name, iid = struct.unpack('<IIQ13sI', body)
                values = dict(price=price, quantity=count, auction_id=aid, name=name, item_id=iid)
            else:raise store.AuctionError('未知拍卖请求。')
            result = store.mutate(server, session, opcode, body, values)
    except Exception as error:
        code = getattr(error, 'code', 0x99)
        if not isinstance(error, store.AuctionError):code = 0x99
        result = ([server._codec_response(1, response_opcode, bytes([0, code]))], 'auction rejected: ' + str(error)[:180])
    logger = getattr(server, 'log', None)
    if callable(logger):logger('[Auction] opcode=0x%04x cid=%s bytes=%d result=%s' % (opcode, getattr(session,'selected_character_id',None), len(body), result[1]))
    return result


def before_mailbox(server, session):
    """Expiry delivery on mailbox open, without changing existing mailbox binaries."""
    from game.native_mailbox import store_for, identity
    try:
        with store_for(server).connect() as conn:cid = identity(server, session, conn)
        store.settle_due(server, cid)
    except Exception as error:
        logger = getattr(server, 'log', None)
        if callable(logger):logger('[Auction] expiry deferred: ' + str(error)[:180])
