# -*- coding: utf-8 -*-
"""C1/0x002C ENUM_CMDPACKET_USE_STACKABLE — 使用消耗品（快捷栏/背包）。

PacketEnum: CMD USE_STACKABLE = 44 (0x2C)

Stage3 直播（2026-07-19 11:22 地下城快捷栏喝药）::

  plain: 03 00 00 07 09 00 00 59 04 00 00 00 00 00 00 00
  → i16 slot=3 | u8 list=0 | i32 instance=0x907 | i32 item=0x459 | u32 reserved=0
  （codec 常 pad 到 16B；语义体 15B，与 90qiyuan / S4A12 一致）

死亡之塔实机（2026-07-31 21:47，塔内药剂 6518）同样编码 ``list=0``，
临时库存的服务端同步容器才是 S0/000E ``list=0x1C``。因此塔内分流必须
使用活跃塔会话与临时槽/物品匹配，不能要求 C1 字段等于 ``0x1C``。

回包（S4A12 UseStackableAckBuilder / 90qiyuan buildUseStackableSuccessAck）::

  成功 S1/002C:
    u8  01
    i16 slot
    u8  list_type
    i32 instance_value   # 回声请求值
    i32 item_template_id
  失败 S1/002C:
    u8  00
    u8  list_type
    i32 item_template_id
    i32 instance_value

S4/90 普通消耗：成功只回 class1 ACK（客户端本地扣 1），**不**再发 000E。
本服：DB 扣 1 + S1/002C ACK；默认再发 S0/000E 单格同步剩余（防重开漂移）。
副本内成功路径在单格同步后补空 S0/0012，收束 Stage3 共享库存动作等待态。

空槽硬规则（S4A12 ``CreateEmptyCommonItem`` / ``WriteEmptyEntry``）::
  item_template_id = **-1** (wire ``0xFFFFFFFF``)，count=0
  **禁止** item_id=0：list0 上 item_id=0 是**钱包金币槽**语义，
  会把快捷栏/背包格刷成金币图标（2026-07-19 用户反馈）。
"""
from __future__ import annotations

from contextlib import nullcontext
import struct
from typing import Any

from game.chain_loader import load_chain
from game.character import exp_bonus_consumable as _exp_bonus_consumable
from game.character import experience_consumable as _experience_consumable
from game.character.item_metadata import resolve_item_metadata
from game.quest.quest_common import env_bool
from game.quest.quest_residual import (
    apply_used_item_to_active_quests,
    arm_seeking_capture_ground_drop,
    pending_seeking_capture_drop,
    resolve_seeking_capture_use,
)
from game.quest.quest_session import (
    bind_session_quest_state,
    pack_set_trigger_ack_packets,
    resolve_selected_character_detail,
)

_payloads = load_chain("helper-dungeon_payloads.py")
build_item_list_update_000e = _payloads.build_item_list_update_000e
build_item_list_clear_slot_000e = _payloads.build_item_list_clear_slot_000e

_tower_inventory = load_chain("helper-death_tower_inventory.py")
_skill_point_books = load_chain("helper-skill_point_books.py")
skill_point_book_effect = _skill_point_books.skill_point_book_effect


def parse_use_stackable_body(plain: bytes) -> dict[str, Any] | None:
    """解析 C1/002C。语义体 15B：slot|list|instance|item|reserved。"""
    data = bytes(plain or b"")
    if len(data) < 11:
        return None
    slot = int(struct.unpack_from("<h", data, 0)[0])
    list_type = int(data[2]) & 0xFF
    instance = int(struct.unpack_from("<i", data, 3)[0])
    item_id = int(struct.unpack_from("<i", data, 7)[0]) if len(data) >= 11 else 0
    reserved = int(struct.unpack_from("<I", data, 11)[0]) if len(data) >= 15 else 0
    if slot < 0:
        return None
    return {
        "layout": "slot_list_instance_item15",
        "slot_index": slot,
        "list_type": list_type,
        "instance_value": instance,
        "item_template_id": item_id,
        "reserved": reserved,
        "raw_len": len(data),
        "raw_hex": data[:16].hex(" "),
    }


