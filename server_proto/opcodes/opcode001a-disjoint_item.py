# -*- coding: utf-8 -*-
"""C1/0x001A ENUM_CMDPACKET_DISJOINT_ITEM — 系统/便携分解装备。

注意双表：
  - C->S flag1/0x001A = DISJOINT_ITEM（本文件）
  - S->C flag0/0x001A = 副本 UDP host（``helper-dungeon_payloads``，勿混用）

Stage3 IDA（2026-07-19 权威）：
  - C2S 组包 ``c2s_DISJOINT_ITEM_001A_send`` @ 0x1B6B9B0::

        BeginOpcode(0x1A)
        WriteU16(target_slot)
        WriteU8(list_type)      # 仅 Main=0
        WriteU16(tool_slot)     # -1 = NPC 系统分解
        WriteU32(context)
        Finalize  # codec_index=12 Noekeon

  - S2C ACK ``s2c_DISJOINT_ITEM_001A_ack`` @ 0x1624920::

        success: u8 01 | i16 target_slot | u8 list | u8 n
                 | (i16 slot | i32 item_id | i32 count)*n
        fail:    u8 00 | u8 error_code
                 0x13 非法, 0x04 背包满

  客户端成功路径（0x1624920 原版）：
    读 n 条材料 → 槽已有则 **SET 绝对 count**（非 delta）→
    无则 create + popup sub_2895410 → 末条 enter_game_state →
    sub_1B68670 扣装备（依赖 C2S 已 push pending=11）。

Stage3 live（2026-07-19）确认原版 create/popup 材料分支会闪退；已有材料槽的
SET/popup 分支与 ``n=0`` 完成分支均由同一 reader 原生处理。

现行原生服务端流程（2026-08-09）：
  1. 单件分解沿用原生服务端三包流程；一键分解的连续请求按短 TCP 静默窗口收集；
  2. 每件装备独立消费预热 PVF 规则计算产物，再按材料 ID 汇总；
  3. 在一个事务内批量扣装、扣便携分解机次数并持久化汇总材料，任一步失败整批回滚；
  4. 只构建一次最终背包 body，先发 ``S0/000D`` 使材料槽成为已有项；
  5. 每个请求仍返回一个 ``S1/001A`` 完成 ACK，仅末个 ACK 携带汇总增量并触发一次 popup；
  6. 最后重发同一最终背包的 ``S0/000D``，恢复数据库绝对数量；
  7. 成功后按实际分解件数推进 ``[disjoint item]``，每条匹配挑战最多发送一条进度更新；
  8. 不注入分解 DLL，不写本地旁路文件，不接管客户端 reader 或动画。

Game C2S 明文由全局 ``usdof_codec_passthrough`` 提供，S2C 继续走原生 codec；
无合法槽 = S4 fail-only。

产物：PVF ``etc/disjoint.etc``（仅共享预加载）。
  - 小晶体 → 账号固定槽 354..359
  - 六级灵魂 → 账号固定投影 360..365
"""
from __future__ import annotations

import os
import random
import struct
import time
from typing import Any

from game.chain_loader import load_chain
from game.character.disjoint_repository import (
    DisjointReward,
    DisjointTarget,
    DisjointToolUse,
    apply_disjoint_batch,
)
from game.character.inventory_item_view import InventoryItemView
from game.character.item_metadata import (
    resolve_item_metadata,
)
from game.character.payloads import build_item_list_mode0
from game.quest.daily_challenge import handle_daily_challenge_disjoint_item
from game.quest.quest_common import env_bool
from game.quest.quest_session import resolve_selected_character_detail

_disjoint = load_chain("helper-disjoint_config.py")
calculate_disjoint_materials = _disjoint.calculate_disjoint_materials
load_system_disjoint = _disjoint.load_system_disjoint
DisjointMaterial = _disjoint.DisjointMaterial
_cube_soul = load_chain("helper-cube_soul_slots.py")
is_cube_fragment = _cube_soul.is_cube_fragment
is_account_soul = _cube_soul.is_account_soul
SLOT_RANGE_SOUL_MATERIAL = _cube_soul.SLOT_RANGE_SOUL_MATERIAL
SLOT_RANGE_CUBE_FRAGMENT = _cube_soul.SLOT_RANGE_CUBE_FRAGMENT

ERROR_INVALID = 0x13
ERROR_INVENTORY_FULL = 0x04

# 背包主栏常见槽位上界（Stage3 特殊带到 366）
_MAX_MAIN_SLOT = 400
# 单条材料数量上限（防御 PVF const 误解析 / 异常 sell_gold）
_MAX_MATERIAL_COUNT = 50


class SimpleInventoryItem:
    __slots__ = (
        "slot_index",
        "item_template_id",
        "stack_count",
        "durability",
        "instance_value",
        "item_kind",
        "seal_flag",
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
    ) -> None:
        self.slot_index = int(slot_index)
        self.item_template_id = int(item_template_id)
        self.stack_count = int(stack_count)
        self.durability = max(0, int(durability or 0)) & 0xFFFF
        self.instance_value = max(0, int(instance_value or 0)) & 0xFFFFFFFF
        self.item_kind = str(item_kind or "")
        self.seal_flag = max(0, int(seal_flag or 0)) & 0xFF


def parse_disjoint_item_body(plain: bytes) -> dict[str, Any] | None:
    """解析 C1/001A。对齐 Stage3 c2s_DISJOINT_ITEM_001A_send / S4 parser。

    合法条件（严）：
      - len >= 5
      - target_slot in 0.._MAX_MAIN_SLOT
      - list_type in {0,1,3}（业务层再限 Main=0）
      - tool_slot == -1 或 0.._MAX_MAIN_SLOT
    """
    data = bytes(plain or b"")
    if len(data) < 5:
        return None

    target_slot = int(struct.unpack_from("<h", data, 0)[0])
    list_type = int(data[2]) & 0xFF
    tool_slot = int(struct.unpack_from("<h", data, 3)[0])
    context = int(struct.unpack_from("<i", data, 5)[0]) if len(data) >= 9 else 0

    if target_slot < 0 or target_slot > _MAX_MAIN_SLOT:
        return None
    if list_type not in (0, 1, 3):
        return None
    if tool_slot < -1 or tool_slot > _MAX_MAIN_SLOT:
        return None

    return {
        "layout": "s4_slot_list_tool",
        "target_slot": target_slot,
        "list_type": list_type,
        "tool_slot": tool_slot,
        "context": context & 0xFFFFFFFF,
        "raw_len": len(data),
        "raw_hex": data[:16].hex(" "),
        "fallback": False,
    }


