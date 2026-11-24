"""Normal raid PVF reward pools and pure inventory placement plans.

CN anton_rewards.go supplies rank/pool semantics. The server must resolve
actual item metadata and commit inventory changes plus the frozen entitlement's
receipt atomically. This module never invents bags, items, mail, or free slots.
"""

import hashlib
import json
from dataclasses import asdict, dataclass

from .progression import _fields
from .runtime import RaidError


class RewardError(RaidError):
    pass


def _integer(value, minimum=0, maximum=0xffffffff):
    try:
        number = int(value)
    except (ValueError, TypeError):
        raise RewardError("invalid actual reward integer") from None
    if not minimum <= number <= maximum:
        raise RewardError("actual reward integer out of range")
    return number


@dataclass(frozen=True)
class RewardChoice:
    kind: str
    rank: int
    weight: int
    item_id: int
    flags: int


@dataclass(frozen=True)
class RankRange:
    minimum: int
    maximum: int
    rank: int


@dataclass(frozen=True)
class PhaseRewards:
    minimum_clears: int
    ranks: tuple[RankRange, ...]
    choices: tuple[RewardChoice, ...]

    def rank_for_deaths(self, deaths):
        if not isinstance(deaths, int) or isinstance(deaths, bool) or deaths < 0:
            raise RewardError("owned death count invalid")
        for row in self.ranks:
            if row.minimum <= deaths <= row.maximum:
                return row.rank
        if self.ranks and self.ranks[-1].rank == 3 and deaths > self.ranks[-1].maximum:
            return 3
        raise RewardError("death count has no actual configured rank")

    def pool(self, kind, rank):
        if rank not in range(4) or kind not in ("party_card", "pcroom_card", "squad_item"):
            raise RewardError("normal reward kind/rank invalid")
        exact = tuple(c for c in self.choices if c.kind == kind and c.rank == rank)
        common = tuple(c for c in self.choices if c.kind == kind and c.rank == -1)
        choices = exact or common
        total = sum(c.weight for c in choices)
        if not 1 <= total <= 0xffffffff:
            raise RewardError("actual reward pool unavailable")
        return choices, total

    def select(self, kind, rank, draw):
        choices, total = self.pool(kind, rank)
        if not isinstance(draw, int) or isinstance(draw, bool) or not 0 <= draw < total:
            raise RewardError("reward draw outside actual pool")
        for choice in choices:
            if draw < choice.weight:
                return choice
            draw -= choice.weight
        raise RewardError("reward draw unresolved")


