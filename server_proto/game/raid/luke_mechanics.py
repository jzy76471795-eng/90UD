"""Frozen Luke resource rules; C0297 is a bounded report, never a raw write.

03171B40 -> action189 -> 02B01360 -> 01199330 writes symbol:u32,
operand:u32, mode:u8. There is no object identity in that native request.
The bridge must bind it to one real, sent, owned room machine.
"""
from dataclasses import dataclass
from hashlib import sha256
from posixpath import basename, dirname, join, normpath
import re
from types import MappingProxyType

from .actual_actor_classifier import _listing
from .catalog import _fields
from .runtime import RaidError

C_RAID_SET_SYMBOL = 0x0297


@dataclass(frozen=True)
class LukeMechanics:
    caps: object
    symbols: object
    object_dungeons: object
    tiers: tuple
    maze_records: tuple
    phase_coin_limits: tuple
    dungeon_coin_limits: object
    evidence: tuple

    @classmethod
    def load(cls, source, config, program, dungeon_resources):
        if config.raid_id != 1 or program.family != 'luke':
            raise RaidError('actual Luke configuration required')
        evidence = {}
        def read(path):
            text = source.read_text(path)
            if not isinstance(text, str) or not text.strip():
                raise RaidError('Luke resource unavailable: ' + path)
            evidence[path] = sha256(text.encode('utf-8')).hexdigest()
            return text
        fields = _fields(read(config.path))
        def one(name):
            values = [f.values for f in fields if f.tag == name]
            if len(values) != 1:
                raise RaidError('one actual Luke field required: ' + name)
            return values[0]
        caps, inside = {}, False
        for f in fields:
            if f.tag == 'DEFAULT OBJECT COUNT':
                if inside or caps:
                    raise RaidError('repeated Luke object budget')
                inside = True
            elif f.tag == '/DEFAULT OBJECT COUNT':
                inside = False
            elif inside:
                if len(f.values) != 2:
                    raise RaidError('invalid Luke object budget')
                cap, dungeon = map(int, f.values)
                if (not 1 <= cap <= 255 or dungeon in caps or
                        program.sanctum_symbols.get(dungeon) != f.tag + ' OBJECT'):
                    raise RaidError('Luke object budget owner mismatch')
                caps[dungeon] = cap
        if inside or set(caps) != set(program.sanctum_symbols) or len(caps) != 3:
            raise RaidError('incomplete Luke object budgets')
        symbols = {d: program.symbols[name] for d, name in program.sanctum_symbols.items()}
        values = tuple(map(int, one('1PHASE OBJ DESTROY')))
        tiers = tuple(tuple(values[i:i+3]) for i in range(0, len(values), 3))
        if not tiers or len(values) % 3:
            raise RaidError('invalid Luke generator tier table')
        lower = 0
        for value, lo, hi in tiers:
            if not 0 <= value <= 255 or lo != lower or hi < lo:
                raise RaidError('Luke generator tier gap')
            lower = hi + 1
        if lower != sum(caps.values()) + 1:
            raise RaidError('Luke generator tier coverage differs from actual budget')
        values = tuple(map(int, one('CARD SET MAP INFO')))
        records = tuple(tuple(values[i:i+3]) for i in range(0,len(values),3))
        if not records or len(values) % 3 or len(records) > 127:
            raise RaidError('invalid Luke maze card records')
        for component in range(3):
            covered = set()
            for row in records:
                cells = set(range(row[component], row[component]+3))
                if row[component] < 0 or covered & cells:
                    raise RaidError('overlapping Luke maze triplets')
                covered.update(cells)
        listing = _listing(read('passiveobject/passiveobject.lst'), 'passiveobject')
        objects = {}
        # Destruction actions are authored as etc-actions rather than LAST ACTION.
        for code, path in listing.items():
            if not (path.startswith('passiveobject/actionobject/map/luke_raid/phase1/')
                    and path.endswith('_raid.obj')):
                continue
            text = read(path)
            refs = re.search(r'(?is)\[etc action\](.*?)\[/etc action\]', text)
            if refs is None:
                continue
            for relative in re.findall(r'`([^`]+)`', refs[1]):
                # Other visual etc-actions include absent legacy references;
                # only these verified terminal scripts can grant machine credit.
                if basename(relative.replace('\\','/')).lower() not in (
                        'luke_mainmchine_destroy_raid.act', 'luke_mainmchine_destroy_mistral_raid.act',
                        'mainmchine_destroy_raid.act', 'mainmchine_destroy.act',
                        'censor_machine_broken_front_raid.act'):
                    continue
                action = normpath(join(dirname(path), relative.replace('\\','/').lower()))
                if not action.startswith('passiveobject/actionobject/map/luke_raid/phase1/'):
                    raise RaidError('Luke action escaped its resource root')
                actual = read(action)
                effect = re.findall(r'(?is)\[RAID SET SYMBOL\]\s*(\d+)\s*\[\+\]\s*(\d+)', actual)
                if not effect:
                    continue
                if (len(effect) != 1 or effect[0][1] != '1' or
                        '[ON SET ACTION]' not in actual.upper()):
                    raise RaidError('unsupported Luke machine destruction action')
                owners = [d for d, symbol in symbols.items() if symbol == int(effect[0][0])]
                if len(owners) != 1 or objects.get(code, owners[0]) != owners[0]:
                    raise RaidError('ambiguous Luke machine symbol owner')
                objects[code] = owners[0]
        if set(objects.values()) != set(caps):
            raise RaidError('Luke indexed machine closure incomplete')
        phase_limits = []
        inside = False
        for f in fields:
            if f.tag == 'PHASE':
                inside = True
                phase_limits.append(0)
            elif f.tag == '/PHASE':
                inside = False
            elif inside and f.tag == 'PHASE COIN LIMIT':
                if len(f.values) != 1 or phase_limits[-1]:
                    raise RaidError('invalid Luke phase coin limit')
                phase_limits[-1] = int(f.values[0])
        dungeon_limits = {}
        for resource in dungeon_resources:
            limits = [f.values for f in _fields(read(resource.path)) if f.tag == 'COIN LIMIT']
            if len(limits) > 1 or limits and len(limits[0]) != 1:
                raise RaidError('invalid Luke dungeon coin limit')
            dungeon_limits[resource.dungeon_id] = int(limits[0][0]) if limits else 0
        if len(phase_limits) != 2 or any(not 0 <= x <= 255 for x in phase_limits + list(dungeon_limits.values())):
            raise RaidError('Luke coin limits outside native range')
        return cls(MappingProxyType(caps),MappingProxyType(symbols),MappingProxyType(objects),
                   tiers,records,tuple(phase_limits),MappingProxyType(dungeon_limits),tuple(sorted(evidence.items())))

    def tier(self, counts):
        if any(type(counts.get(d,0)) is not int or not 0 <= counts.get(d,0) <= cap for d,cap in self.caps.items()):
            raise RaidError('Luke generator count exceeds actual budget')
        total = sum(counts.get(d,0) for d in self.caps)
        return next(value for value,lo,hi in self.tiers if lo <= total <= hi)

    def maze(self, dungeon, record, ordinal):
        if dungeon not in self.caps or not 0 <= record < len(self.maze_records) or not 0 <= ordinal < 3:
            raise RaidError('Luke owned sanctum maze ordinal unavailable')
        component = tuple(sorted(self.caps)).index(dungeon)
        return self.maze_records[record][component] + ordinal
