"""Atomic persistence for one magic-seal random-option change."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable

from game.character.magic_seal_random_option_index import (
    MAX_RANDOM_OPTION_COUNT,
)


@dataclass(frozen=True)
class MagicSealOptionChangeSettlement:
    target_slot: int
    target_item_uid: int
    target_item_id: int
    option_index: int
    previous_option: dict[str, int]
    updated_option: dict[str, int]
    gold_cost: int
    updated_gold: int


class _MagicSealOptionChangeReject(RuntimeError):
    pass


def _normalize_option(entry: Any) -> dict[str, int]:
    if not isinstance(entry, dict):
        raise ValueError("random_option_entry_invalid")
    option_type = int(entry.get("type", 0) or 0) & 0xFF
    if option_type <= 0:
        raise ValueError("random_option_type_invalid")
    return {
        "type": option_type,
        "value1": int(entry.get("value1", 0) or 0) & 0xFF,
        "value2": int(entry.get("value2", 0) or 0) & 0xFF,
    }


def _normalize_options(entries: Iterable[Any]) -> list[dict[str, int]]:
    options = [_normalize_option(entry) for entry in entries]
    if not options or len(options) > MAX_RANDOM_OPTION_COUNT:
        raise ValueError("random_option_count_invalid")
    return options


def _parse_extra_json(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError) as exc:
        raise _MagicSealOptionChangeReject("target_extra_json_invalid") from exc
    if not isinstance(value, dict):
        raise _MagicSealOptionChangeReject("target_extra_json_invalid")
    return value


def apply_magic_seal_option_change(
    repo: Any,
    *,
    character_id: int,
    target_slot: int,
    expected_item_uid: int,
    expected_item_id: int,
    option_index: int,
    expected_options: Iterable[Any],
    updated_option: Any,
    gold_cost: int,
) -> tuple[bool, str, MagicSealOptionChangeSettlement | None]:
    """Replace one option and deduct account gold in one ``BEGIN IMMEDIATE``."""
    cid = int(character_id)
    slot = int(target_slot)
    expected_uid = int(expected_item_uid)
    item_id = int(expected_item_id) & 0xFFFFFFFF
    selected = int(option_index)
    gold_need = int(gold_cost)
    try:
        expected = _normalize_options(expected_options)
        replacement = _normalize_option(updated_option)
    except (TypeError, ValueError) as exc:
        return False, str(exc), None
    if cid <= 0:
        return False, "character_missing", None
    if slot <= 0 or expected_uid <= 0 or item_id <= 0:
        return False, "target_identity_invalid", None
    if selected < 0 or selected >= len(expected):
        return False, "option_index_invalid", None
    if gold_need < 0:
        return False, "gold_cost_invalid", None
    other_types = {
        int(entry["type"])
        for index, entry in enumerate(expected)
        if index != selected
    }
    if int(replacement["type"]) in other_types:
        return False, "random_option_duplicate", None

    repo._ensure_schema()
    try:
        with repo.open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            character = conn.execute(
                "SELECT account_id FROM characters "
                "WHERE character_id = ? AND delete_flag = 0",
                (cid,),
            ).fetchone()
            if character is None:
                raise _MagicSealOptionChangeReject("character_missing")
            account_id = int(character[0])

            target = conn.execute(
                "SELECT item_uid, item_template_id, item_kind, stack_count, "
                "seal_flag, extra_json FROM character_items "
                "WHERE character_id = ? AND list_type = 0 AND slot_index = ?",
                (cid, slot),
            ).fetchone()
            if target is None:
                raise _MagicSealOptionChangeReject("target_slot_empty")
            actual_uid = int(target[0])
            actual_item_id = int(target[1]) & 0xFFFFFFFF
            if actual_uid != expected_uid or actual_item_id != item_id:
                raise _MagicSealOptionChangeReject(
                    "target_item_changed expected_uid=%d actual_uid=%d "
                    "expected_item=%d actual_item=%d"
                    % (expected_uid, actual_uid, item_id, actual_item_id)
                )
            if str(target[2] or "") != "equipment" or int(target[3] or 0) != 1:
                raise _MagicSealOptionChangeReject("target_instance_invalid")

            raw_extra_json = str(target[5] or "{}")
            extra = _parse_extra_json(raw_extra_json)
            if not bool(extra.get("unsealed")):
                raise _MagicSealOptionChangeReject("target_not_unsealed")
            try:
                actual_options = _normalize_options(
                    extra.get("random_options") or ()
                )
            except (TypeError, ValueError) as exc:
                raise _MagicSealOptionChangeReject(str(exc)) from exc
            if actual_options != expected:
                raise _MagicSealOptionChangeReject("target_options_changed")

            wallet = repo._currency_wallet_tx(conn, cid)
            gold_before = max(0, int(wallet["gold"]))
            if gold_before < gold_need:
                raise _MagicSealOptionChangeReject(
                    "gold_insufficient have=%d need=%d"
                    % (gold_before, gold_need)
                )
            updated_gold = gold_before - gold_need

            final_options = [dict(entry) for entry in actual_options]
            previous = dict(final_options[selected])
            final_options[selected] = dict(replacement)
            extra["unsealed"] = True
            extra["random_options"] = final_options
            merged_extra_json = json.dumps(
                extra,
                ensure_ascii=False,
                separators=(",", ":"),
            )

            if gold_need > 0:
                wallet["gold"] = updated_gold
                changed = conn.execute(
                    "UPDATE character_wallets SET gold = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE character_id = ?",
                    (updated_gold, cid),
                )
                if int(changed.rowcount or 0) != 1:
                    raise _MagicSealOptionChangeReject("wallet_changed")
                repo._project_account_wallet_tx(conn, cid, wallet)

            changed = conn.execute(
                "UPDATE character_items SET extra_json = ?, seal_flag = 0, "
                "updated_at = CURRENT_TIMESTAMP WHERE item_uid = ? "
                "AND character_id = ? AND list_type = 0 AND slot_index = ? "
                "AND item_template_id = ? AND item_kind = 'equipment' "
                "AND stack_count = 1 AND extra_json = ?",
                (
                    merged_extra_json,
                    actual_uid,
                    cid,
                    slot,
                    item_id,
                    raw_extra_json,
                ),
            )
            if int(changed.rowcount or 0) != 1:
                raise _MagicSealOptionChangeReject(
                    "target_item_changed_during_commit"
                )

            settlement = MagicSealOptionChangeSettlement(
                target_slot=slot,
                target_item_uid=actual_uid,
                target_item_id=item_id,
                option_index=selected,
                previous_option=previous,
                updated_option=dict(replacement),
                gold_cost=gold_need,
                updated_gold=updated_gold,
            )
        return True, (
            "magic_seal_option_change_ok slot=%d item=%d index=%d "
            "type=%d->%d gold=%d"
            % (
                slot,
                item_id,
                selected,
                previous["type"],
                replacement["type"],
                gold_need,
            )
        ), settlement
    except _MagicSealOptionChangeReject as exc:
        return False, str(exc), None
    except (sqlite3.Error, TypeError, ValueError) as exc:
        return False, "magic_seal_option_change_transaction_error:%s:%s" % (
            type(exc).__name__,
            exc,
        ), None


__all__ = [
    "MagicSealOptionChangeSettlement",
    "apply_magic_seal_option_change",
]
