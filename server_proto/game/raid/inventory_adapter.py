"""Pure normal-raid projections backed by US metadata and the shared PVF.

Final delivery must call the existing repository grant with the frozen real
ItemMetadata and require_full_capacity=True inside the receipt transaction.
These projections never create equipment instance quality or write a database.
The u32 projection limit for US unlimited stacks is a preview representation;
the frozen actual_stack_limit remains zero for the repository's own rules.
"""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from game.chain_loader import load_chain
from game.character import item_metadata
from game.character.guild_medal_upgrade import (
    GUILD_MEDAL_CONTRIBUTION_ITEM_ID, GUILD_MEDAL_CONTRIBUTION_STACK_LIMIT,
)
from game.character.item_metadata import ItemMetadata, SLOT_RANGE_CONSUMABLE, SLOT_RANGE_QUICK

from .rewards import ConfiguredRewardResolver, RewardError, RewardItem, FrozenRewardPlan, _plan_fingerprint


def _valid_metadata(metadata, item_id=None):
    if (not isinstance(metadata, ItemMetadata)
            or isinstance(metadata.item_id, bool) or not isinstance(metadata.item_id, int)
            or not 1 <= metadata.item_id <= 0xffffffff
            or item_id is not None and metadata.item_id != item_id
            or metadata.item_kind not in ('equipment', 'stackable')
            or not isinstance(metadata.note, str)
            or metadata.note != 'ok' and not metadata.note.startswith('ok:')
            or not isinstance(metadata.source_path, str)
            or not metadata.source_path.startswith(metadata.item_kind + '/')
            or isinstance(metadata.stack_limit, bool) or not isinstance(metadata.stack_limit, int)
            or not 0 <= metadata.stack_limit <= 0xffffffff):
        raise RewardError('actual US reward item metadata unavailable')
    return metadata


def _route(metadata):
    helper = load_chain('helper-cube_soul_slots.py')
    if helper.is_cube_fragment(metadata.item_id):
        return 'account_cube', 'account', int(helper.cube_fragment_slot(metadata.item_id))
    if helper.is_account_soul(metadata.item_id):
        return 'account_soul', 'account', int(helper.account_soul_slot(metadata.item_id))
    return 'inventory', 'character', None


def legal_slot_indices(metadata, *, avatar_capacity=None, prefer_quick=True):
    """Return existing US per-template legal cells without querying a DB.

Avatar capacity must come from the repository's actual container state.
Account cube/soul cells are UI projections, never character_items rows.
Do not combine different main-tab ranges into CN plan_reward_slots: that
generic planner cannot enforce a different allowed range for each item.
"""
    meta = _valid_metadata(metadata)
    _grant_route, _owner, fixed_slot = _route(meta)
    if fixed_slot is not None:
        return (fixed_slot,)
    if meta.is_avatar and not meta.is_pet_inventory_item:
        if (isinstance(avatar_capacity, bool) or not isinstance(avatar_capacity, int)
                or not 1 <= avatar_capacity <= 65536):
            raise RewardError('actual US avatar container capacity required')
        return tuple(range(avatar_capacity))
    start, end = meta.slot_range()
    if not 0 <= start <= end <= 65535:
        raise RewardError('actual US reward slot range unavailable')
    cells = tuple(range(start, end + 1))
    if (prefer_quick is True and meta.inventory_list_type() == 0
            and not meta.is_equipment and (start, end) == SLOT_RANGE_CONSUMABLE):
        quick_start, quick_end = SLOT_RANGE_QUICK
        cells = tuple(range(quick_start, quick_end + 1)) + cells
    return cells


def project_reward_item(metadata, quantity, script_text, *, display_kind=None):
    """Freeze the actual template; the repository still owns instance creation."""
    meta = _valid_metadata(metadata)
    if (isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 65535
            or not isinstance(script_text, str) or not script_text.strip()):
        raise RewardError('actual US reward amount or script unavailable')
    # Current US RewardItem+44 selects ordinary card 0 / premium card 1.
    # It is not equipment/stackable classification. Choice-dependent mapping
    # is supplied explicitly by the raid domain, never inferred from metadata.
    if isinstance(display_kind, bool) or not isinstance(display_kind, int) or not 0 <= display_kind <= 255:
        raise RewardError('verified US native reward display kind required')
    route, owner, fixed_slot = _route(meta)
    actual_limit = meta.effective_stack_limit
    if meta.is_equipment:
        actual_limit = 1
    elif meta.item_id == GUILD_MEDAL_CONTRIBUTION_ITEM_ID and meta.inventory_list_type() == 0:
        actual_limit = GUILD_MEDAL_CONTRIBUTION_STACK_LIMIT
    snapshot = (
        ('us_item_metadata', json.dumps(asdict(meta), ensure_ascii=False, sort_keys=True, separators=(',', ':'))),
        ('script_sha256', hashlib.sha256(script_text.encode('utf-8')).hexdigest()),
        ('actual_stack_limit', str(actual_limit)),
        ('grant_route', route),
    )
    return RewardItem(meta.item_id, quantity, display_kind,
                      owner, meta.inventory_list_type(), actual_limit or 0xffffffff,
                      meta.source_path, snapshot, fixed_slot)


