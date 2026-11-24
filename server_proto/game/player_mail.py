"""Ordinary player letters for the frozen US client, no client patch required.

Wire evidence: 01edd7e0 (query), 0160b2c0 (query reply), 01edcbd0
(single/bundle send), 0160b3f0 (send reply), 01edc440 (postage).
Only the ordinary local-realm mode is implemented. Unsupported special mail
and per-character item subsystems fail before spending or removing anything.
"""
from __future__ import annotations

import json
import struct
import time

ITEM_ID_BASE = 0x40000000
MAX_ITEMS = 10
MAX_GOLD = 999999999


class MailError(ValueError):
    def __init__(self, message, code=19):
        super().__init__(message)
        self.code = code


class Reader:
    def __init__(self, body):
        self.body = bytes(body)
        self.offset = 0

    def take(self, size):
        end = self.offset + size
        if size < 0 or end > len(self.body):
            raise MailError('邮件请求长度无效。')
        result = self.body[self.offset:end]
        self.offset = end
        return result

    def number(self, fmt):
        return struct.unpack('<' + fmt, self.take(struct.calcsize('<' + fmt)))[0]

    def string(self, maximum):
        size = self.number('I')
        if size > maximum:
            raise MailError('邮件文字过长。')
        try:
            value = self.take(size).decode('utf-8', errors='strict')
        except UnicodeError:
            raise MailError('邮件文字编码无效。') from None
        if '\0' in value:
            raise MailError('邮件文字包含空字符。')
        return value

    def name(self):
        value = self.string(48)
        if not 1 <= len(value) <= 12:
            raise MailError('收件角色名称长度无效。')
        return value

    def finish(self):
        if self.offset != len(self.body):
            raise MailError('邮件请求有多余字段。')


def parse_query(body):
    reader = Reader(body)
    name, mode = reader.name(), reader.number('B')
    reader.finish()
    if not name or mode != 0:
        raise MailError('仅支持本服普通邮件。', 2)
    return name


def parse_send(opcode, body):
    if opcode not in (0x5e, 0x13b):
        raise MailError('邮件类型无效。')
    reader = Reader(body)
    name, gold = reader.name(), reader.number('I')
    count = reader.number('B') if opcode == 0x13b else 1
    if not 1 <= count <= MAX_ITEMS:
        raise MailError('一封邮件最多10格附件。')
    parts = []
    for _ in range(count):
        part = (reader.number('B'), reader.number('H'), reader.number('I'), reader.number('I'))
        if part == (0, 0, 0, 0) and opcode == 0x5e:
            continue  # the original single-send writer emits one empty slot
        if part[0] != 0 or part[1] == 0 or part[2] == 0 or part[3] == 0:
            raise MailError('附件必须来自当前角色的普通背包。')
        parts.append(part)
    # 0167ff02 -> 02bf4c90 has a 0x201-byte input bound. Preserve the
    # complete text or reject before charging; never silently truncate UTF-8.
    text = reader.string(512)
    special_id, mode = reader.number('I'), reader.number('B')
    reader.finish()
    if not name or gold > MAX_GOLD or len(text.encode('utf-16-le')) > 1024:
        raise MailError('邮件内容或金币数量无效。')
    if special_id or mode:
        raise MailError('特殊邮件暂不支持，请使用普通邮件。')
    if len({p[1] for p in parts}) != len(parts):
        raise MailError('同一背包格不能重复作为附件。')
    if not text and not gold and not parts:
        raise MailError('不能发送空邮件。')
    return name, gold, parts, text


def postage(gold, item_count):
    # Original x86: cvttsd2si(gold * IEEE754 0.05), capped at 10000.
    return 100 + 1000 * item_count + min(10000, int(gold * 0.05))


def ensure_schema(conn):
    """Additive/idempotent old-save compatibility; never implicitly commits."""
    conn.execute('''CREATE TABLE IF NOT EXISTS gmkit_player_mails(
        mail_id INTEGER PRIMARY KEY REFERENCES gmkit_mails(id) ON DELETE CASCADE,
        sender_character_id INTEGER NOT NULL, sender_name TEXT NOT NULL,
        postage INTEGER NOT NULL, gold_claimed INTEGER NOT NULL DEFAULT 0)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS gmkit_player_mail_items(
        attachment_id INTEGER PRIMARY KEY REFERENCES gmkit_mail_items(id) ON DELETE CASCADE,
        item_json TEXT NOT NULL)''')


