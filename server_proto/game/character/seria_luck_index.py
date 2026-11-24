"""US Seria policy; rewards remain in the existing random-container script."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace

from pvf import pvf_runtime
from game.character.random_container_index import _section_token_groups


SOURCE_PATH = "etc/seria_luck.etc"
_LOCK = threading.RLock()
_INDEX = None


@dataclass(frozen=True)
class SeriaPurchaseBonus:
    source_item_id: int
    source_count: int
    rewards: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class SeriaLuckIndex:
    item_id: int
    maximum: int
    bonuses: dict[int, SeriaPurchaseBonus]
    direct_boxes: dict[int, int] = field(default_factory=dict)
    independent_direct_boxes: frozenset[int] = field(default_factory=frozenset)


def parse_seria_luck_text(text: str) -> SeriaLuckIndex:
    luck = _section_token_groups(text, "luck")
    rows = _section_token_groups(text, "purchase bonus")
    if len(luck) != 1 or len(luck[0]) != 2 or len(rows) != 1 or len(rows[0]) % 7:
        raise ValueError("invalid Seria luck/bonus section width")
    item_id, maximum = map(int, luck[0])
    if not 0 < item_id <= 0xFFFFFFFF or not 1 <= maximum <= 255:
        raise ValueError("invalid Seria item or maximum")
    bonuses = {}
    for off in range(0, len(rows[0]), 7):
        commodity, source, count, first, first_count, second, second_count = map(
            int, rows[0][off:off + 7]
        )
        values = (commodity, source, count, first, first_count, second, second_count)
        if any(value <= 0 or value > 0xFFFFFFFF for value in values):
            raise ValueError("invalid Seria purchase bonus value")
        if commodity in bonuses or len({source, first, second}) != 3:
            raise ValueError("duplicate Seria commodity or item")
        bonuses[commodity] = SeriaPurchaseBonus(
            source, count, ((first, first_count), (second, second_count))
        )
    direct_rows = _section_token_groups(text, "direct luck box")
    direct_boxes = {}
    if direct_rows:
        if len(direct_rows) != 1 or not direct_rows[0] or len(direct_rows[0]) % 2:
            raise ValueError("invalid direct luck box section width")
        for off in range(0, len(direct_rows[0]), 2):
            source, exempt = map(int, direct_rows[0][off:off + 2])
            if (not 0 < source <= 0xFFFFFFFF or not 0 < exempt <= 0xFFFFFFFF
                    or source in direct_boxes or source == item_id
                    or exempt in (source, item_id)):
                raise ValueError("invalid or duplicate direct luck box")
            direct_boxes[source] = exempt
    independent_rows = _section_token_groups(text, "independent direct luck box")
    independent = frozenset()
    if independent_rows:
        if len(independent_rows) != 1 or not independent_rows[0]:
            raise ValueError("invalid independent direct luck box section")
        values = tuple(map(int, independent_rows[0]))
        if len(values) != len(set(values)) or not set(values) <= direct_boxes.keys():
            raise ValueError("independent box must be a declared direct luck box")
        independent = frozenset(values)
    return SeriaLuckIndex(item_id, maximum, bonuses, direct_boxes, independent)


def get_seria_luck_index() -> SeriaLuckIndex:
    global _INDEX
    with _LOCK:
        if _INDEX is None:
            reader = pvf_runtime.ensure_shared_reader()
            try:
                text = reader.read_text(SOURCE_PATH)
            except (KeyError, FileNotFoundError):
                # Old PVFs retain their previous shop and box behavior.
                _INDEX = SeriaLuckIndex(0, 0, {})
            else:
                _INDEX = parse_seria_luck_text(text)
        return _INDEX


def clear_seria_luck_index_cache() -> None:
    global _INDEX
    with _LOCK:
        _INDEX = None


def resolve_seria_luck_max(item_id: int) -> int:
    index = get_seria_luck_index()
    return index.maximum if int(item_id) == index.item_id or int(item_id) in index.direct_boxes else 0


def resolve_seria_luck_exempt_items(item_id: int) -> tuple[int, ...]:
    exempt = get_seria_luck_index().direct_boxes.get(int(item_id))
    return (exempt,) if exempt is not None else ()


def resolve_seria_purchase_bonus(product) -> tuple[tuple[int, int], ...]:
    rule = get_seria_luck_index().bonuses.get(int(product.commodity_id))
    if rule is None:
        return ()
    if (int(product.item_id), int(product.quantity)) != (rule.source_item_id, rule.source_count):
        raise ValueError("Seria bonus does not match current shop product")
    return rule.rewards


def _direct_lucky_groups_match(lucky_item_id, direct_groups, lucky_groups):
    if direct_groups == lucky_groups:
        return True
    # Only Seria's Luck retired this ticket; the direct opera box retains it.
    # Every other candidate, count, weight, time window and group must match.
    if int(lucky_item_id) != 50002441:
        return False
    filtered = []
    for group in direct_groups:
        candidates = tuple(row for row in group.candidates if row.item_id != 10158125)
        if candidates:
            filtered.append(replace(group, candidates=candidates))
    return tuple(filtered) == lucky_groups


def warmup_seria_luck_index() -> dict:
    from game.character.item_metadata import resolve_item_metadata
    from game.character.random_container_index import resolve_random_box
    from game.shop.cerashop_index import get_cerashop_index

    index = get_seria_luck_index()
    if not index.item_id:
        return {"ok": 1, "enabled": 0}
    box = resolve_random_box(index.item_id)
    if box is None or box.sealing_removal_item is not None:
        raise ValueError("Seria lucky reward box must open without another material")
    for source, exempt in index.direct_boxes.items():
        outer = resolve_random_box(source)
        if outer is None or not outer.groups:
            raise ValueError("direct lucky box rewards unavailable")
        if source in index.independent_direct_boxes:
            if len(outer.groups) != len(box.groups) + 1:
                raise ValueError("independent direct box must preserve reward group count")
            for group in outer.groups[1:]:
                for reward in group.candidates:
                    meta = resolve_item_metadata(reward.item_id)
                    if not meta.is_stackable or not str(meta.note).startswith("ok"):
                        raise ValueError("independent box reward metadata unavailable: %d" % reward.item_id)
        elif not _direct_lucky_groups_match(index.item_id, outer.groups[1:], box.groups):
            raise ValueError("direct lucky box must preserve all lucky reward groups")
        fixed = outer.groups[0].candidates
        if len(fixed) != 1 or fixed[0].item_id != exempt or fixed[0].count != 2:
            raise ValueError("direct lucky box fixed reward must remain two teleport potions")
    catalog = get_cerashop_index()
    for commodity, rule in index.bonuses.items():
        product = catalog.resolve(commodity)
        if product is None:
            raise ValueError("Seria bonus commodity missing")
        resolve_seria_purchase_bonus(product)
        for item_id, _count in ((rule.source_item_id, rule.source_count),) + rule.rewards:
            meta = resolve_item_metadata(item_id)
            if not meta.is_stackable or not str(meta.note).startswith("ok"):
                raise ValueError("Seria bonus item metadata unavailable: %d" % item_id)
        outer_id, _ = rule.rewards[0]
        hammer_id, _ = rule.rewards[1]
        outer = resolve_random_box(outer_id)
        if (outer is None or outer.sealing_removal_item is None
                or (outer.sealing_removal_item.item_id, outer.sealing_removal_item.count) != (hammer_id, 1)):
            raise ValueError("Seria outer box must consume one bonus hammer")
    return {"ok": 1, "enabled": 1, "item_id": index.item_id,
            "maximum": index.maximum, "bonus_products": len(index.bonuses)}
