"""Narrow GM growth commands using the server's own PVF/repository/packet helpers.

Install before the management target guard captures and wraps command handlers.
No binary fields are reconstructed here. Online packets come from the existing
GmProfessionControlCommands.pack_level_refresh implementation.
"""
from __future__ import annotations

from typing import Any

from control.contracts import ControlApiError


def _error(code, message, status=409):
    raise ControlApiError(code, message, status=status)


def _point_helper():
    from game.chain_loader import load_chain
    return load_chain('helper-skill_points.py')


def _unlock_helper():
    from game.quest import default_unlocks
    return default_unlocks


def _premium_definition(item_id):
    from game.character.item_metadata import resolve_item_metadata
    from game.shop.premium_contract_index import get_premium_contract_index
    from game.shop.system_reward_activation import classify_system_reward, SYSTEM_REWARD_PREMIUM
    index = get_premium_contract_index()
    contract = index.resolve(item_id)
    activation = classify_system_reward(resolve_item_metadata(item_id))
    if (contract is None or activation is None or activation.kind != SYSTEM_REWARD_PREMIUM
            or str(contract.inventory_activation_mode) != 'created_time'):
        _error('NOT_IMMEDIATE_PREMIUM_CONTRACT',
               'select a PVF immediate account premium contract, not a normal item or wrapper', 400)
    if (int(activation.premium_type) != int(contract.premium_type)
            or int(activation.duration_seconds_per_item) != int(contract.duration_seconds)
            or int(contract.premium_type) <= 0 or int(contract.duration_seconds) <= 0):
        _error('PVF_PREMIUM_MISMATCH', 'PVF premium classification mismatch', 503)
    definition = index.resolve_type(contract.premium_type)
    return contract, definition


def _character(repo, character_id):
    if type(character_id) is not int or character_id <= 0:
        _error('INVALID_PARAMS', 'character_id must be positive', 400)
    with repo.open() as conn:
        row = conn.execute('SELECT character_id,job,level,exp,expertise_exp,bonus_sp,bonus_tp '
                           'FROM characters WHERE character_id=? AND delete_flag=0',
                           (character_id,)).fetchone()
    if row is None:
        _error('CHARACTER_NOT_FOUND', 'character missing', 404)
    return dict(row)