class USInventoryRewardResolver(ConfiguredRewardResolver):
    """Use an already selected shared reader and cache validated actual leaves."""

    def __init__(self, source, metadata_resolver=item_metadata.resolve_item_metadata, *, display_kind_resolver=None):
        if (not callable(getattr(source, 'read_text', None)) or not callable(metadata_resolver)
                or not callable(display_kind_resolver)):
            raise RewardError('actual US shared source, metadata and verified display-kind adapters required')
        self.source = source
        self.metadata_resolver = metadata_resolver
        self.display_kind_resolver = display_kind_resolver
        self._metadata = {}
        self._texts = {}
        self._tables = {}
        super().__init__(self.text_for_item, self.resolve_leaf)

    def _table(self, item_id):
        if item_id not in self._tables:
            self._tables[item_id] = super()._table(item_id)
        return self._tables[item_id]

    def text_for_item(self, item_id):
        if isinstance(item_id, bool) or not isinstance(item_id, int) or not 1 <= item_id <= 0xffffffff:
            raise RewardError('actual US reward item identity invalid')
        if item_id not in self._texts:
            try:
                meta = _valid_metadata(self.metadata_resolver(item_id), item_id)
                text = self.source.read_text(meta.source_path)
            except RewardError:
                raise
            except Exception as exc:
                raise RewardError('actual US reward item source unavailable: %d' % item_id) from exc
            if not isinstance(text, str) or not text.strip():
                raise RewardError('actual US reward item script empty')
            self._metadata[item_id] = meta
            self._texts[item_id] = text
        return self._texts[item_id]

    def resolve_leaf(self, item_id, quantity):
        text = self.text_for_item(item_id)
        # Admission verifies leaf shape only; ordinary kind 0 is proven.
        return project_reward_item(self._metadata[item_id], quantity, text, display_kind=0)

    def __call__(self, choice, roll):
        items = super().__call__(choice, roll)
        # The same template can occur in ordinary and premium choices. Keep
        # this projection local to the call, not in the shared template cache.
        return tuple(project_reward_item(self._metadata[item.item_id], item.quantity,
                                         self._texts[item.item_id],
                                         display_kind=self.display_kind_resolver(self._metadata[item.item_id], choice))
                     for item in items)

    def metadata_for_plan(self, plan):
        if (not isinstance(plan, FrozenRewardPlan) or not plan.items
                or _plan_fingerprint(plan.choice, plan.items) != plan.fingerprint):
            raise RewardError('frozen US reward plan changed')
        result = []
        for item in plan.items:
            meta = self._metadata.get(item.item_id)
            text = self._texts.get(item.item_id)
            if meta is None or text is None:
                raise RewardError('reward metadata was not frozen during warmup')
            expected = project_reward_item(meta, item.quantity, text,
                                          display_kind=self.display_kind_resolver(meta, plan.choice))
            if item != expected:
                raise RewardError('frozen reward metadata or owner route changed')
            result.append(meta)
        return tuple(result)


def build_runtime_reward_resolver(pvf_path, *, display_kind_resolver=None):
    """Build during core warmup; fail closed instead of opening a cold reader."""
    from pvf import pvf_runtime

    reader = pvf_runtime.get_shared_reader(pvf_path)
    if reader is None or not pvf_runtime.is_memory_prepared(pvf_path):
        raise RewardError('actual US shared memory PVF must be prepared before reward warmup')
    if Path(item_metadata._resolve_pvf_path()).resolve() != Path(pvf_path).resolve():
        raise RewardError('US item metadata and raid rewards must use the same PVF identity')
    return USInventoryRewardResolver(reader, display_kind_resolver=display_kind_resolver)
