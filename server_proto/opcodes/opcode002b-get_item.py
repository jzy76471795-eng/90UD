# -*- coding: utf-8 -*-
"""C1/0x002B ENUM_CMDPACKET_GET_ITEM — 场景掉落拾取。

PacketEnum:
  CMD GET_ITEM = 43 (0x2B)
  NOTI GET_ITEM = 39 (0x27)  # 与 C1/DIE_MONSTER 0x27 同号异向

Stage3 IDA（2026-07-19）:
  - DIE 行前缀 u32 sceneObjectKey（``sub_1647080``）
  - S0/0027 读 ``u32 objectKey + u16 picker``（``sub_166E300``）
  - 金币：8×(u8+u32)；物品：8B+u16+u16+u8（共 19B）

流程:
  1. 解析 dropObjectKey（优先 20B / u32）
  2. 从 session.dungeon_drops 按 object_key 取掉落
  3. 金币 → grant_wallet_gold + S0/0027 gold + S0/000E wallet
  4. 物品 → grant_inventory_item + S0/0027 item；复活币再刷新 slot 1 钱包
"""
from __future__ import annotations

from contextlib import nullcontext
from typing import Any

from game.chain_loader import load_chain
from game.character.inventory_item_view import InventoryItemView
from game.character.payloads import build_item_list_mode0
from game.character.epic_book import (
    EPIC_PIECE_BALANCE_SYNC_OPCODE,
    build_epic_piece_balance_sync_047e,
)
from game.character.revive_coin import is_revive_coin_grant_item_id
from game.character.character_db import (
    is_virtual_inventory_grant,
    parse_virtual_premium_grant,
)
from game.drop.drop_service import peek_session_drop, take_session_drop
from game.quest.quest_common import env_bool
from game.quest.quest_residual import apply_seeking_to_active_quests
from game.quest.quest_session import (
    bind_session_quest_state,
    pack_set_trigger_ack_packets,
    resolve_selected_character_detail,
)

_payloads = load_chain("helper-dungeon_payloads.py")
build_get_item_pickup_gold_0027 = _payloads.build_get_item_pickup_gold_0027
build_get_item_pickup_item_0027 = _payloads.build_get_item_pickup_item_0027
build_wallet_gold_update_000e = _payloads.build_wallet_gold_update_000e
build_wallet_revive_coin_update_000e = _payloads.build_wallet_revive_coin_update_000e
build_item_list_update_000e = _payloads.build_item_list_update_000e

_tower_inventory = load_chain("helper-death_tower_inventory.py")
_system_reward = load_chain("helper-system_reward_activation.py")
build_premium_activation_notice = _system_reward.build_premium_activation_notice


# The Stage3 item-pickup reader requires a valid Main-container destination
# slot.  This is only a transient client animation target: the following
# authoritative 000D snapshot immediately restores the database-backed bag.
_EPIC_PIECE_PICKUP_EFFECT_SLOT = 64


def _build_main_inventory_refresh(repo: Any, character_id: int) -> tuple[bytes, int]:
    """Build the authoritative list0 snapshot used after a virtual pickup."""
    rows, list_param = repo.load_main_inventory_snapshot(int(character_id))
    items = [InventoryItemView(row) for row in rows]
    body = build_item_list_mode0(
        items, list_type=0, list_param16=int(list_param)
    )
    return body, len(rows)


def _parse_get_item_body(plain: bytes) -> dict[str, Any]:
    """解析 C1/002B。Stage3 普通拾取与 90qiyuan 同为 20B：u32 DropObjectKey 开头。"""
    raw = bytes(plain or b"")
    out: dict[str, Any] = {
        "raw_len": len(raw),
        "raw_hex": raw.hex(" ") if len(raw) <= 64 else raw[:64].hex(" ") + "...",
        "drop_object_key": None,
        "layout": "unknown",
    }
    views = [raw]
    # 部分 codec 前缀 3B，尝试 residual
    if len(raw) >= 5:
        views.append(raw[3:])

    for view in views:
        if len(view) >= 4:
            key = int.from_bytes(view[0:4], "little")
            if key > 0:
                out["drop_object_key"] = key
                if len(view) >= 20:
                    out["layout"] = "u32_key_20b"
                    out["fixed_zero"] = view[4]
                    out["object_context"] = view[5]
                elif len(view) >= 2 and key <= 0xFFFF:
                    out["layout"] = "u32_or_u16_key"
                else:
                    out["layout"] = "u32_key"
                return out
        if len(view) >= 2:
            slot = int.from_bytes(view[0:2], "little")
            if slot > 0:
                out["drop_object_key"] = slot
                out["layout"] = "u16_slot"
                return out
    return out