def has_schema(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='gmkit_player_mails'").fetchone() is not None


def owner(session):
    return (getattr(session, 'account_id', None),
            getattr(session, 'selected_character_id', None),
            getattr(session, 'chat_selection_generation', 0))


def recipient(conn, name, sender_cid):
    rows = conn.execute('SELECT character_id,account_id,name,level,job,grow_type FROM characters WHERE name=? COLLATE BINARY AND delete_flag=0', (name,)).fetchall()
    if len(rows) != 1:
        raise MailError('收件角色不存在或不唯一。', 21)
    row = dict(rows[0])
    if row['character_id'] == sender_cid:
        raise MailError('不能给当前角色自己发邮件。', 7)
    return row


def _decodable(raw, codec):
    try:
        raw.decode(codec)
        return True
    except UnicodeDecodeError:
        return False


def query(server, session, body):
    from game.native_mailbox import store_for, identity, display_name
    session.player_mail_confirmation = None
    session.player_mail_last_send = None
    name = parse_query(body)
    with store_for(server).connect() as conn:
        cid = identity(server, session, conn)
        row = recipient(conn, name, cid)
    label = display_name(name, 19, row['character_id'])
    display = next((label.decode(enc) for enc in ('utf8','cp949','cp936') if _decodable(label, enc)), None)
    session.player_mail_confirmation = (owner(session), row['character_id'], name, time.monotonic(), display)
    # DSTR (<20 UTF-8 bytes), u8, u16 level, u8 job, three u8 fields,
    # u8 duel-only restriction. Optional restriction fields remain zero for
    # ordinary local characters; no inferred account-equality flag is sent.
    reply = b'\x01' + struct.pack('<I',len(label)) + label + struct.pack('<BHBBBBB', 0,
        int(row['level']), int(row['job']), int(row['grow_type']), 1,
        0, 0)
    return [server._codec_response(1, 0x144, reply)], 'player mail recipient confirmed'


def inventory_packets(server, session):
    packets, _ = server.gm_item_commands._full_container_packets(
        server._repo(), session.selected_character_id, 0,
        selected_character_slot=int(getattr(session, 'selected_slot', 0) or 0))
    return packets


def validate_item(conn, cid, part):
    from game.native_mailbox import metadata
    list_type, slot, iid, count = part
    rows = conn.execute("SELECT * FROM character_items WHERE owner_scope='character' AND owner_id=? AND character_id=? AND list_type=? AND slot_index=?", (cid, cid, list_type, slot)).fetchall()
    if len(rows) != 1:
        raise MailError('附件背包格不存在或不唯一。')
    row = dict(rows[0])
    if row['item_template_id'] != iid:
        raise MailError('附件已发生变化，请重新添加。')
    meta = metadata(iid)
    if row['item_kind'] not in ('equipment', 'stackable') or meta.db_item_kind() != row['item_kind']:
        raise MailError('此附件类型暂不支持普通邮寄。')
    if getattr(meta, 'is_avatar', False) or getattr(meta, 'is_creature', False):
        raise MailError('装扮和宠物附件暂不支持普通邮寄。')
    # Match face-to-face trades: limited-use transfers need a separately
    # validated transfer counter, which ordinary mail does not yet implement.
    if meta.normalized_attach_type() == 'trade limit' or not meta.is_tradable_instance(row['seal_flag']):
        raise MailError('绑定或不可交易物品不能邮寄。')
    if row['equipment_lock_id'] or row['sort_lock_flag'] or row['pet_serial_or_handle'] or row['expire_time']:
        raise MailError('锁定、限时或宠物实例不能邮寄。')
    extra = json.loads(row['extra_json'])
    if not isinstance(extra, dict) or extra.get('customability'):
        raise MailError('角色专属套装实例不能邮寄。')
    if row['item_kind'] == 'equipment':
        if count != 1:
            raise MailError('一格装备附件只能寄一件。')
    elif count > int(row['stack_count']) or count > int(meta.effective_stack_limit or 0xffffffff):
        raise MailError('附件数量不足或超过堆叠上限。')
    return row, meta, count


def send(server, session, opcode, body):
    from game.native_mailbox import store_for, identity
    name, gold, parts, text = parse_send(opcode, body)
    request = (owner(session), opcode, bytes(body))
    old = getattr(session, 'player_mail_last_send', None)
    if old and old[:3] == request:
        return [server._codec_response(1, opcode, b'\x01')], 'player mail send replay already committed'
    confirmed = getattr(session, 'player_mail_confirmation', None)
    if not confirmed or confirmed[0] != owner(session) or name not in (confirmed[2], confirmed[-1]) or not 0 <= time.monotonic() - confirmed[3] <= 300:
        raise MailError('请先确认收件人。')
    name = confirmed[2]
    store, repo = store_for(server), server._repo()
    with repo.inventory_transaction_scope() as conn:
        cid = identity(server, session, conn)
        target = recipient(conn, name, cid)
        if target['character_id'] != confirmed[1]:
            raise MailError('收件角色发生变化，请重新确认。')
        prepared = [validate_item(conn, cid, p) for p in parts]
        ensure_schema(conn)
        sender = conn.execute('SELECT name FROM characters WHERE character_id=?', (cid,)).fetchone()[0]
        fee = postage(gold, len(prepared))
        ok, note, _ = repo.spend_wallet_gold(cid, gold + fee)
        if not ok:
            raise MailError('金币不足，邮件未发送：' + note)
        mid = store.send_mail({'characterId': target['character_id'], 'title': '玩家来信', 'body': text, 'gold': gold}, conn)
        if mid >= ITEM_ID_BASE:
            raise MailError('邮件编号超出客户端范围。')
        conn.execute('INSERT INTO gmkit_player_mails VALUES(?,?,?,?,?)', (mid, cid, sender, fee, int(gold == 0)))
        for position, (row, meta, count) in enumerate(prepared):
            saved = dict(row)
            if row['item_kind'] == 'stackable':
                saved.update(stack_count=count, instance_value=count)
            aid = conn.execute('INSERT INTO gmkit_mail_items(mail_id,position,item_id,quantity,item_name) VALUES(?,?,?,?,?)', (mid, position, row['item_template_id'], count, str(meta.name or ''))).lastrowid
            if aid >= ITEM_ID_BASE:
                raise MailError('附件编号超出客户端范围。')
            conn.execute('INSERT INTO gmkit_player_mail_items VALUES(?,?)', (aid, json.dumps(saved, ensure_ascii=False, sort_keys=True)))
            if row['item_kind'] == 'stackable' and row['stack_count'] > count:
                remaining = row['stack_count'] - count
                conn.execute('UPDATE character_items SET stack_count=?,instance_value=?,updated_at=CURRENT_TIMESTAMP WHERE item_uid=?', (remaining, remaining, row['item_uid']))
            else:
                conn.execute('DELETE FROM character_items WHERE item_uid=?', (row['item_uid'],))
        store.audit('mail.player.send', f'角色#{cid}发送邮件#{mid}至角色#{target["character_id"]}，附件{len(parts)}格，金币{gold}，邮费{fee}', str(cid), conn=conn)
        # Build the sender refresh inside the transaction: serializer failure
        # rolls everything back instead of debiting an apparently failed send.
        packets = inventory_packets(server, session)
        ack = server._codec_response(1, opcode, b'\x01')
    session.player_mail_last_send = request + (mid,)
    session.player_mail_confirmation = None
    return packets + [ack], f'player mail sent id={mid} attachments={len(parts)} postage={fee}'


def enrich_rows(conn, rows):
    if not rows or not has_schema(conn):
        return
    for row in rows:
        player = conn.execute('SELECT * FROM gmkit_player_mails WHERE mail_id=?', (row['id'],)).fetchone()
        if player is None:
            continue
        row['sender_name'] = player['sender_name']
        row['player_mail'] = True
        if player['gold_claimed']:
            row['gold'] = 0
        for part in row['attachments']:
            saved = conn.execute('SELECT item_json FROM gmkit_player_mail_items WHERE attachment_id=?', (part['id'],)).fetchone()
            if saved:
                part['instance'] = json.loads(saved[0])


def restore_item(conn, repo, cid, saved, meta):
    """Preserve the complete instance; never turn equipment into a GM grant."""
    row = dict(saved)
    count = int(row['stack_count'])
    if row['item_kind'] == 'stackable':
        excluded = {'item_uid', 'owner_scope', 'owner_id', 'character_id', 'slot_index', 'created_at', 'updated_at', 'stack_count', 'instance_value'}
        for value in conn.execute("SELECT * FROM character_items WHERE owner_scope='character' AND owner_id=? AND character_id=? AND list_type=0 AND item_template_id=? ORDER BY slot_index", (cid, cid, row['item_template_id'])).fetchall():
            existing = dict(value)
            if any(existing.get(k) != v for k, v in row.items() if k not in excluded):
                continue
            room = max(0, int(meta.effective_stack_limit or 0xffffffff) - existing['stack_count'])
            moved = min(count, room)
            if moved:
                total = existing['stack_count'] + moved
                conn.execute('UPDATE character_items SET stack_count=?,instance_value=?,updated_at=CURRENT_TIMESTAMP WHERE item_uid=?', (total, total, existing['item_uid']))
                count -= moved
            if not count:
                return
        row.update(stack_count=count, instance_value=count)
    slot = repo._find_free_main_slot_for_item(conn, cid, row['item_template_id'])
    if slot is None:
        raise MailError('背包空间不足，附件仍保留在邮件中。')
    columns = {r[1] for r in conn.execute('PRAGMA table_info(character_items)')}
    if set(row) != columns:
        raise MailError('物品存档结构已变化，请先完成附件兼容。')
    row.pop('item_uid')
    row.update(owner_scope='character', owner_id=cid, character_id=cid, list_type=0, slot_index=slot)
    names = list(row)
    # Only names confirmed by PRAGMA above become SQL identifiers.
    conn.execute('INSERT INTO character_items (' + ','.join('"' + n + '"' for n in names) + ') VALUES (' + ','.join('?' for _ in names) + ')', [row[n] for n in names])


def claim_requested(server, session, conn, ids, packets):
    """Claim player escrow under the mailbox's existing outer transaction."""
    if not has_schema(conn):
        return set()
    from game.native_mailbox import identity, metadata, store_for
    from game.mail_store import stamp
    cid = identity(server, session, conn)
    handled, touched, item_changed = set(), set(), False
    for wire in ids:
        if wire >= ITEM_ID_BASE:
            part = conn.execute('SELECT * FROM gmkit_mail_items WHERE id=?', (wire - ITEM_ID_BASE,)).fetchone()
            if part is None:
                continue  # shared mailbox owner validation rejects missing IDs
            mid, selected = part['mail_id'], [dict(part)]
        else:
            mid = wire
            selected = [dict(r) for r in conn.execute('SELECT * FROM gmkit_mail_items WHERE mail_id=? ORDER BY position', (mid,))]
        player = conn.execute('SELECT p.*,m.character_id,m.gold,m.status FROM gmkit_player_mails p JOIN gmkit_mails m ON m.id=p.mail_id WHERE p.mail_id=?', (mid,)).fetchone()
        if player is None:
            continue
        if player['character_id'] != cid or player['status'] not in ('pending', 'claimed'):
            raise MailError('邮件不属于当前角色或已删除。')
        handled.add(wire)
        if player['status'] == 'claimed':
            continue  # Replays must not rewrite the original claim receipt.
        touched.add(mid)
        for part in selected:
            if part['status'] == 'claimed':
                continue
            saved = conn.execute('SELECT item_json FROM gmkit_player_mail_items WHERE attachment_id=?', (part['id'],)).fetchone()
            if saved is None:
                raise MailError('附件实例缺失，未发放替代物品。')
            restore_item(conn, server._repo(), cid, json.loads(saved[0]), metadata(part['item_id']))
            conn.execute("UPDATE gmkit_mail_items SET status='claimed',claimed_at=? WHERE id=?", (stamp(), part['id']))
            item_changed = True
        if wire < ITEM_ID_BASE and not player['gold_claimed']:
            result, _, _ = server.gm_currency_commands.add_gold({'amount': int(player['gold'])}, session)
            packets.extend(result)
            conn.execute('UPDATE gmkit_player_mails SET gold_claimed=1 WHERE mail_id=?', (mid,))
    for mid in touched:
        if not conn.execute("SELECT 1 FROM gmkit_mail_items WHERE mail_id=? AND status='pending'", (mid,)).fetchone() and conn.execute('SELECT gold_claimed FROM gmkit_player_mails WHERE mail_id=?', (mid,)).fetchone()[0]:
            conn.execute("UPDATE gmkit_mails SET status='claimed',claimed_at=? WHERE id=?", (stamp(), mid))
        conn.execute('INSERT INTO gmkit_native_mail_state(mail_id,read_state) VALUES(?,2) ON CONFLICT(mail_id) DO UPDATE SET read_state=2', (mid,))
        store_for(server).audit('mail.player.claim', f'角色#{cid}领取玩家邮件#{mid}', str(cid), conn=conn)
    if item_changed:
        packets.extend(inventory_packets(server, session))
    return handled