@dataclass(frozen=True)
class RewardConfiguration:
    family: str
    phases: tuple[PhaseRewards, ...]

    @classmethod
    def parse(cls, family, text):
        if family not in ("anton", "luke"):
            raise RewardError("normal reward family unsupported")
        phases, ranks, choices, minimum = [], None, None, None
        in_ranks, in_rewards, in_single = False, False, False
        for field in _fields(text):
            name, values = field.name, field.values
            if name == "SINGLE RAID PHASE":
                in_single = True
            elif name == "/SINGLE RAID PHASE":
                in_single = False
            elif in_single:
                continue
            elif name == "PHASE":
                if ranks is not None:
                    raise RewardError("nested reward phase")
                ranks, choices, minimum = [], [], None
            elif name == "/PHASE":
                if ranks is None or in_ranks or in_rewards or minimum is None or not ranks or not choices:
                    raise RewardError("incomplete actual reward phase")
                phases.append(PhaseRewards(minimum, tuple(ranks), tuple(choices)))
                ranks = choices = None
            elif ranks is not None:
                if name == "CONDITION FOR RANK":
                    in_ranks = True
                elif name == "/CONDITION FOR RANK":
                    in_ranks = False
                elif name == "CLEAR REWARD ITEM":
                    in_rewards = True
                elif name == "/CLEAR REWARD ITEM":
                    in_rewards = False
                elif name == "MINIMAL DUNGEON CLEAR COUNT":
                    if len(values) != 1 or minimum is not None:
                        raise RewardError("actual minimum clear count ambiguous")
                    minimum = _integer(values[0])
                    if minimum == 0 and not (family == "luke" and len(phases) == 0):
                        raise RewardError("normal minimum clear count unavailable")
                elif name == "DEAD COUNT" and in_ranks:
                    if len(values) != 3:
                        raise RewardError("actual rank width invalid")
                    low, high, rank = _integer(values[0]), _integer(values[1]), _integer(values[2], 0, 3)
                    if low > high or not ranks and low != 0 or ranks and (low != ranks[-1].maximum + 1 or rank <= ranks[-1].rank):
                        raise RewardError("actual rank ranges overlap or have gaps")
                    ranks.append(RankRange(low, high, rank))
                elif name == "STATE REWARD" and in_rewards:
                    if len(values) != 5 or values[0].lower() not in ("party_card", "pcroom_card", "squad_item"):
                        raise RewardError("actual reward row unsupported")
                    choices.append(RewardChoice(values[0].lower(), _integer(values[1], -1, 3),
                                                _integer(values[2], 1), _integer(values[3], 1),
                                                _integer(values[4], 0, 1)))
        if ranks is not None or in_single or len(phases) != 2:
            raise RewardError("normal reward phases incomplete")
        for index, phase in enumerate(phases):
            kinds = ("party_card", "pcroom_card") if family == "luke" and index == 0 else ("party_card", "pcroom_card", "squad_item")
            for rank in range(4):
                for kind in kinds:
                    phase.pool(kind, rank)
        return cls(family, tuple(phases))


@dataclass(frozen=True)
class LegacyEntry:
    item_id: int
    weight: int
    quantity: int


@dataclass(frozen=True)
class LegacyGroup:
    entries: tuple[LegacyEntry, ...]

    @property
    def total_weight(self):
        return sum(e.weight for e in self.entries)


@dataclass(frozen=True)
class LegacyRewardTable:
    groups: tuple[LegacyGroup, ...]

    @classmethod
    def parse(cls, text):
        fields = _fields(text)
        types = [f.values for f in fields if f.name == "STACKABLE TYPE"]
        if len(types) != 1 or not types[0]:
            raise RewardError("actual legacy table type unavailable")
        kind = types[0][0].strip("[]").lower()
        groups = []
        if kind == "upgradable legacy":
            rows = [f.values for f in fields if f.name == "INT DATA"]
            if len(rows) != 1 or not rows[0] or len(rows[0]) % 3:
                raise RewardError("actual legacy item/weight/count triples invalid")
            groups.append(tuple(LegacyEntry(_integer(rows[0][i], 1), _integer(rows[0][i + 1]),
                                            _integer(rows[0][i + 2], 1, 65535))
                                for i in range(0, len(rows[0]), 3)))
        elif kind == "random upgradable legacy":
            inside = False
            for field in fields:
                if field.name == "RANDOMBOX":
                    if inside:
                        raise RewardError("nested actual random legacy table")
                    inside = True
                elif field.name == "/RANDOMBOX":
                    inside = False
                elif inside and field.name == "INT DATA":
                    values = field.values
                    # CN's actual parser skips a three-integer header; each
                    # remaining quad is (item, weight, count, unused).
                    if len(values) < 7 or (len(values) - 3) % 4:
                        raise RewardError("actual random legacy quads invalid")
                    groups.append(tuple(LegacyEntry(_integer(values[i], 1), _integer(values[i + 1]),
                                                    _integer(values[i + 2], 1, 65535))
                                        for i in range(3, len(values), 4)))
            if inside:
                raise RewardError("unclosed actual random legacy table")
        else:
            raise RewardError("configured item is not a legacy table")
        result = cls(tuple(LegacyGroup(entries) for entries in groups))
        if not result.groups or any(not 1 <= g.total_weight <= 0x7fffffffffffffff for g in result.groups):
            raise RewardError("actual legacy group total unavailable")
        return result

    def draw(self, roll):
        amounts = {}
        for group in self.groups:
            draw = roll(group.total_weight)
            if not isinstance(draw, int) or isinstance(draw, bool) or not 0 <= draw < group.total_weight:
                raise RewardError("legacy draw outside actual pool")
            for entry in group.entries:
                if draw < entry.weight:
                    total = amounts.get(entry.item_id, 0) + entry.quantity
                    if total > 65535:
                        raise RewardError("expanded legacy amount exceeds native reward quantity")
                    amounts[entry.item_id] = total
                    break
                draw -= entry.weight
        return tuple(sorted(amounts.items()))


