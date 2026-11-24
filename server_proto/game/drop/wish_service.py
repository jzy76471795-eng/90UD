# -*- coding: utf-8 -*-
"""Character-scoped Epic wish selection and PVF-backed hydration.

The selection is stored as an ordered list on the character.  Every read is
validated against the prewarmed Hell ``equipment.lst``/``.equ`` index, then
the remaining slots are filled from the same Epic pool.  No hot path opens a
PVF file or scans the unpack tree.
"""
from __future__ import annotations

import random
import struct
from typing import Any, Iterable, Optional

from game.drop.hell_drop_config import HellDropConfig, get_hell_drop_config

MAX_WISH_ITEMS = 5
WISH_ITEMS_SNAPSHOT_OPCODE = 0x050A


def _next_index(rng: Any, size: int) -> int:
    """Choose an index while allowing deterministic test RNGs."""
    count = max(1, int(size))
    if rng is None:
        return int(random.SystemRandom().randrange(count))
    method = getattr(rng, "randrange", None)
    if callable(method):
        return int(method(count)) % count
    method = getattr(rng, "next", None)
    if callable(method):
        return int(method(count)) % count
    method = getattr(rng, "choice", None)
    if callable(method):
        # ``choice`` is useful for small fixture RNGs that do not expose an
        # integer API.  Feed it indexes so the caller can remove by position.
        indexes = list(range(count))
        picked = method(indexes)
        try:
            return indexes.index(picked)
        except ValueError:
            return int(picked) % count
    raise TypeError("wish RNG must expose randrange, next, or choice")


def _epic_pool(config: Optional[HellDropConfig] = None) -> tuple[int, ...]:
    cfg = config or get_hell_drop_config()
    return tuple(int(item_id) for item_id in cfg.epic_item_ids())


def normalize_wish_item_ids(
    item_ids: Iterable[Any],
    *,
    config: Optional[HellDropConfig] = None,
    rng: Any = None,
    max_items: int = MAX_WISH_ITEMS,
) -> tuple[int, ...]:
    """Validate, deduplicate, preserve order, and fill a wish list.

    Invalid/non-Epic IDs are discarded.  The submitted IDs remain in their
    original order; only missing slots use the random remainder of the PVF
    Epic pool.  A pool smaller than ``max_items`` naturally returns fewer
    entries.
    """
    limit = max(0, min(MAX_WISH_ITEMS, int(max_items)))
    if limit == 0:
        return ()
    pool = _epic_pool(config)
    pool_set = set(pool)
    selected: list[int] = []
    seen: set[int] = set()
    for raw in tuple(item_ids or ()):
        try:
            item_id = int(raw)
        except (TypeError, ValueError):
            continue
        if item_id <= 0 or item_id not in pool_set or item_id in seen:
            continue
        selected.append(item_id)
        seen.add(item_id)
        if len(selected) >= limit:
            return tuple(selected)

    remaining = [item_id for item_id in pool if item_id not in seen]
    while len(selected) < limit and remaining:
        index = _next_index(rng, len(remaining))
        selected.append(int(remaining.pop(index)))
    return tuple(selected)


def hydrate_character_wish_items(
    repository: Any,
    character_id: int,
    *,
    config: Optional[HellDropConfig] = None,
    rng: Any = None,
) -> tuple[int, ...]:
    """Read, repair, refill, and persist one character's wish list."""
    cid = int(character_id or 0)
    if cid <= 0:
        return ()
    current = tuple(repository.get_wish_item_ids(cid))
    normalized = normalize_wish_item_ids(current, config=config, rng=rng)
    if normalized != current:
        normalized = tuple(repository.replace_wish_item_ids(cid, normalized))
    return normalized


def hydrate_session_wish_items(
    server: Any,
    session: Any,
    *,
    character_id: Optional[int] = None,
    config: Optional[HellDropConfig] = None,
    rng: Any = None,
) -> tuple[int, ...]:
    """Hydrate the currently bound character and update its hot session cache."""
    cid = int(
        character_id
        if character_id is not None
        else getattr(session, "selected_character_id", 0)
        or 0
    )
    if cid <= 0:
        if session is not None:
            session.dungeon_wish_item_ids = ()
        return ()
    values = hydrate_character_wish_items(
        server._repo(),
        cid,
        config=config,
        rng=rng,
    )
    if session is not None:
        session.dungeon_wish_item_ids = tuple(values)
    return tuple(values)


