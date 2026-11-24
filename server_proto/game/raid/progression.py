"""Actual PVF normal Anton/Luke trigger compiler and immutable reducer.

Translated from CN raid/anton_program.go, anton_progression.go,
luke_program.go and anton_symbol_journal.go. No transport or rewards live here.
The caller must supply server-owned entry/map/object death/clear events.
"""

import copy
import re
from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType

from .runtime import DungeonState, RaidError


class ProgressionError(RaidError):
    pass


@dataclass(frozen=True)
class Timer:
    end: float
    duration: int


@dataclass(frozen=True)
class SymbolWrite:
    sequence: int
    name: str
    symbol_id: int
    value: int


@dataclass(frozen=True)
class _Field:
    name: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class _Clause:
    kind: str
    dungeon: int = 0
    state: int = 0
    timer: int = 0
    value: int = 0
    symbol: str = ""
    add: bool = False


@dataclass(frozen=True)
class _Rule:
    conditions: tuple[_Clause, ...]
    actions: tuple[_Clause, ...]


@dataclass(frozen=True)
class _Phase:
    states: tuple[DungeonState, ...]
    hp: object
    party_max: object
    rules: tuple[_Rule, ...]


@dataclass(frozen=True)
class BarrierConfiguration:
    small: int | None
    large: int
    constructor: int
    infected_constructor: int

    @classmethod
    def from_action_texts(cls, small_text, large_text, constructor_texts):
        def amounts(text):
            fields, result = _fields(text), []
            for i, field in enumerate(fields):
                if field.name != "RAID SET SYMBOL":
                    continue
                if len(field.values) != 1:
                    raise ProgressionError("barrier action symbol width invalid")
                if _uint(field.values[0]) != 110:
                    continue
                if i + 1 >= len(fields) or fields[i + 1].name != "-" or len(fields[i + 1].values) != 1:
                    raise ProgressionError("unsupported formal barrier operation")
                value = _uint(fields[i + 1].values[0])
                if not 1 <= value <= 10000:
                    raise ProgressionError("actual barrier amount invalid")
                result.append(value)
            return fields, result

        small, large = amounts(small_text)[1], amounts(large_text)[1]
        # The old US small-transmitter script only changes Cave_02's local
        # dungeon symbol. It supplies no raid barrier subtraction. Preserve
        # that absence; a small-transmitter damage event must then be rejected.
        if len(small) > 1 or len(large) != 1 or len(constructor_texts) != 4:
            raise ProgressionError("formal barrier actions incomplete/ambiguous")
        constructor, infected = None, None
        for index, text in enumerate(constructor_texts):
            fields, values = amounts(text)
            checks = 0
            for i, field in enumerate(fields):
                if field.name != "CHECK RAID SYMBOL":
                    continue
                if (field.values != ("8",) or i + 1 >= len(fields)
                        or fields[i + 1].name != "=" or fields[i + 1].values != (str(221 + index),)):
                    raise ProgressionError("constructor infection owner mismatch")
                checks += 1
            if len(values) != 2 or checks != 1:
                raise ProgressionError("formal constructor amounts incomplete")
            if constructor is None:
                constructor, infected = values
            elif values != [constructor, infected]:
                raise ProgressionError("constructor damage inconsistent across actual hatcheries")
        return cls(small[0] if small else None, large[0], constructor, infected)


def _tokens(text):
    if text.count("`") % 2:
        raise ProgressionError("unterminated PVF string")
    return re.findall(r"`[^`]*`|\[[^\]\r\n]+\]|[^\s]+", text)


def _fields(text):
    result, name, values = [], None, []
    for token in _tokens(text):
        if token.startswith("["):
            if name is not None:
                result.append(_Field(name, tuple(values)))
            name, values = token[1:-1].strip().upper(), []
        elif name is not None:
            values.append(token[1:-1] if token.startswith("`") else token)
    if name is not None:
        result.append(_Field(name, tuple(values)))
    return tuple(result)


def _uint(value):
    try:
        n = int(value)
    except (ValueError, TypeError):
        raise ProgressionError("invalid literal integer") from None
    if not 0 <= n <= 0xffffffff:
        raise ProgressionError("literal outside native integer range")
    return n