def build_use_stackable_ack_ok(
    *,
    slot_index: int,
    list_type: int,
    instance_value: int,
    item_template_id: int,
) -> bytes:
    """S1/002C 成功体。"""
    return (
        bytes([0x01])
        + struct.pack("<h", int(slot_index))
        + bytes([int(list_type) & 0xFF])
        + struct.pack("<i", int(instance_value))
        + struct.pack("<i", int(item_template_id))
    )


def build_use_stackable_ack_error(
    *,
    list_type: int = 0,
    item_template_id: int = 0,
    instance_value: int = 0,
    native_quest_elixir: bool = False,
) -> bytes:
    """S1/002C 失败体（S4 UseStackableAckBuilder.BuildError）。"""
    if native_quest_elixir:
        # Stage3 consumes the failure code before the item-use callback
        # reads the inventory list and two optional cooldown fields.
        return struct.pack("<BBBii", 0, 0, int(list_type) & 0xFF, 0, 0)
    return (
        bytes([0x00, int(list_type) & 0xFF])
        + struct.pack("<i", int(item_template_id))
        + struct.pack("<i", int(instance_value))
    )


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


def _in_dungeon_run(session: Any) -> bool:
    """Only use the explicit run flag; stale dungeon ids are not a run state."""
    return bool(session is not None and getattr(session, "in_dungeon_run", False))


def _session_hardcore(session: Any) -> bool:
    return bool(
        session is not None
        and (
            getattr(session, "hardcore_mode", False)
            or getattr(session, "is_hardcore_mode", False)
        )
    )