class ConfiguredRewardResolver:
    """Adapter for actual text lookup and metadata-backed leaf construction.

    The leaf callback must return RewardItem with the requested item/quantity,
    actual PVF path, legal list/owner and stack limits. Its exceptions propagate.
    Ordinary booster types remain unopened items, as in CN raid_reward_grant.
    """

    def __init__(self, text_for_item, resolve_leaf):
        if not callable(text_for_item) or not callable(resolve_leaf):
            raise RewardError("actual item text/definition adapters required")
        self.text_for_item, self.resolve_leaf = text_for_item, resolve_leaf

    def _table(self, item_id):
        text = self.text_for_item(item_id)
        if not isinstance(text, str) or not text:
            raise RewardError("actual configured item source unavailable")
        types = [f.values for f in _fields(text) if f.name == "STACKABLE TYPE"]
        if len(types) > 1 or types and not types[0]:
            raise RewardError("actual configured item type ambiguous")
        kind = types[0][0].strip("[]").lower() if types else ""
        return LegacyRewardTable.parse(text) if kind in ("upgradable legacy", "random upgradable legacy") else None

    def _leaf(self, item_id, quantity):
        item = self.resolve_leaf(item_id, quantity)
        if not isinstance(item, RewardItem) or item.item_id != item_id or item.quantity != quantity:
            raise RewardError("resolved leaf identity/amount changed")
        _validate_reward_item(item)
        return item

    def __call__(self, choice, roll):
        table = self._table(choice.item_id)
        amounts = table.draw(roll) if table is not None else ((choice.item_id, 1),)
        return tuple(self._leaf(item, quantity) for item, quantity in amounts)

    def preflight(self, configuration):
        """Resolve every possible leaf before admission spends any ticket."""
        checked = set()
        for phase in configuration.phases:
            for choice in phase.choices:
                if choice.item_id in checked:
                    continue
                checked.add(choice.item_id)
                table = self._table(choice.item_id)
                if table is None:
                    self._leaf(choice.item_id, 1)
                else:
                    for group in table.groups:
                        for entry in group.entries:
                            self._leaf(entry.item_id, entry.quantity)


@dataclass(frozen=True)
class RewardItem:
    item_id: int
    quantity: int
    display_kind: int
    owner: str
    list_type: int
    stack_limit: int
    pvf_path: str
    metadata: tuple[tuple[str, str], ...]
    fixed_slot: int | None = None


def _validate_reward_item(item):
    if (not isinstance(item, RewardItem) or not 1 <= item.item_id <= 0xffffffff
            or not 1 <= item.quantity <= 65535 or not 0 <= item.display_kind <= 255
            or item.owner not in ("character", "account") or not 0 <= item.list_type <= 255
            or not 1 <= item.stack_limit <= 0xffffffff or not item.pvf_path
            or not isinstance(item.metadata, tuple)
            or any(not isinstance(row, tuple) or len(row) != 2 or any(not isinstance(v, str) for v in row)
                   for row in item.metadata)
            or len({row[0] for row in item.metadata}) != len(item.metadata)
            or item.fixed_slot is not None and item.fixed_slot < 0):
        raise RewardError("actual item metadata or native reward quantity invalid")


@dataclass(frozen=True)
class FrozenRewardPlan:
    choice: RewardChoice
    items: tuple[RewardItem, ...]
    fingerprint: str


def _fingerprint(value):
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        raise RewardError("reward snapshot cannot be fingerprinted") from None
    return hashlib.sha256(encoded).hexdigest()