def _state(value):
    if value.lower() not in ("open", "close", "hide", "clear"):
        raise ProgressionError("unknown PVF dungeon state")
    return ("open", "close", "hide", "clear").index(value.lower())


def _symbol_table(text):
    tokens = _tokens(text)
    if len(tokens) % 2:
        raise ProgressionError("unpaired native symbol table")
    result = {}
    for i in range(0, len(tokens), 2):
        symbol_id, token = _uint(tokens[i]), tokens[i + 1]
        if not token.startswith("[") or not symbol_id:
            raise ProgressionError("symbol table requires integer-before-tag")
        name = token[1:-1].strip().upper()
        if name in result or symbol_id in result.values():
            raise ProgressionError("repeated native symbol name/id")
        result[name] = symbol_id
    return MappingProxyType(result)


def _compile_clause(fields, index, action, family):
    field = fields[index]
    name, values = field.name, field.values
    fail = lambda: ProgressionError(f"unsupported {family} {'behavior' if action else 'trigger'} directive {name}")
    if not action and name == "IF" and not values:
        return None, 0
    if (action and name == "SET") or (not action and name == "ON CHANGE SYMBOL"):
        if values or index + 1 >= len(fields):
            raise fail()
        following = fields[index + 1]
        symbol, literal, used, add = following.name, following.values, 1, False
        if not symbol or any(c in symbol for c in "/[]"):
            raise fail()
        if family == "luke" and action and not literal:
            if index + 2 >= len(fields) or fields[index + 2].name != "+":
                raise fail()
            literal, used, add = fields[index + 2].values, 2, True
        if action and len(literal) != 1 or not action and literal:
            raise fail()
        return _Clause(name, value=_uint(literal[0]) if action else 0, symbol=symbol, add=add), used
    if name in ("CHECK DUNGEON STATE", "SET DUNGEON STATE", "RESERVE DUNGEON STATE"):
        if action == (name == "CHECK DUNGEON STATE") or (family == "luke" and name == "RESERVE DUNGEON STATE"):
            raise fail()
        count = 3 if name == "RESERVE DUNGEON STATE" else 2
        if len(values) != count or not _uint(values[0]):
            raise fail()
        duration = _uint(values[2]) if count == 3 else 0
        if count == 3 and not 1 <= duration <= 86400:
            raise fail()
        return _Clause(name, _uint(values[0]), _state(values[1]), value=duration), 0
    if name == "ON CHANGE DUNGEON STATE":
        if action or len(values) != 1 or not _uint(values[0]):
            raise fail()
        return _Clause(name, _uint(values[0])), 0
    if name in ("SET TIMER", "RESET TIMER", "CHECK TIMER END"):
        count = 3 if name == "SET TIMER" else 2
        if action == (name == "CHECK TIMER END") or len(values) != count:
            raise fail()
        timer, dungeon = _uint(values[0]), _uint(values[1])
        duration = _uint(values[2]) if count == 3 else 0
        if timer not in (1, 2, 3) or not dungeon or count == 3 and not 1 <= duration <= 86400:
            raise fail()
        return _Clause(name, dungeon, timer=timer, value=duration), 0
    if name in ("CLEAR PHASE", "OPEN RANDOM HATCHERY", "FAIL PHASE"):
        if not action or values or name == "OPEN RANDOM HATCHERY" and family != "anton" or name == "FAIL PHASE" and family != "luke":
            raise fail()
        return _Clause(name), 0
    if name in ("ON ENTER DUNGEON", "ON GIVEUP DUNGEON", "ON ENTER MAP", "PHASE END", "RESET SANCTUM OBJECT"):
        if family != "luke" or action != (name == "RESET SANCTUM OBJECT") or len(values) != 1:
            raise fail()
        dungeon = _uint(values[0])
        if not dungeon and name != "PHASE END":
            raise fail()
        return _Clause(name, dungeon), 0
    if action or len(values) != 1 or any(c in name for c in "/[]+"):
        raise fail()
    return _Clause("SYMBOL EQUAL", value=_uint(values[0]), symbol=name), 0


