# -*- coding: utf-8 -*-
"""C1/0578 contents-play side-panel request for the frozen Stage3 client."""
from __future__ import annotations

import struct
from typing import Any

from game.character.adventure_group import (
    AdventureGroupOverview,
    load_adventure_group_overview,
)
from game.character.adventure_group_features import (
    ADVENTURE_SHOP_POINT_NOTICE_OPCODE,
    MERCENARY_INFO_OPCODE,
    MERCENARY_STATE_OPCODE,
    build_adventure_shop_point_notice_054b,
    build_mercenary_info_body_01ba,
    build_mercenary_state_body_0540,
    current_period_yyyymm,
    load_mercenary_character_points,
    remember_target_key,
    resolve_account_id,
)
from game.chain_loader import load_chain
from game.quest.quest_index import daily_quest_day_key
from pvf.adventure_group_feature_index import get_adventure_group_feature_index


_tower_progress = load_chain("helper-hundred_floor_tower_progress.py")
CONTENTS_PLAY_INFO_OPCODE = 0x0578
ADVENTURE_GROUP_NARROW_PUSH_OPCODE = 0x0545
ADVENTURE_GROUP_NARROW_STATUS_BLOB_LENGTH = 24
ADVENTURE_GROUP_STATUS_BLOB_LENGTH = 3340
ADVENTURE_GROUP_STATUS_COUNT = 17
ADVENTURE_GROUP_EXPERTISE_SLOT_COUNT = 150
ADVENTURE_GROUP_CONTENT_SLOT_COUNT = 256
ADVENTURE_GROUP_STATUS_OFFSET = 16
ADVENTURE_GROUP_EXPERTISE_OFFSET = 84
ADVENTURE_GROUP_CONTENT_OFFSET = 1292


def parse_contents_play_info_request_0578(body: bytes) -> int | None:
    """Stage3 writer 0x00718F80 emits exactly one u16 target object key."""
    if body is None or len(body) < 2:
        return None
    return struct.unpack_from("<H", body, 0)[0]


def build_contents_play_info_response_0578(
    *,
    target_key: int,
    date_yyyymmdd: int,
    character_count: int = 0,
    flags: tuple[int, int, int, int] = (0, 0, 0, 0),
    used_points: int = 0,
    status_blob: bytes = b"",
) -> bytes:
    """Build the successful body consumed by Stage3 0x0071C520.

    Stage3 0x01AB3810 consumes the leading u8 success before dispatching the
    remaining stream to 0x0071C520.  The reader-specific fixed prefix is:
      u16 target object key, u32 YYYYMMDD, u32 value, 4*u8 flags, u32 value,
      u32 structured-blob length followed by that many bytes.

    The matching character key lets the response clear the writer's one-shot
    wait byte and dispatch side-panel event 193.
    """
    key = int(target_key) & 0xFFFF
    date_value = int(date_yyyymmdd)
    if not 10_000_000 <= date_value <= 99_999_999:
        raise ValueError("contents-play date must be YYYYMMDD: %r" % date_yyyymmdd)
    count = int(character_count)
    points = int(used_points)
    if not 0 <= count <= 0xFFFFFFFF:
        raise ValueError("contents-play character count must fit u32: %r" % count)
    if not 0 <= points <= 0xFFFFFFFF:
        raise ValueError("contents-play used points must fit u32: %r" % points)
    normalized_flags = tuple(int(value) for value in flags)
    if len(normalized_flags) != 4 or any(
        value < 0 or value > 0xFF for value in normalized_flags
    ):
        raise ValueError("contents-play flags must be four u8 values")
    blob = bytes(status_blob or b"")
    return struct.pack(
        "<BHII4BII",
        1,
        key,
        date_value & 0xFFFFFFFF,
        count,
        normalized_flags[0],
        normalized_flags[1],
        normalized_flags[2],
        normalized_flags[3],
        points,
        len(blob),
    ) + blob


def build_adventure_group_narrow_status_blob(
    overview: AdventureGroupOverview,
) -> bytes:
    """Build the 24-byte state consumed by Stage3 reader 0x00719850."""
    progression = overview.progression
    values = (
        int(progression.adventure_level),
        int(progression.adventure_exp),
        int(overview.counters.optimal_dungeon_clears),
        int(overview.counters.consecutive_login_days),
    )
    if not 0 <= values[0] <= 0xFFFFFFFF:
        raise ValueError("adventure-group level must fit u32")
    if not 0 <= values[1] <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError("adventure-group exp must fit u64")
    if any(value < 0 or value > 0xFFFFFFFF for value in values[2:]):
        raise ValueError("adventure-group narrow status must fit u32")
    return struct.pack(
        "<IIQII",
        values[0],
        0,
        values[1],
        values[2],
        values[3],
    )


