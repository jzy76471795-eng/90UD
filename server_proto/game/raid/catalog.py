"""Read-only raid catalog from the current US PVF.

The catalog describes resources and reports gaps in the CN port contract. It
does not assign opcodes, execute battle triggers, charge tickets or grant loot.
Use the shared, warmed PVF runtime when integrating this module into a server.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Protocol


class TextSource(Protocol):
    def read_text(self, path: str) -> str: ...


class CatalogError(ValueError):
    pass


@dataclass(frozen=True)
class ScriptField:
    tag: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class RaidDungeon:
    dungeon_id: int
    initial_state: str
    hp_symbol: str | None
    hit_points: int | None
    party_max: int | None


@dataclass(frozen=True)
class StateReward:
    # Preserve the five PVF values. Their current-US reward consumer still
    # needs verification; the catalog deliberately assigns no grant meaning.
    values: tuple[str, ...]


@dataclass(frozen=True)
class RaidPhase:
    name: str | None
    dungeons: tuple[RaidDungeon, ...]
    rewards: tuple[StateReward, ...]


@dataclass(frozen=True)
class RaidConfig:
    raid_id: int
    path: str
    text_sha256: str
    member_max: int
    minimum_members: int
    waiting_room: tuple[int, ...]
    waiting_npcs: tuple[int, ...]
    entry_cost: tuple[tuple[int, int], ...]
    start_delay_seconds: int
    phase_seconds: tuple[int, ...]
    phase_break_seconds: int
    movie_seconds: tuple[int, ...]
    daily_account_enter_limit: int | None
    shield_charge_rate: tuple[int, ...] | None
    weekly_clear_limit: int | None
    single_minimum_members: int | None
    phases: tuple[RaidPhase, ...]
    single_phases: tuple[RaidPhase, ...]
    fields: tuple[ScriptField, ...]
    disconnected_member_wait_seconds: int | None = None


@dataclass(frozen=True)
class DungeonResource:
    dungeon_id: int
    path: str
    name: str | None
    minimum_level: int | None
    raid_flag: int | None
    map_ids: tuple[int, ...]


@dataclass(frozen=True)
class DependencyIssue:
    scope: str
    kind: str
    ref: str
    raid_id: int | None = None


@dataclass(frozen=True)
class RaidSymbolTable:
    raid_id: int
    path: str
    entries: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class RaidCatalog:
    raids: tuple[RaidConfig, ...]
    dungeons: tuple[DungeonResource, ...]
    symbols: tuple[RaidSymbolTable, ...]
    issues: tuple[DependencyIssue, ...]


def _tokens(text: str) -> list[str]:
    if text.count('`') % 2:
        raise CatalogError('unterminated PVF string')
    return re.findall(r'`[^`]*`|\[[^\]\r\n]+\]|[^\s]+', text)


def _fields(text: str) -> tuple[ScriptField, ...]:
    out: list[ScriptField] = []
    tag = None
    values: list[str] = []
    for token in _tokens(text):
        if token.startswith('['):
            if tag is not None:
                out.append(ScriptField(tag, tuple(values)))
            tag = token[1:-1].strip().upper()
            values = []
        elif tag is not None:
            values.append(token[1:-1] if token.startswith('`') else token)
    if tag is not None:
        out.append(ScriptField(tag, tuple(values)))
    return tuple(out)


def _one(fields: tuple[ScriptField, ...], tag: str, *, optional: bool = False) -> tuple[str, ...] | None:
    rows = [field.values for field in fields if field.tag == tag]
    if len(rows) > 1 or (not rows and not optional):
        raise CatalogError('missing or repeated field: ' + tag)
    return rows[0] if rows else None


def _ints(values: tuple[str, ...] | None, tag: str, count: int | None = None) -> tuple[int, ...] | None:
    if values is None:
        return None
    try:
        out = tuple(int(value) for value in values)
    except ValueError as exc:
        raise CatalogError('non-integer field: ' + tag) from exc
    if count is not None and len(out) != count:
        raise CatalogError('incorrect field width: ' + tag)
    return out


def _number(fields: tuple[ScriptField, ...], tag: str, *, optional: bool = False) -> int | None:
    values = _ints(_one(fields, tag, optional=optional), tag, 1)
    return None if values is None else values[0]


def _blocks(fields: tuple[ScriptField, ...], tag: str) -> tuple[tuple[ScriptField, ...], ...]:
    out = []
    start = None
    for index, field in enumerate(fields):
        if field.tag == tag:
            if start is not None:
                raise CatalogError('nested section: ' + tag)
            start = index + 1
        elif field.tag == '/' + tag:
            if start is None:
                raise CatalogError('unmatched section end: ' + tag)
            out.append(fields[start:index])
            start = None
    if start is not None:
        raise CatalogError('unterminated section: ' + tag)
    return tuple(out)


def parse_list(text: str) -> dict[int, str]:
    tokens = _tokens(text)
    if len(tokens) % 2:
        raise CatalogError('incomplete PVF list row')
    out = {}
    for offset in range(0, len(tokens), 2):
        try:
            entry_id = int(tokens[offset])
        except ValueError as exc:
            raise CatalogError('invalid PVF list id') from exc
        path = tokens[offset + 1]
        if entry_id in out or not path.startswith('`') or not path.endswith('`'):
            raise CatalogError('duplicate id or invalid PVF list path')
        out[entry_id] = path[1:-1].replace('\\', '/').lower()
    return out


def parse_symbols(text: str) -> tuple[tuple[str, int], ...]:
    tokens = _tokens(text)
    if len(tokens) % 2:
        raise CatalogError('incomplete raid symbol row')
    out = []
    names = set()
    identifiers = set()
    for offset in range(0, len(tokens), 2):
        try:
            identifier = int(tokens[offset])
        except ValueError as exc:
            raise CatalogError('invalid raid symbol identifier') from exc
        tag = tokens[offset + 1]
        name = tag[1:-1].strip().upper()
        if not tag.startswith('[') or not tag.endswith(']') or not name or identifier < 0 or identifier > 65535 or name in names or identifier in identifiers:
            raise CatalogError('duplicate or invalid raid symbol')
        names.add(name)
        identifiers.add(identifier)
        out.append((name, identifier))
    return tuple(out)


def _phase(fields: tuple[ScriptField, ...]) -> RaidPhase:
    dungeons = []
    seen = set()
    for rows in _blocks(fields, 'DUNGEON INFO'):
        dungeon_id = _number(rows, 'INDEX')
        state_values = _one(rows, 'INIT STATE')
        if len(state_values) != 1 or state_values[0] not in ('open', 'close', 'hide'):
            raise CatalogError('unsupported initial dungeon state')
        if dungeon_id <= 0 or dungeon_id in seen:
            raise CatalogError('invalid or repeated dungeon id')
        seen.add(dungeon_id)
        hp_index = next((i for i, row in enumerate(rows) if row.tag == 'HP INFO'), None)
        symbol = rows[hp_index + 1] if hp_index is not None and hp_index + 1 < len(rows) else None
        hp = _ints(symbol.values, 'HP INFO', 1)[0] if symbol else None
        dungeons.append(RaidDungeon(dungeon_id, state_values[0], symbol.tag if symbol else None, hp, _number(rows, 'PARTY MAX', optional=True)))
    if not dungeons:
        raise CatalogError('phase has no dungeons')
    rewards = []
    for block in _blocks(fields, 'CLEAR REWARD ITEM'):
        for row in block:
            if row.tag == 'STATE REWARD':
                if len(row.values) != 5:
                    raise CatalogError('incorrect STATE REWARD width')
                rewards.append(StateReward(row.values))
    names = _one(fields, 'PHASE NAME', optional=True)
    return RaidPhase(names[0] if names else None, tuple(dungeons), tuple(rewards))


def parse_raid_config(raid_id: int, path: str, text: str) -> RaidConfig:
    fields = _fields(text)
    first_phase = next((i for i, row in enumerate(fields) if row.tag in ('PHASE', 'SINGLE RAID PHASE')), len(fields))
    top = fields[:first_phase]
    count = _number(top, 'PHASE MAX')
    phases = tuple(_phase(rows) for rows in _blocks(fields, 'PHASE'))
    single = tuple(_phase(rows) for rows in _blocks(fields, 'SINGLE RAID PHASE'))
    if count <= 0 or count != len(phases):
        raise CatalogError('raid phase count mismatch')
    times = _ints(_one(top, 'PHASE TIME OVER'), 'PHASE TIME OVER', count)
    costs = _ints(_one(top, 'ENTRY COST'), 'ENTRY COST')
    if not costs or len(costs) % 2 or any(value <= 0 for value in costs):
        raise CatalogError('invalid ENTRY COST pairs')
    waiting = _ints(_one(top, 'WAITING ROOM'), 'WAITING ROOM', 2)
    member_max = _number(top, 'RAID MEMBER MAX')
    minimum = _number(top, 'RAID START MINIMUM MEMBER')
    if not 1 <= minimum <= member_max or any(value <= 0 for value in times):
        raise CatalogError('invalid member bounds or phase time')
    return RaidConfig(
        raid_id, path.lower(), hashlib.sha256(text.encode('utf-8')).hexdigest(),
        member_max, minimum, waiting, _ints(_one(top, 'WAITING ROOM NPC'), 'WAITING ROOM NPC'),
        tuple(zip(costs[::2], costs[1::2])), _number(top, 'START DELAY TIME'), times,
        _number(top, 'PHASE BREAK TIME'), _ints(_one(top, 'MOVIE END TIME'), 'MOVIE END TIME', 4),
        _number(top, 'DAILY ACCOUNT ENTER LIMIT', optional=True),
        _ints(_one(top, 'SHIELD CHARGE RATE', optional=True), 'SHIELD CHARGE RATE'),
        _number(top, 'WEEKLY CLEAR COUNT', optional=True),
        _number(top, 'SINGLE RAID START MINIUM MEMBER', optional=True), phases, single, fields,
        _number(top, 'DISCONN MEMBER WAIT TIME', optional=True),
    )


def _resource_path(prefix: str, relative: str) -> str:
    return relative if relative.startswith(prefix + '/') else prefix + '/' + relative


def load_catalog(source: TextSource) -> RaidCatalog:
    issues: list[DependencyIssue] = []
    def read(path: str, scope: str, raid_id: int | None = None) -> str | None:
        try:
            return source.read_text(path)
        except (KeyError, FileNotFoundError):
            issues.append(DependencyIssue(scope, 'missing_path', path, raid_id))
            return None

    def listing(path: str, scope: str) -> dict[int, str]:
        text = read(path, scope)
        return {} if text is None else parse_list(text)

    refs = listing('etc/raid/raid.lst', 'catalog')
    raids = []
    for raid_id, path in refs.items():
        text = read(path, 'catalog', raid_id)
        if text is not None:
            raids.append(parse_raid_config(raid_id, path, text))
    dungeon_paths = listing('dungeon/dungeon.lst', 'ordinary_dungeon')
    map_paths = listing('map/map.lst', 'ordinary_dungeon')
    item_paths = listing('stackable/stackable.lst', 'entry_reward')
    resources = {}
    checked_maps = set()
    checked_items = set()
    symbols = []
    for raid in raids:
        symbol_path = raid.path + '.symbol'
        symbol_text = read(symbol_path, 'cn_battle_program', raid.raid_id)
        if symbol_text is not None:
            entries = parse_symbols(symbol_text)
            symbols.append(RaidSymbolTable(raid.raid_id, symbol_path, entries))
            names = {name for name, _identifier in entries}
            for phase in (*raid.phases, *raid.single_phases):
                for dungeon in phase.dungeons:
                    if dungeon.hp_symbol not in names:
                        issues.append(DependencyIssue('cn_battle_program', 'missing_hp_symbol', str(dungeon.hp_symbol), raid.raid_id))
        if raid.weekly_clear_limit is None:
            issues.append(DependencyIssue('cn_battle_program', 'missing_field', 'field:WEEKLY CLEAR COUNT', raid.raid_id))
        if raid.single_minimum_members is None:
            issues.append(DependencyIssue('cn_single_raid', 'missing_field', 'field:SINGLE RAID START MINIUM MEMBER', raid.raid_id))
        if not raid.single_phases:
            issues.append(DependencyIssue('cn_single_raid', 'missing_field', 'field:SINGLE RAID PHASE', raid.raid_id))
        for phase in (*raid.phases, *raid.single_phases):
            for dungeon in phase.dungeons:
                dungeon_id = dungeon.dungeon_id
                if dungeon_id in resources:
                    continue
                relative = dungeon_paths.get(dungeon_id)
                if relative is None:
                    issues.append(DependencyIssue('ordinary_dungeon', 'missing_list_id', 'dungeon:' + str(dungeon_id), raid.raid_id))
                    continue
                path = _resource_path('dungeon', relative)
                text = read(path, 'ordinary_dungeon', raid.raid_id)
                if text is None:
                    continue
                fields = _fields(text)
                names = _one(fields, 'NAME', optional=True)
                ids = []
                for row in fields:
                    if row.tag != 'MAP SPECIFICATION':
                        continue
                    index = 0
                    while index < len(row.values):
                        if row.values[index] not in ('map', 'boss') or index + 3 >= len(row.values):
                            raise CatalogError('unsupported map specification in ' + path)
                        index += 3  # type, cell x, cell y
                        while index < len(row.values) and row.values[index] not in ('map', 'boss'):
                            try:
                                ids.append(int(row.values[index]))
                            except ValueError as exc:
                                raise CatalogError('invalid map id in ' + path) from exc
                            index += 1
                resources[dungeon_id] = DungeonResource(dungeon_id, path, names[0] if names else None, _number(fields, 'MINIMUM REQUIRED LEVEL', optional=True), _number(fields, 'RAID DUNGEON', optional=True), tuple(dict.fromkeys(ids)))
                for map_id in ids:
                    if map_id in checked_maps:
                        continue
                    checked_maps.add(map_id)
                    relative = map_paths.get(map_id)
                    if relative is None:
                        issues.append(DependencyIssue('ordinary_dungeon', 'missing_list_id', 'map:' + str(map_id), raid.raid_id))
                    else:
                        read(_resource_path('map', relative), 'ordinary_dungeon', raid.raid_id)
            reward_items = [int(reward.values[3]) for reward in phase.rewards]
            for item_id in reward_items + [item for item, _count in raid.entry_cost]:
                if item_id in checked_items:
                    continue
                checked_items.add(item_id)
                relative = item_paths.get(item_id)
                if relative is None:
                    issues.append(DependencyIssue('entry_reward', 'missing_list_id', 'item:' + str(item_id), raid.raid_id))
                else:
                    read(_resource_path('stackable', relative), 'entry_reward', raid.raid_id)
    read('etc/raid/singleraidlinksystem.etc', 'cn_single_raid')
    return RaidCatalog(tuple(raids), tuple(resources.values()), tuple(symbols), tuple(issues))


def load_runtime_catalog() -> RaidCatalog:
    from pvf import pvf_runtime
    if not pvf_runtime.is_runtime_ready():
        raise CatalogError('shared PVF must be fully warmed before loading raid catalog')
    return load_catalog(pvf_runtime)