def _plan_fingerprint(choice, items):
    return _fingerprint({"choice": asdict(choice), "items": [asdict(item) for item in items]})


def freeze_reward_plan(phase, kind, deaths, owned_clears, phase_completed, roll, resolve,
                       *, pc_room_eligible=False):
    """Resolve/expand a selected actual reward exactly once, before any retry.

    resolve(choice, roll) must keep ordinary booster boxes unopened and expand
    only actual legacy-table types. Missing definitions/leafs must raise.
    """
    if (phase_completed is not True or not isinstance(owned_clears, int) or isinstance(owned_clears, bool)
            or owned_clears < phase.minimum_clears or owned_clears < 0):
        raise RewardError("owned completed minimum participation required")
    if kind == "pcroom_card" and pc_room_eligible is not True:
        raise RewardError("server-confirmed PC-room eligibility required")
    if not callable(roll) or not callable(resolve):
        raise RewardError("actual random source and item resolver required")
    rank = phase.rank_for_deaths(deaths)
    choices, total = phase.pool(kind, rank)
    choice = phase.select(kind, rank, roll(total))
    items = tuple(resolve(choice, roll))
    if not 1 <= len(items) <= 255:
        raise RewardError("actual expanded reward unavailable")
    for item in items:
        _validate_reward_item(item)
    return FrozenRewardPlan(choice, items, _plan_fingerprint(choice, items))


@dataclass(frozen=True)
class Stack:
    item_id: int
    count: int
    metadata: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Slot:
    owner: str
    list_type: int
    slot_index: int
    stack: Stack | None = None


@dataclass(frozen=True)
class SlotWrite:
    owner: str
    list_type: int
    slot_index: int
    before: Stack | None
    after: Stack


@dataclass(frozen=True)
class SlotPlan:
    snapshot_fingerprint: str
    reward_fingerprint: str
    writes: tuple[SlotWrite, ...]


def plan_reward_slots(plan, slots):
    """Plan against explicit legal US cells inside the eventual transaction.

    No list/slot capacity fallback or mail diversion. The caller must verify
    before stacks or snapshot fingerprint and write every slot plus receipt in
    one transaction. Retry reuses frozen items, re-planning current slots.
    """
    if not isinstance(plan, FrozenRewardPlan) or _plan_fingerprint(plan.choice, plan.items) != plan.fingerprint:
        raise RewardError("frozen reward plan changed")
    slots = tuple(sorted(slots, key=lambda s: (s.owner, s.list_type, s.slot_index)))
    before = {(s.owner, s.list_type, s.slot_index): s.stack for s in slots}
    if len(before) != len(slots) or any(s.owner not in ("character", "account") or not 0 <= s.list_type <= 255 or s.slot_index < 0 for s in slots):
        raise RewardError("explicit legal inventory cells invalid")
    current = dict(before)
    for item in plan.items:
        keys = [k for k in current if k[0] == item.owner and k[1] == item.list_type
                and (item.fixed_slot is None or k[2] == item.fixed_slot)]
        remaining = item.quantity
        for key in keys:
            stack = current[key]
            if stack is None or stack.item_id != item.item_id or stack.metadata != item.metadata:
                continue
            if not 1 <= stack.count <= item.stack_limit:
                raise RewardError("actual existing stack violates metadata limit")
            amount = min(remaining, item.stack_limit - stack.count)
            if amount:
                current[key] = Stack(item.item_id, stack.count + amount, item.metadata)
                remaining -= amount
        for key in keys:
            if not remaining:
                break
            if current[key] is None:
                amount = min(remaining, item.stack_limit)
                current[key] = Stack(item.item_id, amount, item.metadata)
                remaining -= amount
        if remaining:
            raise RewardError("actual reward inventory capacity unavailable")
    writes = tuple(SlotWrite(*key, before[key], current[key]) for key in current if current[key] != before[key])
    if not writes:
        raise RewardError("reward placement produced no owned writes")
    return SlotPlan(_fingerprint([asdict(slot) for slot in slots]), plan.fingerprint, writes)
