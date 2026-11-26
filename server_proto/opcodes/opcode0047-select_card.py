"""C1/0x0047 SELECT_CARD — 通关翻牌 ACK + 冻结奖励领取。

Stage3 IDA 权威（2026-07-18 复测修正）：

C2S（``sub_2A5F830`` free / ``sub_2A5F8C0`` paid）::
  write u8 type          # 0=free, 1/2=paid
  write u8 player_id     # 本机 id（vtable+3028）；**不是** 牌面 index
  牌面 index 只写客户端 this+1780，不进 wire。

S2C CMD opcode=71 读者 ``sub_15F6D20``（``sub_15FDAA0`` 注册）::
  8× (u8 fieldA, u8 fieldB, u8 count, count×(u32,u32), u8 flip)
  - fieldA == 本机 id → free 动画 sub_2A5F9A0
  - fieldB == 本机 id → paid 动画 sub_2A5FAC0（会扫 **全部** paid widget）
  - 禁止把 fieldB 写成 0..7 座位号铺满：solo id=0 时 seat0 必误触发 paid 全开

业务：002E 已从当前 PVF 冻结免费/付费奖励。0047 只领取该计划，不重掷；
免费侧的牌面展示靠 0023 card group，付费侧成功后在同一 0047 记录附物品对。
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any

from game.chain_loader import load_chain
from game.character.gm_item_grant import grant_inventory_item_to_section
from game.character.character_db import (
    is_virtual_inventory_grant,
    parse_virtual_premium_grant,
)
from game.character.revive_coin import is_revive_coin_grant_item_id
from game.character.inventory_item_view import InventoryItemView
from game.character.payloads import (
    build_account_cargo_item_list,
    build_item_list_mode0,
)
from game.dungeon.clear_reward import (
    ORDINARY_CARD_LAYOUT,
    ORDINARY_PAID_CARD_REWARD_MULTIPLIER,
)
from game.quest.adventure_season1 import record_golden_card

_payloads = load_chain("helper-dungeon_payloads.py")
build_select_card_ack_0047_stage3 = _payloads.build_select_card_ack_0047_stage3
build_card_select_right_state_0045 = _payloads.build_card_select_right_state_0045
build_card_layout_ack_0046 = _payloads.build_card_layout_ack_0046
build_item_list_update_000e = _payloads.build_item_list_update_000e


_MAX_U32 = 0xFFFFFFFF


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "off", "no", ""}


def _cid(session: Any) -> int:
    if session is None:
        return 0
    for attr in ("selected_character_id", "character_id", "cid"):
        val = getattr(session, attr, None)
        if val is not None:
            try:
                cid = int(val)
                if cid > 0:
                    return cid
            except Exception:
                pass
    return 0


def _parse_select_card_request(body: bytes) -> dict[str, int]:
    """Parse C1/0047 request.

    Stage3 C2S（``sub_2A5F830`` / ``sub_2A5F8C0``）::
      u8 type       # 0 free / 1 or 2 paid
      u8 player_id  # 本机 id，live solo = 0
    2026-08-02 Stage3 live plain-wire 实测为 ``00 00`` / ``01 00``，
    即 ``plain_len=2``。**无 card index**。
    """
    raw = bytes(body or b"")
    if not raw:
        return {
            "card_type": 0,
            "player_id": 0,
            "plain_len": 0,
            "raw0": 0,
            "raw1": 0,
        }
    raw0 = int(raw[0]) & 0xFF
    raw1 = int(raw[1]) & 0xFF if len(raw) >= 2 else 0
    # type: 0=free; 非 0 视为 paid（1 普通付费 / 2 特殊）
    card_type = 0 if raw0 == 0 else 1
    player_id = raw1
    return {
        "card_type": card_type,
        "player_id": player_id,
        "plain_len": len(raw),
        "raw0": raw0,
        "raw1": raw1,
    }


def _ensure_card_reward_plan(session: Any) -> dict[str, Any]:
    """Return a normalized mutable reward plan without inventing a fallback roll."""
    plan = getattr(session, "dungeon_card_reward_plan", None) if session else None
    if isinstance(plan, dict):
        normalized = plan
        legacy_free_done = bool(normalized.get("free_delivered"))
    else:
        normalized = {
            "schema": 2,
            "card_layout": "",
            "paid_reward_multiplier": 1,
            "free_gold": 0,
            "free_item_id": 0,
            "free_item_count": 0,
            "paid_gold": 0,
            "paid_item_id": 0,
            "paid_item_count": 0,
            "paid_cost": 0,
            "free_gold_delivered": False,
            "free_item_delivered": False,
            "free_delivered": False,
            "paid_cost_charged": False,
            "paid_item_delivered": False,
            "paid_item_delivery_count": 0,
            "paid_item_delivery_list_type": -1,
            "paid_item_delivery_section": "",
            "paid_delivered": False,
        }
        legacy_free_done = False
    defaults = {
        "schema": 2,
        "card_layout": "",
        "paid_reward_multiplier": 1,
        "free_gold": 0,
        "free_item_id": 0,
        "free_item_count": 0,
        "paid_gold": 0,
        "paid_item_id": 0,
        "paid_item_count": 0,
        "paid_cost": 0,
        "free_gold_delivered": False,
        "free_item_delivered": False,
        "free_delivered": False,
        "paid_cost_charged": False,
        "paid_item_delivered": False,
        "paid_item_delivery_count": 0,
        "paid_item_delivery_list_type": -1,
        "paid_item_delivery_section": "",
        "paid_delivered": False,
    }
    for key, value in defaults.items():
        if key in {"free_gold_delivered", "free_item_delivered"}:
            normalized.setdefault(key, legacy_free_done)
        else:
            normalized.setdefault(key, value)
    if session is not None:
        try:
            session.dungeon_card_reward_plan = normalized
        except Exception:
            pass
    return normalized


def _paid_card_reward_multiplier(plan: dict[str, Any]) -> int:
    """Return the frozen multiplier only for the ordinary 4x4 card layout."""
    if str(plan.get("card_layout") or "") != ORDINARY_CARD_LAYOUT:
        return 1
    try:
        multiplier = int(plan.get("paid_reward_multiplier") or 1)
    except (TypeError, ValueError):
        return 1
    if multiplier == ORDINARY_PAID_CARD_REWARD_MULTIPLIER:
        return multiplier
    return 1


def _paid_card_delivery_item_count(plan: dict[str, Any]) -> tuple[int, int]:
    """Scale only a frozen ordinary paid-card reward, never its PVF price."""
    base_count = max(0, int(plan.get("paid_item_count") or 0))
    multiplier = _paid_card_reward_multiplier(plan)
    if base_count <= 0 or base_count > (_MAX_U32 // multiplier):
        return 0, multiplier
    return base_count * multiplier, multiplier


def _remember_free_flip(session: Any, player_id: int) -> None:
    if session is None:
        return
    try:
        session.dungeon_card_flipped = True
        session.dungeon_card_free_player_id = int(player_id) & 0xFF
        session.dungeon_card_auto_flip_armed = False
        session.dungeon_card_auto_flip_deadline_monotonic = 0.0
        session.dungeon_card_auto_flip_run_generation = 0
    except Exception:
        pass


def _remember_paid_flip(session: Any, player_id: int) -> None:
    if session is None:
        return
    try:
        session.dungeon_card_paid_player_id = int(player_id) & 0xFF
    except Exception:
        pass


def _main_inventory_update_entry(
    repo: Any,
    character_id: int,
    slot: int,
    item_id: int,
    requested_count: int,
) -> tuple[int, int, int, int]:
    """Build a list0 row from the persisted item, including equip instance."""
    row = None
    try:
        row = repo.get_inventory_item_at(
            int(character_id),
            list_type=0,
            slot_index=int(slot),
        )
    except Exception:
        row = None
    is_equipment = str((row or {}).get("item_kind") or "") == "equipment"
    value_key = "instance_value" if is_equipment else "stack_count"
    value = int((row or {}).get(value_key, requested_count) or requested_count)
    projected_item_id = int(
        (row or {}).get("item_template_id", item_id) or item_id
    )
    durability = int((row or {}).get("durability") or 0)
    return (
        int(slot) & 0xFFFF,
        projected_item_id & 0xFFFFFFFF,
        max(0, value) & 0xFFFFFFFF,
        max(0, durability) & 0xFFFF,
    )


def _deliver_free_reward(
    repo: Any,
    *,
    character_id: int,
    plan: dict[str, Any],
) -> tuple[list[tuple[int, ...]], list[str], bool]:
    """Apply every still-pending free component once; retain per-part flags."""
    entries: list[tuple[int, ...]] = []
    notes: list[str] = []
    free_gold = max(0, int(plan.get("free_gold") or 0))
    free_item_id = max(0, int(plan.get("free_item_id") or 0))
    free_item_count = max(0, int(plan.get("free_item_count") or 0))

    if free_gold <= 0:
        plan["free_gold_delivered"] = True
    elif not bool(plan.get("free_gold_delivered")):
        ok_g, note_g, wallet_total = repo.grant_wallet_gold(character_id, free_gold)
        notes.append(
            "gold_ok=%s amt=%s wallet=%s %s"
            % (int(ok_g), free_gold, wallet_total, note_g)
        )
        if ok_g:
            plan["free_gold_delivered"] = True
            entries.append(
                (0, 0, _payloads.clamp_wallet_gold_for_client(wallet_total))
            )

    if free_item_id <= 0 or free_item_count <= 0:
        plan["free_item_delivered"] = True
    elif not bool(plan.get("free_item_delivered")):
        ok_i, note_i, slot = repo.grant_inventory_item(
            character_id,
            free_item_id,
            free_item_count,
        )
        notes.append(
            "item_ok=%s id=%s cnt=%s slot=%s %s"
            % (int(ok_i), free_item_id, free_item_count, slot, note_i)
        )
        if ok_i and (int(slot) >= 0 or is_virtual_inventory_grant(note_i)):
            plan["free_item_delivered"] = True
            plan["free_virtual_grant"] = bool(is_virtual_inventory_grant(note_i))
            premium_notice = parse_virtual_premium_grant(note_i)
            if premium_notice is not None:
                plan["free_premium_notice"] = premium_notice
            if is_revive_coin_grant_item_id(free_item_id):
                try:
                    wallet_ok, _wallet_note, wallet = repo.get_account_wallet(
                        int(character_id)
                    )
                    if wallet_ok:
                        entries.append(
                            (
                                1,
                                1,
                                max(0, int(wallet.get("revive_coin", 0) or 0)),
                                0,
                            )
                        )
                except Exception:
                    pass
            if int(slot) >= 0 and not is_revive_coin_grant_item_id(free_item_id):
                entries.append(
                    _main_inventory_update_entry(
                        repo,
                        character_id,
                        int(slot),
                        free_item_id,
                        free_item_count,
                    )
                )

    delivered = bool(
        plan.get("free_gold_delivered") and plan.get("free_item_delivered")
    )
    plan["free_delivered"] = delivered
    return entries, notes, delivered


def _deliver_paid_reward(
    repo: Any,
    *,
    character_id: int,
    plan: dict[str, Any],
) -> tuple[list[tuple[int, ...]], list[str], bool]:
    """Charge once, then grant the frozen ordinary paid-card multiplier."""
    entries: list[tuple[int, ...]] = []
    notes: list[str] = []
    item_id = max(0, int(plan.get("paid_item_id") or 0))
    item_count, multiplier = _paid_card_delivery_item_count(plan)
    cost = max(0, int(plan.get("paid_cost") or 0))
    if item_id <= 0 or item_count <= 0:
        notes.append("skip=no_paid_item_or_count_overflow")
        return entries, notes, False

    # The account wallet is single-owner in this local simulator.  Check
    # before mutation so an insufficient-funds click never gets an animation.
    try:
        _exp, _gold, wallet_before, _junk, _grave = repo.get_character_economy(
            character_id
        )
        if int(wallet_before) < cost:
            notes.append("reject=insufficient have=%s need=%s" % (wallet_before, cost))
            return entries, notes, False
    except Exception as exc:
        notes.append("reject=wallet_read_%s" % type(exc).__name__)
        return entries, notes, False

    ok_g, note_g, wallet_total = repo.spend_wallet_gold(character_id, cost)
    notes.append(
        "gold_ok=%s cost=%s wallet=%s %s"
        % (int(ok_g), cost, wallet_total, note_g)
    )
    if not ok_g:
        return entries, notes, False
    plan["paid_cost_charged"] = True

    delivery_section = ""
    delivery_detail: dict[str, Any] = {}
    ok_i = False
    note_i = "grant_not_attempted"
    # The lower card grants a PVF equipment result.  Preserve the full x2
    # result as one unit: a container is eligible only when it can hold every
    # instance.  The shared section grant preflights capacity before mutation,
    # so a full earlier container cannot leave a partial reward behind.
    for section in ("equipment", "personal_cargo", "account_cargo"):
        try:
            ok_i, note_i, detail_i = grant_inventory_item_to_section(
                repo,
                character_id=character_id,
                item_id=item_id,
                count=item_count,
                section=section,
            )
        except Exception as exc:
            ok_i, note_i, detail_i = (
                False,
                "grant_error_%s" % type(exc).__name__,
                {},
            )
        notes.append(
            "item_section=%s ok=%s id=%s cnt=%s multiplier=%s %s"
            % (
                section,
                int(ok_i),
                item_id,
                item_count,
                multiplier,
                note_i,
            )
        )
        if ok_i:
            delivery_section = section
            delivery_detail = dict(detail_i or {})
            break
        if str(note_i) != "container_full":
            break

    if not ok_i:
        refunded, refund_note, refund_total = repo.grant_wallet_gold(character_id, cost)
        plan["paid_cost_charged"] = False
        notes.append(
            "refund_ok=%s wallet=%s %s"
            % (int(refunded), refund_total, refund_note)
        )
        return entries, notes, False

    plan["paid_item_delivered"] = True
    plan["paid_delivered"] = True
    plan["paid_virtual_grant"] = bool(is_virtual_inventory_grant(note_i))
    premium_notice = parse_virtual_premium_grant(note_i)
    if premium_notice is not None:
        plan["paid_premium_notice"] = premium_notice
    plan["paid_item_delivery_count"] = item_count
    plan["paid_item_delivery_section"] = delivery_section
    try:
        delivery_list_type = int(delivery_detail.get("list_type", -1))
    except (TypeError, ValueError):
        delivery_list_type = -1
    plan["paid_item_delivery_list_type"] = delivery_list_type
    if cost > 0:
        entries.append((0, 0, _payloads.clamp_wallet_gold_for_client(wallet_total)))
    if int(plan["paid_item_delivery_list_type"]) == 0:
        changed_slots = sorted(
            {
                int(slot)
                for slot in list(delivery_detail.get("changed_slots") or ())
                if int(slot) >= 0
            }
        )
        if not changed_slots:
            changed_slots = [int(delivery_detail.get("first_slot", -1))]
        for slot in changed_slots:
            if slot < 0:
                continue
            entries.append(
                _main_inventory_update_entry(
                    repo,
                    character_id,
                    int(slot),
                    item_id,
                    item_count,
                )
            )
    return entries, notes, True


def _build_paid_card_container_refresh(
    repo: Any,
    *,
    character_id: int,
    list_type: int,
) -> tuple[bytes, str]:
    """Build the full list2/list12 refresh after a paid-card fallback grant."""
    target_list_type = int(list_type)
    if target_list_type == 2:
        rows = repo.list_inventory_items(int(character_id), list_type=2)
        body = build_item_list_mode0(
            [InventoryItemView(row) for row in rows],
            list_type=2,
            list_param16=int(
                repo.get_container_list_param(int(character_id), list_type=2)
            ),
        )
        return body, "000D/list2 n=%s" % len(rows)
    if target_list_type == 12:
        account_id = repo.get_account_id_for_character(int(character_id))
        if account_id is None or int(account_id) <= 0:
            raise RuntimeError("account_missing")
        state = repo.get_account_cargo_state(int(account_id), ensure_open=True)
        rows = repo.list_account_cargo_items(int(account_id))
        body = build_account_cargo_item_list(
            [InventoryItemView(row) for row in rows],
            selection_key=int(state.get("selection_key", 0) or 0),
            cargo_gold=int(state.get("value32", 0) or 0),
        )
        return body, "000D/list12 n=%s" % len(rows)
    raise ValueError("unsupported paid-card container list_type=%s" % target_list_type)


class SelectCard0047Service:
    def __init__(self, server: Any) -> None:
        self.server = server
        self.presentation: bytes | None = None

    def _emit(self, flag: int, opcode: int, body: bytes, mode: str = "codec") -> tuple[bytes, str]:
        mode_norm = (mode or "codec").strip().lower()
        if mode_norm in {"raw", "plain"}:
            return self.server._raw_response(flag, opcode, body), "raw"
        return self.server._codec_response(flag, opcode, body), "codec"

    def handle(self, body: bytes, *, session: Any = None, owner_seat: int = 0) -> tuple[list[bytes], str]:
        self.presentation = None
        if _env_bool("USDOF_GAME_DUNGEON_SKIP_CARD_FLIP", False):
            # 仅显式 SKIP 时跳过翻牌 UI；奖励已在 002E 结算发放，不回 0047 动画包
            return [], "C1/0047 SELECT_CARD skip=SKIP_CARD_FLIP"
        if not _env_bool("USDOF_GAME_DUNGEON_SELECT_CARD_HANDLE", True):
            return [], "C1/0047 SELECT_CARD gate=OFF"
        in_run = bool(getattr(session, "in_dungeon_run", False)) if session else False
        phase = int(getattr(session, "dungeon_clear_phase", 0) or 0) if session else 0
        if not in_run or phase < 2:
            return [], (
                "C1/0047 SELECT_CARD reject=not_settled in_run=%s phase=%s"
                % (int(in_run), phase)
            )

        req = _parse_select_card_request(body)
        card_type = int(req["card_type"])
        player_id = int(req["player_id"]) & 0xFF
        cid = _cid(session)
        plan = _ensure_card_reward_plan(session)

        packets: list[bytes] = []
        notes: list[str] = []
        plain_hex = bytes(body or b"")[:8].hex(" ")

        if not bool(getattr(session, "dungeon_card_layout_sent", False)):
            body_45 = build_card_select_right_state_0045(1)
            pkt45, mode45 = self._emit(1, 0x0045, body_45, "codec")
            packets.append(pkt45)
            body_46 = build_card_layout_ack_0046()
            pkt46, mode46 = self._emit(1, 0x0046, body_46, "codec")
            packets.append(pkt46)
            session.dungeon_card_layout_sent = True
            notes.append("card_layout_late 0045/%s 0046/%s" % (mode45, mode46))

        # 002E freezes the plan.  Each owner marker is emitted only after the
        # corresponding reward has committed, so a full bag or insufficient
        # wallet cannot turn either card row into a false success state.
        deliver_on = _env_bool("USDOF_GAME_DUNGEON_SELECT_CARD_DELIVER_REWARD", True)
        update_entries: list[tuple[int, ...]] = []
        grant_notes: list[str] = []
        free_callback = False
        paid_callback = False
        paid_reward_pairs: list[tuple[int, int]] = []
        paid_repo: Any = None
        paid_container_refresh: tuple[bytes, str] | None = None

        if card_type == 0:
            if not deliver_on:
                notes.append("deliver_free skip=gate")
            elif bool(plan.get("free_delivered")):
                notes.append("deliver_free skip=already_delivered")
                # The legacy pre-grant path can enter 0047 with the free
                # reward already persisted but no client-side card animation.
                free_callback = not bool(
                    getattr(session, "dungeon_card_flipped", False)
                )
            elif cid <= 0:
                notes.append("deliver_free reject=no_character")
            else:
                try:
                    repo = self.server._repo()
                    update_entries, grant_notes, free_callback = _deliver_free_reward(
                        repo,
                        character_id=cid,
                        plan=plan,
                    )
                    notes.append("deliver_free [%s]" % "; ".join(grant_notes))
                except Exception as exc:
                    free_callback = False
                    notes.append("deliver_free err=%r" % (exc,))
            if free_callback:
                _remember_free_flip(session, player_id)
        elif not deliver_on:
            notes.append("deliver_paid skip=gate")
        elif bool(plan.get("paid_delivered")):
            notes.append("deliver_paid skip=already_delivered")
        elif cid <= 0:
            notes.append("deliver_paid reject=no_character")
        else:
            try:
                paid_repo = self.server._repo()
                update_entries, grant_notes, paid_callback = _deliver_paid_reward(
                    paid_repo,
                    character_id=cid,
                    plan=plan,
                )
                notes.append("deliver_paid [%s]" % "; ".join(grant_notes))
                if paid_callback:
                    paid_item_delivery_count, _paid_multiplier = (
                        _paid_card_delivery_item_count(plan)
                    )
                    paid_reward_pairs = []
                    paid_gold = max(0, int(plan.get("paid_gold") or 0))
                    if paid_gold > 0 or not bool(plan.get("paid_virtual_grant")):
                        paid_reward_pairs.append((0, paid_gold))
                    # Account-owned rewards are already consumed by the
                    # repository.  Sending their source template in the
                    # 0047 reward pairs makes the client treat it as a normal
                    # item result, which recreates the forbidden bag entry.
                    if not bool(plan.get("paid_virtual_grant")):
                        paid_reward_pairs.append(
                            (
                                max(0, int(plan.get("paid_item_id") or 0)),
                                max(
                                    0,
                                    int(
                                        plan.get("paid_item_delivery_count")
                                        or paid_item_delivery_count
                                    ),
                                ),
                            )
                        )
                    try:
                        delivery_list_type = int(
                            plan.get("paid_item_delivery_list_type", -1)
                        )
                    except (TypeError, ValueError):
                        delivery_list_type = -1
                    if delivery_list_type in {2, 12}:
                        try:
                            paid_container_refresh = _build_paid_card_container_refresh(
                                paid_repo,
                                character_id=cid,
                                list_type=delivery_list_type,
                            )
                        except Exception as refresh_exc:
                            notes.append(
                                "paid_container_refresh_fail=%s"
                                % type(refresh_exc).__name__
                            )
                    _remember_paid_flip(session, player_id)
            except Exception as exc:
                paid_callback = False
                notes.append("deliver_paid err=%r" % (exc,))

        if paid_callback and plan.get("card_layout") == ORDINARY_CARD_LAYOUT:
            try:
                if record_golden_card(self.server, session):
                    notes.append("adventure_season1_gold_card=1")
            except (OSError, ValueError, sqlite3.Error) as exc:
                notes.append("adventure_season1_gold_card_error=%s" % type(exc).__name__)

        # CMD 成功前缀后才是 record 0。上/下四的具体牌位由客户端本地点击
        # 状态决定；最后 8 个 player_status 必须保持清零。
        ack_player_id = player_id
        if (card_type == 0 and not free_callback) or (
            card_type == 1 and not paid_callback
        ):
            # A complete no-owner ACK keeps the reader aligned while leaving
            # the selected row uncommitted on delivery failure.
            ack_player_id = 0xFF
        ack = build_select_card_ack_0047_stage3(
            player_id=ack_player_id,
            card_type=card_type,
            free_owner_seat=owner_seat,
            paid_owner_seat=owner_seat,
            attach_rewards=bool(paid_callback and paid_reward_pairs),
            rewards=paid_reward_pairs if paid_callback else None,
            reward_seat=owner_seat,
        )
        if free_callback or paid_callback:
            self.presentation = ack
        pkt, mode = self._emit(1, 0x0047, ack, "codec")
        packets.append(pkt)
        notes.append(
            "0047/%s/len=%d command_ok=1 reader_body=%d stage3_owner pid=%s "
            "type=%s player_record=%s player_status=all_zero plain=%s raw=%02x%02x "
            "free_callback=%s paid_callback=%s reward_pairs=%s"
            % (
                mode,
                len(ack),
                len(ack) - 1,
                ack_player_id,
                card_type,
                owner_seat,
                plain_hex,
                req["raw0"],
                req["raw1"],
                int(free_callback),
                int(paid_callback),
                len(paid_reward_pairs),
            )
        )

        notice = plan.get(
            "free_premium_notice" if card_type == 0 else "paid_premium_notice"
        )
        if notice and ((card_type == 0 and free_callback) or (card_type == 1 and paid_callback)):
            helper = load_chain("helper-system_reward_activation.py")
            if session is not None:
                try:
                    session.account_premium_effects = None
                except Exception:
                    pass
            packet_42, mode_42 = self._emit(
                0,
                0x0042,
                helper.build_premium_activation_notice(*notice),
            )
            packets.append(packet_42)
            notes.append(
                "0042_premium/%s type=%d remaining=%d"
                % (mode_42, int(notice[0]), int(notice[1]))
            )

        if update_entries and _env_bool("USDOF_GAME_DUNGEON_SELECT_CARD_SEND_000E", True):
            body_000e = build_item_list_update_000e(update_entries, list_type=0)
            pkt_e, mode_e = self._emit(0, 0x000E, body_000e, "codec")
            packets.append(pkt_e)
            notes.append(
                "000E/%s/len=%d entries=%s stage3_108B"
                % (mode_e, len(body_000e), len(update_entries))
            )

        if paid_container_refresh is not None:
            body_000d, refresh_note = paid_container_refresh
            pkt_d, mode_d = self._emit(0, 0x000D, body_000d, "codec")
            packets.append(pkt_d)
            notes.append(
                "%s/%s/len=%d" % (refresh_note, mode_d, len(body_000d))
            )

        return packets, (
            "C1/0047 SELECT_CARD reply=[%s]; ida=sub_15F6D20 owner_not_seat; "
            "chain_file=game/chains/opcode0047-select_card.py"
            % ", ".join(notes)
        )
