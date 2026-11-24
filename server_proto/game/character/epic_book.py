# -*- coding: utf-8 -*-
"""Epic Book account balances, Stage3 synchronization, and crafting domain API."""
from __future__ import annotations

import struct
from typing import Any, Mapping

from game.character.character_db import CharacterRepository
from pvf.epic_piece_index import get_epic_piece_index


EPIC_PIECE_BALANCE_SYNC_OPCODE = 0x047E
_BALANCE_HEADER = struct.Struct("<BI")
_BALANCE_ENTRY = struct.Struct("<II")
_BALANCE_MAX_ENTRIES = 4096


def build_epic_piece_balance_sync_047e(
    balances: Mapping[int, int],
    *,
    reset_client: bool,
) -> bytes:
    """Serialize the frozen Stage3 ``S0/047E`` special-material ledger.

    Reader ``sub_164F980`` consumes ``u8 reset_flag, u32 count`` followed by
    ``count * (u32 material_id, u32 absolute_balance)``.  A zero reset flag
    clears the client ledger first.  ``sub_BEF960`` assigns the supplied value,
    so incremental refreshes must send the latest absolute balance.
    """
    rows = sorted(
        (
            int(piece_id),
            min(max(0, int(balance)), 0x7FFFFFFF),
        )
        for piece_id, balance in balances.items()
        if 0 < int(piece_id) <= 0xFFFFFFFF
    )
    if len(rows) > _BALANCE_MAX_ENTRIES:
        raise ValueError("epic_piece_balance_sync_overflow")

    payload = bytearray(_BALANCE_HEADER.pack(0 if reset_client else 1, len(rows)))
    for piece_id, balance in rows:
        payload.extend(_BALANCE_ENTRY.pack(piece_id, balance))
    return bytes(payload)


def build_account_epic_piece_balance_sync_047e(
    repo: CharacterRepository,
    account_id: int,
    *,
    reset_client: bool = True,
) -> bytes:
    """Build an account-scoped sync containing only PVF-known Epic fragments."""
    aid = int(account_id)
    if aid <= 0:
        return build_epic_piece_balance_sync_047e({}, reset_client=reset_client)
    balances = repo.list_epic_piece_balances(aid)
    piece_ids = frozenset(
        int(piece_id)
        for piece_id in get_epic_piece_index().piece_by_equipment.values()
    )
    filtered = {
        int(piece_id): int(balance)
        for piece_id, balance in balances.items()
        if int(piece_id) in piece_ids and int(balance) > 0
    }
    return build_epic_piece_balance_sync_047e(filtered, reset_client=reset_client)


def craft_epic_book_item(
    repo: CharacterRepository, character_id: int, output_item_id: int
) -> tuple[bool, str, dict[str, Any]]:
    """Craft one exact PVF recipe after a verified opcode reaches this domain."""
    recipe = get_epic_piece_index().recipe_for_output(int(output_item_id))
    if recipe is None:
        return False, "epic_book_recipe_not_found", {}
    ok, note, detail = repo.craft_epic_book_item(
        int(character_id),
        output_item_id=int(recipe.output_item_id),
        piece_id=int(recipe.piece_id),
        piece_count=int(recipe.piece_count),
        materials=tuple((int(item_id), int(count)) for item_id, count in recipe.materials),
    )
    return ok, note, detail