@dataclass(frozen=True)
class PvfProgram:
    family: str
    phases: tuple[_Phase, ...]
    symbols: object
    hatcheries: tuple[int, ...]
    hatchery_open: int
    shield_charge_rate: tuple[int, ...]
    sanctum_symbols: object
    barrier: BarrierConfiguration | None
    luke: object = None

    @classmethod
    def compile(cls, family, text, symbol_text, *, barrier=None):
        if family not in ("anton", "luke"):
            raise ProgressionError("unknown raid family")
        fields, symbols = _fields(text), _symbol_table(symbol_text)
        initial, hp, party, rules, phases = None, None, None, None, []
        dungeon, state, threshold, party_limit = 0, None, None, None
        sanctum, in_object, hatcheries, open_count, rates = {}, False, (), 0, ()
        in_single = False
        i = 0
        while i < len(fields):
            f = fields[i]
            name, values = f.name, f.values
            if name == "SINGLE RAID PHASE":
                in_single = True
            elif name == "/SINGLE RAID PHASE":
                in_single = False
            elif in_single:
                pass
            elif name == "HATCHERY INFO":
                nums = tuple(_uint(v) for v in values)
                if family != "anton" or len(nums) < 3 or nums[0] != len(nums) - 2 or not 1 <= nums[1] <= nums[0]:
                    raise ProgressionError("actual hatchery declaration invalid")
                hatcheries, open_count = nums[2:], nums[1]
                if len(set(hatcheries)) != len(hatcheries) or not all(hatcheries):
                    raise ProgressionError("actual hatchery owners invalid")
            elif name == "SHIELD CHARGE RATE":
                rates = tuple(_uint(v) for v in values)
                if family != "anton" or len(rates) != 2 or any(not 1 <= r <= 10000 for r in rates):
                    raise ProgressionError("actual barrier charge invalid")
            elif name == "PHASE":
                if initial is not None:
                    raise ProgressionError("nested phase")
                initial, hp, party, rules = [], {}, {}, []
            elif name == "/PHASE":
                if initial is None or not initial or dungeon:
                    raise ProgressionError("incomplete phase")
                phases.append(_Phase(tuple(initial), MappingProxyType(hp), MappingProxyType(party), tuple(rules)))
                initial = hp = party = rules = None
            elif initial is not None and name == "DUNGEON INFO":
                if dungeon:
                    raise ProgressionError("nested dungeon")
                dungeon, state, threshold, party_limit, in_object = 0, None, None, None, False
            elif initial is not None and name == "INDEX":
                if len(values) != 1 or dungeon or not _uint(values[0]):
                    raise ProgressionError("actual dungeon index invalid")
                dungeon = _uint(values[0])
            elif initial is not None and dungeon and name == "INIT STATE":
                if len(values) != 1 or state is not None:
                    raise ProgressionError("actual initial state invalid")
                state = _state(values[0])
                if state == 3:
                    raise ProgressionError("initial dungeon cannot be already clear")
            elif initial is not None and dungeon and name == f"{dungeon} DGN HP":
                if len(values) != 1 or threshold is not None or not 1 <= _uint(values[0]) <= 255:
                    raise ProgressionError("actual dungeon clear threshold invalid")
                threshold = _uint(values[0])
            elif initial is not None and dungeon and name == "PARTY MAX":
                if len(values) != 1 or party_limit is not None or not 1 <= _uint(values[0]) <= (2 if family == "luke" else 5):
                    raise ProgressionError("actual party limit invalid")
                party_limit = _uint(values[0])
            elif initial is not None and dungeon and name == "DESTROY OBJECT INFO":
                in_object = True
            elif initial is not None and dungeon and in_object and name.endswith(" SANCTUM OBJECT"):
                if family != "luke" or values or name not in symbols or dungeon in sanctum:
                    raise ProgressionError("actual sanctum symbol invalid")
                sanctum[dungeon] = name
            elif initial is not None and name == "/DUNGEON INFO":
                if not dungeon or state is None or threshold is None or party_limit is None or dungeon in hp:
                    raise ProgressionError("incomplete/repeated actual dungeon")
                initial.append(DungeonState(dungeon, state))
                hp[dungeon], party[dungeon], dungeon, in_object = threshold, party_limit, 0, False
            elif name == "TRIGGER":
                if initial is None:
                    raise ProgressionError("trigger outside phase")
                conditions, actions = [], []
                i += 1
                while i < len(fields) and fields[i].name != "/TRIGGER":
                    clause, used = _compile_clause(fields, i, False, family)
                    i += used + 1
                    if clause:
                        conditions.append(clause)
                if not conditions or i + 1 >= len(fields) or fields[i + 1].name != "BEHAVIOR":
                    raise ProgressionError("incomplete trigger/behavior")
                i += 2
                while i < len(fields) and fields[i].name != "/BEHAVIOR":
                    clause, used = _compile_clause(fields, i, True, family)
                    i += used + 1
                    actions.append(clause)
                if not actions or i >= len(fields):
                    raise ProgressionError("incomplete behavior")
                rules.append(_Rule(tuple(conditions), tuple(actions)))
            i += 1
        if initial is not None or len(phases) != 2 or in_single:
            raise ProgressionError("actual two-phase graph incomplete")
        if family == "anton" and (not hatcheries or not rates):
            raise ProgressionError("actual Anton hatchery/barrier dependency unavailable")
        known = {d.dungeon_id for p in phases for d in p.states}
        for phase_number, p in enumerate(phases):
            phase_ids = {d.dungeon_id for d in p.states}
            for d in phase_ids:
                if f"{d} DGN HP" not in symbols:
                    raise ProgressionError("actual native HP symbol missing")
            for rule in p.rules:
                for clause in rule.conditions + rule.actions:
                    if clause.symbol and clause.symbol not in symbols:
                        raise ProgressionError("unknown native directive symbol")
                    if clause.dungeon and clause.kind not in ("ON ENTER MAP", "PHASE END"):
                        allowed = known if clause.timer else phase_ids
                        if clause.dungeon not in allowed:
                            raise ProgressionError("directive references foreign phase dungeon")
                    if clause.kind == "RESET SANCTUM OBJECT" and clause.dungeon not in sanctum:
                        raise ProgressionError("sanctum object owner unavailable")
                    if clause.kind == "PHASE END" and clause.dungeon != phase_number:
                        raise ProgressionError("phase-end owner mismatch")
        if barrier is not None:
            if family != "anton" or symbols.get("BLACK VOLCANO BARRIER") != 110 or symbols.get("INFECTION DUNGEON INDEX") != 8:
                raise ProgressionError("formal barrier actions/native symbols disagree")
        return cls(family, tuple(phases), symbols, hatcheries, open_count, rates, MappingProxyType(sanctum), barrier)

    def begin(self, phase, now, seed):
        if phase not in (0, 1) or not seed or not isfinite(now) or now <= 0:
            raise ProgressionError("phase/time/random owner unavailable")
        return Progression(self, phase, now, seed)


