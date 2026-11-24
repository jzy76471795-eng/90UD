"""Stage3 account mileage balance and purchase notices."""
import struct


CERA_PER_MILEAGE_POINT = 100
MAX_MILEAGE_POINTS = 0x7FFFFFFF
MILEAGE_BALANCE_OPCODE = 0x01AC
MILEAGE_AWARD_OPCODE = 0x021A
MILEAGE_SHOP_EVENT_OPCODE = 0x006C
MILEAGE_SHOP_EVENT_ID = 0x0083
LOCAL_RAID_FEATURE_IDS = (0x00B5, 0x011A)


def build_mileage_shop_event(*, mileage_enabled: bool = True, raid_enabled: bool = False) -> bytes:
    # EVENT_INFO replaces the full feature map. Every row here has no extra data.
    ids = LOCAL_RAID_FEATURE_IDS if raid_enabled else ()
    if mileage_enabled:
        ids += (MILEAGE_SHOP_EVENT_ID,)
    rows = b"".join(struct.pack("<HB", event, 0) for event in ids)
    trailer = b"\x00" if raid_enabled else b""
    return struct.pack("<H", len(ids)) + rows + trailer


def build_mileage_balance(points: int) -> bytes:
    # S0/01AC reader 0x16565A0 reads a byte and the absolute balance.
    return struct.pack("<Bi", 0, points)


def build_mileage_award(before: int, after: int) -> bytes:
    # S0/021A reader 0x16612C0 uses reason 0 for the normal points message.
    return struct.pack("<Bii", 0, before, after)
