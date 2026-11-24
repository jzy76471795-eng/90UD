"""Town Seria/gate spawn rooms for login START_MAP (no Game PVF read).

Offline source:
  pytools/extract_town_cera_rooms_from_usdof_pvf.py
  -> server_proto/data/resources/town_cera_rooms_usdof.json

Rule (s4a12 design, USDOF PVF data):
  force spawn to a real `[gate]` area (Seria room) with x/y from PVF
  if the saved town is Time Gate, use the verified Shonan Seria room
  if the saved town is Devildom Contact, use the Central Park Seria room
  if every other saved town has no Seria room, use the verified Dark City Seria room
  if the saved town is unknown or malformed, use Dark City's Seria room
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from config import BASE_DIR

DEFAULT_TOWN_CERA_ROOMS_JSON = BASE_DIR / "data" / "resources" / "town_cera_rooms_usdof.json"
DARK_CITY_TOWN_ID = 2
DEFAULT_LOGIN_TOWN_ID = DARK_CITY_TOWN_ID

# Explicit destinations for no-gate towns whose matching Seria room lives in
# another town. All other no-gate towns keep the Dark City fallback.
TIME_GATE_TOWN_ID = 12
SHONAN_TOWN_ID = 11
DEVILDOM_CONTACT_TOWN_ID = 35
CENTRAL_PARK_TOWN_ID = 31
_LOGIN_SERIA_TOWN_OVERRIDES: dict[int, tuple[int, str]] = {
    TIME_GATE_TOWN_ID: (SHONAN_TOWN_ID, "timegate-shonan-seria"),
    DEVILDOM_CONTACT_TOWN_ID: (
        CENTRAL_PARK_TOWN_ID,
        "devildom-contact-central-park-seria",
    ),
}


@dataclass(frozen=True)
class TownCeraRoom:
    town_id: int
    area_id: int
    pos_x: int
    pos_y: int
    direction: int = 5
    area_state: int = 3
    map_path: str = ""
    file_name: str = ""
    source: str = "fallback"


def resolve_town_cera_rooms_json_path() -> Path | None:
    raw = (os.environ.get("USDOF_TOWN_CERA_ROOMS_JSON") or "").strip()
    candidates: list[Path] = []
    if raw:
        candidates.append(Path(raw))
    candidates.append(DEFAULT_TOWN_CERA_ROOMS_JSON)
    for path in candidates:
        if path.exists():
            return path
    return None


@lru_cache(maxsize=1)
def _load_table() -> dict[str, Any]:
    path = resolve_town_cera_rooms_json_path()
    if path is None:
        return {
            "fallback": {
                "area_id": 0,
                "pos_x": 474,
                "pos_y": 234,
                "direction": 5,
                "area_state": 3,
            },
            "towns": {},
            "_path": None,
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "fallback": {
                "area_id": 0,
                "pos_x": 474,
                "pos_y": 234,
                "direction": 5,
                "area_state": 3,
            },
            "towns": {},
            "_path": str(path),
        }
    payload["_path"] = str(path)
    return payload


def _fallback_room(town_id: int) -> TownCeraRoom:
    table = _load_table()
    fb = table.get("fallback") or {}
    return TownCeraRoom(
        town_id=max(1, int(town_id) & 0xFF) if int(town_id) > 0 else 1,
        area_id=int(fb.get("area_id", 0)) & 0xFF,
        pos_x=int(fb.get("pos_x", 474)),
        pos_y=int(fb.get("pos_y", 234)),
        direction=int(fb.get("direction", 5)) & 0xFF,
        area_state=int(fb.get("area_state", 3)) & 0xFF,
        source="fallback",
    )


def _normalize_town_id(town_id: int | None) -> int:
    try:
        tid = int(town_id or 0)
    except (TypeError, ValueError):
        return DEFAULT_LOGIN_TOWN_ID
    if tid <= 0 or tid > 0xFF:
        return DEFAULT_LOGIN_TOWN_ID
    return tid


def _known_town_entry(town_id: int | None) -> tuple[int, dict[str, Any]] | None:
    """Return a declared PVF town entry without silently wrapping bad IDs."""
    try:
        tid = int(town_id or 0)
    except (TypeError, ValueError):
        return None
    if tid <= 0 or tid > 0xFF:
        return None
    entry = (_load_table().get("towns") or {}).get(str(tid))
    if not isinstance(entry, dict):
        return None
    return tid, entry


def has_cera_room_for_town(town_id: int | None) -> bool:
    """Whether the PVF table contains a real Seria/gate room for this town."""
    known = _known_town_entry(town_id)
    return known is not None and isinstance(known[1].get("gate"), dict)


def _resolve_seria_npc_town_id(town_id: int | None) -> tuple[int, str]:
    """Resolve a login town without treating an arbitrary gate as Seria's room."""
    known = _known_town_entry(town_id)
    if known is None:
        return DEFAULT_LOGIN_TOWN_ID, "default-dark-city"
    requested_town_id, entry = known
    if isinstance(entry.get("gate"), dict):
        return requested_town_id, "pvf-gate"
    override = _LOGIN_SERIA_TOWN_OVERRIDES.get(requested_town_id)
    if override is not None:
        target_town_id, source = override
        if has_cera_room_for_town(target_town_id):
            return target_town_id, source
    return DEFAULT_LOGIN_TOWN_ID, "seria-npc-fallback"


