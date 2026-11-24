"""Offline USDOF worldmap dungeon index (no runtime PVF).

Built by pytools/extract_dungeon_worldmap_from_usdof_pvf.py from
server_proto/data/pvf/USDOF_client_script_readable_20260705.pvf
into server_proto/data/resources/dungeon_worldmap_usdof.json.

Used by enter-select 0005 permission builder so nodes match the
town `[dungeon gate]` worldmap (e.g. SilverCrown area2 -> Abnova 144+).
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

_DEFAULT_JSON = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "resources"
    / "dungeon_worldmap_usdof.json"
)


def _json_path() -> Path:
    env = (os.environ.get("USDOF_DUNGEON_WORLDMAP_JSON") or "").strip()
    if env:
        return Path(env)
    return _DEFAULT_JSON


@lru_cache(maxsize=1)
def load_worldmap_index() -> dict[str, Any]:
    path = _json_path()
    if not path.is_file():
        return {
            "worldmap_areas": {},
            "town_area_to_worldmap": {},
            "all_worldmap_dungeon_ids": [],
        }
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {
            "worldmap_areas": {},
            "town_area_to_worldmap": {},
            "all_worldmap_dungeon_ids": [],
        }
    return data


def resolve_worldmap_id(town_id: int, area_id: int) -> int | None:
    idx = load_worldmap_index()
    mapping = idx.get("town_area_to_worldmap") or {}
    key = "%d:%d" % (int(town_id) & 0xFF, int(area_id) & 0xFF)
    if key in mapping:
        return int(mapping[key])
    # If area was already forced to 0xFF, try any gate for this town.
    town_prefix = "%d:" % (int(town_id) & 0xFF)
    candidates = [
        int(v)
        for k, v in mapping.items()
        if str(k).startswith(town_prefix)
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def dungeon_ids_for_worldmap(worldmap_id: int) -> list[int]:
    idx = load_worldmap_index()
    area = (idx.get("worldmap_areas") or {}).get(str(int(worldmap_id)))
    if not isinstance(area, dict):
        return []
    out: list[int] = []
    for raw in area.get("dungeon_ids") or []:
        try:
            did = int(raw)
        except Exception:
            continue
        if did > 0:
            out.append(did)
    return out


def worldmap_id_for_dungeon(dungeon_id: int) -> int | None:
    """Resolve the owning worldmap from the current PVF-derived index.

    This is the inverse of ``dungeon_ids_for_worldmap`` and deliberately has
    no fixed dungeon-id table, so a refreshed index also covers added maps.
    """
    target = int(dungeon_id)
    if target <= 0:
        return None
    areas = load_worldmap_index().get("worldmap_areas") or {}
    for raw_id, area in areas.items():
        if not isinstance(area, dict):
            continue
        for raw_dungeon in area.get("dungeon_ids") or []:
            try:
                if int(raw_dungeon) == target:
                    return int(area.get("worldmap_id", raw_id))
            except Exception:
                continue
    return None


def dungeon_ids_for_town_area(town_id: int, area_id: int) -> list[int]:
    wm = resolve_worldmap_id(town_id, area_id)
    if wm is None:
        return []
    return dungeon_ids_for_worldmap(wm)


def all_worldmap_dungeon_ids() -> list[int]:
    idx = load_worldmap_index()
    out: list[int] = []
    for raw in idx.get("all_worldmap_dungeon_ids") or []:
        try:
            did = int(raw)
        except Exception:
            continue
        if did > 0:
            out.append(did)
    return out


def merge_permission_ids(
    *groups: Iterable[int],
    max_n: int = 0,
) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for group in groups:
        for raw in group:
            try:
                did = int(raw)
            except Exception:
                continue
            if did <= 0 or did in seen:
                continue
            seen.add(did)
            out.append(did)
            if max_n and len(out) >= max_n:
                return out
    return out