def build_adventure_group_narrow_push_body_0545(
    *,
    target_key: int,
    status_blob: bytes,
    flags: tuple[int, int, int, int] = (0, 0, 0, 0),
) -> bytes:
    """Build ``u16 key + len/blob + 4*u8`` for Stage3 S0/0545."""
    blob = bytes(status_blob or b"")
    if len(blob) != ADVENTURE_GROUP_NARROW_STATUS_BLOB_LENGTH:
        raise ValueError(
            "adventure-group narrow status blob must be %d bytes"
            % ADVENTURE_GROUP_NARROW_STATUS_BLOB_LENGTH
        )
    normalized_flags = tuple(int(value) for value in flags)
    if len(normalized_flags) != 4 or any(
        value < 0 or value > 0xFF for value in normalized_flags
    ):
        raise ValueError("adventure-group narrow flags must be four u8 values")
    return (
        struct.pack("<HI", int(target_key) & 0xFFFF, len(blob))
        + blob
        + struct.pack("<4B", *normalized_flags)
    )


def build_adventure_group_status_blob(
    overview: AdventureGroupOverview,
    *,
    shop_point_exp: int = 0,
    shop_points: int = 0,
    shop_purchase_rows: tuple[tuple[int, int], ...] = (),
) -> bytes:
    """Build the fixed Stage3 account-state object parsed by 0x007194E0."""
    blob = bytearray(ADVENTURE_GROUP_STATUS_BLOB_LENGTH)
    progression = overview.progression
    adventure_level = int(progression.adventure_level)
    adventure_exp = int(progression.adventure_exp)
    if not 0 <= adventure_level <= 0xFFFFFFFF:
        raise ValueError("adventure-group level must fit u32")
    if not 0 <= adventure_exp <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError("adventure-group exp must fit u64")
    struct.pack_into("<IIQ", blob, 0, adventure_level, 0, adventure_exp)

    counters = overview.counters
    status_values = (
        counters.optimal_dungeon_clears,
        counters.consecutive_login_days,
        counters.anton_phase2_clears,
        counters.luke_phase2_clears,
        counters.rank_arena_wins,
        counters.tower_despair_clears,
        counters.tower_grief_clears,
    ) + (0,) * (ADVENTURE_GROUP_STATUS_COUNT - 7)
    for index, raw_value in enumerate(status_values):
        value = int(raw_value)
        if not 0 <= value <= 0xFFFFFFFF:
            raise ValueError("adventure-group status %d must fit u32" % index)
        struct.pack_into(
            "<I",
            blob,
            ADVENTURE_GROUP_STATUS_OFFSET + index * 4,
            value,
        )

    if len(overview.expertise_slots) > ADVENTURE_GROUP_EXPERTISE_SLOT_COUNT:
        raise ValueError("adventure-group expertise slot count exceeds 150")
    for index, slot in enumerate(overview.expertise_slots):
        job_key = int(slot.job_key)
        grow_key = int(slot.grow_key)
        expertise_level = int(slot.expertise_level)
        if not 0 <= job_key <= 0x7F or not 0 <= grow_key <= 0x7F:
            raise ValueError("adventure-group expertise key must fit positive i8")
        if not 0 <= expertise_level <= 0xFFFFFFFF:
            raise ValueError("adventure-group expertise level must fit u32")
        struct.pack_into(
            "<bbHI",
            blob,
            ADVENTURE_GROUP_EXPERTISE_OFFSET + index * 8,
            job_key,
            grow_key,
            0,
            expertise_level,
        )

    point_exp = int(shop_point_exp)
    point_balance = int(shop_points)
    if not 0 <= point_exp <= 0xFFFFFFFF or not 0 <= point_balance <= 0xFFFFFFFF:
        raise ValueError("adventure shop state must fit u32")
    struct.pack_into("<II", blob, 1284, point_exp, point_balance)

    rows = tuple(shop_purchase_rows)
    if len(rows) > ADVENTURE_GROUP_CONTENT_SLOT_COUNT:
        raise ValueError("adventure shop purchase row count exceeds 256")
    seen_content_ids: set[int] = set()
    for index, (raw_content_id, raw_purchase_count) in enumerate(rows):
        content_id = int(raw_content_id)
        purchase_count = int(raw_purchase_count)
        if (
            not 0 < content_id <= 0xFFFFFFFF
            or not 0 <= purchase_count <= 0xFFFFFFFF
            or content_id in seen_content_ids
        ):
            raise ValueError("invalid adventure shop purchase row=%r" % ((raw_content_id, raw_purchase_count),))
        seen_content_ids.add(content_id)
        struct.pack_into(
            "<II",
            blob,
            ADVENTURE_GROUP_CONTENT_OFFSET + index * 8,
            content_id,
            purchase_count,
        )
    return bytes(blob)


