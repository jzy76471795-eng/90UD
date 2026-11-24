"""Attribute view used by Stage3 ITEMLIST payload builders."""
from __future__ import annotations

from typing import Any

from game.chain_loader import load_chain


class InventoryItemView:
    __slots__ = (
        "slot_index",
        "item_template_id",
        "stack_count",
        "durability",
        "instance_value",
        "item_kind",
        "seal_flag",
        "option_value",
        "expire_time",
        "marker_16",
        "pet_serial_or_handle",
        "sort_lock_flag",
        "equipment_lock_id",
        "random_options",
        "amplify_type",
        "amplify_value",
        "forging_level",
        "enchant_index",
        "enchant_upgrade_count",
        "extra_json",
    )

    def __init__(self, row: dict[str, Any]) -> None:
        self.slot_index = int(row.get("slot_index", 0) or 0)
        self.item_template_id = int(row.get("item_template_id", 0) or 0)
        self.stack_count = max(0, int(row.get("stack_count", 0) or 0))
        self.durability = max(0, int(row.get("durability", 0) or 0)) & 0xFFFF
        self.instance_value = max(
            0, int(row.get("instance_value", 0) or 0)
        ) & 0xFFFFFFFF
        self.item_kind = str(row.get("item_kind", "") or "")
        self.seal_flag = max(0, int(row.get("seal_flag", 0) or 0)) & 0xFF
        self.option_value = max(0, int(row.get("option_value", 0) or 0)) & 0xFF
        self.expire_time = max(0, int(row.get("expire_time", 0) or 0)) & 0xFFFFFFFF
        self.marker_16 = int(row.get("marker_16", 0) or 0) & 0xFFFFFFFF
        self.pet_serial_or_handle = max(
            0, int(row.get("pet_serial_or_handle", 0) or 0)
        ) & 0xFFFFFFFF
        self.sort_lock_flag = 1 if int(row.get("sort_lock_flag", 0) or 0) else 0
        self.equipment_lock_id = (
            max(0, int(row.get("equipment_lock_id", 0) or 0)) & 0xFF
        )
        self.extra_json = row.get("extra_json")
        random_option_helper = load_chain("helper-random_option.py")
        self.random_options = random_option_helper.inventory_row_random_options(row)
        from game.character.amplify_option import parse_amplify_state

        self.amplify_type, self.amplify_value = parse_amplify_state(
            row.get("extra_json")
        )
        from game.character.weapon_forging import parse_forging_level

        self.forging_level = parse_forging_level(row.get("extra_json"))
        from game.character.enchant_by_bead import parse_enchant_state

        self.enchant_index, self.enchant_upgrade_count = parse_enchant_state(
            row.get("extra_json")
        )


__all__ = ["InventoryItemView"]