def apply_skill_points(repo, character_id, params):
    fields = {'remaining_sp', 'remaining_tp'}
    if not isinstance(params, dict) or not params or set(params) - fields:
        _error('INVALID_PARAMS', 'only remaining_sp and remaining_tp are supported', 400)
    for value in params.values():
        if type(value) is not int or not 0 <= value <= 0xffff:
            _error('INVALID_PARAMS', 'remaining skill points must be 0..65535', 400)
    helper = _point_helper()
    with repo.inventory_transaction_scope():
        record = _character(repo, character_id)
        # Canonicalize against the real PVF table and persistent bonus first.
        # Merely changing ledger totals would be undone on the next login.
        helper.ensure_skill_points_ledger(repo, character_id=character_id,
            job=int(record['job']), level=int(record['level']))
        previous = repo.get_character_skill_points(character_id)
        if previous is None:
            _error('SKILL_LEDGER_UNAVAILABLE', 'canonical skill ledger unavailable')
        values = dict(previous)
        bonuses = {'sp': int(record['bonus_sp']), 'tp': int(record['bonus_tp'])}
        for kind in ('sp', 'tp'):
            remaining, total = 'remaining_' + kind, 'total_' + kind
            old_remaining, old_total = int(values[remaining]), int(values[total])
            if not 0 <= old_remaining <= old_total <= 0xffffffff or bonuses[kind] < 0:
                _error('SKILL_LEDGER_INVALID', 'existing skill ledger is inconsistent')
            target = params.get(remaining, old_remaining)
            # An increase is a persistent GM bonus, preserving already-spent
            # points. A decrease changes only the remaining balance and never
            # removes learned skills or rewrites the authored PVF baseline.
            increase = max(0, target - old_remaining)
            values[remaining] = target
            values[total] = old_total + increase
            bonuses[kind] += increase
            if values[total] > 0xffffffff or bonuses[kind] > 0xffffffff:
                _error('SKILL_POINT_LIMIT', 'skill point total would overflow')
        with repo.open() as conn:
            conn.execute('UPDATE characters SET bonus_sp=?,bonus_tp=?,updated_at=CURRENT_TIMESTAMP '
                         'WHERE character_id=? AND delete_flag=0',
                         (bonuses['sp'], bonuses['tp'], character_id))
        ok = repo.upsert_character_skill_points(character_id,
            total_sp=values['total_sp'], remaining_sp=values['remaining_sp'],
            total_tp=values['total_tp'], remaining_tp=values['remaining_tp'],
            total_sfp=int(previous.get('total_sfp', 0)),
            remaining_sfp=int(previous.get('remaining_sfp', 0)),
            synced_level=int(record['level']))
        if not ok:
            _error('SKILL_POINTS_UPDATE_FAILED', 'repository rejected skill points')
        for page in (0, 1):
            repo.update_character_skills_page_header(character_id, page=page,
                                                     page_header=values['remaining_sp'])
        after = helper.ensure_skill_points_ledger(repo, character_id=character_id,
            job=int(record['job']), level=int(record['level']))
        if any(int(after.get(key, -1)) != int(values[key]) for key in fields):
            _error('SKILL_POINTS_RECONCILIATION_FAILED', 'canonical point reconciliation mismatch')
    return {'character_id': character_id, 'level': int(record['level']),
            'exp': int(record['exp']), 'expertise_exp': int(record['expertise_exp']),
            'remaining_sp': int(after['remaining_sp']), 'remaining_tp': int(after['remaining_tp']),
            'total_sp': int(after['total_sp']), 'total_tp': int(after['total_tp']),
            'bonus_sp': bonuses['sp'], 'bonus_tp': bonuses['tp'],
            'previous_remaining_sp': int(previous['remaining_sp']),
            'previous_remaining_tp': int(previous['remaining_tp']),
            'committed': True, 'learned_skills_changed': False}


def apply_unlock_progress(repo, character_id, params):
    if (not isinstance(params, dict) or set(params) != {'kind'}
            or not isinstance(params['kind'], str) or params['kind'] not in {'dungeons', 'equipment'}):
        _error('INVALID_PARAMS', 'kind must be dungeons or equipment', 400)
    helper = _unlock_helper()
    try:
        index = helper.get_default_unlock_index()
    except RuntimeError:
        _error('PVF_UNLOCK_INDEX_UNAVAILABLE', 'current PVF unlock index is not warmed', 503)
    kind = params['kind']
    if kind == 'dungeons':
        quest_ids = tuple(index.hell_chain_quest_ids or index.hell_quest_ids)
        state = 0
        if not quest_ids:
            _error('PVF_UNLOCK_INDEX_EMPTY', 'current PVF has no default Hell quest chain', 503)
    else:
        quest_ids = tuple(index.slot_expansion_quest_ids)
        state = int(helper.EX_EQUIP_ALL_OPEN_STATE)
    with repo.inventory_transaction_scope():
        record = _character(repo, character_id)
        ok, result = repo.grant_default_unlock_progress(character_id,
            quest_ids=quest_ids, ex_equip_slot_state=state)
        if not ok:
            _error('UNLOCK_FAILED', str(result.get('reason') or 'repository rejected unlock'))
    result = dict(result)
    result.update({'character_id': character_id, 'level': int(record['level']),
        'exp': int(record['exp']), 'expertise_exp': int(record['expertise_exp']),
        'kind': kind, 'quest_ids': list(quest_ids), 'committed': True,
        'semantics': 'pvf_default_hell_quest_chain' if kind == 'dungeons' else 'pvf_default_equipment_slots',
        'all_dungeon_difficulties_unlocked': False})
    return result


