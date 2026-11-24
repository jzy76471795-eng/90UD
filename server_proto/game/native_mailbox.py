"""System and ordinary player mail for the frozen US Stage3 client (14b85f7e...).

Wire widths are from GNU objdump observations of 0x0167f860, 0x0160b420,
0x0161e780 and the class-0 registration at 0x01681e83 in the frozen EXE.
The US item summary is variable fields, NOT the CN 0x77-byte inventory row.
"""
from __future__ import annotations
from datetime import datetime
import struct

PAGE_SIZE=100
MAX_BATCH=100


def store_for(server):
    from game.mail_store import MailStore
    return MailStore(server._repo().db_path)


def identity(server,session,conn):
    cid=getattr(session,'selected_character_id',None)
    aid=getattr(session,'account_id',None)
    if type(cid) is not int or cid<=0 or type(aid) is not int or aid<=0:
        raise ValueError('请先进入收件角色。')
    owner=conn.execute('SELECT account_id FROM characters WHERE character_id=? AND delete_flag=0',(cid,)).fetchone()
    if owner is None or int(owner[0])!=aid:raise ValueError('收件角色与登录账号不符。')
    return cid


def metadata(item_id):
    from game.character.item_metadata import resolve_item_metadata
    result=resolve_item_metadata(item_id)
    if result.item_kind not in ('equipment','stackable'):raise ValueError('附件 PVF 元数据尚未就绪。')
    return result


def dstring(value,maximum=512):
    data=str(value or '').encode('utf-8')[:maximum]
    data=data.decode('utf-8',errors='ignore').encode('utf-8')
    return struct.pack('<I',len(data))+data


def display_name(name, limit, identity):
    name = str(name)
    raw = name.encode('utf-8')
    if len(raw) <= limit:
        return raw
    # Follow the client text decoder's strict whole-string priority.
    for codec in ('cp949', 'cp936'):
        try:
            compact = name.encode(codec)
        except UnicodeEncodeError:
            continue
        if len(compact) > limit:
            continue
        for decoder in ('utf-8', 'cp949', 'cp936'):
            try:
                decoded = compact.decode(decoder)
            except UnicodeDecodeError:
                continue
            if decoded == name:
                return compact
            break
    suffix = ('#' + str(int(identity))).encode('ascii')
    prefix = raw[:limit-len(suffix)].decode('utf-8', 'ignore').encode('utf-8')
    return prefix + suffix


def sender_field(row):
    raw = display_name(row.get('sender_name', 'GM 系统'), 29,
                       row.get('sender_character_id') or row['id'])
    return struct.pack('<I',len(raw)) + raw


def item_summary(row,meta):
    iid=int(row['item_id']);quantity=int(row['quantity'])
    equipment=bool(meta and meta.is_equipment)
    creature=bool(meta and getattr(meta,'is_creature',False))
    amount=0 if creature else 999999998 if equipment else quantity
    seal=int(equipment and meta.normalized_attach_type()=='sealing')
    # read_u32 calls 167f965,167f97d,167f989; sender DSTR is between
    # object ID and gold. Unlike the CN build, no sender-ID precedes the DSTR.
    instance=row.get('instance')
    if instance:
        amount=int(instance['instance_value']) if equipment else quantity
        seal=int(instance['seal_flag'])
    result=struct.pack('<I',int(row['id']))+sender_field(row)
    result+=struct.pack('<IIBIHBI BBH',int(row['gold']),iid,seal,amount,
                        int(instance['durability'] if instance else getattr(meta,'durability',0) or 0),seal,0,0,0,0)
    if creature:result+=struct.pack('<IB',0,0) # type-26 optional detail disabled
    # 167fab1 calls 1570e00, which itself consumes a u8 count followed
    # by three u8s per entry (1570e4a/69/74/7f). Empty instance options
    # still require the count byte before the direct reads at 167fac5.
    # Omitting it shifts the remaining lifetime/letter ID and detail count.
    result+=struct.pack('<IBBBBHBBBBB',0,0,0,0,0,0,0,0,0,0,0)
    result+=dstring('')+dstring('')+b'\x00'+dstring('')
    if meta is not None and bool(getattr(meta,'is_avatar',False)):
        result+=dstring('') # avatar socket extension (167fce7)
    # read_u32 remaining-seconds, read_u32 letter ID, read_u8 type.
    return result+struct.pack('<IIB',0,int(row.get('letter_id',row['id'])),0)


