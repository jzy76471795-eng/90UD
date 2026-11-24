"""Blade Dancer secondary lightsaber equipment rules.

Stage3 live 2026-07-27:
  item 101040487 (equipment/character/swordman/weapon/beamsword/101040487.equ)
  was sent by the client from Main slot 9 to Equipment slot 24.

Slot 24 is the shared support-weapon body slot used by Guardian shields.  A
Blade Dancer may place a beamsword in either the normal weapon slot (12) or
this secondary slot; other ATSwordman grow types and other weapon families
must keep their normal PVF equipment slot.
"""
from __future__ import annotations

from typing import Any


BLADE_DANCER_JOB = 11
BLADE_DANCER_GROW_NIBBLE = 4
SUPPORT_WEAPON_EQUIP_SLOT = 24


def is_blade_dancer(*, job: int, grow_type: int) -> bool:
    return (
        int(job) == BLADE_DANCER_JOB
        and (int(grow_type) & 0x0F) == BLADE_DANCER_GROW_NIBBLE
    )


def is_beamsword(metadata: Any) -> bool:
    if not bool(getattr(metadata, "is_equipment", False)):
        return False
    equipment_type = str(getattr(metadata, "equipment_type", "") or "").strip().lower()
    if equipment_type not in {"[weapon]", "weapon"}:
        return False
    source_path = str(getattr(metadata, "source_path", "") or "").replace("\\", "/").lower()
    return "/weapon/beamsword/" in "/%s" % source_path.lstrip("/")


def can_equip_secondary(
    metadata: Any,
    *,
    job: int,
    grow_type: int,
    destination_slot: int,
) -> bool:
    return (
        int(destination_slot) == SUPPORT_WEAPON_EQUIP_SLOT
        and is_blade_dancer(job=int(job), grow_type=int(grow_type))
        and is_beamsword(metadata)
    )


__all__ = [
    "BLADE_DANCER_JOB",
    "BLADE_DANCER_GROW_NIBBLE",
    "SUPPORT_WEAPON_EQUIP_SLOT",
    "is_blade_dancer",
    "is_beamsword",
    "can_equip_secondary",
]
