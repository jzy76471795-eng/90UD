"""Session-only inventory for Tower of Death temporary items.

Stage3 syncs these items through S0/000E inventory list ``0x1C`` while the run
is active.  C1 use requests may still encode list ``0``; the server keeps the
authoritative counts on the Game session and never writes them to
``character_items`` or an account material balance.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from game.chain_loader import load_chain
from game.character.item_metadata import resolve_item_metadata


_tower_drop_policy = load_chain("helper-tower_drop_policy.py")

DEATH_TOWER_LIST_TYPE = 0x1C
_QUICK_SLOT_START = 3
_QUICK_SLOT_END = 8
_MAX_STACK_COUNT = 0x7FFFFFFF


@dataclass(frozen=True)
class DeathTowerItemDefinition:
    item_template_id: int
    use_kind: str
    stack_limit: int
    allocation_order: tuple[int, ...]


def is_active_death_tower_session(session: Any) -> bool:
    """Return whether ``session`` currently owns a live death-tower run."""
    return bool(
        session is not None
        and getattr(session, "in_dungeon_run", False)
        and str(getattr(session, "dungeon_tower_kind", "") or "").lower()
        == "death"
    )


def _temporary_item_ids() -> frozenset[int]:
    try:
        return frozenset(
            int(item_id) & 0xFFFFFFFF
            for item_id in _tower_drop_policy.death_tower_item_template_ids()
            if int(item_id) > 0
        )
    except Exception:
        return frozenset()


def is_death_tower_temporary_item(item_template_id: int) -> bool:
    return (int(item_template_id) & 0xFFFFFFFF) in _temporary_item_ids()


def _inventory(session: Any, *, create: bool = True) -> dict[int, dict[str, Any]]:
    if session is None:
        return {}
    raw = getattr(session, "dungeon_tower_inventory", None)
    if isinstance(raw, dict):
        return raw
    if not create:
        return {}
    state: dict[int, dict[str, Any]] = {}
    session.dungeon_tower_inventory = state
    return state


def _reserved_slots(session: Any) -> set[int]:
    if session is None:
        return set()
    raw = getattr(session, "dungeon_tower_persistent_main_slots", None)
    if not isinstance(raw, (set, frozenset, list, tuple)):
        return set()
    return {
        int(slot)
        for slot in raw
        if 0 <= int(slot) <= 0x7FFF
    }


def initialize_death_tower_inventory(
    session: Any,
    *,
    occupied_main_slots: Iterable[int] = (),
) -> int:
    """Start a fresh temporary inventory and reserve persistent Main slots."""
    if session is None:
        return 0
    reserved = {
        int(slot)
        for slot in occupied_main_slots or ()
        if 0 <= int(slot) <= 0x7FFF
    }
    session.dungeon_tower_inventory = {}
    session.dungeon_tower_persistent_main_slots = reserved
    return len(reserved)


def clear_death_tower_inventory(session: Any) -> int:
    """Discard every temporary entry and its per-run slot reservations."""
    if session is None:
        return 0
    removed_slots = len(_inventory(session, create=False))
    session.dungeon_tower_inventory = {}
    session.dungeon_tower_persistent_main_slots = set()
    return removed_slots


def _normalized_stackable_type(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _dedupe_slots(values: Iterable[int]) -> tuple[int, ...]:
    out: list[int] = []
    seen: set[int] = set()
    for raw in values:
        slot = int(raw)
        if slot < 0 or slot > 0x7FFF or slot in seen:
            continue
        seen.add(slot)
        out.append(slot)
    return tuple(out)


def resolve_death_tower_item_definition(
    item_template_id: int,
) -> DeathTowerItemDefinition | None:
    """Resolve one PVF-derived temporary template and its legal slot policy."""
    item_id = int(item_template_id) & 0xFFFFFFFF
    if item_id <= 0 or not is_death_tower_temporary_item(item_id):
        return None
    try:
        metadata = resolve_item_metadata(item_id)
    except Exception:
        return None
    if not bool(getattr(metadata, "is_stackable", False)):
        return None

    stackable_type = _normalized_stackable_type(
        getattr(metadata, "stackable_type", "")
    )
    if stackable_type.startswith("[waste") or stackable_type.startswith(
        "[unlimited waste"
    ):
        use_kind = "waste"
    elif stackable_type.startswith("[material"):
        use_kind = "material"
    else:
        use_kind = "other"

    try:
        start, end = metadata.slot_range()
        start, end = sorted((int(start), int(end)))
    except Exception:
        return None
    native_slots = range(max(0, start), min(0x7FFF, end) + 1)
    if use_kind == "waste":
        allocation_order = _dedupe_slots(
            list(range(_QUICK_SLOT_START, _QUICK_SLOT_END + 1))
            + list(native_slots)
        )
    else:
        allocation_order = _dedupe_slots(native_slots)
    if not allocation_order:
        return None

    try:
        stack_limit = int(getattr(metadata, "effective_stack_limit", 0) or 0)
    except Exception:
        stack_limit = int(getattr(metadata, "stack_limit", 0) or 0)
    if stack_limit <= 0:
        stack_limit = _MAX_STACK_COUNT
    stack_limit = min(_MAX_STACK_COUNT, max(1, stack_limit))
    return DeathTowerItemDefinition(
        item_template_id=item_id,
        use_kind=use_kind,
        stack_limit=stack_limit,
        allocation_order=allocation_order,
    )


def _new_item_row(
    definition: DeathTowerItemDefinition,
    stack_count: int,
) -> dict[str, Any]:
    return {
        "item_template_id": int(definition.item_template_id),
        "stack_count": max(0, int(stack_count)),
        "stack_limit": int(definition.stack_limit),
        "use_kind": str(definition.use_kind),
    }


def add_death_tower_item(
    session: Any,
    *,
    item_template_id: int,
    count: int = 1,
) -> tuple[bool, str, dict[str, Any]]:
    """Atomically add one temporary drop to the session inventory."""
    item_id = int(item_template_id) & 0xFFFFFFFF
    amount = int(count)
    empty_detail = {
        "item_template_id": item_id,
        "destination_slot": -1,
        "changed_slots": (),
        "added": 0,
        "total": 0,
    }
    if not is_active_death_tower_session(session):
        return False, "inactive_death_tower", empty_detail
    if amount <= 0 or amount > _MAX_STACK_COUNT:
        return False, "invalid_count", empty_detail
    definition = resolve_death_tower_item_definition(item_id)
    if definition is None:
        return False, "unsupported_temporary_item", empty_detail

    inventory = _inventory(session)
    reserved = _reserved_slots(session)
    remaining = amount
    additions: dict[int, int] = {}

    for slot in definition.allocation_order:
        row = inventory.get(slot)
        if not row or int(row.get("item_template_id", 0) or 0) != item_id:
            continue
        current = max(0, int(row.get("stack_count", 0) or 0))
        limit = max(1, int(row.get("stack_limit", definition.stack_limit) or 1))
        available = max(0, limit - current)
        add = min(remaining, available)
        if add <= 0:
            continue
        additions[slot] = additions.get(slot, 0) + add
        remaining -= add
        if remaining == 0:
            break

    if remaining > 0:
        for slot in definition.allocation_order:
            if slot in inventory or slot in reserved or slot in additions:
                continue
            add = min(remaining, definition.stack_limit)
            additions[slot] = add
            remaining -= add
            if remaining == 0:
                break

    if remaining > 0 or not additions:
        return False, "temporary_inventory_full", empty_detail

    changed_slots: list[int] = []
    for slot, add in additions.items():
        row = inventory.get(slot)
        if row is None:
            inventory[slot] = _new_item_row(definition, add)
        else:
            row["stack_count"] = max(0, int(row.get("stack_count", 0) or 0)) + add
        changed_slots.append(int(slot))

    total = sum(
        max(0, int(row.get("stack_count", 0) or 0))
        for row in inventory.values()
        if int(row.get("item_template_id", 0) or 0) == item_id
    )
    detail = {
        "item_template_id": item_id,
        "destination_slot": changed_slots[0],
        "changed_slots": tuple(changed_slots),
        "added": amount,
        "total": total,
        "use_kind": definition.use_kind,
    }
    return (
        True,
        "tower_item_added item=%d count=%d total=%d slot=%d kind=%s"
        % (item_id, amount, total, changed_slots[0], definition.use_kind),
        detail,
    )


def get_death_tower_item(session: Any, slot_index: int) -> dict[str, Any] | None:
    row = _inventory(session, create=False).get(int(slot_index))
    return dict(row) if isinstance(row, dict) else None


def death_tower_item_counts(session: Any) -> dict[int, int]:
    counts: dict[int, int] = {}
    for row in _inventory(session, create=False).values():
        item_id = int(row.get("item_template_id", 0) or 0)
        count = max(0, int(row.get("stack_count", 0) or 0))
        if item_id > 0 and count > 0:
            counts[item_id] = min(
                _MAX_STACK_COUNT,
                counts.get(item_id, 0) + count,
            )
    return counts


def build_death_tower_update_entries(
    session: Any,
    slots: Iterable[int],
) -> list[tuple[int, int, int]]:
    """Build 108-byte common-entry inputs for one authoritative ``000E``."""
    inventory = _inventory(session, create=False)
    entries: list[tuple[int, int, int]] = []
    seen: set[int] = set()
    for raw_slot in slots or ():
        slot = int(raw_slot)
        if slot in seen or slot < 0 or slot > 0x7FFF:
            continue
        seen.add(slot)
        row = inventory.get(slot)
        if row is None:
            entries.append((slot, -1, 0))
            continue
        item_id = int(row.get("item_template_id", 0) or 0)
        count = max(0, int(row.get("stack_count", 0) or 0))
        if item_id <= 0 or count <= 0:
            entries.append((slot, -1, 0))
        else:
            entries.append((slot, item_id, count))
    return entries


def _consume_death_tower_item(
    session: Any,
    *,
    slot_index: int,
    expected_item_id: int,
    count: int,
    required_use_kind: str,
) -> tuple[bool, str, dict[str, Any]]:
    slot = int(slot_index)
    expected = int(expected_item_id) & 0xFFFFFFFF
    amount = int(count)
    detail = {
        "slot_index": slot,
        "item_template_id": expected,
        "removed": 0,
        "remaining": 0,
        "changed_slots": (),
    }
    if not is_active_death_tower_session(session):
        return False, "inactive_death_tower", detail
    row = _inventory(session, create=False).get(slot)
    if row is None:
        return False, "tower_slot_empty", detail
    actual_item_id = int(row.get("item_template_id", 0) or 0) & 0xFFFFFFFF
    remaining_before = max(0, int(row.get("stack_count", 0) or 0))
    detail.update(
        {
            "item_template_id": actual_item_id,
            "remaining": remaining_before,
        }
    )
    if expected > 0 and expected != actual_item_id:
        return False, "tower_item_mismatch", detail
    if str(row.get("use_kind", "") or "") != str(required_use_kind):
        return False, "tower_item_type_mismatch", detail
    if amount <= 0 or amount > remaining_before:
        return False, "tower_item_insufficient", detail

    remaining = remaining_before - amount
    inventory = _inventory(session)
    if remaining > 0:
        row["stack_count"] = remaining
    else:
        inventory.pop(slot, None)
    detail.update(
        {
            "removed": amount,
            "remaining": remaining,
            "changed_slots": (slot,),
        }
    )
    return (
        True,
        "tower_item_consumed item=%d count=%d remain=%d slot=%d kind=%s"
        % (actual_item_id, amount, remaining, slot, required_use_kind),
        detail,
    )


def consume_death_tower_waste(
    session: Any,
    *,
    slot_index: int,
    expected_item_id: int,
) -> tuple[bool, str, dict[str, Any]]:
    return _consume_death_tower_item(
        session,
        slot_index=slot_index,
        expected_item_id=expected_item_id,
        count=1,
        required_use_kind="waste",
    )


def request_targets_death_tower_waste(
    session: Any,
    *,
    slot_index: int,
    expected_item_id: int,
) -> bool:
    """Identify a tower ``002C`` request before persistent bag handling."""
    if not is_active_death_tower_session(session):
        return False
    row = _inventory(session, create=False).get(int(slot_index))
    if row is not None and str(row.get("use_kind", "") or "") == "waste":
        return True
    expected = int(expected_item_id) & 0xFFFFFFFF
    definition = resolve_death_tower_item_definition(expected)
    return bool(definition is not None and definition.use_kind == "waste")


def request_targets_death_tower_material(
    session: Any,
    *,
    slot_index: int,
    expected_item_id: int,
) -> bool:
    """Identify the item-less ``0012`` route before persistent cube handling."""
    if not is_active_death_tower_session(session):
        return False
    row = _inventory(session, create=False).get(int(slot_index))
    if row is not None and str(row.get("use_kind", "") or "") == "material":
        return True
    expected = int(expected_item_id) & 0xFFFFFFFF
    definition = resolve_death_tower_item_definition(expected)
    return bool(definition is not None and definition.use_kind == "material")


def consume_death_tower_material(
    session: Any,
    *,
    slot_index: int,
    expected_item_id: int,
    count: int,
) -> tuple[bool, str, dict[str, Any]]:
    return _consume_death_tower_item(
        session,
        slot_index=slot_index,
        expected_item_id=expected_item_id,
        count=count,
        required_use_kind="material",
    )


def move_death_tower_item(
    session: Any,
    *,
    source_slot: int,
    destination_slot: int,
    requested_count: int,
) -> tuple[bool, str, dict[str, Any]]:
    """Move, merge, or swap two temporary slots without persistent mutations."""
    src = int(source_slot)
    dst = int(destination_slot)
    requested = int(requested_count)
    detail = {
        "move_value32": requested,
        "changed_slots": (),
        "item_template_id": 0,
    }
    if not is_active_death_tower_session(session):
        return False, "inactive_death_tower", detail
    if min(src, dst) < 0 or max(src, dst) > 0x7FFF:
        return False, "tower_slot_out_of_range", detail

    inventory = _inventory(session)
    source = inventory.get(src)
    if source is None:
        return False, "tower_source_empty", detail
    source_item_id = int(source.get("item_template_id", 0) or 0)
    source_count = max(0, int(source.get("stack_count", 0) or 0))
    source_definition = resolve_death_tower_item_definition(source_item_id)
    detail["item_template_id"] = source_item_id
    if source_definition is None or dst not in source_definition.allocation_order:
        return False, "tower_destination_type_blocked", detail
    if dst in _reserved_slots(session):
        return False, "tower_destination_persistent_occupied", detail
    if src == dst:
        return True, "tower_move_noop", detail

    move_count = source_count if requested <= 0 else min(requested, source_count)
    if move_count <= 0:
        return False, "tower_move_count_invalid", detail

    destination = inventory.get(dst)
    changed = (src, dst)
    if destination is None:
        inventory[dst] = dict(source)
        inventory[dst]["stack_count"] = move_count
        source["stack_count"] = source_count - move_count
        if int(source["stack_count"]) <= 0:
            inventory.pop(src, None)
        detail["changed_slots"] = changed
        return True, "tower_move_empty count=%d" % move_count, detail

    destination_item_id = int(destination.get("item_template_id", 0) or 0)
    destination_count = max(0, int(destination.get("stack_count", 0) or 0))
    if destination_item_id == source_item_id:
        destination_limit = max(
            1,
            int(
                destination.get("stack_limit", source_definition.stack_limit)
                or source_definition.stack_limit
            ),
        )
        merged = min(move_count, max(0, destination_limit - destination_count))
        if merged <= 0:
            return False, "tower_destination_stack_full", detail
        destination["stack_count"] = destination_count + merged
        source["stack_count"] = source_count - merged
        if int(source["stack_count"]) <= 0:
            inventory.pop(src, None)
        detail["changed_slots"] = changed
        return True, "tower_move_merge count=%d" % merged, detail

    if move_count != source_count:
        return False, "tower_partial_swap_forbidden", detail
    destination_definition = resolve_death_tower_item_definition(destination_item_id)
    if (
        destination_definition is None
        or src not in destination_definition.allocation_order
        or src in _reserved_slots(session)
    ):
        return False, "tower_source_type_blocked_for_swap", detail
    inventory[src], inventory[dst] = destination, source
    detail["changed_slots"] = changed
    return True, "tower_move_swap", detail
