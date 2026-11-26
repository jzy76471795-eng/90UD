"""Opaque Stage3 quick-chat banks; encoding is owned by the native client.

0186E930 emits bank u8 + u32 length + 300 raw bytes for each bank.
0164D2A0 -> 0186E9C0 -> 02BF4BC0 reads both length-prefixed banks after
the main GAME_OPTION block. Never decode/re-encode or guess lost characters.
"""
import struct

BANK_SIZE = 300
_COLUMNS = ('quickchat_bank0', 'quickchat_bank1')


def parse_save(body):
    raw = bytes(body)
    if (not 305 <= len(raw) <= 312 or raw[0] not in (0, 1)
            or struct.unpack_from('<I', raw, 1)[0] != BANK_SIZE or any(raw[305:])):
        raise ValueError('invalid quick-chat bank packet')
    return raw[0], raw[5:305]


def save(server, body, *, session=None):
    try:
        bank, data = parse_save(body)
    except ValueError:
        return [], 'C1/0170 QUICKCHAT reject=invalid_body'
    cid = int(getattr(session, 'selected_character_id', 0) or 0)
    if cid <= 0:
        return [], 'C1/0170 QUICKCHAT reject=no_selected_character'
    repo = server._repo()
    account = repo.get_account_id_for_character(cid)
    session_account = getattr(session, 'account_id', None)
    if account is None or (session_account is not None and int(session_account) != int(account)):
        return [], 'C1/0170 QUICKCHAT reject=owner_mismatch'
    column = _COLUMNS[bank]
    with repo.open() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('SELECT 1 FROM characters WHERE character_id=? AND account_id=? '
                'AND delete_flag=0', (cid,account)).fetchone() is None:
            return [], 'C1/0170 QUICKCHAT reject=owner_changed'
        repo._ensure_account_settings_schema(conn)
        conn.execute('INSERT INTO account_settings(account_id,'+column+') VALUES(?,?) '
            'ON CONFLICT(account_id) DO UPDATE SET '+column+'=excluded.'+column+
            ',updated_at=CURRENT_TIMESTAMP', (account,data))
    return [], 'C1/0170 QUICKCHAT saved bank=%d bytes=300 encoding=opaque' % bank


def load_banks(repo, account_id):
    if type(account_id) is not int or account_id <= 0:
        raise ValueError('quick-chat account owner unavailable')
    with repo.open() as conn:
        repo._ensure_account_settings_schema(conn)
        row = conn.execute('SELECT quickchat_bank0,quickchat_bank1 FROM account_settings '
            'WHERE account_id=?', (account_id,)).fetchone()
    result = tuple(bytes(value) if value is not None else bytes(BANK_SIZE)
        for value in (row if row is not None else (None,None)))
    if any(len(bank) != BANK_SIZE for bank in result):
        raise ValueError('stored quick-chat bank size is invalid; data preserved')
    return result