def _plain_hex(plain: bytes, n: int = 16) -> str:
    data = bytes(plain or b"")
    if not data:
        return ""
    return data[:n].hex(" ")


def build_disjoint_item_ack_ok(
    *,
    target_slot: int,
    list_type: int,
    materials: list[dict[str, int]],
) -> bytes:
    """S1/001A 成功体（IDA 0x1624920 / S4 DisjointItemAckBuilder.BuildSuccess）。"""
    mats = list(materials or [])
    n = min(len(mats), 255)
    out = bytearray()
    out.append(0x01)
    out += struct.pack("<h", int(target_slot))
    out.append(int(list_type) & 0xFF)
    out.append(n & 0xFF)
    for i in range(n):
        m = mats[i]
        out += struct.pack("<h", int(m.get("slot_index", -1)))
        out += struct.pack("<i", int(m.get("item_template_id", 0)) & 0xFFFFFFFF)
        out += struct.pack("<i", int(m.get("count", 0)) & 0xFFFFFFFF)
    return bytes(out)


def build_disjoint_item_ack_error(error_code: int = ERROR_INVALID) -> bytes:
    return bytes([0x00, int(error_code) & 0xFF])


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


def _normalize_token(value: str) -> str:
    text = str(value or "").strip().strip("`").strip().lower()
    if text.startswith("[") and text.endswith("]") and len(text) >= 2:
        text = text[1:-1].strip()
    return " ".join(text.split())


def _portable_max_level(portable_disjoint: int) -> int:
    # S4 GetPortableDisjointMaxLevel
    table = {0: 30, 1: 50, 2: 70, 3: 85}
    return int(table.get(int(portable_disjoint), -1))


def _extract_portable_disjoint(meta: Any) -> int:
    """从 stackable 脚本读 ``[portable disjoint]``；无则 -1。"""
    path = str(getattr(meta, "source_path", "") or "")
    if not path:
        return -1
    try:
        from pvf import pvf_runtime

        text = pvf_runtime.read_text(path)
    except Exception:
        return -1
    from game.character.item_metadata import _extract_int_field

    raw = _extract_int_field(text, "portable disjoint")
    if raw is None:
        return -1
    return int(raw)


def _is_disassemblable_equipment(row: dict[str, Any]) -> bool:
    item_id = int(row.get("item_template_id") or 0)
    kind = str(row.get("item_kind") or "")
    stack = max(0, int(row.get("stack_count") or 0))
    equipment_lock_id = max(0, int(row.get("equipment_lock_id") or 0)) & 0xFF
    if item_id <= 0 or stack <= 0 or equipment_lock_id > 0:
        return False
    meta = resolve_item_metadata(item_id)
    if not (meta.is_equipment or kind == "equipment"):
        return False
    imposs = tuple(_normalize_token(x) for x in (meta.impossible_contents or ()))
    if "disjoint" in imposs:
        return False
    if meta.normalized_attach_type() == "trade delete":
        return False
    return True


def _fallback_target_from_inventory(
    repo: Any,
    cid: int,
    *,
    prefer_slots: list[int] | None = None,
) -> dict[str, Any] | None:
    """codec12 明文乱码时：取 Main 背包「最可能被点」的可分解装备。

    优先级：
      1. prefer_slots（会话内最近购买/操作的装备槽，若仍可分解）
      2. 装备带 9..64 内 **updated_at / item_uid 最新** 的可分解装备
         （刚买的 27002 通常比老戒指更新；避免旧逻辑「最高槽」拆错装）
    """
    # 直接读 uid/updated_at，list_inventory_items 不带这两列
    candidates: list[dict[str, Any]] = []
    by_slot: dict[int, dict[str, Any]] = {}
    try:
        with repo.open() as conn:
            db_rows = conn.execute(
                "SELECT slot_index, item_template_id, stack_count, durability, "
                "instance_value, item_kind, seal_flag, equipment_lock_id, item_uid, "
                "COALESCE(updated_at, created_at, '') "
                "FROM character_items "
                "WHERE character_id = ? AND list_type = 0 AND slot_index > 0 "
                "ORDER BY slot_index",
                (int(cid),),
            ).fetchall()
        for db in db_rows:
            row = {
                "slot_index": int(db[0]),
                "item_template_id": int(db[1]),
                "stack_count": int(db[2] or 0),
                "durability": int(db[3] or 0),
                "instance_value": int(db[4] or 0),
                "item_kind": str(db[5] or ""),
                "seal_flag": int(db[6] or 0),
                "equipment_lock_id": int(db[7] or 0),
                "item_uid": int(db[8] or 0),
                "updated_at": str(db[9] or ""),
            }
            slot = int(row["slot_index"])
            if slot <= 0 or slot > _MAX_MAIN_SLOT:
                continue
            if not _is_disassemblable_equipment(row):
                continue
            candidates.append(row)
            by_slot[slot] = row
    except Exception:
        # 退化：无 uid 时用 list API
        try:
            rows = list(repo.list_inventory_items(int(cid), list_type=0) or [])
        except Exception:
            return None
        for row in rows:
            slot = int(row.get("slot_index") or -1)
            if slot <= 0 or slot > _MAX_MAIN_SLOT:
                continue
            if not _is_disassemblable_equipment(row):
                continue
            candidates.append(row)
            by_slot[slot] = row
    if not candidates:
        return None

    best: dict[str, Any] | None = None
    layout = "codec_fallback_newest_equip"
    for slot in prefer_slots or ():
        hit = by_slot.get(int(slot))
        if hit is not None:
            best = hit
            layout = "codec_fallback_prefer_slot"
            break

    if best is None:
        # 优先装备带 9..64，再按 item_uid / updated_at 新→旧
        def _rank(r: dict[str, Any]) -> tuple[int, int, str, int]:
            slot = int(r.get("slot_index") or 0)
            in_equip_band = 1 if 9 <= slot <= 64 else 0
            uid = int(r.get("item_uid") or 0)
            updated = str(r.get("updated_at") or "")
            return (in_equip_band, uid, updated, slot)

        best = max(candidates, key=_rank)

    return {
        "layout": layout,
        "target_slot": int(best["slot_index"]),
        "list_type": 0,
        "tool_slot": -1,
        "context": 0,
        "raw_len": 0,
        "raw_hex": "",
        "fallback": True,
        "fallback_item_id": int(best.get("item_template_id") or 0),
    }


