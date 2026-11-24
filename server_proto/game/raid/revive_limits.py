"""Prewarmed resource limits for shared ordinary-raid revival budgets."""
from dataclasses import dataclass
from types import MappingProxyType
from .catalog import _fields
from .runtime import RaidError


@dataclass(frozen=True)
class ReviveLimits:
    phase: tuple
    dungeon: object

    @classmethod
    def load(cls, source, config, resources):
        phases, inside = [], False
        for f in config.fields:
            if f.tag == 'PHASE':
                inside = True
                phases.append(0)
            elif f.tag == '/PHASE':
                inside = False
            elif inside and f.tag == 'PHASE COIN LIMIT':
                if len(f.values) != 1 or phases[-1]:
                    raise RaidError('invalid raid phase coin limit')
                phases[-1] = int(f.values[0])
        ids = {d.dungeon_id for p in config.phases for d in p.dungeons}
        limits = {}
        for resource in resources:
            if resource.dungeon_id not in ids:
                continue
            rows = [f.values for f in _fields(source.read_text(resource.path)) if f.tag == 'COIN LIMIT']
            if any(len(row) != 1 for row in rows):
                raise RaidError('invalid raid dungeon coin limit')
            # Sequential DGN scalar assignments use the last authored value.
            # Frozen Anton fleet_fire declares 4 first, then overrides it to 12.
            limits[resource.dungeon_id] = int(rows[-1][0]) if rows else 0
        if (len(phases) != len(config.phases) or set(limits) != ids
                or any(not 0 <= n <= 255 for n in phases + list(limits.values()))):
            raise RaidError('incomplete raid revive limits')
        return cls(tuple(phases), MappingProxyType(limits))
