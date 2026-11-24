"""Stage3 SP/TP skill-point book effects and durable consumption flow."""
from __future__ import annotations

from typing import Any

from game.character import experience_consumable as _status_consumables
from game.chain_loader import load_chain


def skill_point_book_effect(item_id: int) -> tuple[int, int] | None:
    """Return the warmed PVF ``(SP, TP)`` grant for one template."""
    rule = _status_consumables.resolve_skill_point_consumable(item_id)
    if rule is None:
        return None
    effect = tuple(int(value) for value in rule.effect)
    return effect if (effect[0] > 0) != (effect[1] > 0) else None


def consume_skill_point_book_at(
    repo: Any,
    *,
    character_id: int,
    list_type: int,
    slot_index: int,
    expected_item_id: int,
    hardcore: bool = False,
) -> tuple[bool, str, dict[str, Any]]:
    """Ensure the ledger then atomically consume one book and award its points."""
    cid = int(character_id)
    item_id = int(expected_item_id) & 0xFFFFFFFF
    rule = _status_consumables.resolve_skill_point_consumable(item_id)
    effect = tuple(int(value) for value in rule.effect) if rule is not None else None
    if repo is None or cid <= 0 or effect is None:
        return False, "not_skill_point_book", {}

    try:
        with repo.open() as conn:
            row = conn.execute(
                "SELECT job, level FROM characters "
                "WHERE character_id = ? AND delete_flag = 0",
                (cid,),
            ).fetchone()
    except Exception as exc:
        return False, "character_lookup_fail:%s" % type(exc).__name__, {}
    if row is None:
        return False, "character_missing", {}

    job = int(row["job"] or 0) & 0xFF
    level = max(1, int(row["level"] or 1))
    usable, usable_note = rule.is_usable(
        level=level,
        job=job,
        hardcore=bool(hardcore),
    )
    if not usable:
        return False, usable_note, {}
    try:
        ledger = repo.get_character_skill_points(cid)
    except Exception:
        ledger = None
    if ledger is None or int(ledger.get("synced_level") or 0) < level:
        skill_points = load_chain("helper-skill_points.py")
        skill_points.ensure_skill_points_ledger(
            repo,
            character_id=cid,
            job=job,
            level=level,
        )

    ok, note, detail = repo.consume_stackable_with_skill_point_bonus_at(
        cid,
        list_type=int(list_type),
        slot_index=int(slot_index),
        expected_item_id=item_id,
        bonus_sp_delta=int(effect[0]),
        bonus_tp_delta=int(effect[1]),
    )
    if detail:
        detail["skill_point_book_sp"] = int(effect[0])
        detail["skill_point_book_tp"] = int(effect[1])
        detail["job"] = job
        detail["level"] = level
    return ok, note, detail