def get_cera_room_for_town(town_id: int | None) -> TownCeraRoom:
    """Return Seria/gate spawn for town_id (generic for all towns in JSON)."""
    tid = _normalize_town_id(town_id)
    table = _load_table()
    entry = (table.get("towns") or {}).get(str(tid)) or {}
    gate = entry.get("gate") if isinstance(entry, dict) else None
    if not isinstance(gate, dict):
        room = _fallback_room(tid)
        return TownCeraRoom(
            town_id=tid,
            area_id=room.area_id,
            pos_x=room.pos_x,
            pos_y=room.pos_y,
            direction=room.direction,
            area_state=room.area_state,
            file_name=str(entry.get("file") or ""),
            source="fallback-no-gate",
        )
    return TownCeraRoom(
        town_id=tid,
        area_id=int(gate.get("area_id", 0)) & 0xFF,
        pos_x=int(gate.get("pos_x", 474)),
        pos_y=int(gate.get("pos_y", 234)),
        direction=int(gate.get("direction", 5)) & 0xFF,
        area_state=int(gate.get("area_state", 3)) & 0xFF,
        map_path=str(gate.get("map") or ""),
        file_name=str(entry.get("file") or ""),
        source="pvf-gate",
    )


def resolve_login_spawn(
    *,
    town_id: int | None,
    force_town_id: int | None = None,
    saved_area_id: int | None = None,
    saved_pos_x: int | None = None,
    saved_pos_y: int | None = None,
    saved_direction: int | None = None,
    saved_area_state: int | None = None,
    restore_saved_position: bool = False,
) -> TownCeraRoom:
    """Resolve login to a real Seria/gate room.

    Saved area and coordinate fields are deliberately ignored.  A town without
    a gate cannot be reconstructed safely by sending its old area to Stage3.
    The optional parameters remain for call-site compatibility while all login
    paths converge on a confirmed Seria NPC room.  A generic `[gate]` marker
    is insufficient to prove that a town has Seria.
    """
    del saved_area_id, saved_pos_x, saved_pos_y, saved_direction, saved_area_state
    del restore_saved_position
    requested = force_town_id if force_town_id is not None else town_id
    target_town_id, source = _resolve_seria_npc_town_id(requested)
    room = get_cera_room_for_town(target_town_id)
    if source == "pvf-gate":
        return room
    return TownCeraRoom(
        town_id=room.town_id,
        area_id=room.area_id,
        pos_x=room.pos_x,
        pos_y=room.pos_y,
        direction=room.direction,
        area_state=room.area_state,
        map_path=room.map_path,
        file_name=room.file_name,
        source=source,
    )
