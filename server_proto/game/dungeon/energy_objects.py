"""Resource-selected Energy Containment objects and their native minimap cells.

001D -> 016411A0 -> 01BA04C0 -> 01B94500: the fifth i32 is a
namespace selector (0 monster, 1 passive), not unused padding. Selection uses
the shared entry seed; revisits retain the client's existing object graph.
"""
from dataclasses import dataclass
from functools import lru_cache
import random
import re

from game.quest import quest_index
from game.quest.quest_index import _parse_maze_ridable_script, _section_first
from pvf import pvf_runtime


@dataclass(frozen=True)
class EnergyObject:
    map_x: int
    map_y: int
    object_index: int
    pos_x: int
    pos_y: int
    faction: int
    reserved: int

    def wire(self):
        return dict(vars(self))


@lru_cache(maxsize=1)
def program():
    """Prewarm the actual two-group DGN; no hardcoded spawn coordinates."""
    dungeon = quest_index.get_quest_index().get_dungeon(218)
    text = pvf_runtime.read_text(dungeon.path)
    blocks = re.findall(r'(?is)\[randomized\s+object\s+creation\](.*?)'
                        r'\[/randomized\s+object\s+creation\]', text)
    groups = []
    for body in blocks:
        parsed = _parse_maze_ridable_script('[randomized object creation]' + body
                                          + '[/randomized object creation]')
        entries = re.findall(r'(?is)\[object\](.*?)\[/object\]', body)
        if parsed is None or len(entries) != len(parsed.objects) or parsed.regenerate:
            raise ValueError('Energy randomized declarations are incomplete or regenerate')
        rows = []
        for raw, obj in zip(entries, parsed.objects):
            kind = _section_first(raw, 'object type', '').strip().strip('`').lower()
            if kind not in ('', 'passive object'):
                raise ValueError('unverified Energy randomized namespace')
            rows.append(EnergyObject(obj.map_x, obj.map_y, obj.object_index,
                                     obj.pos_x, obj.pos_y, obj.faction,
                                     int(kind == 'passive object')))
        if not 0 < parsed.select_count <= len(rows) or not 0 <= parsed.minimap_icon <= 255:
            raise ValueError('invalid Energy randomized selection')
        groups.append((parsed.select_count, parsed.minimap_icon, tuple(rows)))
    # This is intentionally scoped to the frozen raid, not every randomized
    # DGN mechanic (regeneration and multiple maze variants need other owners).
    if len(groups) != 2 or [g[0] for g in groups] != [5, 1]:
        raise ValueError('Energy randomized group contract changed')
    return tuple(groups)


def select(seed):
    rng = random.Random(int(seed) & 0xffffffff)
    result = []
    for count, icon, rows in program():
        chosen = set(rng.sample(range(len(rows)), count))
        result.append((icon, tuple(row for i, row in enumerate(rows) if i in chosen)))
    return tuple(result)


def room_objects(dungeon_id, maze_index, x, y, *, seed):
    if int(dungeon_id) != 218:
        return []
    if int(maze_index) != 0:
        raise ValueError('Energy object owner requires its actual maze')
    return [row.wire() for _, group in select(seed) for row in group
            if (row.map_x, row.map_y) == (int(x), int(y))]


def initialize(session, dungeon_id, maze_index, *, seed):
    if session is None:
        return ()
    session.dungeon_energy_objects = None
    if int(dungeon_id) != 218:
        return ()
    if int(maze_index) != 0:
        raise ValueError('Energy object owner requires its actual maze')
    groups = select(seed)
    session.dungeon_energy_objects = (int(seed), groups)
    return tuple(tuple((row.map_x, row.map_y) for row in group)
                 for _icon, group in groups)


def run_seed(session):
    owner = getattr(session, 'dungeon_energy_objects', None)
    if owner is None:
        raise ValueError('Energy randomized objects have no accepted entry owner')
    return owner[0]


def passive_shadows(session, dungeon_id, x, y):
    """Bind only selected native passives so DIE preserves their last action."""
    if int(dungeon_id) != 218:
        return []
    owner = getattr(session, 'dungeon_energy_objects', None)
    if owner is None:
        raise ValueError('Energy randomized objects have no entry owner')
    return [dict(kind='passive_object', code=row.object_index, type=9,
                 x=row.pos_x, y=row.pos_y, faction='monster', is_hostile=True,
                 is_ally=False, is_blocking=False, grants_exp=False, killable=True,
                 quest_enemy_type=3, wire_visible=False, seq=0, killed=False,
                 passive_source_kind='randomized_energy')
            for _, group in owner[1] for row in group
            if row.reserved == 1 and (row.map_x, row.map_y) == (int(x), int(y))]
