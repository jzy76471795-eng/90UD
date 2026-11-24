"""MOVE_ITEMSPACE C->S 0x0013 (Main <-> Equipment / PersonalCargo / AccountCargo / QuickSlot).

Wire notes (Stage3 live + S4A12)：
- equip: src=Main bag slot, dst=Equipment wear slot, SourceInstanceValue=item_id
- right-click unequip: same list directions, but SourceInstanceValue=0
  (server must take item off dst wear slot and put it into src bag slot)
- **empty-src**（源空 + 目标有物，iv 常 0）：把目标整格移入源空槽
  - 快捷栏 list0 槽 3..8：背包道具拖到空快捷格
  - 金库 list2：有物→空槽 live 常发 src=空 dst=占用
- 个人仓库 list_type=2：
  - 空槽直移 / empty-src 填空
  - 金库内占用：整格互换
  - 背包→金库占用：旧物弹回背包对应栏目再移入
- 账号金库 list_type=12（账户级，全角色共享 account_cargo_items）：
  - 与个人金库同 wire（C1/0013）；规则对齐 list2
  - 成功后刷 S0/000D list12（selection_key + gold + entries）
- 快捷栏互换：Main list0 内 swap
- 公会背包 internal list36：勋章页 0..48、守护珠页 49..97；
  全量刷新走 S0/000D discriminator 39，勋章穿戴位为 list3/slot31
"""
from __future__ import annotations

import struct
from typing import Any

from game.character.amplify_option import parse_amplify_state
from game.shop.cerashop_index import resolve_mileage_inventory_item
from game.character.enchant_by_bead import parse_enchant_state
from game.character.inventory_equipment_lock_ui import (
    build_equipment_lock_restore_packets,
)
from game.character.inventory_sort_lock_ui import (
    build_inventory_sort_lock_restore_packets,
)
from game.character.native_loadout_bridge import (
    note_native_loadout_move_result,
    validate_native_loadout_move,
)
from game.character.weapon_forging import parse_forging_level
from game.character.payloads import (
    build_account_cargo_item_list,
    build_avatar_item_list_mode0,
    build_creature_item_list,
    build_guild_medal_item_list_mode0,
    build_item_list_mode0,
    build_item_list_update_000e as build_common_item_list_update_000e,
    build_pet_item_list_mode0,
)
from game.chain_loader import load_chain
from game.quest.quest_common import env_bool
from game.quest.quest_session import resolve_selected_character_detail
from game.shop.premium_contract_index import get_active_premium_effects
from game.titlebook.titlebook_repository import TitleBookCustomRepository


_tower_inventory = load_chain("helper-death_tower_inventory.py")
_dungeon_payloads = load_chain("helper-dungeon_payloads.py")
build_item_list_update_000e = _dungeon_payloads.build_item_list_update_000e


class SimpleInventoryItem:
    __slots__ = (
        "slot_index",
        "item_template_id",
        "stack_count",
        "durability",
        "instance_value",
        "item_kind",
        "seal_flag",
        "random_options",
        "option_value",
        "expire_time",
        "marker_16",
        "equipment_lock_id",
        "amplify_type",
        "amplify_value",
        "forging_level",
        "enchant_index",
        "enchant_upgrade_count",
        "pet_serial_or_handle",
        "extra_json",
    )

    def __init__(
        self,
        slot_index: int,
        item_template_id: int,
        stack_count: int,
        durability: int = 0,
        *,
        instance_value: int = 0,
        item_kind: str = "",
        seal_flag: int = 0,
        random_options: list | None = None,
        option_value: int = 0,
        expire_time: int = 0,
        marker_16: int = 0,
        equipment_lock_id: int = 0,
        extra_json: Any = None,
        pet_serial_or_handle: int = 0,
    ) -> None:
        self.slot_index = int(slot_index)
        self.item_template_id = int(item_template_id)
        self.stack_count = int(stack_count)
        self.durability = max(0, int(durability or 0)) & 0xFFFF
        self.instance_value = max(0, int(instance_value or 0)) & 0xFFFFFFFF
        self.item_kind = str(item_kind or "")
        self.seal_flag = max(0, int(seal_flag or 0)) & 0xFF
        self.random_options = list(random_options or [])
        self.option_value = max(0, int(option_value or 0)) & 0xFF
        self.expire_time = max(0, int(expire_time or 0)) & 0xFFFFFFFF
        self.marker_16 = max(0, int(marker_16 or 0)) & 0xFFFFFFFF
        self.equipment_lock_id = (
            max(0, int(equipment_lock_id or 0)) & 0xFF
        )
        self.amplify_type, self.amplify_value = parse_amplify_state(extra_json)
        self.forging_level = parse_forging_level(extra_json)
        self.enchant_index, self.enchant_upgrade_count = parse_enchant_state(
            extra_json
        )
        self.pet_serial_or_handle = max(
            0, int(pet_serial_or_handle or 0)
        ) & 0xFFFFFFFF
        self.extra_json = extra_json


