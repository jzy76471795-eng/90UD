"""Read-only raid login landing resolved against the actual US town structure.

Gate areas are ordinary town gates, not invented Seria rooms. The current
PVF gives map, area and position. Direction/area state come from the existing
US town index and must be present. Load once during shared PVF core warmup;
login uses the resulting immutable TownCeraRoom values without further I/O.
"""
from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Mapping

from game.town_cera_rooms import TownCeraRoom
from .catalog import CatalogError, TextSource, _fields, parse_list

_CHANNEL_TOWNS = {23: 19, 32: 32}


def is_raid_waiting_position(session, character_id: int) -> bool:
    """Temporary raid coordinates belong only to this selected full actor ID."""
    channel = getattr(session, 'selected_channel_id', None)
    selected = getattr(session, 'selected_character_id', None)
    if type(channel) is not int or type(selected) is not int or type(character_id) is not int:
        return False
    marker = {23: 'anton_waiting_position_character_id',
              32: 'luke_waiting_position_character_id'}.get(channel)
    owner = getattr(session, marker, None) if marker else None
    return type(owner) is int and owner > 0 and owner == selected == character_id


class LandingError(ValueError):
    pass


def _actual_gate(text: str) -> tuple[int, str, int, int]:
    fields = _fields(text)
    gates = []
    index = 0
    while index < len(fields):
        row = fields[index]
        if row.tag != 'AREA':
            index += 1
            continue
        if len(row.values) != 2:
            raise LandingError('town AREA header must provide one id and one map')
        try:
            area_id = int(row.values[0])
        except ValueError as exc:
            raise LandingError('town area id is invalid') from exc
        path = row.values[1]
        index += 1
        while index < len(fields) and fields[index].tag != '/AREA':
            if fields[index].tag == 'AREA':
                raise LandingError('nested or unterminated town area')
            values = fields[index].values
            if '[gate]' in values:
                marker = values.index('[gate]')
                if marker + 3 != len(values):
                    raise LandingError('town gate marker must have exactly two coordinates')
                try:
                    x, y = (int(value) for value in values[marker + 1:])
                except ValueError as exc:
                    raise LandingError('town gate position is invalid') from exc
                gates.append((area_id, path, x, y))
            index += 1
        if index >= len(fields):
            raise LandingError('town area has no end')
        index += 1
    if len(gates) != 1 or gates[0][0] != 0:
        raise LandingError('raid landing requires one actual area-0 gate')
    return gates[0]


def build_raid_login_index(source: TextSource, town_index: Mapping) -> dict[int, TownCeraRoom]:
    """Validate existing static gate fields against real town/town.lst and T​​WN."""
    try:
        paths = parse_list(source.read_text('town/town.lst'))
        result = {}
        for town_id in _CHANNEL_TOWNS.values():
            relative = paths[town_id]
            path = relative if relative.startswith('town/') else 'town/' + relative
            area_id, map_path, x, y = _actual_gate(source.read_text(path))
            row = town_index['towns'][str(town_id)]
            gate = row['gate']
            if int(row['town_id']) != town_id or str(row['file']).lower() != PurePosixPath(path).name.lower():
                raise LandingError('raid town identity does not match current PVF')
            if (int(gate['area_id']), str(gate['map']).lower(), int(gate['pos_x']), int(gate['pos_y'])) != (area_id, map_path.lower(), x, y):
                raise LandingError('raid town gate does not match current PVF')
            direction = int(gate['direction'])
            state = int(gate['area_state'])
            if not 0 <= direction <= 255 or not 0 <= state <= 255:
                raise LandingError('raid town direction or area state is outside u8')
            result[town_id] = TownCeraRoom(
                town_id=town_id, area_id=area_id, pos_x=x, pos_y=y,
                direction=direction, area_state=state, map_path=map_path,
                file_name=PurePosixPath(path).name, source='raid-pvf-gate',
            )
        return result
    except (KeyError, TypeError, FileNotFoundError, CatalogError) as exc:
        raise LandingError('raid landing dependencies are missing or invalid') from exc


def resolve_raid_login_spawn(session, actual_town_structure_index: Mapping[int, TownCeraRoom]) -> TownCeraRoom | None:
    """Return an actual raid gate or None to retain the normal login flow."""
    town_id = _CHANNEL_TOWNS.get(getattr(session, 'selected_channel_id', None))
    if town_id is None:
        return None
    try:
        spawn = actual_town_structure_index[town_id]
    except KeyError as exc:
        raise LandingError('selected raid channel has no validated town gate') from exc
    if not isinstance(spawn, TownCeraRoom) or spawn.town_id != town_id or spawn.area_id != 0 or spawn.source != 'raid-pvf-gate':
        raise LandingError('selected raid channel town gate is not validated')
    return spawn


def load_runtime_raid_login_index() -> dict[int, TownCeraRoom]:
    from game.town_cera_rooms import resolve_town_cera_rooms_json_path
    from pvf import pvf_runtime
    if not pvf_runtime.is_runtime_ready():
        raise LandingError('shared PVF must be fully warmed before loading raid gates')
    path = resolve_town_cera_rooms_json_path()
    if path is None:
        raise LandingError('existing town gate index is missing')
    town_index = json.loads(path.read_text(encoding='utf-8'))
    return build_raid_login_index(pvf_runtime, town_index)
