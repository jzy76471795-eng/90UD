"""Pure eligibility selector matching current-US native 011A6EF0.

Each (item_id, required_count) is an alternative. Native 011A6FEB branches
directly to success 011A701C on the first sufficient inventory count;
011A6FFC advances by eight bytes only when that pair is insufficient.
An empty vector succeeds at 011A6F7B. This selector never mutates inventory;
the existing authoritative ledger must charge the returned pair atomically.
"""
from typing import Mapping, Sequence


class EntryCostUnavailable(ValueError):
    pass


def choose_entry_cost(alternatives: Sequence[tuple[int, int]],
                       stock: Mapping[int, int]) -> tuple[int, int] | None:
    """Return the first sufficient PVF pair, None for free, or raise.

    Validate every configured row before choosing, so malformed later rows
    cannot be hidden by an earlier sufficient alternative.
    """
    for item_id, required_count in alternatives:
        if (type(item_id) is not int or not 1 <= item_id <= 0x7FFFFFFF or
                type(required_count) is not int or
                not 1 <= required_count <= 0x7FFFFFFF):
            raise ValueError('invalid positive native entry-cost pair')
    if not alternatives:
        return None
    for item_id, required_count in alternatives:
        available = stock.get(item_id, 0)
        if type(available) is not int or available < 0:
            raise ValueError('invalid authoritative inventory count')
        if available >= required_count:
            return item_id, required_count
    raise EntryCostUnavailable('none of the PVF entry-cost alternatives is sufficient')
