"""Settle native avatar-shop coupons without changing the direct selection boxes."""
from functools import lru_cache
import re,time
from pvf import pvf_runtime

TICKET_GRADES={2681590:1,2681593:2,2681594:3}
AVATAR_PARTS=frozenset(('[hat avatar]','[hair avatar]','[face avatar]','[coat avatar]',
    '[pants avatar]','[shoes avatar]','[breast avatar]','[waist avatar]','[skin avatar]'))
MAP_PATH='etc/codex_avatar_voucher_shop.etc'

@lru_cache(maxsize=1)
def ticket_only_catalog():
    text=pvf_runtime.read_text(MAP_PATH)
    match=re.fullmatch(r'\s*\[avatar voucher shop\]\s*([\d\s]+)\[/avatar voucher shop\]\s*',text)
    if not match:raise ValueError('invalid avatar voucher shop')
    values=[int(v) for v in match[1].split()]
    if len(values)%4:raise ValueError('invalid avatar voucher row width')
    rows={}
    for i in range(0,len(values),4):
        cid,item,ticket,count=values[i:i+4]
        if cid in rows or cid<=0 or item<=0 or ticket not in TICKET_GRADES or count!=1:
            raise ValueError('invalid avatar voucher rule')
        rows[cid]=(item,ticket,count)
    return rows

class VoucherRejected(Exception):pass

def purchase_with_avatar_ticket(repo,*,product,metadata,ticket_slot,required_ticket=0,**purchase):
    if (not metadata.is_avatar or metadata.equipment_type not in AVATAR_PARTS
        or int(metadata.grade) not in TICKET_GRADES.values()
        or product.section not in ('avatar','dont trade avatar') or int(product.quantity)!=1):
        return False,'avatar_ticket_invalid_target',{}
    try:
        with repo.inventory_transaction_scope():
            cid=int(purchase['character_id'])
            if ticket_slot==0xffff:
                # Companion client requests server-side matching. Never trust
                # the client for price, grade, item ID, or coupon availability.
                required_ticket=required_ticket or next(item for item,grade in TICKET_GRADES.items() if grade==int(metadata.grade))
                ticket_slot=None
            if ticket_slot is None:
                matches=[row for row in repo.list_inventory_items(cid,list_type=0)
                         if int(row['item_template_id'])==required_ticket and int(row['stack_count'])>=1]
                current_time=int(time.time())
                matches=[row for row in matches if row.get('item_kind')=='stackable'
                         and (not row.get('expire_time') or int(row['expire_time'])>current_time)]
                if not matches:raise VoucherRejected('avatar_ticket_missing')
                ticket_slot=min(int(row['slot_index']) for row in matches)
            row=repo.get_inventory_item_at(cid,list_type=0,slot_index=int(ticket_slot))
            if row is None or row.get('item_kind')!='stackable':raise VoucherRejected('avatar_ticket_slot_missing')
            item=int(row['item_template_id']);expires=int(row.get('expire_time') or 0)
            if (TICKET_GRADES.get(item)!=int(metadata.grade)
                or required_ticket and item!=required_ticket):raise VoucherRejected('avatar_ticket_grade_mismatch')
            if expires and expires<=int(time.time()):raise VoucherRejected('avatar_ticket_expired')
            ok,note,consumed=repo.consume_stackable_at(cid,list_type=0,slot_index=int(ticket_slot),expected_item_id=item,count=1)
            if not ok:raise VoucherRejected('avatar_ticket_consume:'+note)
            purchase.update(total_cera_cost=0,total_gold_cost=0,total_mileage_cost=0)
            ok,note,detail=repo.purchase_cerashop_item(metadata=metadata,**purchase)
            if not ok:raise VoucherRejected('avatar_ticket_grant:'+note)
            detail.update(avatar_ticket_item=item,avatar_ticket_slot=int(ticket_slot),avatar_ticket_remaining=int(consumed['remaining']))
            return True,'avatar_ticket_atomic '+note,detail
    except VoucherRejected as exc:
        return False,str(exc),{}
    except Exception as exc:
        return False,'avatar_ticket_transaction:'+type(exc).__name__+':'+str(exc),{}