def _session_prefer_slots(session: Any) -> list[int]:
    """从会话收集最近操作过的背包装备槽（buy / move 可写入）。"""
    if session is None:
        return []
    out: list[int] = []
    for attr in (
        "last_disjoint_prefer_slots",
        "last_buy_item_slots",
        "recent_bag_equip_slots",
    ):
        raw = getattr(session, attr, None)
        if not raw:
            continue
        try:
            for x in list(raw):
                s = int(x)
                if s > 0 and s not in out:
                    out.append(s)
        except Exception:
            continue
    # 单槽属性
    for attr in ("last_buy_item_slot", "last_bag_equip_slot"):
        val = getattr(session, attr, None)
        if val is None:
            continue
        try:
            s = int(val)
            if s > 0 and s not in out:
                out.append(s)
        except Exception:
            continue
    return out


def _materials_mode() -> str:
    """empty | cube_only | full — 控制 **DB 发材料**，与 ACK 是否带材料列表解耦。

    - empty：不发材料（只扣装）
    - cube_only：只要无属性魔能碎片（调试用）
    - full（默认）：PVF ``etc/disjoint.etc`` 完整产物（cube + additional + expand）
    """
    raw = (os.environ.get("USDOF_GAME_DISJOINT_MATERIALS_MODE") or "full").strip().lower()
    if raw in {"full", "all", "pvf"}:
        return "full"
    if raw in {"empty", "none", "0", "zero"}:
        return "empty"
    if raw in {"cube_only", "cube", "simple"}:
        return "cube_only"
    return "full"


def _clamp_materials(
    materials: list[Any],
) -> list[Any]:
    """钳制单条材料数量，并过滤不可创建的 template。"""
    cap = _MAX_MATERIAL_COUNT
    env_cap = os.environ.get("USDOF_GAME_DISJOINT_MAX_MATERIAL_COUNT")
    if env_cap is not None and str(env_cap).strip():
        try:
            cap = max(1, int(env_cap))
        except Exception:
            pass
    out: list[Any] = []
    for mat in materials or []:
        iid = int(getattr(mat, "item_template_id", 0) or 0)
        cnt = int(getattr(mat, "count", 0) or 0)
        if iid <= 0 or cnt <= 0:
            continue
        # 客户端 create/popup 需要能解析的 stackable；装备/未知 id 易空指针
        try:
            meta = resolve_item_metadata(iid)
            if meta.is_equipment:
                continue
            if not (meta.is_stackable or str(meta.item_kind or "") == "stackable"):
                # 无 path 的幽灵 id 也丢掉
                if not str(getattr(meta, "source_path", "") or ""):
                    continue
        except Exception:
            continue
        if cnt > cap:
            try:
                mat.count = cap
            except Exception:
                pass
            cnt = cap
        if cnt <= 0:
            continue
        out.append(mat)
    return out


def _select_disjoint_materials(
    *,
    sell_gold: int,
    rarity: int,
    minimum_level: int,
) -> tuple[list[Any], str]:
    """按 mode 选产物列表。"""
    mode = _materials_mode()
    if mode == "empty":
        return [], "mode=empty"

    try:
        load_system_disjoint()
    except Exception:
        pass

    if mode == "cube_only":
        # 只跑计算器再截断到第一条 cube（calculate 总是先加 cube）
        mats = calculate_disjoint_materials(
            sell_gold=int(sell_gold),
            rarity=int(rarity),
            minimum_level=int(minimum_level),
            rng=random.Random(0),  # 稳定：cube 不依赖 rng
        )
        mats = _clamp_materials(mats)
        if not mats:
            # 保底 cube 3037 x1
            mats = [DisjointMaterial(item_template_id=3037, count=1)]
        else:
            # 仅保留 cube（通常 item 为 no-element cube）
            cube = mats[0]
            cube.count = max(1, min(int(cube.count or 1), _MAX_MATERIAL_COUNT))
            mats = [cube]
        return mats, "mode=cube_only n=%s id=%s" % (
            len(mats),
            mats[0].item_template_id if mats else 0,
        )

    mats = calculate_disjoint_materials(
        sell_gold=int(sell_gold),
        rarity=int(rarity),
        minimum_level=int(minimum_level),
    )
    mats = _clamp_materials(mats)
    return mats, "mode=full n=%s" % len(mats)


