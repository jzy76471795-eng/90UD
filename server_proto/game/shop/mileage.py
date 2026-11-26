"""Stage3 account mileage balance and purchase notices."""
import struct
from time import time

from game.quest.adventure_season1 import active_start as battle_pass_start
from game.quest.weekly_attendance import EVENT_ID, activity_start, current_week


CERA_PER_MILEAGE_POINT = 100
MAX_MILEAGE_POINTS = 0x7FFFFFFF
MILEAGE_BALANCE_OPCODE = 0x01AC
MILEAGE_AWARD_OPCODE = 0x021A
MILEAGE_SHOP_EVENT_OPCODE = 0x006C
MILEAGE_SHOP_EVENT_ID = 0x0083
LOCAL_RAID_FEATURE_IDS = (0x00B5, 0x011A)
ADVENTURE_SEASON1_EVENT_ID = 2520
EMPTY_FEATURE_SNAPSHOT = b"\x00\x00\x00"


def build_mileage_shop_event(*, mileage_enabled: bool = True, raid_enabled: bool = False) -> bytes:
    # EVENT_INFO replaces the full feature map. Every row here has no extra data.
    ids = LOCAL_RAID_FEATURE_IDS if raid_enabled else ()
    if mileage_enabled:
        ids += (MILEAGE_SHOP_EVENT_ID,)
    now = time()
    if current_week(activity_start(), now):
        ids += (EVENT_ID,)
    try:
        adventure_active = battle_pass_start(now) is not None
    except ValueError:
        adventure_active = False
    if adventure_active:
        ids += (ADVENTURE_SEASON1_EVENT_ID,)
    rows = b"".join(struct.pack("<HB", event, 0) for event in ids)
    # The client reads a zero after the last row. A single row used to get
    # that byte from codec padding. Two rows fill a block before the footer.
    return struct.pack("<H", len(ids)) + rows + b"\x00"


def build_mileage_balance(points: int) -> bytes:
    # S0/01AC reader 0x16565A0 reads a byte and the absolute balance.
    return struct.pack("<Bi", 0, points)


def build_mileage_award(before: int, after: int) -> bytes:
    # S0/021A reader 0x16612C0 uses reason 0 for the normal points message.
    return struct.pack("<Bii", 0, before, after)