class ContentsPlayInfo0578Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_contents_play_info_0578(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        target_key = parse_contents_play_info_request_0578(body)
        if target_key is None:
            return [], "CONTENTS_PLAY_INFO/0578 malformed body_len=%d" % len(body or b"")
        from game.character.public_profiles import resolve_profile_target, require_profile_target_current
        from game.raid.runtime import RaidError
        repo = self.server._repo()
        try:
            account_id, owner, selected = resolve_profile_target(self.server, repo, session, target_key)
        except (RaidError, ValueError, AttributeError) as exc:
            return [], 'CONTENTS_PLAY_INFO/0578 rejected: ' + str(exc)
        if selected is not None:
            # Inspection is public and read-only. Do not grant somebody else's
            # accrued shop points, return their mercenary roster, or replace
            # the requester's own shop refresh target.
            packets, note, _ = self.build_full_state_refresh_0578(
                target_key=target_key, session=session, reason='inspect-peer',
                profile_account_id=account_id, read_only=True)
            try:
                require_profile_target_current(self.server, session, owner, selected)
            except RaidError as exc:
                return [], 'CONTENTS_PLAY_INFO/0578 rejected: ' + str(exc)
            return packets, note
        remember_target_key(self.server, session, target_key)
        state_packets, state_note, shop_state = self.build_full_state_refresh_0578(
            target_key=target_key,
            session=session,
            reason="open-page",
        )
        repo = self.server._repo()
        account_id = resolve_account_id(self.server, repo, session)
        mercenary_records = load_mercenary_character_points(repo, account_id)
        mercenary_body = build_mercenary_info_body_01ba(mercenary_records)
        dispatch_state = repo.load_adventure_dispatch_state(account_id)
        dispatch_body = build_mercenary_state_body_0540(dispatch_state)
        packets = list(state_packets)
        if int(shop_state.get("earned_points", 0) or 0) > 0:
            packets.append(
                self.server._codec_response(
                    0,
                    ADVENTURE_SHOP_POINT_NOTICE_OPCODE,
                    build_adventure_shop_point_notice_054b(
                        int(shop_state.get("old_shop_points", 0) or 0),
                        int(shop_state.get("shop_points", 0) or 0),
                    ),
                )
            )
        packets.extend(
            (
                self.server._codec_response(1, MERCENARY_INFO_OPCODE, mercenary_body),
                self.server._codec_response(0, MERCENARY_STATE_OPCODE, dispatch_body),
            )
        )
        return packets, (
            "%s; MERCENARY_INIT S1/01BA records=%d points=%d; "
            "S0/0540 state=%d area=%d committed=%d"
            % (
                state_note,
                len(mercenary_records),
                sum(record.total_points for record in mercenary_records),
                int(dispatch_state["dispatch_state"]),
                int(dispatch_state["area_index"]),
                int(dispatch_state["committed_points"]),
            )
        )

    def build_full_state_refresh_0578(
        self,
        *,
        target_key: int,
        session: Any = None,
        reason: str = "refresh",
        profile_account_id: int | None = None,
        read_only: bool = False,
        cn_shop_receipt: tuple[int, int] = (0, 0),
    ) -> tuple[list[bytes], str, dict[str, Any]]:
        repo = self.server._repo()
        account_id = (resolve_account_id(self.server, repo, session)
                      if profile_account_id is None else int(profile_account_id))
        overview = load_adventure_group_overview(repo, account_id)
        feature_index = get_adventure_group_feature_index()
        shop_state = (dict(shop_points=0, old_shop_points=0, point_exp_remainder=0,
                           earned_points=0, purchase_counts={}) if read_only else repo.load_adventure_shop_state(
            account_id,
            total_character_exp=overview.progression.total_character_exp,
            maximum_points=feature_index.maximum_shop_points,
            exp_per_award=feature_index.shop_exp_per_award,
            points_per_award=feature_index.shop_points_per_award,
            period_yyyymm=current_period_yyyymm(),
        ))
        purchase_counts = dict(shop_state.get("purchase_counts") or {})
        shop_rows = tuple(
            (
                product.content_id,
                max(0, int(purchase_counts.get(product.content_id, 0) or 0)),
            )
            for product in feature_index.shop_products
        )
        if not read_only and profile_account_id is None and session is not None and (
            int(getattr(session, "account_id", 0) or 0) == account_id
            and getattr(session, "login_session_id", None)
        ):
            from game.character import cn2018_shop
            if cn2018_shop.enabled():
                import time
                try:
                    shop_rows += cn2018_shop.snapshot_rows(repo, account_id, int(time.time()), cn_shop_receipt)
                except (KeyError, FileNotFoundError, ValueError):
                    # The legacy account data remains usable with a base-only PVF.
                    pass
        status_blob = build_adventure_group_status_blob(
            overview,
            shop_point_exp=int(shop_state.get("point_exp_remainder", 0) or 0),
            shop_points=int(shop_state.get("shop_points", 0) or 0),
            shop_purchase_rows=shop_rows,
        )
        response_body = build_contents_play_info_response_0578(
            target_key=target_key,
            date_yyyymmdd=overview.created_date_yyyymmdd,
            character_count=overview.character_count,
            flags=_tower_progress.tower_entry_display(repo, account_id, daily_quest_day_key()),
            used_points=overview.used_points,
            status_blob=status_blob,
        )
        packet = self.server._codec_response(
            1,
            CONTENTS_PLAY_INFO_OPCODE,
            response_body,
        )
        return [packet], (
            "CONTENTS_PLAY_INFO/0578 success=1 reason=%s target_key=%d account=%d %s "
            "shop_points=%d point_exp=%d purchases=%d blob_len=%d S1/0578 wire=%d"
            % (
                reason,
                target_key,
                account_id,
                overview.describe(),
                int(shop_state.get("shop_points", 0) or 0),
                int(shop_state.get("point_exp_remainder", 0) or 0),
                sum(1 for _content_id, count in shop_rows if count > 0),
                len(status_blob),
                len(packet),
            )
        ), shop_state

    def build_adventure_group_narrow_push_0545(
        self,
        *,
        target_key: int,
        reason: str = "character-select",
    ) -> tuple[bytes, str]:
        """Build the narrow shared-state notification used after roster USERINFO."""
        repo = self.server._repo()
        account_id = int(getattr(self.server, "account_id", 1) or 1)
        overview = load_adventure_group_overview(repo, account_id)
        status_blob = build_adventure_group_narrow_status_blob(overview)
        body = build_adventure_group_narrow_push_body_0545(
            target_key=target_key,
            status_blob=status_blob,
            flags=_tower_progress.tower_entry_display(repo, account_id, daily_quest_day_key()),
        )
        packet = self.server._codec_response(
            0,
            ADVENTURE_GROUP_NARROW_PUSH_OPCODE,
            body,
        )
        return packet, (
            "ADVENTURE_GROUP_INIT S0/0545 reason=%s target_key=%d account=%d "
            "level=%d exp=%d optimal=%d login=%d blob_len=%d wire=%d"
            % (
                reason,
                int(target_key) & 0xFFFF,
                account_id,
                overview.progression.adventure_level,
                overview.progression.adventure_exp,
                overview.counters.optimal_dungeon_clears,
                overview.counters.consecutive_login_days,
                len(status_blob),
                len(packet),
            )
        )


__all__ = [
    "ADVENTURE_GROUP_NARROW_PUSH_OPCODE",
    "ADVENTURE_GROUP_NARROW_STATUS_BLOB_LENGTH",
    "CONTENTS_PLAY_INFO_OPCODE",
    "ADVENTURE_GROUP_STATUS_BLOB_LENGTH",
    "ContentsPlayInfo0578Service",
    "build_adventure_group_narrow_push_body_0545",
    "build_adventure_group_narrow_status_blob",
    "build_adventure_group_status_blob",
    "build_contents_play_info_response_0578",
    "parse_contents_play_info_request_0578",
]