class DisjointItem001AService:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _emit(self, flag: int, opcode: int, body: bytes, mode: str = "codec") -> tuple[bytes, str]:
        mode_norm = (mode or "codec").strip().lower()
        if mode_norm in {"raw", "plain"}:
            return self.server._raw_response(flag, opcode, body), "raw"
        return self.server._codec_response(flag, opcode, body), "codec"

    def _fail(
        self, base: str, *, error: int = ERROR_INVALID, extra: str = ""
    ) -> tuple[list[bytes], str]:
        ack = build_disjoint_item_ack_error(error)
        pkt, mode = self._emit(1, 0x001A, ack)
        note = base
        if extra:
            note = base + "; " + extra
        return [pkt], note + "; ACK_fail/%s err=0x%02x" % (mode, error)

    def _daily_challenge_progress(
        self,
        repo: Any,
        *,
        character_id: int,
        event_count: int = 1,
    ) -> tuple[list[bytes], str]:
        try:
            _exp, level = repo.get_character_exp_level(int(character_id))
            count = max(1, int(event_count))
            if count > 1:
                return handle_daily_challenge_disjoint_item(
                    self.server,
                    character_repo=repo,
                    character_id=int(character_id),
                    level=max(1, int(level)),
                    event_count=count,
                )
            return handle_daily_challenge_disjoint_item(
                self.server,
                character_repo=repo,
                character_id=int(character_id),
                level=max(1, int(level)),
            )
        except Exception as exc:
            return [], "daily_challenge_disjoint_err=%s:%s" % (
                type(exc).__name__,
                exc,
            )

    def handle_disjoint_item_001a(self, body: bytes, *, session: Any = None) -> tuple[list[bytes], str]:
        if not env_bool("USDOF_GAME_HANDLE_DISJOINT_ITEM_001A", True):
            return [], "C1/001A DISJOINT_ITEM gate=OFF"

        plain = bytes(body or b"")
        plain_hex = _plain_hex(plain)
        # 直播特征：codec12 C2S 密文/乱码 plain 跨局恒定 → 非 seed0；记入 note 便于对照
        known_garbage = plain_hex == "42 bb 15 05 dd db 5c 81 0a 85 74 8f ed 27 d9 55"
        parsed = parse_disjoint_item_body(plain)
        parse_note = ""
        if parsed is None:
            parse_note = "parse_strict_fail plain_len=%s plain=%s%s" % (
                len(plain),
                plain_hex or "-",
                " seed0_c2s_garbage=1" if known_garbage else "",
            )
            # S4：parse 失败只 fail ACK，不猜槽 success（猜槽会闪退）。
            # 可选 env 打开 fallback 仅供离线探针，默认 OFF。
            if env_bool("USDOF_GAME_DISJOINT_ITEM_CODEC_FALLBACK", False):
                cid_probe = _cid(session, self.server)
                if cid_probe is not None:
                    prefer = _session_prefer_slots(session)
                    fb = _fallback_target_from_inventory(
                        self.server._repo(),
                        int(cid_probe),
                        prefer_slots=prefer,
                    )
                    if fb is not None:
                        parsed = fb
                        parse_note += (
                            "; codec_fallback layout=%s slot=%s item=%s prefer=%s"
                            % (
                                fb.get("layout"),
                                fb["target_slot"],
                                fb.get("fallback_item_id"),
                                prefer[:6] if prefer else [],
                            )
                        )
            if parsed is None:
                # 对齐 S4 DisjointItemRequestParser / InventoryHandler：
                # 无合法体 → ErrorInvalidRequest，不改库。
                return self._fail(
                    "C1/001A DISJOINT_ITEM " + parse_note,
                    error=ERROR_INVALID,
                    extra="s4_fail_only(no_valid_slot)",
                )

        target_slot = int(parsed["target_slot"])
        list_type = int(parsed["list_type"]) & 0xFF
        tool_slot = int(parsed["tool_slot"])
        base = (
            "C1/001A DISJOINT_ITEM layout=%s list=%s slot=%s tool=%s ctx=0x%08x plain=%s"
            % (
                parsed.get("layout"),
                list_type,
                target_slot,
                tool_slot,
                int(parsed.get("context") or 0),
                plain_hex or parsed.get("raw_hex") or "-",
            )
        )
        if parse_note:
            base = base + "; " + parse_note

        # S4 / Stage3：仅 Main 列表
        if list_type != 0:
            return self._fail(base, extra="reject=list_type")

        if target_slot == 0:
            return self._fail(base, extra="reject=wallet_slot")

        if tool_slot < -1:
            return self._fail(base, extra="reject=tool_slot")

        cid = _cid(session, self.server)
        if cid is None:
            return self._fail(base, extra="skip=no_cid")

        repo = self.server._repo()
        source = repo.get_inventory_item_at(
            int(cid), list_type=0, slot_index=int(target_slot)
        )
        if source is None:
            return self._fail(base, extra="reject=slot_empty")

        item_id = int(source.get("item_template_id") or 0)
        kind = str(source.get("item_kind") or "")
        stack = max(0, int(source.get("stack_count") or 0))
        if item_id <= 0 or stack <= 0:
            return self._fail(base, extra="reject=invalid_item")

        equipment_lock_id = (
            max(0, int(source.get("equipment_lock_id") or 0)) & 0xFF
        )
        if equipment_lock_id > 0:
            return self._fail(
                base,
                extra="reject=equipment_locked lock_id=%d" % equipment_lock_id,
            )

        meta = resolve_item_metadata(item_id)
        if not (meta.is_equipment or kind == "equipment"):
            return self._fail(
                base,
                extra="reject=not_equipment kind=%s" % (meta.item_kind or kind),
            )

        imposs = tuple(_normalize_token(x) for x in (meta.impossible_contents or ()))
        if "disjoint" in imposs:
            return self._fail(base, extra="reject=impossible_disjoint")

        attach = meta.normalized_attach_type()
        if attach == "trade delete":
            return self._fail(base, extra="reject=trade_delete")

        tool_note = "npc"
        if tool_slot == target_slot:
            return self._fail(base, extra="reject=tool_is_target")

        if tool_slot >= 0:
            tool = repo.get_inventory_item_at(
                int(cid), list_type=0, slot_index=int(tool_slot)
            )
            if tool is None:
                return self._fail(base, extra="reject=tool_empty")
            tool_id = int(tool.get("item_template_id") or 0)
            tool_kind = str(tool.get("item_kind") or "")
            tool_stack = max(0, int(tool.get("stack_count") or 0))
            tool_meta = resolve_item_metadata(tool_id)
            if (
                tool_id <= 0
                or tool_stack <= 0
                or not (tool_meta.is_stackable or tool_kind == "stackable")
            ):
                return self._fail(base, extra="reject=tool_not_stackable")
            portable = _extract_portable_disjoint(tool_meta)
            max_lv = _portable_max_level(portable)
            if max_lv < 0:
                return self._fail(base, extra="reject=tool_not_portable")
            if int(meta.minimum_level or 0) > max_lv:
                return self._fail(
                    base,
                    extra="reject=tool_level_cap need=%s max=%s"
                    % (meta.minimum_level, max_lv),
                )
            tool_note = "portable id=%s max_lv=%s" % (tool_id, max_lv)

        # 先算产物（不改库）
        materials, mat_mode_note = _select_disjoint_materials(
            sell_gold=int(meta.sell_gold),
            rarity=int(getattr(meta, "rarity", 0) or 0),
            minimum_level=int(meta.minimum_level or 0),
        )
        # empty 模式允许 n=0；其它模式无产物则失败
        if not materials and _materials_mode() != "empty":
            return self._fail(
                base, extra="reject=no_materials %s" % mat_mode_note
            )

        # 扣源装备（整格）
        ok, consume_note, detail = repo.consume_inventory_item(
            int(cid),
            list_type=0,
            slot_index=int(target_slot),
            count=max(1, stack),
            reject_locked=True,
        )
        removed = int(detail.get("removed") or 0)
        if not ok or removed <= 0:
            return self._fail(
                base, extra="consume_fail note=%s" % consume_note
            )

        # 扣便携机（装备已扣；失败则整单失败 ACK，本地可能不一致——尽量少见）
        if tool_slot >= 0:
            tok, tnote, _td = repo.consume_inventory_item(
                int(cid),
                list_type=0,
                slot_index=int(tool_slot),
                count=1,
            )
            if not tok:
                return self._fail(
                    base,
                    extra="tool_consume_fail note=%s after_equip_removed" % tnote,
                )

        # 发材料：
        #   小晶体 → 账号固定槽 354..359（叠加 accounts.cube_*）
        #   六级灵魂 → 账号固定投影 360..365
        #   其它 → metadata.slot_range() 自动路由 + 叠加
        # 000D 携带数据库最终状态，故允许 stack merge。
        granted: list[dict[str, int]] = []
        grant_notes: list[str] = []

        for mat in materials:
            mid = int(mat.item_template_id)
            mcnt = int(mat.count)
            # 小晶体/六级灵魂均走账号账本；其它分解产物遵从 PVF 正常材料槽。
            if is_cube_fragment(mid) or is_account_soul(mid):
                gok, gnote, slot = repo.grant_inventory_item(
                    int(cid), mid, mcnt, list_type=0
                )
                band_note = "cube354-359" if is_cube_fragment(mid) else "account_soul360-365"
            else:
                gok, gnote, slot = repo.grant_inventory_item(
                    int(cid),
                    mid,
                    mcnt,
                    list_type=0,
                )
                band_note = "pvf_normal"
            if not gok or int(slot) < 0:
                return self._fail(
                    base,
                    error=ERROR_INVENTORY_FULL,
                    extra=(
                        "inventory_full after_remove item=%s; granted=%s; "
                        "grant_fail=%s band=%s mid=%s"
                        % (item_id, granted, gnote, band_note, mid)
                    ),
                )
            # 最终堆叠：cube 从 gnote/accounts；普通从 DB 槽
            final_count = int(mcnt)
            try:
                if is_cube_fragment(mid) or is_account_soul(mid):
                    # 账号账本通过 Main 虚拟行读取最终绝对数量。
                    rows = repo.list_inventory_items(int(cid), list_type=0)
                    for r in rows:
                        if int(r.get("item_template_id") or 0) == mid and int(
                            r.get("slot_index") or -1
                        ) == int(slot):
                            final_count = max(
                                1, int(r.get("stack_count") or final_count)
                            )
                            break
                else:
                    row_after = repo.get_inventory_item_at(
                        int(cid), list_type=0, slot_index=int(slot)
                    )
                    if row_after is not None:
                        final_count = max(
                            1, int(row_after.get("stack_count") or final_count)
                        )
            except Exception:
                pass
            granted.append(
                {
                    "slot_index": int(slot),
                    "item_template_id": int(mid),
                    "count": int(final_count),
                    "obtained_count": int(mcnt),
                }
            )
            grant_notes.append(
                "0x%X x%s@%s final=%s obtained=%s %s (%s)"
                % (mid, mcnt, slot, final_count, mcnt, band_note, gnote)
            )

        packets: list[bytes] = []
        notes: list[str] = [base]
        notes.append(
            "OK source=%s rarity=%s lv=%s sell_gold=%s tool=%s %s materials=[%s] "
            "consume=%s cube_band=%s..%s soul_band=%s..%s"
            % (
                item_id,
                getattr(meta, "rarity", 0),
                meta.minimum_level,
                meta.sell_gold,
                tool_note,
                mat_mode_note,
                ", ".join(grant_notes) if grant_notes else "-",
                consume_note,
                SLOT_RANGE_CUBE_FRAGMENT[0],
                SLOT_RANGE_CUBE_FRAGMENT[1],
                SLOT_RANGE_SOUL_MATERIAL[0],
                SLOT_RANGE_SOUL_MATERIAL[1],
            )
        )

        try:
            inv_rows = repo.list_inventory_items(int(cid), list_type=0)
            items = [InventoryItemView(row) for row in inv_rows]
            list_param16 = int(repo.get_container_list_param(int(cid), list_type=0))
            body_d = build_item_list_mode0(
                items, list_type=0, list_param16=list_param16
            )
        except Exception as exc:
            # 没有预同步时禁止携带材料，避免客户端重新进入已确认会崩溃的 create 分支。
            ack = build_disjoint_item_ack_ok(
                target_slot=int(target_slot),
                list_type=0,
                materials=[],
            )
            pkt_ack, mode_ack = self._emit(1, 0x001A, ack)
            packets.append(pkt_ack)
            notes.append(
                "000D_build_err=%r; ACK001A/%s native_complete_n=0"
                % (exc, mode_ack)
            )
            challenge_packets, challenge_note = self._daily_challenge_progress(
                repo,
                character_id=int(cid),
            )
            packets.extend(challenge_packets)
            notes.append(challenge_note)
            return packets, "; ".join(notes)

        pkt_d_pre, mode_d_pre = self._emit(0, 0x000D, body_d)
        packets.append(pkt_d_pre)
        notes.append("000D_pre/%s entries=%d" % (mode_d_pre, len(items)))

        popup_materials = [
            {
                "slot_index": int(mat["slot_index"]),
                "item_template_id": int(mat["item_template_id"]),
                "count": int(mat["obtained_count"]),
            }
            for mat in granted
        ]
        ack = build_disjoint_item_ack_ok(
            target_slot=int(target_slot),
            list_type=0,
            materials=popup_materials,
        )
        pkt_ack, mode_ack = self._emit(1, 0x001A, ack)
        packets.append(pkt_ack)
        notes.append(
            "ACK001A/%s plain=%s native_popup_mats=%s count=session_delta"
            % (mode_ack, ack.hex(" "), len(popup_materials))
        )

        pkt_d_post, mode_d_post = self._emit(0, 0x000D, body_d)
        packets.append(pkt_d_post)
        notes.append("000D_post/%s entries=%d" % (mode_d_post, len(items)))

        challenge_packets, challenge_note = self._daily_challenge_progress(
            repo,
            character_id=int(cid),
        )
        packets.extend(challenge_packets)
        notes.append(challenge_note)

        return packets, "; ".join(notes)

    @staticmethod
    def _batch_window_seconds() -> float:
        raw = os.environ.get("USDOF_GAME_DISJOINT_BATCH_WINDOW_MS", "20")
        try:
            milliseconds = max(0, min(250, int(str(raw or "20"), 0)))
        except (TypeError, ValueError):
            milliseconds = 20
        return float(milliseconds) / 1000.0

    @staticmethod
    def has_pending_batch(session: Any) -> bool:
        return bool(getattr(session, "pending_disjoint_item_bodies", None))

    @staticmethod
    def pending_batch_deadline(session: Any) -> float | None:
        if not DisjointItem001AService.has_pending_batch(session):
            return None
        try:
            return float(getattr(session, "pending_disjoint_item_deadline", 0.0))
        except (TypeError, ValueError):
            return 0.0

    def queue_disjoint_item_001a(
        self,
        body: bytes,
        *,
        session: Any,
    ) -> tuple[list[bytes], str]:
        """Collect one client request until the current TCP burst goes quiet."""
        if not env_bool("USDOF_GAME_HANDLE_DISJOINT_ITEM_001A", True):
            return [], "C1/001A DISJOINT_ITEM gate=OFF"
        if session is None:
            return self.handle_disjoint_item_001a(body, session=session)

        pending = getattr(session, "pending_disjoint_item_bodies", None)
        if not isinstance(pending, list):
            pending = []
            setattr(session, "pending_disjoint_item_bodies", pending)

        released_packets: list[bytes] = []
        released_note = ""
        if len(pending) >= 128:
            released_packets, released_note = self.flush_disjoint_item_001a_batch(
                session=session
            )
            pending = getattr(session, "pending_disjoint_item_bodies", None)
            if not isinstance(pending, list):
                pending = []
                setattr(session, "pending_disjoint_item_bodies", pending)

        pending.append(bytes(body or b""))
        window_seconds = self._batch_window_seconds()
        deadline = time.monotonic() + window_seconds
        setattr(session, "pending_disjoint_item_deadline", deadline)
        note = "C1/001A DISJOINT_ITEM queued batch_count=%d window_ms=%d" % (
            len(pending),
            round(window_seconds * 1000.0),
        )
        if released_note:
            note = released_note + "; " + note
        return released_packets, note

    def flush_disjoint_item_001a_batch(
        self,
        *,
        session: Any,
    ) -> tuple[list[bytes], str]:
        pending = getattr(session, "pending_disjoint_item_bodies", None)
        if not isinstance(pending, list) or not pending:
            return [], "C1/001A DISJOINT_ITEM batch_flush empty"
        bodies = tuple(bytes(body or b"") for body in pending)
        pending.clear()
        setattr(session, "pending_disjoint_item_deadline", 0.0)
        return self.handle_disjoint_item_batch_001a(bodies, session=session)

    def _fail_batch(
        self,
        request_count: int,
        base: str,
        *,
        error: int = ERROR_INVALID,
        extra: str = "",
    ) -> tuple[list[bytes], str]:
        packets: list[bytes] = []
        modes: list[str] = []
        for _ in range(max(1, int(request_count))):
            packet, mode = self._emit(
                1,
                0x001A,
                build_disjoint_item_ack_error(error),
            )
            packets.append(packet)
            modes.append(mode)
        note = base
        if extra:
            note += "; " + extra
        return packets, (
            note
            + "; ACK_fail_batch count=%d modes=%s err=0x%02x"
            % (len(packets), ",".join(modes), int(error) & 0xFF)
        )

    def handle_disjoint_item_batch_001a(
        self,
        bodies: Any,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        """Settle one TCP burst as one inventory transaction and one reward."""
        requests = tuple(bytes(body or b"") for body in (bodies or ()))
        if not requests:
            return [], "C1/001A DISJOINT_ITEM batch empty"
        if len(requests) == 1:
            return self.handle_disjoint_item_001a(requests[0], session=session)
        if not env_bool("USDOF_GAME_HANDLE_DISJOINT_ITEM_001A", True):
            return [], "C1/001A DISJOINT_ITEM gate=OFF"

        started = time.perf_counter()
        cid = _cid(session, self.server)
        if cid is None:
            return self._fail_batch(
                len(requests),
                "C1/001A DISJOINT_ITEM batch=%d" % len(requests),
                extra="skip=no_cid rollback=all",
            )
        repo = self.server._repo()

        parsed_requests: list[dict[str, Any]] = []
        bases: list[str] = []
        requested_target_slots: set[int] = set()
        for index, body in enumerate(requests):
            plain_hex = _plain_hex(body)
            parsed = parse_disjoint_item_body(body)
            if parsed is None:
                return self._fail_batch(
                    len(requests),
                    "C1/001A DISJOINT_ITEM batch=%d" % len(requests),
                    extra=(
                        "reject=parse_strict_fail index=%d plain_len=%d plain=%s "
                        "rollback=all"
                        % (index, len(body), plain_hex or "-")
                    ),
                )
            target_slot = int(parsed["target_slot"])
            list_type = int(parsed["list_type"]) & 0xFF
            tool_slot = int(parsed["tool_slot"])
            base = (
                "index=%d layout=%s list=%s slot=%s tool=%s ctx=0x%08x plain=%s"
                % (
                    index,
                    parsed.get("layout"),
                    list_type,
                    target_slot,
                    tool_slot,
                    int(parsed.get("context") or 0),
                    plain_hex or parsed.get("raw_hex") or "-",
                )
            )
            if list_type != 0:
                return self._fail_batch(
                    len(requests), base, extra="reject=list_type rollback=all"
                )
            if target_slot == 0:
                return self._fail_batch(
                    len(requests), base, extra="reject=wallet_slot rollback=all"
                )
            if tool_slot < -1:
                return self._fail_batch(
                    len(requests), base, extra="reject=tool_slot rollback=all"
                )
            if tool_slot == target_slot:
                return self._fail_batch(
                    len(requests), base, extra="reject=tool_is_target rollback=all"
                )
            if target_slot in requested_target_slots:
                return self._fail_batch(
                    len(requests),
                    base,
                    extra="reject=duplicate_target_slot rollback=all",
                )
            requested_target_slots.add(target_slot)
            parsed_requests.append(parsed)
            bases.append(base)

        try:
            snapshot_rows, list_param16 = repo.load_main_inventory_snapshot(int(cid))
        except Exception as exc:
            return self._fail_batch(
                len(requests),
                "C1/001A DISJOINT_ITEM batch=%d" % len(requests),
                extra="inventory_snapshot_error=%s:%s rollback=all"
                % (type(exc).__name__, exc),
            )
        rows_by_slot = {
            int(row.get("slot_index") or -1): row for row in snapshot_rows
        }

        target_specs: list[DisjointTarget] = []
        tool_specs_by_slot: dict[int, DisjointToolUse] = {}
        material_order: list[int] = []
        material_totals: dict[int, int] = {}
        source_notes: list[str] = []

        for index, parsed in enumerate(parsed_requests):
            target_slot = int(parsed["target_slot"])
            tool_slot = int(parsed["tool_slot"])

            source = rows_by_slot.get(target_slot)
            if source is None:
                return self._fail_batch(
                    len(requests),
                    bases[index],
                    extra="reject=slot_empty rollback=all",
                )
            item_id = int(source.get("item_template_id") or 0)
            kind = str(source.get("item_kind") or "")
            stack = max(0, int(source.get("stack_count") or 0))
            lock_id = max(0, int(source.get("equipment_lock_id") or 0)) & 0xFF
            if item_id <= 0 or stack <= 0:
                return self._fail_batch(
                    len(requests),
                    bases[index],
                    extra="reject=invalid_item rollback=all",
                )
            if lock_id > 0:
                return self._fail_batch(
                    len(requests),
                    bases[index],
                    extra="reject=equipment_locked lock_id=%d rollback=all" % lock_id,
                )

            meta = resolve_item_metadata(item_id)
            if not (meta.is_equipment or kind == "equipment"):
                return self._fail_batch(
                    len(requests),
                    bases[index],
                    extra="reject=not_equipment kind=%s rollback=all"
                    % (meta.item_kind or kind),
                )
            imposs = tuple(
                _normalize_token(value) for value in (meta.impossible_contents or ())
            )
            if "disjoint" in imposs:
                return self._fail_batch(
                    len(requests),
                    bases[index],
                    extra="reject=impossible_disjoint rollback=all",
                )
            if meta.normalized_attach_type() == "trade delete":
                return self._fail_batch(
                    len(requests),
                    bases[index],
                    extra="reject=trade_delete rollback=all",
                )

            tool_note = "npc"
            if tool_slot >= 0:
                if tool_slot in requested_target_slots:
                    return self._fail_batch(
                        len(requests),
                        bases[index],
                        extra="reject=tool_is_batch_target rollback=all",
                    )
                tool = rows_by_slot.get(tool_slot)
                if tool is None:
                    return self._fail_batch(
                        len(requests),
                        bases[index],
                        extra="reject=tool_empty rollback=all",
                    )
                tool_id = int(tool.get("item_template_id") or 0)
                tool_kind = str(tool.get("item_kind") or "")
                tool_stack = max(0, int(tool.get("stack_count") or 0))
                tool_meta = resolve_item_metadata(tool_id)
                if (
                    tool_id <= 0
                    or tool_stack <= 0
                    or not (tool_meta.is_stackable or tool_kind == "stackable")
                ):
                    return self._fail_batch(
                        len(requests),
                        bases[index],
                        extra="reject=tool_not_stackable rollback=all",
                    )
                portable = _extract_portable_disjoint(tool_meta)
                max_lv = _portable_max_level(portable)
                if max_lv < 0:
                    return self._fail_batch(
                        len(requests),
                        bases[index],
                        extra="reject=tool_not_portable rollback=all",
                    )
                if int(meta.minimum_level or 0) > max_lv:
                    return self._fail_batch(
                        len(requests),
                        bases[index],
                        extra="reject=tool_level_cap need=%s max=%s rollback=all"
                        % (meta.minimum_level, max_lv),
                    )
                previous_tool = tool_specs_by_slot.get(tool_slot)
                tool_count = 1
                if previous_tool is not None:
                    if int(previous_tool.item_template_id) != tool_id:
                        return self._fail_batch(
                            len(requests),
                            bases[index],
                            extra="reject=tool_identity_conflict rollback=all",
                        )
                    tool_count = int(previous_tool.count) + 1
                if tool_count > tool_stack:
                    return self._fail_batch(
                        len(requests),
                        bases[index],
                        extra="reject=tool_insufficient have=%d need=%d rollback=all"
                        % (tool_stack, tool_count),
                    )
                tool_specs_by_slot[tool_slot] = DisjointToolUse(
                    slot_index=tool_slot,
                    item_template_id=tool_id,
                    count=tool_count,
                )
                tool_note = "portable id=%s max_lv=%s" % (tool_id, max_lv)

            materials, mat_note = _select_disjoint_materials(
                sell_gold=int(meta.sell_gold),
                rarity=int(getattr(meta, "rarity", 0) or 0),
                minimum_level=int(meta.minimum_level or 0),
            )
            if not materials and _materials_mode() != "empty":
                return self._fail_batch(
                    len(requests),
                    bases[index],
                    extra="reject=no_materials %s rollback=all" % mat_note,
                )
            for material in materials:
                material_id = int(material.item_template_id)
                material_count = int(material.count)
                if material_id not in material_totals:
                    material_order.append(material_id)
                    material_totals[material_id] = 0
                total = int(material_totals[material_id]) + material_count
                if total > 0x7FFFFFFF:
                    return self._fail_batch(
                        len(requests),
                        bases[index],
                        extra="reject=material_count_overflow item=%d rollback=all"
                        % material_id,
                    )
                material_totals[material_id] = total

            target_specs.append(
                DisjointTarget(
                    slot_index=target_slot,
                    item_template_id=item_id,
                    stack_count=stack,
                )
            )
            source_notes.append(
                "%d@%d rarity=%s lv=%s sell_gold=%s tool=%s %s"
                % (
                    item_id,
                    target_slot,
                    getattr(meta, "rarity", 0),
                    meta.minimum_level,
                    meta.sell_gold,
                    tool_note,
                    mat_note,
                )
            )

        if len(material_order) > 255:
            return self._fail_batch(
                len(requests),
                "C1/001A DISJOINT_ITEM batch=%d" % len(requests),
                extra="reject=too_many_material_types count=%d rollback=all"
                % len(material_order),
            )

        reward_specs = [
            DisjointReward(
                item_template_id=material_id,
                count=int(material_totals[material_id]),
                metadata=resolve_item_metadata(material_id),
            )
            for material_id in material_order
            if int(material_totals[material_id]) > 0
        ]
        ok, settlement_note, settlement = apply_disjoint_batch(
            repo,
            character_id=int(cid),
            targets=target_specs,
            tool_uses=[tool_specs_by_slot[key] for key in sorted(tool_specs_by_slot)],
            rewards=reward_specs,
        )
        if not ok:
            error = (
                ERROR_INVENTORY_FULL
                if str(settlement_note).startswith("inventory_full")
                else ERROR_INVALID
            )
            return self._fail_batch(
                len(requests),
                "C1/001A DISJOINT_ITEM batch=%d" % len(requests),
                error=error,
                extra="settlement_fail=%s rollback=all" % settlement_note,
            )

        packets: list[bytes] = []
        notes: list[str] = [
            "C1/001A DISJOINT_ITEM batch=%d atomic=1 sources=[%s]"
            % (len(requests), ", ".join(source_notes)),
            settlement_note,
        ]
        try:
            final_rows, final_list_param16 = repo.load_main_inventory_snapshot(int(cid))
            items = [InventoryItemView(row) for row in final_rows]
            body_d = build_item_list_mode0(
                items,
                list_type=0,
                list_param16=int(final_list_param16),
            )
        except Exception as exc:
            for parsed in parsed_requests:
                ack = build_disjoint_item_ack_ok(
                    target_slot=int(parsed["target_slot"]),
                    list_type=0,
                    materials=[],
                )
                packet, mode = self._emit(1, 0x001A, ack)
                packets.append(packet)
                notes.append("ACK001A/%s native_complete_n=0" % mode)
            notes.append("000D_build_err=%r" % (exc,))
            challenge_packets, challenge_note = self._daily_challenge_progress(
                repo,
                character_id=int(cid),
                event_count=len(target_specs),
            )
            packets.extend(challenge_packets)
            notes.append(challenge_note)
            return packets, "; ".join(notes)

        final_by_slot = {
            int(row.get("slot_index") or -1): row for row in final_rows
        }
        popup_materials: list[dict[str, int]] = []
        grant_notes: list[str] = []
        for reward in settlement.get("reward_rows", []):
            slot = int(reward.get("slot_index") or -1)
            item_id = int(reward.get("item_template_id") or 0)
            obtained = int(reward.get("granted_count") or 0)
            final_row = final_by_slot.get(slot)
            final_count = (
                max(1, int(final_row.get("stack_count") or obtained))
                if final_row is not None
                else max(1, obtained)
            )
            popup_materials.append(
                {
                    "slot_index": slot,
                    "item_template_id": item_id,
                    "count": obtained,
                }
            )
            grant_notes.append(
                "0x%X x%s@%s final=%s (%s)"
                % (item_id, obtained, slot, final_count, reward.get("note") or "")
            )

        packet_pre, mode_pre = self._emit(0, 0x000D, body_d)
        packets.append(packet_pre)
        notes.append(
            "000D_pre/%s entries=%d snapshot_builds=1" % (mode_pre, len(items))
        )

        reward_ack_index = len(parsed_requests) - 1
        for index, parsed in enumerate(parsed_requests):
            materials = popup_materials if index == reward_ack_index else []
            ack = build_disjoint_item_ack_ok(
                target_slot=int(parsed["target_slot"]),
                list_type=0,
                materials=materials,
            )
            packet_ack, mode_ack = self._emit(1, 0x001A, ack)
            packets.append(packet_ack)
            notes.append(
                "ACK001A/%s index=%d native_popup_mats=%d count=batch_delta"
                % (mode_ack, index, len(materials))
            )

        packet_post, mode_post = self._emit(0, 0x000D, body_d)
        packets.append(packet_post)
        notes.append("000D_post/%s entries=%d" % (mode_post, len(items)))

        challenge_packets, challenge_note = self._daily_challenge_progress(
            repo,
            character_id=int(cid),
            event_count=len(target_specs),
        )
        packets.extend(challenge_packets)
        notes.append(challenge_note)
        notes.append(
            "batch_reward_once=1 aggregate=[%s] elapsed_ms=%.3f"
            % (
                ", ".join(grant_notes) if grant_notes else "-",
                (time.perf_counter() - started) * 1000.0,
            )
        )
        return packets, "; ".join(notes)


__all__ = [
    "DisjointItem001AService",
    "parse_disjoint_item_body",
    "build_disjoint_item_ack_ok",
    "build_disjoint_item_ack_error",
    "ERROR_INVALID",
    "ERROR_INVENTORY_FULL",
]
