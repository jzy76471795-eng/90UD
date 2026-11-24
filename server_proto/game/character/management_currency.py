"""Atomic GM target balances using the canonical character/account wallets."""
from __future__ import annotations

from control.contracts import ControlApiError


LIMITS = {'gold': 999_999_999, 'cera': 99_999_999,
          'token_cera': 99_999_999, 'revive_coin': 9_999}


def _error(code, message, status=409):
    raise ControlApiError(code, message, status=status)


def _wallet_body(currency, balance):
    from game.chain_loader import load_chain
    helper = load_chain('helper-dungeon_payloads.py')
    function = (helper.build_wallet_gold_update_000e if currency == 'gold'
                else helper.build_wallet_revive_coin_update_000e)
    return function(balance)


def apply_currency_set(repo, character_id, params):
    if not isinstance(params, dict) or set(params) != {'currency', 'amount'}:
        _error('INVALID_PARAMS', 'currency and amount are required', 400)
    currency, amount = params['currency'], params['amount']
    if currency == 'happy_token_cera':
        _error('CURRENCY_CONSUMER_UNVERIFIED', 'happy token consumer has not been verified', 501)
    if not isinstance(currency, str) or currency not in LIMITS:
        _error('INVALID_PARAMS', 'unsupported currency', 400)
    if type(amount) is not int or not 0 <= amount <= LIMITS[currency]:
        _error('INVALID_PARAMS', 'target balance outside reviewed tool bounds', 400)
    if type(character_id) is not int or character_id <= 0:
        _error('INVALID_PARAMS', 'character_id must be positive', 400)
    with repo.inventory_transaction_scope() as conn:
        if currency in {'gold', 'revive_coin'}:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {'account_wallets', 'character_wallets', 'character_wallet_migrations', 'character_items'}
            if currency == 'revive_coin':
                required.add('account_revive_coin_ledger')
            if not required.issubset(tables):
                _error('WALLET_SCHEMA_UNAVAILABLE', 'canonical wallet schema is missing; no migration attempted', 503)
        row = conn.execute('SELECT account_id FROM characters WHERE character_id=? AND delete_flag=0',
                           (character_id,)).fetchone()
        if row is None:
            _error('CHARACTER_NOT_FOUND', 'character missing', 404)
        account_id = int(row[0])
        if currency in {'gold', 'revive_coin'}:
            # Calling the official wallet reader validates the required gold
            # migration marker and the retired account-gold invariant.
            ok, note, wallet = repo.get_account_wallet(character_id)
            if not ok:
                _error('WALLET_INVALID', note)
            previous = int(wallet[currency])
            delta = amount - previous
            if delta:
                if currency == 'gold':
                    function = repo.grant_wallet_gold if delta > 0 else repo.spend_wallet_gold
                    ok, note, balance = function(character_id, abs(delta))
                else:
                    function = repo.grant_account_revive_coin if delta > 0 else repo.spend_account_revive_coin
                    ok, note, balance = function(character_id, abs(delta), reason='management_gm_set')
                if not ok:
                    _error('CURRENCY_SET_FAILED', note)
                if int(balance) != amount:
                    _error('CURRENCY_READBACK_MISMATCH', 'repository did not persist target balance')
            ok, note, after = repo.get_account_wallet(character_id)
            if not ok or int(after[currency]) != amount:
                _error('CURRENCY_READBACK_MISMATCH', 'wallet readback did not match target')
            sync = 'native_wallet_packet'
        else:
            # The original account Cera command has no live balance packet.
            # These are the reviewed authority columns, never SQL supplied by
            # the browser. Keep the current server's next-start semantics for
            # Cera; token storage is exposed without claiming client support.
            columns = {row[1] for row in conn.execute('PRAGMA table_info(accounts)')}
            if currency not in columns:
                _error('CURRENCY_FIELD_MISSING', 'server account currency column missing', 501)
            row = conn.execute('SELECT %s FROM accounts WHERE account_id=?' % currency,
                               (account_id,)).fetchone()
            if row is None:
                _error('ACCOUNT_MISSING', 'account missing', 404)
            if type(row[0]) is not int or row[0] < 0:
                _error('CURRENCY_STORAGE_INVALID', 'existing account balance is invalid')
            previous = row[0]
            delta = amount - previous
            if delta:
                conn.execute('UPDATE accounts SET %s=? WHERE account_id=?' % currency,
                             (amount, account_id))
            if conn.execute('SELECT %s FROM accounts WHERE account_id=?' % currency,
                            (account_id,)).fetchone()[0] != amount:
                _error('CURRENCY_READBACK_MISMATCH', 'account balance readback mismatch')
            sync = 'next_start' if currency == 'cera' else 'storage_only_unverified'
    return {'character_id': character_id, 'account_id': account_id, 'currency': currency,
            'previous_balance': previous, 'balance': amount, 'delta': delta,
            'changed': bool(delta), 'committed': True, 'client_sync': sync,
            'scope': 'character' if currency == 'gold' else 'account', 'packet_flow': []}


class ManagementCurrencyCommands:
    def __init__(self, server):
        self.server = server

    def set_currency(self, params, session):
        cid = getattr(session, 'selected_character_id', None)
        if type(cid) is not int or cid <= 0:
            _error('SESSION_NOT_READY', 'selected character unavailable')
        repo = self.server._repo()
        # Build the original native packet inside the same outer transaction:
        # an exception cannot leave a committed but rejected balance edit.
        with repo.inventory_transaction_scope():
            result = apply_currency_set(repo, cid, params)
            packets = []
            if result['currency'] in {'gold', 'revive_coin'}:
                body = _wallet_body(result['currency'], result['balance'])
                packets = [self.server._codec_response(0, 0x000e, body)]
                result['packet_flow'] = ['S0/000E wallet']
                result['other_account_sessions_sync'] = 'next_login'
        return packets, result, 'management GM target currency balance committed'


__all__ = ['LIMITS', 'apply_currency_set', 'ManagementCurrencyCommands']
