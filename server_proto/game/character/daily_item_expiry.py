"""PVF daily-delete instances: Beijing 06:00, on login and while online."""
from datetime import datetime, timezone
from time import time

from game.character.item_metadata import resolve_item_expire_time, resolve_item_metadata
from game.character.inventory_item_view import InventoryItemView
from game.character.payloads import build_item_list_update_000e


def sweep(repo, character_id, account_id, now):
    """Remove only daily-delete templates owned by this authenticated actor."""
    changed = {}
    with repo.inventory_transaction_scope() as conn:
        owner = conn.execute('SELECT account_id FROM characters WHERE character_id=? AND delete_flag=0',
                             (character_id,)).fetchone()
        if owner is None or int(owner[0]) != account_id:
            raise ValueError('daily expiry actor is not account-owned')
        repo._ensure_account_cargo_schema(conn)
        for table, owner_field, owner_id, list_expr in (
                ('character_items', 'character_id', character_id, 'list_type'),
                ('account_cargo_items', 'account_id', account_id, '12')):
            rows = conn.execute(
                f'SELECT item_uid,item_template_id,expire_time,created_at,slot_index,{list_expr} '
                f'FROM {table} WHERE {owner_field}=? AND (expire_time=0 OR expire_time<=?)',
                (owner_id, now)).fetchall()
            for row in rows:
                meta = resolve_item_metadata(int(row[1]))
                if not getattr(meta, 'daily_delete', False):
                    continue
                expiry = int(row[2])
                if not expiry:
                    try:
                        created = datetime.fromisoformat(str(row[3]).replace('Z', '+00:00'))
                        if created.tzinfo is None:
                            created = created.replace(tzinfo=timezone.utc)
                        granted = int(created.timestamp())
                    except (ValueError, TypeError, OverflowError):
                        granted = now
                    expiry = resolve_item_expire_time(meta, now_unix=granted)
                if expiry <= now:
                    conn.execute(f'DELETE FROM {table} WHERE item_uid=? AND {owner_field}=?',
                                 (int(row[0]), owner_id))
                    changed.setdefault(int(row[5]), set()).add(int(row[4]))
                elif not int(row[2]):
                    conn.execute(f'UPDATE {table} SET expire_time=? WHERE item_uid=? AND {owner_field}=?',
                                 (expiry, int(row[0]), owner_id))
        if 12 in changed:
            repo._refresh_account_cargo_item_count_tx(conn, account_id)
    return changed


def pump(server, session, client, *, now=None):
    cid = getattr(session, 'selected_character_id', None)
    if type(cid) is not int or cid <= 0 or not (
            getattr(session, 'enter_seria_settled', False) or getattr(session, 'in_dungeon_run', False)):
        return
    now = int(time()) if now is None else int(now)
    directory = getattr(server, 'native_game_connections', None)
    if directory is None:
        return
    # Checking the day first avoids a DB/metadata scan on every game poll.
    key = (cid, (now + 7200) // 86400, getattr(session, 'chat_selection_generation', 0))
    if getattr(session, '_daily_item_expiry_key', None) == key:
        return
    from game.raid.runtime import RaidError
    try:
        selection = directory.chat_selection(session, sender=True)
    except RaidError:
        return
    repo = server._repo()
    changed = sweep(repo, cid, selection[2], now)
    for list_type, slots in sorted(changed.items()):
        rows = (repo.list_account_cargo_items(selection[2]) if list_type == 12 else
                repo.list_inventory_items(cid, list_type=list_type, now_unix=now))
        by_slot = {int(row['slot_index']): row for row in rows}
        items = [InventoryItemView(by_slot.get(slot, dict(slot_index=slot,
                    item_template_id=-1, item_kind='unknown', stack_count=0, instance_value=0)))
                 for slot in sorted(slots)]
        body = build_item_list_update_000e(items, list_type=list_type, avatar=(list_type == 1), now_unix=now)
        client.sendall(server._codec_response(0, 0x000e, body))
    session._daily_item_expiry_key = key
    if changed:
        server.log('[DailyItemExpiry] cid=%d deleted=%d' % (cid, sum(map(len, changed.values()))))
