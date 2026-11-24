"""Read the frozen client's native [enchant random] ticket rules from PVF."""
from dataclasses import dataclass
from functools import lru_cache
import re

from game.character.item_metadata import resolve_item_metadata
from game.character.upgrade_ticket import _section, _integers, _section_tokens, _eligibility_result, _optional_integer_section
from pvf import pvf_runtime

BLESSING_TICKETS = frozenset((2682155, 2682156, 2682157))
SLOTS = {1: frozenset(('[weapon]', '[support weapon]')),
         2: frozenset(('[coat]', '[pants]', '[shoulder]', '[waist]', '[shoes]')),
         3: frozenset(('[amulet]', '[wrist]', '[ring]'))}


@dataclass(frozen=True)
class RandomEnchantTicket:
    item_id: int
    grades: tuple[int, ...]
    slot: int
    seal: int
    levels: tuple[tuple[int, int], ...]
    minimum_level: int | None
    maximum_level: int | None
    usable_jobs: tuple[str, ...]
    impossible_contents: tuple[str, ...]

    def is_usable(self, *, level, job, hardcore):
        return _eligibility_result(level=level, job=job, hardcore=hardcore,
            minimum_level=self.minimum_level, maximum_level=self.maximum_level,
            usable_jobs=self.usable_jobs, impossible_contents=self.impossible_contents)

    def allows_target(self, metadata, *, seal_flag):
        return (metadata.is_equipment and not metadata.is_avatar
                and metadata.equipment_type in SLOTS[self.slot]
                and metadata.rarity in self.grades
                and (self.seal == 2 or bool(seal_flag) == bool(self.seal)))

    def level_for_roll(self, roll):
        if not 0 <= roll < 100000:
            raise ValueError('random enchant roll outside PVF scale')
        for level, weight in self.levels:
            if roll < weight:
                return level
            roll -= weight
        raise ValueError('random enchant weights incomplete')


def parse_random_enchant_ticket(item_id, text):
    if int(item_id) not in BLESSING_TICKETS:
        return None
    block = re.search(r'\[enchant random\](.*?)\[/enchant random\]', text, re.S | re.I)
    if block is None:
        raise ValueError('blessing ticket missing enchant random')
    body = block.group(1)
    grades = _integers(_section(body, 'er_grade'))
    slot, = _integers(_section(body, 'er_slot'))
    seal, = _integers(_section(body, 'er_seal'))
    distribution = _integers(_section(body, 'er_enchant'))
    count = distribution[0]
    levels = tuple(zip(distribution[1::2], distribution[2::2], strict=True))
    if (not grades or len(grades)>7 or not set(grades)<=set(range(7)) or slot not in SLOTS
            or seal not in (0,1,2) or not 1<=count<=20 or len(levels)!=count
            or sum(weight for _,weight in levels)!=100000
            or any(weight<0 or not 0<=level<=20 for level,weight in levels)
            or tuple(level for level,_ in levels)!=tuple(range(levels[0][0],levels[0][0]+count))):
        raise ValueError('invalid native random enchant rule')
    return RandomEnchantTicket(int(item_id),grades,slot,seal,levels,
        _optional_integer_section(text,'minimum level',item_id=item_id),
        _optional_integer_section(text,'maximum level',item_id=item_id),
        _section_tokens(text,'usable job'),_section_tokens(text,'impossible contents'))


@lru_cache(maxsize=3)
def find_random_enchant_ticket(item_id):
    if int(item_id) not in BLESSING_TICKETS:
        return None
    metadata = resolve_item_metadata(int(item_id))
    if not metadata.is_stackable or not metadata.source_path:
        return None
    return parse_random_enchant_ticket(item_id,pvf_runtime.read_text(metadata.source_path))