def _cid(session: Any, server: Any) -> int | None:
    if session is not None:
        for attr in ("selected_character_id", "character_id", "cid"):
            val = getattr(session, attr, None)
            if val is not None:
                try:
                    cid = int(val)
                    if cid > 0:
                        return cid
                except Exception:
                    pass
    try:
        detail = resolve_selected_character_detail(server, session)
        cid = detail.get("character_id")
        if cid is not None and int(cid) > 0:
            return int(cid)
    except Exception:
        pass
    return None


def _picker_actor(session: Any, cid: int) -> int:
    if session is not None:
        for attr in ("user_id", "actor_id", "selected_character_id", "character_id"):
            val = getattr(session, attr, None)
            if val is not None:
                try:
                    uid = int(val) & 0xFFFF
                    if uid > 0:
                        return uid
                except Exception:
                    pass
    return int(cid) & 0xFFFF


def _sync_seeking_capture_pickup(
    server: Any,
    repo: Any,
    *,
    character_id: int,
    item_id: int,
    drop: Any,
    session: Any,
) -> tuple[list[bytes], str]:
    """Advance only the scanner quest that owns a picked-up capture result."""
    if str(getattr(drop, "source", "") or "") != "seeking_capture":
        return [], "capture_pickup=none"
    quest_id = int(getattr(drop, "quest_id", 0) or 0)
    if quest_id <= 0:
        return [], "capture_pickup=skip missing_quest"
    active_rows = repo.list_active_quests(int(character_id))
    active_pairs = [
        (int(active_quest_id), int(trigger))
        for _slot, active_quest_id, trigger in active_rows
        if int(active_quest_id) == quest_id
    ]
    if not active_pairs:
        return [], "capture_pickup=skip inactive q=%d" % quest_id
    held = repo.count_inventory_items_by_template(
        int(character_id),
        list_type=0,
        item_ids=[int(item_id)],
    )
    changed = apply_seeking_to_active_quests(
        active_pairs,
        held_count_by_item=held,
        item_filter=[int(item_id)],
    )
    updated: list[tuple[int, int]] = []
    for changed_quest_id, _old, new, _progress_note in changed:
        ok, _update_note = repo.update_active_quest_trigger(
            int(character_id),
            int(changed_quest_id),
            int(new),
        )
        if ok:
            updated.append((int(changed_quest_id), int(new)))
    if session is not None:
        rows = repo.list_active_quests(int(character_id))
        bind_session_quest_state(
            session,
            active_pairs=[
                (int(active_quest_id), int(trigger))
                for _slot, active_quest_id, trigger in rows
            ],
            finished_ids=[
                int(finished_quest_id)
                for finished_quest_id, _flag in repo.list_finished_quests(int(character_id))
            ],
        )
    if updated and env_bool("USDOF_GAME_QUEST_RESIDUAL_SEND_0021", True):
        packets, packet_note = pack_set_trigger_ack_packets(server, updated)
    elif updated:
        packets, packet_note = [], "S1/0021 skipped"
    else:
        packets, packet_note = [], "S1/0021 none"
    return packets, "capture_pickup q=%d item=%d progress=%d %s" % (
        quest_id,
        int(item_id),
        len(updated),
        packet_note,
    )