class ManagementCommands:
    def __init__(self, server: Any):
        self.server = server

    @staticmethod
    def _target(session):
        cid = getattr(session, 'selected_character_id', None)
        if type(cid) is not int or cid <= 0:
            _error('SESSION_NOT_READY', 'selected character unavailable')
        if bool(getattr(session, 'in_dungeon_run', False)):
            _error('GROWTH_REQUIRES_TOWN', 'return to town before editing character growth')
        return cid

    def _refresh(self, session, result):
        try:
            packets, refresh_result, _ = self.server.gm_profession_commands.pack_level_refresh(
                session=session, character_id=result['character_id'], level=result['level'],
                exp=result['exp'], expertise_exp=result['expertise_exp'],
                include_0164=False)
            result['packet_flow'] = list(refresh_result.get('packet_flow') or [])
            result['client_sync'] = 'native_refresh_packets'
            return packets, result, 'management GM growth committed'
        except Exception as exc:
            # The transaction has committed. Report this explicitly rather
            # than turn a packet refresh error into a retryable write failure.
            result.update({'packet_flow': [], 'client_sync': 'next_login',
                           'refresh_error_type': type(exc).__name__})
            return [], result, 'management GM growth committed; reconnect to refresh'

    def set_skill_points(self, params, session):
        cid = self._target(session)
        return self._refresh(session, apply_skill_points(self.server._repo(), cid, params))

    def unlock_progress(self, params, session):
        cid = self._target(session)
        return self._refresh(session, apply_unlock_progress(self.server._repo(), cid, params))

    def grant_premium(self, params, session):
        if not isinstance(params, dict) or set(params) != {'item_id', 'count'}:
            _error('INVALID_PARAMS', 'item_id and count are required', 400)
        if (type(params['item_id']) is not int or not 1 <= params['item_id'] <= 0xffffffff
                or type(params['count']) is not int or not 1 <= params['count'] <= 9999):
            _error('INVALID_PARAMS', 'invalid premium item or count', 400)
        cid = self._target(session)
        contract, definition = _premium_definition(params['item_id'])
        repo = self.server._repo()
        with repo.inventory_transaction_scope():
            account_id = repo.get_account_id_for_character(cid)
            if account_id is None:
                _error('ACCOUNT_MISSING', 'account missing')
            # Delegate both virtual entitlement storage and 0042 notification
            # to the real grant handler. Keep its packet building inside the
            # outer transaction so a failed handler cannot leave a grant that
            # was reported rejected and subsequently repeated.
            packets, result, note = self.server.gm_item_commands.grant_item(dict(params), session)
            if result.get('changed_slots') != []:
                _error('PREMIUM_DELIVERY_MISMATCH', 'premium was not delivered as a virtual account timer')
            premiums = repo.list_active_account_premiums(int(account_id))
        try:
            session.account_premium_effects = None
        except Exception:
            pass
        result = dict(result)
        result.update({'character_id': cid, 'account_id': int(account_id),
            'premium_type': int(contract.premium_type),
            'premium_name': str(getattr(definition, 'name', '') or contract.name or ''),
            'duration_seconds_per_item': int(contract.duration_seconds),
            'count': params['count'], 'premiums': premiums,
            'source': 'pvf_premium_contract', 'committed': True,
            'delivery_kind': 'account_premium', 'inventory_slot_used': False})
        return packets, result, note


def install(game):
    if getattr(game, '_manager_gm_growth_installed', False):
        return
    commands = ManagementCommands(game)
    from game.character.management_currency import ManagementCurrencyCommands
    currency = ManagementCurrencyCommands(game)
    game._control_handlers.update({
        'character.set_skill_points': commands.set_skill_points,
        'character.unlock_progress': commands.unlock_progress,
        'account.grant_premium': commands.grant_premium,
        'currency.set_balance': currency.set_currency,
    })
    game._manager_gm_growth_installed = True


__all__ = ['ManagementCommands', 'apply_skill_points', 'apply_unlock_progress', 'install']
