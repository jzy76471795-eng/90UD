"""Persistent cleanup for the Tower of Death's session-only inventory items."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from game.chain_loader import load_chain

_tower_drop_policy = load_chain("helper-tower_drop_policy.py")


@dataclass(frozen=True)
class DeathTowerItemCleanup:
    character_id: int
    item_ids: tuple[int, ...]
    removed_count: int
    removed_slots: int
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def note(self) -> str:
        ids = ",".join(str(item_id) for item_id in self.item_ids) or "-"
        state = "ok" if self.ok else "partial"
        suffix = (" errors=" + "|".join(self.errors)) if self.errors else ""
        return (
            "death_tower_temp_items_cleanup=%s cid=%d ids=%s removed=%d slots=%d%s"
            % (
                state,
                int(self.character_id),
                ids,
                int(self.removed_count),
                int(self.removed_slots),
                suffix,
            )
        )


def cleanup_death_tower_items(
    repo: Any,
    character_id: int,
) -> DeathTowerItemCleanup:
    """Delete all current-PVF Tower of Death temporary items from Main inventory."""
    cid = int(character_id or 0)
    item_ids = tuple(_tower_drop_policy.death_tower_item_template_ids())
    if cid <= 0:
        return DeathTowerItemCleanup(0, item_ids, 0, 0, ("invalid_character",))
    if repo is None:
        return DeathTowerItemCleanup(cid, item_ids, 0, 0, ("repository_missing",))

    removed_count = 0
    removed_slots = 0
    errors: list[str] = []
    for item_id in item_ids:
        try:
            ok, detail, mutations = repo.remove_all_inventory_by_template(
                cid,
                int(item_id),
                list_type=0,
            )
        except Exception as exc:
            errors.append("item=%d exc=%s" % (int(item_id), type(exc).__name__))
            continue
        if not ok:
            errors.append("item=%d %s" % (int(item_id), str(detail)))
            continue
        for mutation in mutations or ():
            removed_count += max(0, int(mutation.get("removed", 0) or 0))
            removed_slots += 1
    return DeathTowerItemCleanup(
        cid,
        item_ids,
        removed_count,
        removed_slots,
        tuple(errors),
    )
