"""Launcher assistance settled on the selected Game connection thread."""
from __future__ import annotations

import re
import struct
import time

from control.contracts import ControlApiError
from game.chain_loader import load_chain
from game.character.character_db import EQUIP_TOP_QUALITY_SEED
from game.character.disjoint_repository import DisjointReward, DisjointTarget, apply_disjoint_batch
from game.character.inventory_item_view import InventoryItemView
from game.character.item_metadata import resolve_item_metadata
from game.character.payloads import build_item_list_mode0
from game.normal_party import current as current_party
from game.quest.quest_common import env_bool


RARITIES = frozenset(range(7))
PICKUP_LEASE = 12.0
PROTECTED_FIELDS = (
    "equipment_lock_id", "sort_lock_flag", "option_value",
    "expire_time", "pet_serial_or_handle",
)


def _automatic_pickup_keys(session):
    """Only pick drops whose creation the server has already observed.

    001D building rewards are registered before the client destroys their
    building. Consuming those reservations here leaves later client-created
    ground objects with no corresponding server drop to pick up.
    """
    return [
        key for key, drop in (getattr(session, "dungeon_drops", None) or {}).items()
        if str(getattr(drop, "source", "") or "") != "passive_object_item"
    ]


def _reject(code, message, status=409):
    raise ControlApiError(code, message, status=status)


def _town_target(session):
    if (not session.selected_character_id or not session.enter_seria_settled
            or session.enter_seria_in_progress or session.in_dungeon_run
            or session.in_dungeon_select or session.channel_move_settled):
        return ""
    return f"{session.selected_character_id}:{session.chat_selection_generation}"


def _candidates(rows, rarities, action):
    for row in rows:
        slot = int(row["slot_index"])
        if (slot <= 1 or row["stack_count"] != 1 or row["item_kind"] != "equipment"
                or any(row.get(field) for field in PROTECTED_FIELDS)
                or int(row.get("instance_value") or 0) not in (0, EQUIP_TOP_QUALITY_SEED)
                or row.get("extra_json") not in (None, "", "{}")):
            continue
        meta = resolve_item_metadata(row["item_template_id"])
        if (not meta.is_equipment or meta.is_avatar or meta.is_pet_inventory_item or meta.is_guild_medal
                or meta.rarity not in rarities or meta.normalized_attach_type() == "trade delete"
                or action in {str(value).strip("[] ").lower() for value in meta.impossible_contents or ()}):
            continue
        if action == "sell" and meta.sell_gold <= 0:
            continue
        yield slot, meta


def _settle(game, session, action, rarities):
    repo = game._repo()
    cid = int(session.selected_character_id)
    gold = 0
    rewards = []
    with repo.inventory_transaction_scope():
        rows, _ = repo.load_main_inventory_snapshot(cid)
        candidates = list(_candidates(rows, rarities, action))
        if not candidates:
            return [], {"processed": 0}, "assist no eligible equipment"
        if action == "sell":
            for slot, meta in candidates:
                # The snapshot and deletion share the same immediate transaction.
                ok, _, detail = repo.consume_inventory_item(cid, list_type=0, slot_index=slot, count=1)
                if not ok or detail["item_template_id"] != meta.item_id:
                    _reject("ITEM_CHANGED", "背包装备已变化，本次未出售")
                gold += int(meta.sell_gold)
            ok, _, _ = repo.grant_wallet_gold(cid, gold)
            if not ok:
                _reject("WALLET_LIMIT", "金币无法入账，本次未出售")
        else:
            disjoint = load_chain("opcode001a-disjoint_item.py")
            targets = []
            for slot, meta in candidates:
                materials, _ = disjoint._select_disjoint_materials(
                    sell_gold=int(meta.sell_gold), rarity=int(meta.rarity), minimum_level=int(meta.minimum_level or 0)
                )
                if not materials:
                    continue
                targets.append(DisjointTarget(slot, int(meta.item_id), 1))
                for material in materials:
                    item_id = int(material.item_template_id)
                    rewards.append(DisjointReward(item_id, int(material.count), resolve_item_metadata(item_id)))
            if not targets:
                return [], {"processed": 0}, "assist no equipment with disjoint rewards"
            ok, reason, _ = apply_disjoint_batch(
                repo, character_id=cid, targets=targets, tool_uses=(), rewards=rewards,
            )
            if not ok:
                if reason.startswith("inventory_full"):
                    _reject("INVENTORY_FULL", "分解材料放不下，本次未分解")
                _reject("DISJOINT_FAILED", "无法结算分解材料，本次未分解")
            candidates = targets
        final_rows, param = repo.load_main_inventory_snapshot(cid)
        snapshot = build_item_list_mode0(
            [InventoryItemView(row) for row in final_rows], list_type=0, list_param16=param,
        )
        packets = [game._codec_response(0, 0x000D, snapshot)]

    if action == "disjoint":
        progress, _ = game.disjoint_item_001a._daily_challenge_progress(
            repo, character_id=cid, event_count=len(candidates),
        )
        packets.extend(progress)
    return packets, {"processed": len(candidates), "gold": gold}, f"assist {action} count={len(candidates)} gold={gold}"