def mailbox_body(rows,append=False):
    if len(rows)>PAGE_SIZE:raise ValueError('邮件页过大。')
    summaries=[];details=[]
    for value in rows:
        row=dict(value);note=''
        if row['status']=='claimed':row.update(item_id=0,quantity=0,gold=0)
        meta=None
        if row['item_id']:
            try:meta=metadata(int(row['item_id']))
            except ValueError:
                row.update(item_id=0,quantity=0,gold=0)
                note='\n附件准备中，请稍后重新打开邮箱。'
        parts=row.get('attachments',[])
        pending=[a for a in parts if a['status']=='pending'] if row['status']=='pending' else []
        if parts and pending:
            from game.mail_store import ITEM_ID_BASE
            for part in pending:
                item=dict(row,id=ITEM_ID_BASE+part['id'],letter_id=row['id'],item_id=part['item_id'],quantity=part['quantity'],gold=0,instance=part.get('instance'))
                try:summaries.append(item_summary(item,metadata(part['item_id'])))
                except ValueError:
                    item.update(item_id=0,quantity=0);summaries.append(item_summary(item,None))
                    note='\n附件准备中，请稍后重新打开邮箱。'
            if row.get('player_mail') and row['gold']:
                summaries.append(item_summary(dict(row,item_id=0,quantity=0),None))
        else:summaries.append(item_summary(row,meta))
        try:created=int(datetime.fromisoformat(row['created_at']).timestamp())
        except (ValueError,TypeError):created=0
        state=int(row.get('read_state') or 1)
        body=(row['body'] if row.get('player_mail') else row['title']+'\n'+row['body'])+note
        body=body.strip()
        # US detail has an additional final u32 (167ff7f), absent in CN.
        details.append(struct.pack('<II',row['id'],0)+sender_field(row)+dstring(body)+
                       struct.pack('<IHB I',max(0,min(created,0xffffffff)),state,0,0))
    if len(summaries)>PAGE_SIZE:raise ValueError('邮件附件页过大。')
    return bytes([len(summaries),int(append)])+b''.join(summaries)+struct.pack('<HH',0,len(rows))+b''.join(details)


def mail_rows(conn,cid):
    from game.mail_store import attachments
    rows=[dict(row) for row in conn.execute("SELECT m.*,COALESCE(s.read_state,1) AS read_state FROM gmkit_mails m LEFT JOIN gmkit_native_mail_state s ON s.mail_id=m.id WHERE m.character_id=? AND m.status IN ('pending','claimed') ORDER BY m.id DESC",(cid,))]
    for row in rows:row['attachments']=attachments(conn,row['id'])
    from game.player_mail import enrich_rows
    enrich_rows(conn,rows)
    return rows


def snapshot_packets(server,session):
    store=store_for(server)
    with store.connect() as conn:
        cid=identity(server,session,conn);rows=mail_rows(conn,cid)
    packets=[]
    if not rows:packets.append(server._codec_response(0,0x61,mailbox_body([])))
    pages=[];page=[];slots=0
    for row in rows:
        pending=sum(a['status']=='pending' for a in row.get('attachments',[]))
        count=max(1,pending+int(bool(pending and row.get('player_mail') and row['gold'])))
        if slots+count>PAGE_SIZE:pages.append(page);page=[];slots=0
        page.append(row);slots+=count
    if page:pages.append(page)
    for offset,page in enumerate(pages):
        body=mailbox_body(page,append=offset>0)
        packets.append(server._codec_response(0,0x61,body))
        logger=getattr(server,'log',None)
        if callable(logger):logger('[Mailbox] snapshot wire_revision=us-bundle-v2 cid=%d body_len=%d' % (cid,len(body)))
    unread=sum(row['read_state']==1 for row in rows)
    packets.append(server._codec_response(0,0x63,struct.pack('<H',min(65535,unread))))
    return packets,len(rows)


def parse_ids(body,with_state=False):
    if len(body)<4:raise ValueError('邮件请求不完整。')
    count=struct.unpack_from('<I',body)[0]
    if not 1<=count<=MAX_BATCH or len(body)!=4+count*4+(2 if with_state else 0):raise ValueError('邮件请求数量或长度无效。')
    ids=list(struct.unpack_from('<'+'I'*count,body,4))
    if len(set(ids))!=count or any(mid<1 for mid in ids):raise ValueError('邮件编号无效或重复。')
    state=struct.unpack_from('<H',body,4+4*count)[0] if with_state else None
    return ids,state


def claim(server,session,ids):
    store=store_for(server);packets=[]
    with server._repo().inventory_transaction_scope() as conn:
        cid=identity(server,session,conn)
        from game.mail_store import resolve_claim
        rows=resolve_claim(conn,cid,ids) # validate every owner before awarding anything
        from game.player_mail import claim_requested
        handled=claim_requested(server,session,conn,ids,packets)
        rows=resolve_claim(conn,cid,[mid for mid in ids if mid not in handled])
        for row in rows:
            if row['status']=='claimed':continue
            def award(current):
                if current['item_id']:
                    result,_,_=server.gm_item_commands.grant_item({'item_id':int(current['item_id']),'count':int(current['quantity'])},session)
                    packets.extend(result)
                if current.get('gold',0):
                    result,_,_=server.gm_currency_commands.add_gold({'amount':int(current['gold'])},session)
                    packets.extend(result)
            if row.get('legacy'):
                def legacy_award(current):award(current);return {'mail_id':current['id'],'claimed':True}
                store.claim_mail(row['id'],cid,legacy_award,conn);mid=row['id']
            else:
                from game.mail_store import stamp
                award(row);mid=row['mail_id']
                conn.execute("UPDATE gmkit_mail_items SET status='claimed',claimed_at=? WHERE id=? AND status='pending'",(stamp(),row['id']))
                if not conn.execute("SELECT 1 FROM gmkit_mail_items WHERE mail_id=? AND status='pending'",(mid,)).fetchone():
                    conn.execute("UPDATE gmkit_mails SET status='claimed',claimed_at=? WHERE id=?",(stamp(),mid))
                store.audit('mail.claim',f'邮件#{mid}附件#{row["id"]}已领取',str(cid),conn=conn)
            conn.execute('INSERT INTO gmkit_native_mail_state(mail_id,read_state) VALUES(?,2) ON CONFLICT(mail_id) DO UPDATE SET read_state=2',(mid,))
    return packets