def build_move_itemspace_ack_ok(
    *,
    source_list_type: int,
    source_slot: int,
    move_value32: int,
    destination_list_type: int,
    destination_slot: int,
) -> bytes:
    # Packed without alignment padding: u8 u8 i16 i32 u8 i16
    return (
        bytes([0x01, int(source_list_type) & 0xFF])
        + struct.pack("<h", int(source_slot))
        + struct.pack("<i", int(move_value32) & 0xFFFFFFFF)
        + bytes([int(destination_list_type) & 0xFF])
        + struct.pack("<h", int(destination_slot))
    )



def build_move_itemspace_ack_error(
    *,
    error_code: int = 0x04,
    source_list_type: int = 0,
    destination_list_type: int = 0,
) -> bytes:
    return bytes(
        [
            0x00,
            int(error_code) & 0xFF,
            int(source_list_type) & 0xFF,
            int(destination_list_type) & 0xFF,
        ]
    )


def parse_move_itemspace_body(body: bytes) -> dict[str, int] | None:
    data = bytes(body or b"")
    if len(data) < 14:
        return None
    src_lt = data[0]
    src_slot = struct.unpack_from("<h", data, 1)[0]
    src_iv = struct.unpack_from("<i", data, 3)[0]
    move_count = struct.unpack_from("<i", data, 7)[0]
    dst_lt = data[11]
    dst_slot = struct.unpack_from("<h", data, 12)[0]
    dst_iv = struct.unpack_from("<i", data, 14)[0] if len(data) >= 18 else 0
    return {
        "source_list_type": int(src_lt) & 0xFF,
        "source_slot": int(src_slot),
        "source_instance_value": int(src_iv),
        "move_count": int(move_count),
        "destination_list_type": int(dst_lt) & 0xFF,
        "destination_slot": int(dst_slot),
        "destination_instance_value": int(dst_iv),
        "raw_len": len(data),
    }


def _main_equipment_refresh_slots(result: dict[str, Any]) -> tuple[int, ...]:
    """Return Main slots that now contain an item displaced from list3."""
    if int(result.get("mutated", 0) or 0) != 1:
        return ()
    source_list_type = int(result.get("source_list_type", -1))
    destination_list_type = int(result.get("destination_list_type", -1))
    slots: list[int] = []
    if source_list_type == 0 and destination_list_type == 3:
        slots.append(int(result.get("source_slot", -1)))
    if source_list_type == 3 and destination_list_type == 0:
        slots.append(int(result.get("destination_slot", -1)))
    return tuple(slot for slot in slots if slot >= 0)


def _uses_dungeon_weapon_avatar_native_commit(
    result: dict[str, Any],
    session: Any,
) -> bool:
    if not bool(getattr(session, "in_dungeon_run", False)):
        return False
    if int(result.get("avatar_changed", 0) or 0) != 1:
        return False

    source_list_type = int(result.get("source_list_type", -1))
    source_slot = int(result.get("source_slot", -1))
    destination_list_type = int(result.get("destination_list_type", -1))
    destination_slot = int(result.get("destination_slot", -1))
    return (
        source_list_type == 1
        and destination_list_type == 3
        and destination_slot == 10
    ) or (
        source_list_type == 3
        and source_slot == 10
        and destination_list_type == 1
    )


def _equipped_weapon_avatar_item_id(repo: Any, character_id: int) -> int:
    """Return the live slot-10 weapon-avatar item, or zero when absent."""
    if repo is None or int(character_id or 0) <= 0:
        return 0
    try:
        rows = repo.list_equipped_items(int(character_id)) or ()
    except Exception:
        return 0
    for row in rows:
        if int(row.get("slot", -1) or -1) != 10:
            continue
        item_id = int(row.get("item_id", 0) or 0) & 0xFFFFFFFF
        if item_id > 0:
            return item_id
    return 0


def _uses_dungeon_physical_weapon_skip_000e(
    result: dict[str, Any],
    session: Any,
    repo: Any,
    character_id: int,
) -> int:
    """Return the live weapon-avatar ID when a slot-12 move must skip 000E.

    The native S1/0013 callback already commits the primary weapon render. A
    full list3 update after that callback can overwrite the render proxy while
    the dungeon state machine forbids the USERINFO reprime that would repair it.
    Keep this gate limited to the primary weapon and an actually equipped slot
    10 avatar; callers use the returned ID for a diagnostic note.
    """
    if not bool(getattr(session, "in_dungeon_run", False)):
        return 0
    if int(result.get("mutated", 0) or 0) != 1:
        return 0
    if int(result.get("avatar_changed", 0) or 0) == 1:
        return 0

    source_list_type = int(result.get("source_list_type", -1))
    source_slot = int(result.get("source_slot", -1))
    destination_list_type = int(result.get("destination_list_type", -1))
    destination_slot = int(result.get("destination_slot", -1))
    primary_weapon_move = (
        source_list_type == 0
        and destination_list_type == 3
        and destination_slot == 12
    ) or (
        source_list_type == 3
        and source_slot == 12
        and destination_list_type == 0
    )
    if not primary_weapon_move:
        return 0
    return _equipped_weapon_avatar_item_id(repo, character_id)