class UseStackable002CService:
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

    def _handle_death_tower_use(
        self,
        parsed: dict[str, Any],
        *,
        session: Any,
        base: str,
    ) -> tuple[list[bytes], str]:
        list_type = int(parsed["list_type"]) & 0xFF
        slot = int(parsed["slot_index"])
        instance = int(parsed["instance_value"])
        requested_item = int(parsed["item_template_id"])
        with self._stage("session_consume"):
            ok, consume_note, detail = (
                _tower_inventory.consume_death_tower_waste(
                    session,
                    slot_index=slot,
                    expected_item_id=requested_item,
                )
            )
        actual_item = int(detail.get("item_template_id", requested_item) or 0)
        if not ok:
            ack = build_use_stackable_ack_error(
                list_type=list_type,
                item_template_id=actual_item,
                instance_value=instance,
            )
            packet, mode = self._emit(1, 0x002C, ack)
            return [packet], "%s; reject=death_tower:%s; ACK_fail/%s" % (
                base,
                consume_note,
                mode,
            )

        ack = build_use_stackable_ack_ok(
            slot_index=slot,
            list_type=list_type,
            instance_value=instance,
            item_template_id=actual_item,
        )
        packets: list[bytes] = []
        packet_ack, mode_ack = self._emit(1, 0x002C, ack)
        packets.append(packet_ack)

        update_entries = _tower_inventory.build_death_tower_update_entries(
            session,
            detail["changed_slots"],
        )
        body_e = build_item_list_update_000e(
            update_entries,
            list_type=_tower_inventory.DEATH_TOWER_LIST_TYPE,
        )
        packet_e, mode_e = self._emit(0, 0x000E, body_e)
        packets.append(packet_e)

        completion_body = struct.pack("<H", 0)
        packet_done, mode_done = self._emit(0, 0x0012, completion_body)
        packets.append(packet_done)
        return packets, (
            "%s; OK death_tower removed=%d remain=%d item=%d; "
            "ACK_ok/%s; 000E/%s list=0x%02X; "
            "0012_COMPLETE/%s entry_count=0; %s; "
            "chain_file=opcodes/opcode002c-use_stackable.py"
            % (
                base,
                int(detail.get("removed", 0) or 0),
                int(detail.get("remaining", 0) or 0),
                actual_item,
                mode_ack,
                mode_e,
                _tower_inventory.DEATH_TOWER_LIST_TYPE,
                mode_done,
                consume_note,
            )
        )

    def _sync_use_item_progress(
        self,
        character_id: int,
        *,
        item_id: int,
        used_count: int,
        session: Any,
    ) -> tuple[list[bytes], str]:
        if not env_bool("USDOF_GAME_QUEST_RESIDUAL_USE_ITEM", True):
            return [], "use_item_progress=OFF"
        repo = self.server._repo()
        active_rows = repo.list_active_quests(int(character_id))
        active_pairs = [
            (int(quest_id), int(trigger))
            for _slot, quest_id, trigger in active_rows
        ]
        changed = apply_used_item_to_active_quests(
            active_pairs,
            item_id=int(item_id),
            used_count=int(used_count),
        )
        if not changed:
            return [], "use_item_progress=none"

        updated: list[tuple[int, int]] = []
        notes: list[str] = []
        for quest_id, _old, new, progress_note in changed:
            ok, update_note = repo.update_active_quest_trigger(
                int(character_id),
                int(quest_id),
                int(new),
            )
            if ok:
                updated.append((int(quest_id), int(new)))
                notes.append(progress_note)
            else:
                notes.append("q=%s db_skip=%s" % (quest_id, update_note))
        if not updated:
            return [], "use_item_progress updated=0 %s" % "; ".join(notes)

        if session is not None:
            current_rows = repo.list_active_quests(int(character_id))
            bind_session_quest_state(
                session,
                active_pairs=[
                    (int(quest_id), int(trigger))
                    for _slot, quest_id, trigger in current_rows
                ],
                finished_ids=[
                    int(quest_id)
                    for quest_id, _flag in repo.list_finished_quests(
                        int(character_id)
                    )
                ],
            )

        packets: list[bytes] = []
        packet_note = "S1/0021 skipped"
        if env_bool("USDOF_GAME_QUEST_RESIDUAL_SEND_0021", True):
            packets, packet_note = pack_set_trigger_ack_packets(
                self.server,
                updated,
            )
        return packets, "use_item_progress n=%s %s %s" % (
            len(updated),
            packet_note,
            "; ".join(notes),
        )

    def handle_use_stackable_002c(
        self, body: bytes, *, session: Any = None
    ) -> tuple[list[bytes], str]:
        if not env_bool("USDOF_GAME_HANDLE_USE_STACKABLE_002C", True):
            return [], "C1/002C USE_STACKABLE gate=OFF"

        parsed = parse_use_stackable_body(body)
        if parsed is None:
            ack = build_use_stackable_ack_error()
            pkt, mode = self._emit(1, 0x002C, ack)
            return [pkt], (
                "C1/002C USE_STACKABLE parse_fail plain_len=%s; ACK_fail/%s"
                % (len(body or b""), mode)
            )

        list_type = int(parsed["list_type"]) & 0xFF
        slot = int(parsed["slot_index"])
        instance = int(parsed["instance_value"])
        req_item = int(parsed["item_template_id"])
        reserved = int(parsed["reserved"])
        base = (
            "C1/002C USE_STACKABLE layout=%s list=%s slot=%s item=%s iv=0x%X res=0x%X plain=%s"
            % (
                parsed.get("layout"),
                list_type,
                slot,
                req_item,
                instance & 0xFFFFFFFF,
                reserved & 0xFFFFFFFF,
                parsed.get("raw_hex"),
            )
        )

        if (
            list_type in (0, _tower_inventory.DEATH_TOWER_LIST_TYPE)
            and _tower_inventory.request_targets_death_tower_waste(
                session,
                slot_index=slot,
                expected_item_id=req_item,
            )
        ):
            return self._handle_death_tower_use(
                parsed,
                session=session,
                base=base,
            )

        # Main / Avatar / PersonalCargo / Pet
        if list_type not in (0, 1, 2, 7):
            ack = build_use_stackable_ack_error(
                list_type=list_type, item_template_id=req_item, instance_value=instance
            )
            pkt, mode = self._emit(1, 0x002C, ack)
            return [pkt], base + "; reject=list_type; ACK_fail/%s" % mode

        if list_type == 0 and slot == 0:
            ack = build_use_stackable_ack_error(
                list_type=list_type, item_template_id=req_item, instance_value=instance
            )
            pkt, mode = self._emit(1, 0x002C, ack)
            return [pkt], base + "; reject=wallet_slot; ACK_fail/%s" % mode

        cid = _cid(session, self.server)
        if cid is None:
            ack = build_use_stackable_ack_error(
                list_type=list_type, item_template_id=req_item, instance_value=instance
            )
            pkt, mode = self._emit(1, 0x002C, ack)
            return [pkt], base + "; skip=no_cid; ACK_fail/%s" % mode

        repo = self.server._repo()
        # add-fatigue uses the independently observed C1/0204 action 0x36.
        # Do not let the generic drink path consume a potion without its effect.
        dedicated_actions = {
            "[add fatigue]": "fatigue_requires_0204",
            "[character name change]": "rename_requires_01fe",
            "[cargo transport stone]": "cargo_transport_requires_022e",
        }
        reason = ("zero_template" if req_item == 0 else
                  dedicated_actions.get(resolve_item_metadata(req_item & 0xFFFFFFFF).action_type))
        if reason:
            ack = build_use_stackable_ack_error(
                list_type=list_type, item_template_id=req_item, instance_value=instance
            )
            packet, mode = self._emit(1, 0x002C, ack)
            return [packet], base + "; reject=%s; ACK_fail/%s" % (reason, mode)
        active_rows = repo.list_active_quests(int(cid))
        active_pairs = [
            (int(quest_id), int(trigger))
            for _slot, quest_id, trigger in active_rows
        ]
        capture, capture_match_note = resolve_seeking_capture_use(
            active_pairs,
            item_id=req_item,
            session=session,
        )
        if capture is None and capture_match_note.startswith("capture=reject"):
            ack = build_use_stackable_ack_error(
                list_type=list_type,
                item_template_id=req_item,
                instance_value=instance,
            )
            pkt, mode = self._emit(1, 0x002C, ack)
            return [pkt], "%s; %s; ACK_fail/%s" % (base, capture_match_note, mode)
        if capture is not None:
            pending_capture, _pending_note = pending_seeking_capture_drop(session)
            if pending_capture is not None:
                ack = build_use_stackable_ack_error(
                    list_type=list_type,
                    item_template_id=req_item,
                    instance_value=instance,
                )
                pkt, mode = self._emit(1, 0x002C, ack)
                return [pkt], (
                    "%s; %s; capture_drop=reject already_armed; ACK_fail/%s"
                    % (base, capture_match_note, mode)
                )
        point_book_effect = skill_point_book_effect(req_item)
        experience_rule = _experience_consumable.resolve_experience_consumable(req_item)
        if point_book_effect is not None or experience_rule is not None:
            # Stage3 uses C1/001E (i16 Main quickslot), not USE_STACKABLE, for
            # SP/TP books and direct experience items. Keep the wrong opcode
            # side-effect free.
            ack = build_use_stackable_ack_error(
                list_type=list_type,
                item_template_id=req_item,
                instance_value=instance,
            )
            pkt, mode = self._emit(1, 0x002C, ack)
            required_note = (
                "skill_point_book_requires_001e"
                if point_book_effect is not None
                else "experience_item_requires_001e"
            )
            return [pkt], (
                "%s; reject=%s; ACK_fail/%s"
                % (base, required_note, mode)
            )
        exp_bonus_rule = _exp_bonus_consumable.resolve_exp_bonus_consumable(req_item)
        with self._stage("db_consume"):
            if exp_bonus_rule is not None:
                ok, consume_note, detail = (
                    _exp_bonus_consumable.activate_exp_bonus_consumable_at(
                        repo,
                        int(cid),
                        list_type=list_type,
                        slot_index=int(slot),
                        expected_item_id=req_item,
                        rule=exp_bonus_rule,
                        hardcore=_session_hardcore(session),
                    )
                )
            else:
                ok, consume_note, detail = repo.consume_stackable_at(
                    int(cid),
                    list_type=list_type,
                    slot_index=int(slot),
                    expected_item_id=req_item,
                    count=1,
                )
        item_id = int(detail.get("actual_item_id") or req_item or 0)
        stack = max(0, int(detail.get("original_count") or 0))
        removed = int(detail.get("removed") or 0)
        remaining = int(detail.get("remaining") or 0)
        if not ok or removed <= 0:
            ack = build_use_stackable_ack_error(
                list_type=list_type, item_template_id=item_id, instance_value=instance,
                native_quest_elixir=exp_bonus_rule is not None and exp_bonus_rule.quest_bonus,
            )
            pkt, mode = self._emit(1, 0x002C, ack)
            return [
                pkt
            ], base + "; reject=%s db=%s; ACK_fail/%s" % (
                consume_note,
                int(detail.get("actual_item_id") or 0),
                mode,
            )

        echo_iv = instance if instance != 0 else max(0, stack)
        with self._stage("packet_build"):
            ack = build_use_stackable_ack_ok(
                slot_index=int(slot),
                list_type=list_type,
                instance_value=int(echo_iv),
                item_template_id=int(item_id),
            )
        packets: list[bytes] = []
        notes = [
            base,
            "OK removed=1 remain=%s item=%s consume=%s"
            % (remaining, item_id, consume_note),
        ]
        if exp_bonus_rule is not None:
            notes.append(
                "exp_bonus rate_ppm=%d duration_ms=%d expires_at=%d source=%s"
                % (
                    int(detail.get("bonus_rate_ppm") or 0),
                    int(exp_bonus_rule.duration_ms),
                    int(detail.get("expires_at") or 0),
                    exp_bonus_rule.source_path,
                )
            )

        pkt_ack, mode_ack = self._emit(1, 0x002C, ack)
        packets.append(pkt_ack)
        notes.append("ACK_ok/%s len=%d" % (mode_ack, len(ack)))

        if env_bool("USDOF_GAME_USE_STACKABLE_SEND_000E", True):
            if remaining > 0:
                with self._stage("packet_build"):
                    body_e = build_item_list_update_000e(
                        [(int(slot), int(item_id), int(remaining))],
                        list_type=list_type,
                    )
                clear_note = "remain=%s item=%s" % (remaining, item_id)
            else:
                # 空槽：item_id=-1，绝不用 0（0=金币）
                with self._stage("packet_build"):
                    body_e = build_item_list_clear_slot_000e(
                        int(slot), list_type=list_type
                    )
                clear_note = "remain=0 clear_empty item_id=-1"
            pkt_e, mode_e = self._emit(0, 0x000E, body_e)
            packets.append(pkt_e)
            notes.append("000E/%s %s" % (mode_e, clear_note))

        if _in_dungeon_run(session):
            # Stage3's S0/0012 reader clears the shared inventory-action wait
            # even when entry_count is zero.  ACK+000E alone left that wait
            # armed across leave/re-enter and after the revive wallet refresh,
            # so the client stopped emitting the next C1/002C.
            completion_body = struct.pack("<H", 0)
            pkt_done, mode_done = self._emit(0, 0x0012, completion_body)
            packets.append(pkt_done)
            notes.append(
                "0012_COMPLETE/%s entry_count=0 clear_inventory_wait" % mode_done
            )

        if capture is not None:
            capture_armed, capture_drop_note = arm_seeking_capture_ground_drop(
                session,
                capture,
            )
            notes.append(capture_match_note)
            notes.append(capture_drop_note)
            if not capture_armed:
                notes.append("capture_drop=arm_failed_after_consume")
        else:
            notes.append(capture_match_note)

        quest_packets, quest_note = self._sync_use_item_progress(
            int(cid),
            item_id=int(item_id),
            used_count=int(removed),
            session=session,
        )
        packets.extend(quest_packets)
        notes.append(quest_note)

        return packets, "%s; chain_file=opcodes/opcode002c-use_stackable.py" % (
            "; ".join(notes)
        )