def install(game, handlers, live_contexts):
    original_drain = game._drain_control_commands
    from game.instant_door import InstantDoor,ACTIONS as DOOR_ACTIONS
    door = InstantDoor(game)

    def handle(params, session):
        action = params["action"]
        if action in DOOR_ACTIONS:
            return door.handle(params,session)
        target = _town_target(session)
        if action == "configure":
            session.assist_pickup_until = time.monotonic() + PICKUP_LEASE if params["pickup"] else 0
            return [], {"target": f'{params["connection_generation"]}/{target}' if target else "", "door_target":door.snapshot(session)}, "assist settings"
        if not target or target != params["target"].split("/", 1)[1]:
            _reject("TARGET_CHANGED", "请在当前角色的城镇中使用")
        if session.pending_disjoint_item_bodies:
            _reject("DISJOINT_ACTIVE", "请等待当前分解完成")
        if action == "disjoint" and not env_bool("USDOF_GAME_HANDLE_DISJOINT_ITEM_001A", True):
            _reject("DISJOINT_DISABLED", "分解暂不可用")
        now = time.monotonic()
        if now < getattr(session, "assist_batch_next", 0):
            _reject("ASSIST_COOLDOWN", "装备处理过于频繁，请稍后重试")
        session.assist_batch_next = now + 0.5
        from game.player_exchange import service_for
        exchange = service_for(game)
        with exchange.lock:
            if exchange.busy(exchange.owned(session)):
                _reject("TRADE_ACTIVE", "交易期间无法批量处理装备")
            return _settle(game, session, action, params["rarities"])

    def request(body):
        aid, sid = body["account_id"], body["session_id"]
        action, rid = body["action"], body["request_id"]
        if (type(aid) is not int or aid < 1 or not isinstance(sid, str) or not sid
                or not isinstance(rid, str) or re.fullmatch(r"[a-f0-9]{32}", rid) is None):
            _reject("INVALID_REQUEST", "无效的游戏辅助请求", 400)
        params = {"action": action}
        if action == "configure" and type(body.get("pickup")) is bool and set(body) == {
                "account_id", "session_id", "action", "request_id", "pickup"}:
            params["pickup"] = body["pickup"]
        elif action in DOOR_ACTIONS and set(body)=={"account_id","session_id","action","request_id","target"}:
            target=body["target"]
            if not isinstance(target,str) or re.fullmatch(r"[a-f0-9]{32}",target) is None:
                _reject("INVALID_REQUEST","无效的过门房间凭据",400)
            params["target"]=target
        elif action in ("sell", "disjoint") and set(body) == {
                "account_id", "session_id", "action", "request_id", "target", "rarities"}:
            rarities, target = body["rarities"], body["target"]
            if (not isinstance(target, str) or re.fullmatch(r"[0-9]+/[1-9][0-9]*:[0-9]+", target) is None
                    or not isinstance(rarities, list) or not 1 <= len(rarities) <= len(RARITIES)
                    or any(type(value) is not int or value not in RARITIES for value in rarities)
                    or len(set(rarities)) != len(rarities)):
                _reject("INVALID_REQUEST", "请选择有效的装备品阶", 400)
            params.update(target=target, rarities=sorted(rarities))
        else:
            _reject("INVALID_REQUEST", "无效的游戏辅助设置", 400)
        contexts = [entry["context"] for entry in live_contexts() if entry["account_id"] == aid
                    and entry["context"].session.login_session_id == sid]
        if len(contexts) != 1:
            _reject("SESSION_NOT_READY", "请先进入游戏角色")
        context = contexts[0]
        if action == "configure":
            params["connection_generation"] = context.generation
        elif action not in DOOR_ACTIONS and body["target"].split("/", 1)[0] != str(context.generation):
            _reject("TARGET_CHANGED", "游戏连接已改变，请稍后重试")
        params["_manager_expected_character"] = int(context.session.selected_character_id or 0)
        params["_manager_expected_epoch"] = context.generation
        return game.submit_control_command(
            request_id=f"assist:{sid}:{rid}", command="player.assist", params=params,
            timeout=2 if action == "configure" else 8,
        )

    def drain(context):
        original_drain(context)
        session = context.session
        now = time.monotonic()
        if (getattr(session, "assist_pickup_until", 0) <= now
                or not session.selected_character_id or not session.in_dungeon_run
                or not getattr(session, "dungeon_finish_loading", False)
                or session.dungeon_death_pending or session.selected_channel_id in (23, 32)
                or session.dungeon_tower_kind
                or not session.dungeon_drops or getattr(session, "assist_pickup_next", 0) > now
                or current_party(game, session) is not None):
            return
        session.assist_pickup_next = now + 0.25
        keys = _automatic_pickup_keys(session)
        if not keys:
            return
        start = getattr(session, "assist_pickup_cursor", 0) % len(keys)
        session.assist_pickup_cursor = start + 8
        for key in (keys[start:] + keys[:start])[:8]:
            drop = session.dungeon_drops.get(key)
            identity = "key=%s item=%s count=%s source=%s" % (
                key, getattr(drop, "template_id", None),
                getattr(drop, "stack_count", None), getattr(drop, "source", None),
            )
            try:
                packets, note = game._handle_get_item_002b(struct.pack("<I16x", key), session=session)
                if packets:
                    game._send_control_packets(context, packets, command="player.pickup", note=note)
                elif getattr(session, "assist_pickup_diagnostic_next", 0) <= now:
                    session.assist_pickup_diagnostic_next = now + 5
                    game.log("[Assist] pickup deferred " + identity + "; " + note)
            except OSError as exc:
                game.log("[Assist] pickup transport failure " + identity + "; " + repr(exc))
                raise
            except Exception as exc:
                game.log("[Assist] pickup failed " + identity + "; " + repr(exc))
                break

    handlers["player.assist"] = handle
    game._player_assist = request
    game._drain_control_commands = drain