def save_character_wish_items(
    repository: Any,
    character_id: int,
    item_ids: Iterable[Any],
    *,
    config: Optional[HellDropConfig] = None,
    rng: Any = None,
) -> tuple[tuple[int, ...], str]:
    """Persist one submitted list while protecting an empty client placeholder.

    Stage3 can submit five zero-valued slots before its saved wish vector has
    been restored.  When this character already has a list, a submission with
    no valid Epic identity keeps and repairs that list.  A partial valid
    submission remains an intentional edit and is filled back to five items.
    """
    cid = int(character_id or 0)
    if cid <= 0:
        return (), "invalid_character"

    cfg = config or get_hell_drop_config()
    submitted = tuple(item_ids or ())
    valid_submitted = valid_wish_item_ids(submitted, config=cfg)
    current = tuple(repository.get_wish_item_ids(cid))
    if not valid_submitted and current:
        saved = hydrate_character_wish_items(
            repository,
            cid,
            config=cfg,
            rng=rng,
        )
        return tuple(saved), "preserved_existing_zero_valid_submission"

    normalized = normalize_wish_item_ids(
        submitted,
        config=cfg,
        rng=rng,
    )
    saved = repository.replace_wish_item_ids(cid, normalized)
    return tuple(saved), "replaced_from_submission"


def build_wish_items_snapshot_body(item_ids: Iterable[Any]) -> bytes:
    """Build Stage3 ``S0/050A``: ``u8 count + count * u32 item_id``.

    Frozen Stage3 reader ``sub_777CB0`` clears its process-local vector, reads
    one byte of count, then appends that many little-endian 32-bit item IDs.
    Callers pass the already normalized persistent list, so malformed state is
    rejected instead of being silently projected to the client.
    """
    values = tuple(int(item_id) for item_id in tuple(item_ids or ()))
    if len(values) > MAX_WISH_ITEMS:
        raise ValueError("wish snapshot count exceeds %d" % MAX_WISH_ITEMS)
    if len(set(values)) != len(values):
        raise ValueError("wish snapshot item IDs must be unique")
    for item_id in values:
        if not 0 < item_id <= 0xFFFFFFFF:
            raise ValueError("wish snapshot item ID must fit positive u32: %r" % item_id)
    return bytes((len(values),)) + b"".join(
        struct.pack("<I", item_id) for item_id in values
    )


def build_wish_items_snapshot_packet(
    server: Any,
    item_ids: Iterable[Any],
) -> tuple[bytes, str]:
    """Encode the paired Stage3 S0 snapshot used by C1/0547 wish updates."""
    values = tuple(int(item_id) for item_id in tuple(item_ids or ()))
    body = build_wish_items_snapshot_body(values)
    packet = server._codec_response(0, WISH_ITEMS_SNAPSHOT_OPCODE, body)
    return packet, "S0/050A wish_items_snapshot n=%d items=%s plain_len=%d" % (
        len(values),
        ",".join(str(item_id) for item_id in values) or "-",
        len(body),
    )


def valid_wish_item_ids(
    item_ids: Iterable[Any],
    *,
    config: Optional[HellDropConfig] = None,
) -> tuple[int, ...]:
    """Filter an already hydrated list without filling or changing order."""
    cfg = config or get_hell_drop_config()
    out: list[int] = []
    seen: set[int] = set()
    for raw in tuple(item_ids or ()):
        try:
            item_id = int(raw)
        except (TypeError, ValueError):
            continue
        if item_id <= 0 or item_id in seen or not cfg.is_epic_item(item_id):
            continue
        out.append(item_id)
        seen.add(item_id)
        if len(out) >= MAX_WISH_ITEMS:
            break
    return tuple(out)


__all__ = [
    "MAX_WISH_ITEMS",
    "WISH_ITEMS_SNAPSHOT_OPCODE",
    "build_wish_items_snapshot_body",
    "build_wish_items_snapshot_packet",
    "hydrate_character_wish_items",
    "hydrate_session_wish_items",
    "normalize_wish_item_ids",
    "save_character_wish_items",
    "valid_wish_item_ids",
]