class GetItem002BService:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _emit(self, flag: int, opcode: int, body: bytes, mode: str = "codec") -> tuple[bytes, str]:
        mode_norm = (mode or "codec").strip().lower()
        if mode_norm in {"raw", "plain"}:
            return self.server._raw_response(flag, opcode, body), "raw"
        with self._stage("codec"):
            return self.server._codec_response(flag, opcode, body), "codec"

    def _stage(self, name: str) -> Any:
        metrics = getattr(self.server, "hotpath_metrics", None)
        return metrics.stage(name) if metrics is not None else nullcontext()

    def handle_get_item_002b(self, body: bytes, *, session: Any = None) -> tuple[list[bytes], str]:
        if not env_bool("USDOF_GAME_DUNGEON_ITEM_PICKUP", True):
            return [], "C1/002B GET_ITEM gate=OFF"

        parsed = _parse_get_item_body(body)
        key = parsed.get("drop_object_key")
        base = (
            "C1/002B GET_ITEM layout=%s key=%s plain_len=%s"
            % (parsed.get("layout"), key, parsed.get("raw_len"))
        )
        if key is None or int(key) <= 0:
            return [], base + "; skip=no_key"

        key_i = int(key) & 0xFFFFFFFF
        drop = peek_session_drop(session, key_i)
        if drop is None:
            return [], base + "; skip=not_found bag_n=%s" % (
                len(getattr(session, "dungeon_drops", None) or {}) if session else 0
            )

        wire_key = int(drop.wire_object_key) & 0xFFFFFFFF
        cid = _cid(session, self.server)
        if cid is None:
            return [], base + "; skip=no_cid"

        picker = _picker_actor(session, int(cid))
        packets: list[bytes] = []
        notes: list[str] = []

        if str(getattr(drop, "source", "") or "") == "tower_item":
            item_id = int(drop.template_id) & 0xFFFFFFFF
            count = max(1, int(drop.stack_count or 1))
            with self._stage("session_credit"):
                ok, tower_note, mutation = _tower_inventory.add_death_tower_item(
                    session,
                    item_template_id=item_id,
                    count=count,
                )
            if not ok:
                return [], "%s; fail=death_tower_inventory %s" % (
                    base,
                    tower_note,
                )

            # Session mutation is authoritative. Remove the ground object only
            # after the whole addition preflight succeeds.
            take_session_drop(session, key_i)
            destination_slot = int(mutation["destination_slot"])
            with self._stage("packet_build"):
                body_27 = build_get_item_pickup_item_0027(
                    wire_key,
                    picker,
                    destination_slot & 0xFFFF,
                    move_flag=0,
                )
            pkt27, mode27 = self._emit(0, 0x0027, body_27, "codec")
            packets.append(pkt27)

            update_entries = _tower_inventory.build_death_tower_update_entries(
                session,
                mutation["changed_slots"],
            )
            with self._stage("packet_build"):
                body_e = build_item_list_update_000e(
                    update_entries,
                    list_type=_tower_inventory.DEATH_TOWER_LIST_TYPE,
                )
            pkt_e, mode_e = self._emit(0, 0x000E, body_e, "codec")
            packets.append(pkt_e)
            return packets, (
                "%s; OK death_tower_item item=%d count=%d slot=%d; "
                "0027_item/%s/len=%d; 000E/%s list=0x%02X slots=%s; %s; "
                "chain_file=opcodes/opcode002b-get_item.py"
                % (
                    base,
                    item_id,
                    count,
                    destination_slot,
                    mode27,
                    len(body_27),
                    mode_e,
                    _tower_inventory.DEATH_TOWER_LIST_TYPE,
                    ",".join(str(slot) for slot in mutation["changed_slots"]),
                    tower_note,
                )
            )

        if str(getattr(drop, "source", "")) == "epic_piece":
            repo = self.server._repo()
            count = max(1, int(drop.stack_count or 1))
            with self._stage("db_credit"):
                ok, note, balance = repo.add_epic_piece_for_character(
                    int(cid), int(drop.template_id), count
                )
            if not ok:
                return [], base + "; fail=epic_piece %s" % note
            # 账号入账已提交，立刻移除会话对象，避免后续回包构造异常时重复入账。
            take_session_drop(session, key_i)
            # S0/0027's material path uses the ground item's PVF type for its
            # pickup sound.  It requires a Main-slot number even though Epic
            # fragments belong only to the account book.  Follow it with an
            # authoritative list0 snapshot, so the temporary client target is
            # never persisted or retained in the bag.
            with self._stage("packet_build"):
                body_27 = build_get_item_pickup_item_0027(
                    wire_key,
                    picker,
                    _EPIC_PIECE_PICKUP_EFFECT_SLOT,
                    move_flag=0,
                )
            pkt27, mode27 = self._emit(0, 0x0027, body_27, "codec")
            packets.append(pkt27)
            with self._stage("inventory_snapshot"):
                body_d, snapshot_count = _build_main_inventory_refresh(
                    repo, int(cid)
                )
            pkt_d, mode_d = self._emit(0, 0x000D, body_d, "codec")
            packets.append(pkt_d)
            with self._stage("packet_build"):
                body_epic = build_epic_piece_balance_sync_047e(
                    {int(drop.template_id): int(balance)},
                    reset_client=False,
                )
            pkt_epic, mode_epic = self._emit(
                0,
                EPIC_PIECE_BALANCE_SYNC_OPCODE,
                body_epic,
                "codec",
            )
            packets.append(pkt_epic)
            return packets, "%s; OK epic_piece id=%d +%d balance=%d; 0027_item/%s/len=%d effect_slot=%d; 000D/%s entries=%d; 047E/%s absolute=%d" % (
                base,
                int(drop.template_id),
                count,
                balance,
                mode27,
                len(body_27),
                _EPIC_PIECE_PICKUP_EFFECT_SLOT,
                mode_d,
                snapshot_count,
                mode_epic,
                balance,
            )

        if drop.is_gold:
            amount = max(0, int(drop.stack_count or 0))
            if amount <= 0:
                take_session_drop(session, key_i)
                return [], base + "; skip=gold_amount_0 removed"

            repo = self.server._repo()
            with self._stage("db_credit"):
                ok, wnote, total = repo.grant_wallet_gold(int(cid), amount)
            if not ok:
                return [], base + "; fail=wallet %s" % wnote

            # 提交成功后再从场景移除，防重复发奖
            take_session_drop(session, key_i)

            with self._stage("packet_build"):
                body_27 = build_get_item_pickup_gold_0027(
                    wire_key, picker, amount, extra_gold=0
                )
            pkt27, mode27 = self._emit(0, 0x0027, body_27, "codec")
            packets.append(pkt27)
            notes.append(
                "0027_gold/%s/len=%d key=%d amt=%d"
                % (mode27, len(body_27), wire_key, amount)
            )

            if env_bool("USDOF_GAME_DUNGEON_PICKUP_SEND_000E", True):
                with self._stage("packet_build"):
                    body_e = build_wallet_gold_update_000e(int(total))
                pkt_e, mode_e = self._emit(0, 0x000E, body_e, "codec")
                packets.append(pkt_e)
                notes.append("000E_wallet/%s total=%d" % (mode_e, total))

            notes.append("wallet %s" % wnote)
            return packets, "%s; OK gold; %s; chain_file=opcodes/opcode002b-get_item.py" % (
                base,
                ", ".join(notes),
            )

        # 普通物品
        item_id = int(drop.template_id) & 0xFFFFFFFF
        count = max(1, int(drop.stack_count or 1))
        if item_id <= 0:
            take_session_drop(session, key_i)
            return [], base + "; skip=bad_item_id"

        repo = self.server._repo()
        with self._stage("db_credit"):
            ok, detail, inv_slot = repo.grant_inventory_item(
                int(cid), item_id, count
            )
        virtual_grant = bool(ok and is_virtual_inventory_grant(detail))
        if not ok or (inv_slot < 0 and not virtual_grant):
            return [], base + "; fail=inv_full_or_err %s slot=%s" % (detail, inv_slot)

        take_session_drop(session, key_i)
        with self._stage("packet_build"):
            body_27 = build_get_item_pickup_item_0027(
                wire_key,
                picker,
                (
                    1
                    if virtual_grant and is_revive_coin_grant_item_id(item_id)
                    else (_EPIC_PIECE_PICKUP_EFFECT_SLOT if virtual_grant else int(inv_slot))
                )
                & 0xFFFF,
                move_flag=0,
            )
        pkt27, mode27 = self._emit(0, 0x0027, body_27, "codec")
        packets.append(pkt27)
        notes.append(
            "0027_item/%s/len=%d key=%d tid=%d inv=%d %s"
            % (mode27, len(body_27), wire_key, item_id, inv_slot, detail)
        )
        if is_revive_coin_grant_item_id(item_id):
            wallet_ok, wallet_note, wallet = repo.get_account_wallet(int(cid))
            if not wallet_ok:
                return packets, "%s; fail=revive_wallet_refresh %s" % (
                    base,
                    wallet_note,
                )
            revive_total = int(wallet.get("revive_coin", 0) or 0)
            with self._stage("packet_build"):
                body_e = build_wallet_revive_coin_update_000e(revive_total)
            pkt_e, mode_e = self._emit(0, 0x000E, body_e, "codec")
            packets.append(pkt_e)
            notes.append(
                "000E_revive_wallet/%s total=%d" % (mode_e, revive_total)
            )
        if virtual_grant:
            notes.append("virtual_grant=account_consumed")
            if session is not None:
                try:
                    session.account_premium_effects = None
                except Exception:
                    pass
            premium_notice = parse_virtual_premium_grant(detail)
            if premium_notice is not None:
                body_d, snapshot_count = _build_main_inventory_refresh(
                    repo,
                    int(cid),
                )
                pkt_d, mode_d = self._emit(0, 0x000D, body_d, "codec")
                packets.append(pkt_d)
                premium_type, remaining = premium_notice
                packet_42, mode_42 = self._emit(
                    0,
                    0x0042,
                    build_premium_activation_notice(premium_type, remaining),
                )
                packets.append(packet_42)
                notes.append(
                    "0042_premium/%s type=%d remaining=%d"
                    % (mode_42, premium_type, remaining)
                )
                notes.append(
                    "000D_main/%s entries=%d" % (mode_d, snapshot_count)
                )
        try:
            capture_packets, capture_note = _sync_seeking_capture_pickup(
                self.server,
                repo,
                character_id=int(cid),
                item_id=int(item_id),
                drop=drop,
                session=session,
            )
            packets.extend(capture_packets)
            if capture_note != "capture_pickup=none":
                notes.append(capture_note)
        except Exception as exc:
            notes.append("capture_pickup=error type=%s" % type(exc).__name__)
        return packets, "%s; OK item; %s; chain_file=opcodes/opcode002b-get_item.py" % (
            base,
            ", ".join(notes),
        )