class MoveItemspace0013Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_move_itemspace_0013(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        handle_on = env_bool("USDOF_GAME_HANDLE_MOVE_ITEMSPACE_0013", True)
        parsed = parse_move_itemspace_body(body)
        detail = resolve_selected_character_detail(self.server, session)
        cid = detail.get("character_id")
        job = int(detail.get("job") or 0)
        level = int(detail.get("level") or 1)
        base = (
            "C1/0013 MOVE_ITEMSPACE cid=%s job=%s level=%s plain_len=%s"
            % (cid, job, level, len(body or b""))
        )
        if not handle_on:
            return [], base + "; gate=OFF"
        if parsed is None:
            packets = [
                self.server._codec_response(
                    1,
                    0x0013,
                    build_move_itemspace_ack_error(error_code=0x04),
                )
            ]
            return packets, base + "; reject=short_body"
        if cid is None:
            packets = [
                self.server._codec_response(
                    1,
                    0x0013,
                    build_move_itemspace_ack_error(
                        error_code=0x04,
                        source_list_type=parsed["source_list_type"],
                        destination_list_type=parsed["destination_list_type"],
                    ),
                )
            ]
            return packets, base + "; reject=no_character"

        # A dungeon native-loadout plan is only allowed to commit its one
        # currently released action.  The client native mover serializes the
        # live source cell, so validate the complete C1 request before the
        # repository transaction can move a different item after state drift.
        try:
            native_move_allowed, native_loadout_note = validate_native_loadout_move(
                session,
                parsed,
            )
        except OSError as exc:
            # The plan has already been removed and session state cleared by
            # the abort helper.  Still reject this request rather than letting
            # a move through without a matching side-channel acknowledgement.
            native_move_allowed = False
            native_loadout_note = (
                "native_loadout_abort_ack_write_fail:%s" % type(exc).__name__
            )
        if not native_move_allowed:
            packets = [
                self.server._codec_response(
                    1,
                    0x0013,
                    build_move_itemspace_ack_error(
                        error_code=0x04,
                        source_list_type=parsed["source_list_type"],
                        destination_list_type=parsed["destination_list_type"],
                    ),
                )
            ]
            return packets, (
                "%s; src=(%s,%s,iv=%s,cnt=%s) dst=(%s,%s); "
                "FAIL native_loadout_guard %s"
                % (
                    base,
                    parsed["source_list_type"],
                    parsed["source_slot"],
                    parsed["source_instance_value"],
                    parsed["move_count"],
                    parsed["destination_list_type"],
                    parsed["destination_slot"],
                    native_loadout_note or "rejected",
                )
            )

        source_list_type = int(parsed["source_list_type"]) & 0xFF
        destination_list_type = int(parsed["destination_list_type"]) & 0xFF
        touches_tower_inventory = (
            source_list_type == _tower_inventory.DEATH_TOWER_LIST_TYPE
            or destination_list_type == _tower_inventory.DEATH_TOWER_LIST_TYPE
        )
        if touches_tower_inventory:
            if (
                not _tower_inventory.is_active_death_tower_session(session)
                or source_list_type != _tower_inventory.DEATH_TOWER_LIST_TYPE
                or destination_list_type != _tower_inventory.DEATH_TOWER_LIST_TYPE
            ):
                packet = self.server._codec_response(
                    1,
                    0x0013,
                    build_move_itemspace_ack_error(
                        error_code=0x04,
                        source_list_type=source_list_type,
                        destination_list_type=destination_list_type,
                    ),
                )
                return [packet], (
                    "%s; reject=death_tower_cross_container src=%s dst=%s"
                    % (base, source_list_type, destination_list_type)
                )

            ok, move_note, result = _tower_inventory.move_death_tower_item(
                session,
                source_slot=int(parsed["source_slot"]),
                destination_slot=int(parsed["destination_slot"]),
                requested_count=int(parsed["move_count"]),
            )
            if not ok:
                packet = self.server._codec_response(
                    1,
                    0x0013,
                    build_move_itemspace_ack_error(
                        error_code=0x04,
                        source_list_type=source_list_type,
                        destination_list_type=destination_list_type,
                    ),
                )
                return [packet], "%s; FAIL death_tower_move %s" % (
                    base,
                    move_note,
                )

            packets = [
                self.server._codec_response(
                    1,
                    0x0013,
                    build_move_itemspace_ack_ok(
                        source_list_type=source_list_type,
                        source_slot=int(parsed["source_slot"]),
                        move_value32=int(result["move_value32"]),
                        destination_list_type=destination_list_type,
                        destination_slot=int(parsed["destination_slot"]),
                    ),
                )
            ]
            changed_slots = tuple(result.get("changed_slots", ()) or ())
            if changed_slots:
                update_entries = (
                    _tower_inventory.build_death_tower_update_entries(
                        session,
                        changed_slots,
                    )
                )
                body_e = build_item_list_update_000e(
                    update_entries,
                    list_type=_tower_inventory.DEATH_TOWER_LIST_TYPE,
                )
                packets.append(self.server._codec_response(0, 0x000E, body_e))
            return packets, (
                "%s; OK death_tower_move src=%s dst=%s count=%s "
                "changed=%s list=0x%02X %s"
                % (
                    base,
                    parsed["source_slot"],
                    parsed["destination_slot"],
                    parsed["move_count"],
                    ",".join(str(slot) for slot in changed_slots) or "-",
                    _tower_inventory.DEATH_TOWER_LIST_TYPE,
                    move_note,
                )
            )

        repo = self.server._repo()
        account_id = repo.get_account_id_for_character(int(cid))
        if account_id is None:
            account_id = int(getattr(self.server, "account_id", 1) or 1)
        premium_effects = get_active_premium_effects(
            repo,
            int(account_id),
            session=session,
        )
        equip_level_reduction = max(
            0,
            int(premium_effects.equipment_level_reduction),
        )
        base += "; equip_level_reduction=%s" % equip_level_reduction
        title_right_click = (
            int(parsed["source_list_type"]) == 0
            and int(parsed["destination_list_type"]) == 3
            and int(parsed["destination_slot"]) == 13
            and int(parsed["source_instance_value"]) == 0
        )
        title_custom_repo = None
        title_transition_note = ""
        prepare_ok = True
        if title_right_click:
            title_custom_repo = TitleBookCustomRepository(repo)
            prepare_ok, prepare_note, _prepare_result = (
                title_custom_repo.prepare_equipped_custom_for_main(
                    int(cid),
                    equip_slot=int(parsed["destination_slot"]),
                )
            )
            title_transition_note = prepare_note

        if prepare_ok:
            source_value = int(parsed["source_instance_value"])
            # Immediately after checkout the client can still hold the shop
            # display ID in its local avatar or pet cell. The repository
            # still checks that the persisted item matches the selected slot.
            if source_value and (
                source_list_type in (1, 7)
                or (source_list_type == 3 and destination_list_type in (1, 7))
            ):
                source_value = resolve_mileage_inventory_item(source_value)
            ok, note, result = repo.try_move_itemspace(
                int(cid),
                source_list_type=source_list_type,
                source_slot=int(parsed["source_slot"]),
                source_instance_value=source_value,
                move_count=int(parsed["move_count"]),
                destination_list_type=int(parsed["destination_list_type"]),
                destination_slot=int(parsed["destination_slot"]),
                job=int(job),
                level=int(level),
                equipment_level_reduction=equip_level_reduction,
            )
        else:
            ok, note, result = False, title_transition_note, {}

        if ok and title_right_click and title_custom_repo is not None:
            capture_ok, capture_note, _capture_result = (
                title_custom_repo.capture_unequipped_main_title(
                    int(cid),
                    equip_slot=int(parsed["destination_slot"]),
                    main_slot=int(parsed["source_slot"]),
                    item_id=int(result.get("item_id", 0) or 0),
                )
            )
            title_transition_note = "%s;%s" % (
                title_transition_note,
                capture_note,
            )
            if not capture_ok:
                note = "%s; %s" % (note, capture_note)
        packets: list[bytes] = []
        if not ok:
            native_loadout_note = ""
            if session is not None:
                try:
                    native_loadout_note = note_native_loadout_move_result(
                        session,
                        parsed,
                        ok=False,
                    )
                except OSError as exc:
                    native_loadout_note = (
                        "native_loadout_ack_write_fail:%s" % type(exc).__name__
                    )
            err = 0x04
            # restriction failures use a distinct code so client can reverse cleanly
            # 骑士盾校验失败勿用 0x04（客户端当「金库/位置满」）
            if str(note).startswith("equip_blocked:") or str(note).startswith(
                "knight_shield_"
            ):
                err = 0x02
            packets.append(
                self.server._codec_response(
                    1,
                    0x0013,
                    build_move_itemspace_ack_error(
                        error_code=err,
                        source_list_type=parsed["source_list_type"],
                        destination_list_type=parsed["destination_list_type"],
                    ),
                )
            )
            response_note = (
                "%s; src=(%s,%s,iv=%s,cnt=%s) dst=(%s,%s); FAIL %s"
                % (
                    base,
                    parsed["source_list_type"],
                    parsed["source_slot"],
                    parsed["source_instance_value"],
                    parsed["move_count"],
                    parsed["destination_list_type"],
                    parsed["destination_slot"],
                    note,
                )
            )
            if native_loadout_note:
                response_note += "; " + native_loadout_note
            if title_transition_note:
                response_note += "; " + title_transition_note
            return packets, response_note

        native_loadout_note = ""
        if session is not None:
            try:
                native_loadout_note = note_native_loadout_move_result(
                    session,
                    parsed,
                    ok=True,
                )
            except OSError as exc:
                native_loadout_note = (
                    "native_loadout_ack_write_fail:%s" % type(exc).__name__
                )
        packets.append(
            self.server._codec_response(
                1,
                0x0013,
                build_move_itemspace_ack_ok(
                    source_list_type=int(result["source_list_type"]),
                    source_slot=int(result["source_slot"]),
                    move_value32=int(result["move_value32"]),
                    destination_list_type=int(result["destination_list_type"]),
                    destination_slot=int(result["destination_slot"]),
                ),
            )
        )

        # Stage3 S1/0013 calls the native container-to-equipment mover before
        # returning from the success callback. For dungeon weapon-avatar slot
        # 10 this is also the render commit: publishing full list1/list3 again
        # replaces the objects that the callback has just installed and leaves
        # the actor empty-handed. Keep this path on the native ACK transaction;
        # the repository mutation above remains the persistent authority.
        if _uses_dungeon_weapon_avatar_native_commit(result, session):
            response_note = (
                "%s; src=(%s,%s,iv=%s,cnt=%s) dst=(%s,%s); OK %s item=%s; "
                "dungeon_weapon_avatar=native_0013_commit_only attempt=2/3"
                % (
                    base,
                    parsed["source_list_type"],
                    parsed["source_slot"],
                    parsed["source_instance_value"],
                    parsed["move_count"],
                    parsed["destination_list_type"],
                    parsed["destination_slot"],
                    note,
                    result.get("item_id", 0),
                )
            )
            if native_loadout_note:
                response_note += "; " + native_loadout_note
            if title_transition_note:
                response_note += "; " + title_transition_note
            return packets, response_note

        dungeon_physical_weapon_avatar_id = (
            _uses_dungeon_physical_weapon_skip_000e(
                result,
                session,
                repo,
                int(cid),
            )
        )

        # 刷新本次实际涉及的容器：Main=list0，Avatar=list1，衣柜=list33。若涉及
        # 装备栏 list3 穿/卸，再发
        # S0/000E list_type=3（对照 S4A12 SendUpdateItemList Equipment /
        # 90qiyuan op14 equipment path）+ S0/0002 USERINFO0（纸娃娃模型；
        # 对照 S4A12 SendNoti2AppearanceUpdate——客户端不会因 ACK/000E 自刷模型）。
        # list1/list33 使用专用时装 wire entry；list33 的头没有 list_param16。
        # 涉及 PersonalCargo list2 时再刷一份 list_type=2 的 000D。
        # 骑士盾 list31/32 是虚拟目录/装备栏：不刷 list0。
        list_notes: list[str] = []
        main_equipment_refresh_slots = frozenset(
            _main_equipment_refresh_slots(result)
        )
        knight_shield_only = (
            int(result.get("knight_shield_changed", 0) or 0) == 1
            and int(result.get("source_list_type", -1))
            in (31, 32)
            and int(result.get("destination_list_type", -1)) in (31, 32)
        )
        if (
            env_bool("USDOF_GAME_MOVE_ITEMSPACE_SEND_ITEMLIST", True)
            and not knight_shield_only
        ):
            try:
                from game.chain_loader import load_chain as _load_ro

                _row_ro = _load_ro("helper-random_option.py").inventory_row_random_options
            except Exception:
                _row_ro = lambda _r: []  # noqa: E731
            touched_container_types = [
                lt
                for lt in (
                    0,
                    1,
                    repo.INVENTORY_LIST_AVATAR_CLOSET,
                    7,
                    repo.INVENTORY_LIST_GUILD_MEDAL,
                )
                if int(result.get("source_list_type", -1)) == lt
                or int(result.get("destination_list_type", -1)) == lt
            ]
            for lt in touched_container_types:
                rows = repo.list_inventory_items(int(cid), list_type=lt)
                items = [
                    SimpleInventoryItem(
                        row["slot_index"],
                        row["item_template_id"],
                        row["stack_count"],
                        row.get("durability", 0),
                        instance_value=int(row.get("instance_value", 0) or 0),
                        item_kind=str(row.get("item_kind", "") or ""),
                        seal_flag=int(row.get("seal_flag", 0) or 0),
                        random_options=_row_ro(row),
                        option_value=int(row.get("option_value", 0) or 0),
                        expire_time=int(row.get("expire_time", 0) or 0),
                        marker_16=int(row.get("marker_16", 0) or 0),
                        equipment_lock_id=int(
                            row.get("equipment_lock_id", 0) or 0
                        ),
                        extra_json=row.get("extra_json"),
                        pet_serial_or_handle=int(
                            row.get("pet_serial_or_handle", 0) or 0
                        ),
                    )
                    for row in rows
                ]
                list_param16 = 0
                if lt not in (
                    repo.INVENTORY_LIST_AVATAR_CLOSET,
                    repo.INVENTORY_LIST_GUILD_MEDAL,
                ):
                    list_param16 = int(
                        repo.get_container_list_param(int(cid), list_type=lt)
                    )
                if lt in (1, repo.INVENTORY_LIST_AVATAR_CLOSET):
                    body_000d = build_avatar_item_list_mode0(
                        items,
                        list_type=lt,
                        list_param16=list_param16,
                    )
                elif lt == 7:
                    body_000d = build_pet_item_list_mode0(items)
                elif lt == repo.INVENTORY_LIST_GUILD_MEDAL:
                    body_000d = build_guild_medal_item_list_mode0(
                        items,
                        selected_character_slot=int(
                            getattr(session, "selected_slot", 0) or 0
                        ),
                    )
                else:
                    body_000d = build_item_list_mode0(
                        items,
                        list_type=lt,
                        list_param16=list_param16,
                    )
                if (
                    lt == int(repo.INVENTORY_LIST_MAIN)
                    and main_equipment_refresh_slots
                    and not dungeon_physical_weapon_avatar_id
                ):
                    recreate_items = [
                        item
                        for item in items
                        if int(item.slot_index) in main_equipment_refresh_slots
                        and str(item.item_kind or "") == "equipment"
                    ]
                    if recreate_items:
                        # 01666D80 creates a fresh full-state item when the
                        # slot is absent, without 01667BD0's acquisition queue.
                        # Clear before the authoritative 000D, so enchant and
                        # other instance fields reload quietly after unequip.
                        clear_entries = [
                            (int(item.slot_index), -1, 0)
                            for item in recreate_items
                        ]
                        body_clear = build_item_list_update_000e(
                            clear_entries,
                            list_type=int(repo.INVENTORY_LIST_MAIN),
                        )
                        packets.append(
                            self.server._codec_response(
                                0,
                                0x000E,
                                body_clear,
                            )
                        )
                        list_notes.append(
                            "000E/list0 clear_before_quiet_000D n=%s slots=%s"
                            % (
                                len(recreate_items),
                                ",".join(
                                    str(int(item.slot_index))
                                    for item in recreate_items
                                ),
                            )
                        )
                packets.append(self.server._codec_response(0, 0x000D, body_000d))
                list_notes.append("000D/list%s n=%s" % (lt, len(items)))
                if lt in (
                    int(repo.INVENTORY_LIST_MAIN),
                    int(repo.INVENTORY_LIST_AVATAR),
                ):
                    try:
                        restore_packets, restore_note = (
                            build_inventory_sort_lock_restore_packets(
                                self.server,
                                repo,
                                character_id=int(cid),
                                list_type=lt,
                            )
                        )
                        packets.extend(restore_packets)
                        list_notes.append(restore_note)
                    except Exception as lock_exc:
                        list_notes.append(
                            "sort_lock_restore_fail:%s:%s"
                            % (type(lock_exc).__name__, lock_exc)
                        )
                if lt in (
                    int(repo.INVENTORY_LIST_MAIN),
                    int(repo.INVENTORY_LIST_AVATAR),
                    int(repo.INVENTORY_LIST_GUILD_MEDAL),
                ):
                    try:
                        equipment_restore_packets, equipment_restore_note = (
                            build_equipment_lock_restore_packets(
                                self.server,
                                repo,
                                character_id=int(cid),
                                list_types=(lt,),
                            )
                        )
                        packets.extend(equipment_restore_packets)
                        list_notes.append(equipment_restore_note)
                    except Exception as lock_exc:
                        list_notes.append(
                            "equipment_lock_restore_fail:%s:%s"
                            % (type(lock_exc).__name__, lock_exc)
                        )

            touched_cargo = (
                int(result.get("source_list_type", 0)) == 2
                or int(result.get("destination_list_type", 0)) == 2
            )
            if touched_cargo and env_bool(
                "USDOF_GAME_MOVE_ITEMSPACE_SEND_CARGO_ITEMLIST", True
            ):
                rows2 = repo.list_inventory_items(int(cid), list_type=2)
                items2 = [
                    SimpleInventoryItem(
                        row["slot_index"],
                        row["item_template_id"],
                        row["stack_count"],
                        row.get("durability", 0),
                        instance_value=int(row.get("instance_value", 0) or 0),
                        item_kind=str(row.get("item_kind", "") or ""),
                        seal_flag=int(row.get("seal_flag", 0) or 0),
                        random_options=_row_ro(row),
                        option_value=int(row.get("option_value", 0) or 0),
                        expire_time=int(row.get("expire_time", 0) or 0),
                        marker_16=int(row.get("marker_16", 0) or 0),
                        extra_json=row.get("extra_json"),
                    )
                    for row in rows2
                ]
                param2 = int(repo.get_container_list_param(int(cid), list_type=2))
                body2 = build_item_list_mode0(
                    items2,
                    list_type=2,
                    list_param16=param2,
                )
                packets.append(self.server._codec_response(0, 0x000D, body2))
                list_notes.append("000D/list2 n=%s" % len(items2))

            # 账号金库 list12：账户级刷新（全角色共享）
            touched_acargo = (
                int(result.get("source_list_type", 0)) == 12
                or int(result.get("destination_list_type", 0)) == 12
            )
            if touched_acargo and env_bool(
                "USDOF_GAME_MOVE_ITEMSPACE_SEND_ACCOUNT_CARGO_ITEMLIST", True
            ):
                try:
                    aid = repo.get_account_id_for_character(int(cid))
                    if aid is not None and int(aid) > 0:
                        ac_state = repo.get_account_cargo_state(
                            int(aid),
                            ensure_open=True,
                            character_level=int(level),
                        )
                        wire_key = int(ac_state.get("selection_key") or 0)
                        if wire_key <= 0:
                            wire_key = int(repo.DEFAULT_ACCOUNT_CARGO_SELECTION_KEY)
                        ac_rows = repo.list_account_cargo_items(
                            int(aid), character_level=int(level)
                        )
                        items12 = [
                            SimpleInventoryItem(
                                row["slot_index"],
                                row["item_template_id"],
                                row["stack_count"],
                                row.get("durability", 0),
                                instance_value=int(row.get("instance_value", 0) or 0),
                                item_kind=str(row.get("item_kind", "") or ""),
                                seal_flag=int(row.get("seal_flag", 0) or 0),
                                random_options=_row_ro(row),
                                option_value=int(row.get("option_value", 0) or 0),
                                expire_time=int(row.get("expire_time", 0) or 0),
                                marker_16=int(row.get("marker_16", 0) or 0),
                                extra_json=row.get("extra_json"),
                            )
                            for row in ac_rows
                        ]
                        body12 = build_account_cargo_item_list(
                            items12,
                            selection_key=wire_key,
                            cargo_gold=int(ac_state.get("value32", 0) or 0),
                        )
                        packets.append(
                            self.server._codec_response(0, 0x000D, body12)
                        )
                        list_notes.append(
                            "000D/list12 n=%s key=%s" % (len(items12), wire_key)
                        )
                except Exception as ac_exc:
                    list_notes.append(
                        "000D/list12_fail:%s" % type(ac_exc).__name__
                    )

        pet_equipment_changed = int(
            result.get("pet_equipment_changed", 0) or 0
        ) == 1
        pet_creature_changed = pet_equipment_changed and (
            (
                int(result.get("source_list_type", -1)) == 3
                and int(result.get("source_slot", -1)) == 26
            )
            or (
                int(result.get("destination_list_type", -1)) == 3
                and int(result.get("destination_slot", -1)) == 26
            )
        )
        creature_note = ""
        if pet_equipment_changed:
            try:
                creature_states = repo.list_creature_states(int(cid))
                creature_body = build_creature_item_list(creature_states)
                packets.append(
                    self.server._codec_response(0, 0x0069, creature_body)
                )
                creature_note = "S0/0069 creature_item_list n=%s" % len(
                    creature_states
                )
            except Exception as creature_exc:
                creature_note = "0069_fail:%s" % type(creature_exc).__name__

        # 骑士盾牌栏变更：不刷新普通背包 list0（目录/装备栏是虚拟空间）
        knight_shield_changed = int(result.get("knight_shield_changed", 0) or 0) == 1

        touched_equip_bar = (
            int(result.get("source_list_type", 0)) == 3
            or int(result.get("destination_list_type", 0)) == 3
            or int(result.get("titlebook_changed", 0)) == 1
            or int(result.get("title_unequip_noop", 0)) == 1
            or (
                knight_shield_changed
                and int(result.get("equipment_changed", 0) or 0) == 1
            )
        )
        equip_note = ""
        appearance_note = ""
        pending_equip_packet: bytes | None = None
        if touched_equip_bar and env_bool(
            "USDOF_GAME_MOVE_ITEMSPACE_SEND_EQUIP_ITEMLIST", True
        ) and not dungeon_physical_weapon_avatar_id:
            try:
                from game.chain_loader import load_chain

                equip_helper = load_chain("helper-equip_itemlist.py")
                equip_body, _equip_n, equip_note = equip_helper.build_equipment_bar_update_000e_body(
                    repo,
                    int(cid),
                )
                # USERINFO1 rebuilds list3 slot objects for body equipment as
                # well as newly occupied avatar/creature slots. Apply instance
                # state (forging, amplify, RO) only after that rebuild.
                pending_equip_packet = self.server._codec_response(
                    0, 0x000E, equip_body
                )
            except Exception as equip_exc:
                equip_note = "equip_bar_000e_fail:%s" % equip_exc
        elif dungeon_physical_weapon_avatar_id:
            equip_note = (
                "dungeon_physical_weapon_with_avatar="
                "native_0013_commit_skip_000e attempt=3/3 avatar_item=%s"
                % dungeon_physical_weapon_avatar_id
            )

        # 外观模型同步：武器/甲/称号等穿卸后必须 USERINFO0（装扮>装备>默认）。
        # 与称号簿路径共用；titlebook_changed 时若已发 USERINFO0 则不重复。
        # 骑士主盾变更：副本内也放行 **仅 U0**（属性/纸娃娃读 U0 装备段；
        # 000E 只刷装备栏 slot，不刷属性面板）。U1 仍禁。
        if pet_creature_changed and env_bool(
            "USDOF_GAME_MOVE_ITEMSPACE_SEND_USERINFO0", True
        ):
            try:
                from game.chain_loader import load_chain

                u0_helper = load_chain("helper-userinfo_equipment.py")
                u0_packets, appearance_note = u0_helper.build_userinfo0_appearance_packets(
                    self.server,
                    character_id=int(cid),
                    record=detail.get("record"),
                    repo=repo,
                    session=session,
                )
                packets.extend(u0_packets)
            except Exception as u0_exc:
                appearance_note = "USERINFO0_fail:%s" % type(u0_exc).__name__
        elif pet_equipment_changed:
            appearance_note = "USERINFO0_skip=pet_artifact"
        elif dungeon_physical_weapon_avatar_id:
            appearance_note = "USERINFO0_skip=dungeon_physical_weapon_native_commit"
        elif touched_equip_bar and env_bool(
            "USDOF_GAME_MOVE_ITEMSPACE_SEND_USERINFO0", True
        ):
            try:
                from game.chain_loader import load_chain

                u0_helper = load_chain("helper-userinfo_equipment.py")
                allow_dungeon_u0 = bool(
                    knight_shield_changed
                    and int(result.get("equipment_changed", 0) or 0) == 1
                )
                # 必须传 session：副本内禁 U1 reprime（否则无法 002D 换房）
                u0_packets, appearance_note = u0_helper.build_userinfo0_appearance_packets(
                    self.server,
                    character_id=int(cid),
                    record=detail.get("record"),
                    repo=repo,
                    session=session,
                    allow_dungeon_userinfo0=allow_dungeon_u0,
                )
                packets.extend(u0_packets)
            except Exception as u0_exc:
                appearance_note = "USERINFO0_fail:%s" % type(u0_exc).__name__

        if pending_equip_packet is not None:
            packets.append(pending_equip_packet)
            try:
                equipment_restore_packets, equipment_restore_note = (
                    build_equipment_lock_restore_packets(
                        self.server,
                        repo,
                        character_id=int(cid),
                        list_types=(3,),
                    )
                )
                packets.extend(equipment_restore_packets)
                equip_note = "%s; %s" % (equip_note, equipment_restore_note)
            except Exception as lock_exc:
                equip_note = "%s; equipment_lock_restore_fail:%s:%s" % (
                    equip_note,
                    type(lock_exc).__name__,
                    lock_exc,
                )

        # 骑士盾 deck：S0/0245 SEND_DECK_INFO（Stage3 sub_1674530 读 5×u32）。
        # 禁止 0x0244（频道 settle）。0013 变更后必须下发，否则 UI 底部槽与 DB 脱节。
        knight_deck_note = ""
        if knight_shield_changed:
            try:
                from game.chain_loader import load_chain

                ks = load_chain("helper-knight_shield.py")
                deck = repo.load_knight_shield_deck(int(cid))
                knight_deck_note = ks.describe_deck(deck)
                send_deck = env_bool(
                    "USDOF_GAME_MOVE_ITEMSPACE_SEND_KNIGHT_DECK", True
                ) and ks.should_send_deck_notification()
                if send_deck:
                    body_deck = ks.build_send_deck_info_body(deck)
                    packets.append(
                        self.server._codec_response(
                            0, int(ks.NOTI_SEND_DECK_INFO), body_deck
                        )
                    )
                    knight_deck_note = "S0/%04X %s" % (
                        int(ks.NOTI_SEND_DECK_INFO),
                        knight_deck_note,
                    )
                else:
                    knight_deck_note = "%s (deck_noti_off)" % knight_deck_note
            except Exception as deck_exc:
                knight_deck_note = "deck_note_fail:%s" % type(deck_exc).__name__

        # 称号簿内称号经 0013 卸回簿 / 换装挤回簿时刷 0166
        # （USERINFO0 已由上面外观路径统一发送，避免双发）
        titlebook_note = ""
        if int(result.get("titlebook_changed", 0)) == 1 and env_bool(
            "USDOF_GAME_MOVE_ITEMSPACE_SEND_TITLEBOOK", True
        ):
            try:
                from game.chain_loader import load_chain

                tb_payloads = load_chain("helper-titlebook_payloads.py")
                cat = int(result.get("title_category", 0) or 0)
                rows = repo.list_titlebook_items(int(cid), category=cat)
                body166 = tb_payloads.build_titlebook_list_0166(
                    cat, tb_payloads.entries_from_repo_rows(rows)
                )
                packets.append(self.server._codec_response(0, 0x0166, body166))
                titlebook_note = "0166 cat=%s n=%s" % (cat, len(rows))
            except Exception as tb_exc:
                titlebook_note = "0166_fail:%s" % type(tb_exc).__name__
            # 兼容旧开关：仅当总开关关、称号专用开时补发 USERINFO0
            if (
                not env_bool("USDOF_GAME_MOVE_ITEMSPACE_SEND_USERINFO0", True)
                and env_bool("USDOF_GAME_MOVE_ITEMSPACE_SEND_USERINFO0_ON_TITLE", True)
            ):
                try:
                    from game.chain_loader import load_chain

                    u0_helper = load_chain("helper-userinfo_equipment.py")
                    u0_packets, u0_note = u0_helper.build_userinfo0_appearance_packets(
                        self.server,
                        character_id=int(cid),
                        record=detail.get("record"),
                        repo=repo,
                        session=session,
                    )
                    packets.extend(u0_packets)
                    titlebook_note += ";%s" % u0_note
                except Exception as u_exc:
                    titlebook_note += ";USERINFO0_fail:%s" % type(u_exc).__name__

        extra = ""
        if list_notes:
            extra += "; " + ",".join(list_notes)
        if equip_note:
            extra += "; " + equip_note
        if creature_note:
            extra += "; " + creature_note
        if appearance_note:
            extra += "; " + appearance_note
        if knight_deck_note:
            extra += "; " + knight_deck_note
        if titlebook_note:
            extra += "; " + titlebook_note
        response_note = (
            "%s; src=(%s,%s,iv=%s,cnt=%s) dst=(%s,%s); OK %s item=%s%s"
            % (
                base,
                parsed["source_list_type"],
                parsed["source_slot"],
                parsed["source_instance_value"],
                parsed["move_count"],
                parsed["destination_list_type"],
                parsed["destination_slot"],
                note,
                result.get("item_id", 0),
                extra,
            )
        )
        if native_loadout_note:
            response_note += "; " + native_loadout_note
        if title_transition_note:
            response_note += "; " + title_transition_note
        return packets, response_note