def change_state(server,session,ids,state):
    if state not in (0,2,3):raise ValueError('邮件状态无效。')
    store=store_for(server)
    with store.transaction() as conn:
        cid=identity(server,session,conn)
        for mid in ids:
            row=conn.execute('SELECT * FROM gmkit_mails WHERE id=? AND character_id=? AND status IN (\'pending\',\'claimed\')',(mid,cid)).fetchone()
            if row is None:raise ValueError('邮件不存在或不属于当前角色。')
            if state==0 and row['status']=='pending' and (row['item_id'] or row['gold'] or conn.execute("SELECT 1 FROM gmkit_mail_items WHERE mail_id=? AND status='pending'",(mid,)).fetchone()):raise ValueError('请先领取邮件附件。')
        for mid in ids:
            if state==0:conn.execute("UPDATE gmkit_mails SET status='deleted' WHERE id=?",(mid,))
            else:conn.execute('INSERT INTO gmkit_native_mail_state(mail_id,read_state) VALUES(?,?) ON CONFLICT(mail_id) DO UPDATE SET read_state=excluded.read_state',(mid,state))
        store.audit('mail.state','游戏内邮件状态已变更',str(getattr(session,'account_id',0)),conn=conn)


def _route(server,opcode,body,session):
    try:
        if opcode in (0x144,0x5e,0x13b):
            from game.player_mail import query,send
            if opcode==0x144:return query(server,session,body)
            return send(server,session,opcode,body)
        if opcode==0x60:
            if body:raise ValueError('打开邮箱请求必须为空。')
            packets,count=snapshot_packets(server,session)
            return [server._codec_response(1,0x60,b'\x01'+struct.pack('<H',min(count,65535)))]+packets,'native mailbox opened'
        if opcode==0x5f:
            ids,_=parse_ids(body)
            from game.mail_store import ITEM_ID_BASE
            letters={}
            with store_for(server).connect() as conn:
                cid=identity(server,session,conn)
                from game.mail_store import resolve_claim
                resolve_claim(conn,cid,ids)
                for mid in ids:
                    letters[mid]=int(conn.execute('SELECT mail_id FROM gmkit_mail_items WHERE id=?',(mid-ITEM_ID_BASE,)).fetchone()[0]) if mid>=ITEM_ID_BASE else mid
            updates=claim(server,session,ids)
            ack=b'\x01'+struct.pack('<I',len(ids))+b''.join(struct.pack('<III',letters[mid],mid,0) for mid in ids)
            refresh,_=snapshot_packets(server,session)
            return updates+[server._codec_response(1,0x5f,ack)]+refresh,'native mailbox attachments claimed'
        if opcode==0x86:
            ids,state=parse_ids(body,True);change_state(server,session,ids,state)
            ack=b'\x01'+struct.pack('<I',len(ids))+b''.join(struct.pack('<IH',mid,state) for mid in ids)
            refresh,_=snapshot_packets(server,session)
            return [server._codec_response(1,0x86,ack)]+refresh,'native mailbox state changed'
        raise ValueError('当前仅支持系统来信。')
    except Exception as error:
        # Awards roll back before this ACK. Never return success after a failed
        # recipient check, insufficient capacity or failed native grant.
        code=getattr(error,'code',19)
        if type(code) is not int or not 0<=code<=255:code=19
        if opcode==0x144 and code not in (2,7,21):code=2
        return [server._codec_response(1,opcode,bytes([0,code]))],'native mailbox rejected: '+str(error)[:200]


def route(server,opcode,body,session):
    packets,note=_route(server,opcode,body,session)
    logger=getattr(server,'log',None)
    if callable(logger):
        logger('[Mailbox] opcode=0x%04x cid=%s body_len=%d body_prefix=%s packets=%d result=%s' % (
            opcode,getattr(session,'selected_character_id',None),len(body),bytes(body[:24]).hex(),len(packets),note))
    return packets,note


def prepare_rewards(rewards):
    result=[];letters=0
    for reward in rewards:
        if reward['kind']=='item':
            meta=metadata(int(reward['itemId']))
            quantity=int(reward['quantity'])
            split=bool(meta.is_equipment)
            pieces=quantity if split else 1
            letters+=pieces
            if letters>100:raise ValueError('每次兑换最多生成100封附件邮件。')
            result.extend(dict(reward,quantity=1 if split else quantity) for _ in range(pieces))
        else:result.append(reward)
    return result