class Progression:
    def __init__(self, program, phase, now, seed):
        self._program, self.family, self.phase, self.epoch = program, program.family, phase, 1
        self.cleared, self.failed, self.infection_dungeon = False, False, 0
        self._states = {d.dungeon_id: d.state for d in program.phases[phase].states}
        self._counts, self._symbols, self._timers, self._reserved, self._runs = {}, {}, {}, {}, {}
        self._entered, self._prepared, self._abandoned, self._completed = {}, {}, {}, {}
        self._barrier_events = {}
        self._luke_events = {}
        self._writes, self._sequence, self._random, self._last, self._barrier_next = (), 0, seed & 0xffffffffffffffff, now, 0
        self._luke_card_record = self._next_random(len(program.luke.maze_records)) if program.luke is not None and phase == 0 else None
        self._evaluate(now, [("initial", 0, "")])
        if self.family == "anton" and phase == 1:
            if self._symbols.get("BLACK VOLCANO BARRIER", 0) != 10000:
                raise ProgressionError("actual phase-two barrier range unavailable")
            self._barrier_next = now + 1

    @property
    def states(self):
        return tuple(DungeonState(d.dungeon_id, self._states[d.dungeon_id]) for d in self._program.phases[self.phase].states)

    @property
    def symbols(self):
        return dict(self._symbols)

    @property
    def clear_counts(self):
        return dict(self._counts)

    @property
    def timers(self):
        return dict(self._timers)

    @property
    def symbol_writes(self):
        return self._writes

    @property
    def entered_runs(self):
        return dict(self._entered)

    @property
    def luke_rules(self):
        return self._program.luke

    def sanctum_maze(self, dungeon_id):
        if self.family != 'luke' or self.phase != 0 or self._program.luke is None or dungeon_id not in self._program.luke.caps:
            return None
        return self._program.luke.maze(dungeon_id, self._luke_card_record, self._counts.get(dungeon_id, 0))

    @property
    def battle_dependencies_ready(self):
        return self.family == "luke" or self._program.barrier is not None

    def clone(self):
        n = copy.copy(self)
        for name in ("_states", "_counts", "_symbols", "_timers", "_reserved", "_runs", "_entered", "_prepared", "_abandoned", "_completed", "_barrier_events", "_luke_events"):
            setattr(n, name, getattr(self, name).copy())
        return n

    def writes_after(self, cursor):
        if not 0 <= cursor <= self._sequence:
            raise ProgressionError("symbol cursor outside this phase")
        if cursor == self._sequence:
            return ()
        if not self._writes or cursor + 1 < self._writes[0].sequence:
            raise ProgressionError("symbol journal gap requires controlled resubscription")
        return tuple(w for w in self._writes if w.sequence > cursor)

    def _write(self, name, value):
        if name not in self._program.symbols:
            raise ProgressionError("native symbol owner unavailable")
        self._sequence += 1
        self._writes = (self._writes + (SymbolWrite(self._sequence, name, self._program.symbols[name], value),))[-4096:]

    def _assign(self, name, value):
        if self._symbols.get(name, 0) != value:
            self._write(name, value)
        self._symbols[name] = value

    def _symbol(self, name, value, events):
        self._write(name, value)
        if self._symbols.get(name, 0) == value:
            return
        self._symbols[name] = value
        if self.family == "anton" and self.phase == 0 and name == "1PHASE FAIL" and value == 1:
            self.epoch += 1
            for dungeon in self._counts:
                self._counts[dungeon] = 0
                self._assign(f"{dungeon} DGN HP", 0)
        if name.endswith(" DGN HP"):
            self._counts[int(name[:-7])] = value
        events.append(("symbol", 0, name))

    def _dungeon(self, dungeon, state, events):
        if self._states[dungeon] == state:
            return
        self._states[dungeon] = state
        if state == 0:
            self._counts[dungeon] = 0
            self._assign(f"{dungeon} DGN HP", 0)
            if dungeon == self.infection_dungeon:
                self._symbol("IS EXIST INFECTION DUNGEON", 1, events)
        if state == 2:
            if dungeon == self.infection_dungeon:
                self.infection_dungeon = 0
                self._symbol("IS EXIST INFECTION DUNGEON", 0, events)
                self._symbol("INFECTION DUNGEON INDEX", 0, events)
            self._reserved.pop(dungeon, None)
            self._timers = {key: timer for key, timer in self._timers.items() if key[1] != dungeon}
        events.append(("dungeon", dungeon, ""))

    def _match(self, c, event, now):
        kind, dungeon, symbol = event
        if c.kind in ("ON ENTER DUNGEON", "ON GIVEUP DUNGEON", "ON ENTER MAP", "PHASE END"):
            return kind == c.kind and dungeon == c.dungeon
        if c.kind == "ON CHANGE DUNGEON STATE":
            return kind == "dungeon" and dungeon == c.dungeon
        if c.kind == "ON CHANGE SYMBOL":
            return kind == "symbol" and symbol == c.symbol
        if c.kind == "CHECK DUNGEON STATE":
            return self._states.get(c.dungeon, 0) == c.state
        if c.kind == "SYMBOL EQUAL":
            return self._symbols.get(c.symbol, 0) == c.value
        if c.kind == "CHECK TIMER END":
            timer = self._timers.get((c.timer, c.dungeon))
            return timer is not None and now >= timer.end
        return False

    def _evaluate(self, now, events):
        index = 0
        while index < len(events) and not self.cleared and not self.failed:
            if index > 2048:
                raise ProgressionError("cyclic directive budget exceeded")
            event, index = events[index], index + 1
            for rule in self._program.phases[self.phase].rules:
                if not all(self._match(c, event, now) for c in rule.conditions):
                    continue
                for action in rule.actions:
                    kind = action.kind
                    if kind == "SET DUNGEON STATE":
                        self._dungeon(action.dungeon, action.state, events)
                    elif kind == "RESERVE DUNGEON STATE":
                        self._reserved[action.dungeon] = (action.state, now + action.value)
                    elif kind == "SET":
                        value = action.value + (self._symbols.get(action.symbol, 0) if action.add else 0)
                        if value > 0xffffffff:
                            raise ProgressionError("native symbol addition overflow")
                        self._symbol(action.symbol, value, events)
                    elif kind == "RESET SANCTUM OBJECT":
                        self._symbol(self._program.sanctum_symbols[action.dungeon], 0, events)
                    elif kind == "SET TIMER":
                        self._timers[(action.timer, action.dungeon)] = Timer(now + action.value, action.value)
                    elif kind == "RESET TIMER":
                        self._timers.pop((action.timer, action.dungeon), None)
                    elif kind in ("CLEAR PHASE", "FAIL PHASE"):
                        self.cleared, self.failed = kind == "CLEAR PHASE", kind == "FAIL PHASE"
                        self._timers, self._reserved = {}, {}
                    elif kind == "OPEN RANDOM HATCHERY":
                        ids = list(self._program.hatcheries)
                        for i in range(len(ids) - 1, 0, -1):
                            j = self._next_random(i + 1)
                            ids[i], ids[j] = ids[j], ids[i]
                        ids = ids[:self._program.hatchery_open]
                        self.infection_dungeon = ids[self._next_random(len(ids))]
                        self._symbol("IS EXIST INFECTION DUNGEON", 1, events)
                        self._symbol("INFECTION DUNGEON INDEX", self.infection_dungeon, events)
                        for dungeon in ids:
                            self._dungeon(dungeon, 0, events)
                    else:
                        raise ProgressionError("uncompiled action")
                if self.cleared or self.failed:
                    break

    def _next_random(self, bound):
        self._random ^= (self._random << 13) & 0xffffffffffffffff
        self._random ^= self._random >> 7
        self._random ^= (self._random << 17) & 0xffffffffffffffff
        return self._random % bound

    def _barrier_active(self):
        return self.family == "anton" and self.phase == 1 and not self.cleared and self._states.get(219) == 0 and self._barrier_next

    def credit_luke_generator(self, run_id, event_id, dungeon_id, attempt, now):
        rules = self._program.luke
        if (self.family != 'luke' or self.phase != 0 or rules is None or not event_id or len(event_id) > 256
                or self._entered.get(run_id) != dungeon_id or attempt != self.epoch or dungeon_id not in rules.caps):
            raise ProgressionError('Luke generator requires its accepted owned sanctum')
        previous = self._luke_events.get(event_id)
        if previous is not None:
            if previous != (run_id, dungeon_id):
                raise ProgressionError('Luke machine replay changed owner')
            return self.clone()
        if len(self._luke_events) >= 4096:
            raise ProgressionError('Luke generator journal limit reached')
        n = self.tick(now)
        if n.cleared or n.failed or n._states.get(dungeon_id) != 0 or n.epoch != attempt:
            raise ProgressionError('Luke machine phase expired')
        name = n._program.sanctum_symbols[dungeon_id]
        old = n._symbols.get(name, 0)
        if old < rules.caps[dungeon_id]:
            events = []
            n._symbol(name, old + 1, events)
            n._evaluate(now, events)
        n._luke_events[event_id] = (run_id, dungeon_id)
        return n

    def apply_barrier_damage(self, event_id, source, dungeon_id, attempt, now):
        """Source is classified from the owned dead object/monster PVF path.

        No C2S symbol ID, amount, or completed dungeon is sufficient evidence.
        The adapter supplies a unique accepted run/room/death identity.
        """
        if not event_id or len(event_id) > 256 or attempt != self.epoch:
            raise ProgressionError("owned barrier death identity unavailable")
        if event_id in self._barrier_events:
            if self._barrier_events[event_id] != (source, dungeon_id):
                raise ProgressionError("barrier replay changed source/dungeon")
            return self.clone()
        n = self.tick(now)
        if not n._barrier_active() or n.epoch != attempt or n._states.get(dungeon_id) != 0 or n._program.barrier is None:
            raise ProgressionError("barrier event phase/dungeon/dependencies unavailable")
        config = n._program.barrier
        if source in ("small_transmission", "large_transmission"):
            if dungeon_id != 218:
                raise ProgressionError("transmission outside actual energy dungeon")
            amount = config.small if source == "small_transmission" else config.large
        elif source == "hatchery_constructor":
            if dungeon_id not in n._program.hatcheries:
                raise ProgressionError("constructor outside actual hatchery")
            amount = config.constructor + (config.infected_constructor if dungeon_id == n.infection_dungeon else 0)
        else:
            raise ProgressionError("unsupported barrier death source")
        if amount is None or amount <= 0:
            raise ProgressionError("this actual source has no barrier subtraction")
        old, events = n._symbols.get("BLACK VOLCANO BARRIER", 0), []
        value = max(0, old - amount)
        if value != old:
            n._symbol("BLACK VOLCANO BARRIER", value, events)
        n._evaluate(now, events)
        n._barrier_events[event_id] = (source, dungeon_id)
        return n

    def tick(self, now):
        if not isfinite(now) or now < self._last:
            raise ProgressionError("raid clock moved backwards")
        n = self.clone()
        if n.cleared or n.failed:
            n._last = now
            return n
        for steps in range(4097):
            deadlines = [t.end for t in n._timers.values() if n._last < t.end <= now]
            deadlines += [end for state, end in n._reserved.values() if end <= now]
            if n._barrier_active() and n._barrier_next <= now:
                deadlines.append(n._barrier_next)
            if not deadlines:
                break
            if steps == 4096:
                raise ProgressionError("elapsed timer budget exceeded")
            due, events = min(deadlines), [("tick", 0, "")]
            if n._barrier_active() and n._barrier_next <= due:
                rate = n._program.shield_charge_rate[1 if n._symbols.get("IS EXIST INFECTION DUNGEON") == 1 else 0]
                value = min(10000, n._symbols.get("BLACK VOLCANO BARRIER", 0) + rate)
                if value != n._symbols.get("BLACK VOLCANO BARRIER", 0):
                    n._symbol("BLACK VOLCANO BARRIER", value, events)
                n._barrier_next = due + 1
            for dungeon in sorted(d for d, (state, end) in n._reserved.items() if end <= due):
                state, end = n._reserved.pop(dungeon)
                n._dungeon(dungeon, state, events)
            n._evaluate(due, events)
            n._last = due
            if n.cleared or n.failed:
                break
        n._evaluate(now, [("tick", 0, "")])
        n._last = now
        return n

    def clear(self, run_id, dungeon_id, attempt, now):
        if not run_id or len(run_id) > 128 or attempt != self.epoch:
            raise ProgressionError("owned clear attempt unavailable")
        if run_id in self._runs:
            if self._runs[run_id] != dungeon_id:
                raise ProgressionError("clear replay changed dungeon")
            return self.clone()
        if self.family == "luke" and self._entered.get(run_id) != dungeon_id:
            raise ProgressionError("Luke clear requires accepted owned entry")
        n = self.tick(now)
        if n.epoch != attempt or n.cleared or n.failed or n._states.get(dungeon_id) != 0:
            raise ProgressionError("dungeon/attempt is no longer open")
        n._runs[run_id] = dungeon_id
        n._counts[dungeon_id] = n._counts.get(dungeon_id, 0) + 1
        n._assign(f"{dungeon_id} DGN HP", n._counts[dungeon_id])
        if n._counts[dungeon_id] >= n._program.phases[n.phase].hp[dungeon_id]:
            events = []
            if dungeon_id in n._program.hatcheries:
                for timer in (1, 3):
                    n._timers.pop((timer, dungeon_id), None)
                n._symbol(f"{dungeon_id} HATCHERY FAIL", 0, events)
                n._symbol(f"{dungeon_id} HATCHERY NAMED CREATE", 0, events)
                if dungeon_id == n.infection_dungeon:
                    n._symbol("IS EXIST INFECTION DUNGEON", 0, events)
            n._dungeon(dungeon_id, 3, events)
            n._evaluate(now, events)
        if n.family == "luke":
            n._entered.pop(run_id, None)
            n._completed[run_id] = dungeon_id
        return n

    def _event(self, kind, dungeon, now):
        n = self.tick(now)
        if n.cleared or n.failed:
            raise ProgressionError("event has no active phase")
        if kind == "ON ENTER DUNGEON" and n._states.get(dungeon) != 0:
            raise ProgressionError("owned entry dungeon not open")
        n._evaluate(now, [(kind, dungeon, "")])
        return n

    def reserve(self, run_id, dungeon_id, attempt, now):
        if self.family != "luke":
            raise ProgressionError("reservation belongs only to Luke")
        if not run_id or len(run_id) > 128 or attempt != self.epoch or self.cleared or self.failed or now < self._last:
            raise ProgressionError("reservation identity unavailable")
        if run_id in self._prepared or run_id in self._entered:
            if (self._prepared.get(run_id) or self._entered.get(run_id)) != dungeon_id:
                raise ProgressionError("reservation replay changed dungeon")
            return self.clone()
        if run_id in self._abandoned or run_id in self._completed or sum(map(len, (self._prepared, self._entered, self._abandoned, self._completed))) >= 4096:
            raise ProgressionError("reservation retired or journal full")
        if self._states.get(dungeon_id) != 0:
            raise ProgressionError("reservation dungeon not open")
        active = list(self._prepared.values()).count(dungeon_id) + list(self._entered.values()).count(dungeon_id)
        if active >= self._program.phases[self.phase].party_max.get(dungeon_id, 0):
            raise ProgressionError("actual dungeon party limit reached")
        n = self.clone()
        n._prepared[run_id] = dungeon_id
        return n

    def cancel_reservation(self, run_id, dungeon_id):
        if not run_id or self._prepared.get(run_id, dungeon_id) != dungeon_id:
            raise ProgressionError("reservation cancellation owner changed")
        n = self.clone()
        n._prepared.pop(run_id, None)
        return n

    def enter(self, run_id, dungeon_id, attempt, now):
        if self.family != "luke" or not run_id or attempt != self.epoch:
            raise ProgressionError("Luke entry owner unavailable")
        if run_id in self._entered:
            if self._entered[run_id] != dungeon_id:
                raise ProgressionError("entry replay changed dungeon")
            return self.clone()
        reserved = self.reserve(run_id, dungeon_id, attempt, now)
        n = reserved._event("ON ENTER DUNGEON", dungeon_id, now)
        n._prepared.pop(run_id, None)
        n._entered[run_id] = dungeon_id
        return n

    def give_up(self, run_id, dungeon_id, now):
        if self.family != "luke" or not run_id:
            raise ProgressionError("Luke abandonment owner unavailable")
        if self._abandoned.get(run_id) == dungeon_id:
            return self.clone()
        if self._entered.get(run_id) != dungeon_id:
            raise ProgressionError("abandonment has no owned entry")
        n = self._event("ON GIVEUP DUNGEON", dungeon_id, now)
        n._entered.pop(run_id, None)
        n._abandoned[run_id] = dungeon_id
        return n

    def enter_map(self, map_id, now):
        if self.family != "luke" or not map_id:
            raise ProgressionError("Luke map owner unavailable")
        if not any(c.kind == "ON ENTER MAP" and c.dungeon == map_id
                   for r in self._program.phases[self.phase].rules for c in r.conditions):
            return self.clone()
        return self._event("ON ENTER MAP", map_id, now)

    def end_phase(self, now):
        if self.family != "luke":
            raise ProgressionError("Luke phase-end event required")
        return self._event("PHASE END", self.phase, now)

    def next_phase(self, now):
        if self.phase != 0 or not self.cleared or self.failed or now < self._last:
            raise ProgressionError("next phase requires genuine completed first phase")
        n = self._program.begin(1, now, self._random)
        if self.family == "luke":
            n._assign("2PHASE DUNGEON TYPE", self._symbols.get("2PHASE DUNGEON TYPE", 0))
            if self._program.luke is not None:
                counts = {d: self._symbols.get(name, 0) for d, name in self._program.sanctum_symbols.items()}
                for dungeon, name in self._program.sanctum_symbols.items():
                    n._assign(name, counts[dungeon])
                n._assign('ENERGY CONTROL OPEN', self._program.luke.tier(counts))
        return n
