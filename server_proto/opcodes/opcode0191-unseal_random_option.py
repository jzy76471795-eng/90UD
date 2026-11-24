# -*- coding: utf-8 -*-
"""C1/0x0191 ENUM_CMDPACKET_UNSEAL_RANDOM_OPTION — 魔法封印开启。

对齐 S4A12（只读对照，Stage3 以本实现为准）::
  原地解封同一槽 → S0/000E 单槽刷新（108B 含 RO tail）→ S1/0191 ACK
  **不** 删装再 CreateItem（客户端 Create 默认仍是密制 →「解封后还是密制」）

客户端：
  - C2S 明文由 ``usdof_codec_passthrough`` 全局 encode 提供
  - 服务端在同一会话使用全局 plain-wire
  - 无 S→C 0x0191 业务 handler → 靠 **000E** 把解封属性灌进槽

流程：
  1. 解析 plain ``i16 slot + i16 scroll``
  2. 校验背包装备 + PVF ``[random option]``
  3. roll 选项 → 写 ``extra_json``（同槽原地）
  4. ``S0/000E`` list0 单槽 + ``S0/000D`` list0 全量（RO@60..71）
  5. ``S1/0191`` ACK 完成客户端原生解封流程
"""
from __future__ import annotations

import json
import struct
from typing import Any

from game.chain_loader import load_chain
from game.character.inventory_item_view import InventoryItemView
from game.character.item_metadata import resolve_item_metadata
from game.character.magic_seal_random_option_index import (
    MagicSealRandomOptionIndex,
    get_magic_seal_random_option_index,
)
from game.character.payloads import (
    EQUIP_TOP_QUALITY_SEED,
    build_item_list_mode0,
    build_item_list_update_000e,
)
from game.quest.quest_common import env_bool
from game.quest.quest_session import resolve_selected_character_detail

_ro = load_chain("helper-random_option.py")
item_has_random_option = _ro.item_has_random_option
format_options_brief = _ro.format_options_brief
inventory_row_random_options = _ro.inventory_row_random_options

_MAX_MAIN_SLOT = 400

ERROR_INVALID = 0x13


def parse_unseal_body(plain: bytes) -> dict[str, Any] | None:
    """解析 C1/0191。参考 S4：slot(i16)+scrollSlot(i16)。"""
    data = bytes(plain or b"")
    if len(data) < 2:
        return None
    target_slot = int(struct.unpack_from("<h", data, 0)[0])
    scroll_slot = -1
    if len(data) >= 4:
        scroll_slot = int(struct.unpack_from("<h", data, 2)[0])
    if target_slot < 0 or target_slot > _MAX_MAIN_SLOT:
        return None
    if scroll_slot < -1 or scroll_slot > _MAX_MAIN_SLOT:
        scroll_slot = -1
    return {
        "layout": "slot_scroll",
        "target_slot": target_slot,
        "scroll_slot": scroll_slot,
        "list_type": 0,
        "raw_len": len(data),
        "raw_hex": data[:16].hex(" "),
    }


def _plain_hex(plain: bytes, n: int = 16) -> str:
    data = bytes(plain or b"")
    if not data:
        return ""
    return data[:n].hex(" ")


def build_unseal_ack_ok() -> bytes:
    return bytes([0x01])


def build_unseal_ack_fail() -> bytes:
    return bytes([0x00])


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


def _write_extra_json(
    repo: Any, cid: int, list_type: int, slot_index: int, payload: dict[str, Any]
) -> str:
    """写 character_items.extra_json。"""
    try:
        with repo.open() as conn:
            row = conn.execute(
                "SELECT item_uid, extra_json FROM character_items "
                "WHERE character_id = ? AND list_type = ? AND slot_index = ?",
                (int(cid), int(list_type), int(slot_index)),
            ).fetchone()
            if not row:
                return "extra_json=no_row"
            uid = int(row[0])
            try:
                current = json.loads(str(row[1] or "{}"))
                if not isinstance(current, dict):
                    current = {}
            except Exception:
                current = {}
            current.update(payload)
            text = json.dumps(current, ensure_ascii=False, separators=(",", ":"))
            conn.execute(
                "UPDATE character_items SET extra_json = ?, seal_flag = 0, "
                "updated_at = CURRENT_TIMESTAMP WHERE item_uid = ?",
                (text, uid),
            )
            return "extra_json=ok uid=%s" % uid
    except Exception as exc:
        return "extra_json_err=%r" % (exc,)


def _already_unsealed(row: dict[str, Any]) -> bool:
    try:
        ej = row.get("extra_json") or ""
        if not isinstance(ej, str) or not ej.strip():
            return False
        parsed = json.loads(ej)
        if not isinstance(parsed, dict):
            return False
        if parsed.get("unsealed"):
            return True
        opts = parsed.get("random_options") or []
        return isinstance(opts, list) and len(opts) > 0
    except Exception:
        return False


class UnsealRandomOption0191Service:
    def __init__(
        self,
        server: Any,
        *,
        index: MagicSealRandomOptionIndex | None = None,
        rng: Any = None,
    ) -> None:
        self.server = server
        self.index = index
        self.rng = rng

    def _emit(
        self, flag: int, opcode: int, body: bytes, mode: str = "codec"
    ) -> tuple[bytes, str]:
        mode_norm = (mode or "codec").strip().lower()
        if mode_norm in {"raw", "plain"}:
            return self.server._raw_response(flag, opcode, body), "raw"
        return self.server._codec_response(flag, opcode, body), "codec"

    def _fail(self, base: str, *, extra: str = "") -> tuple[list[bytes], str]:
        notes = [base]
        if extra:
            notes.append(extra)
        packets: list[bytes] = []
        pkt, mode = self._emit(1, 0x0191, build_unseal_ack_fail())
        packets.append(pkt)
        notes.append("ACK0191_fail/%s" % mode)
        return packets, "; ".join(notes)

    def handle_unseal_random_option_0191(
        self, body: bytes, *, session: Any = None
    ) -> tuple[list[bytes], str]:
        if not env_bool("USDOF_GAME_HANDLE_UNSEAL_0191", True):
            return [], "C1/0191 UNSEAL_RANDOM_OPTION gate=OFF"

        plain = bytes(body or b"")
        plain_hex = _plain_hex(plain)
        parsed = parse_unseal_body(plain)
        if parsed is None:
            return self._fail(
                "C1/0191 UNSEAL_RANDOM_OPTION parse_fail plain_len=%s plain=%s"
                % (len(plain), plain_hex or "-"),
                extra="requires global codec passthrough C2S plain wire",
            )

        target_slot = int(parsed["target_slot"])
        scroll_slot = int(parsed["scroll_slot"])
        base = (
            "C1/0191 UNSEAL_RANDOM_OPTION layout=%s slot=%s scroll=%s plain=%s"
            % (
                parsed.get("layout"),
                target_slot,
                scroll_slot,
                plain_hex or parsed.get("raw_hex") or "-",
            )
        )

        if target_slot == 0:
            return self._fail(base, extra="reject=wallet_slot")

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

        meta = resolve_item_metadata(item_id)
        if not (meta.is_equipment or kind == "equipment"):
            return self._fail(
                base,
                extra="reject=not_equipment kind=%s" % (meta.item_kind or kind),
            )

        require_flag = env_bool("USDOF_GAME_UNSEAL_REQUIRE_PVF_FLAG", True)
        if require_flag and not item_has_random_option(item_id):
            return self._fail(
                base,
                extra="reject=no_random_option item=%s" % item_id,
            )

        # 已解封：禁止 bare fail（客户端右键再点会回滚成「魔法封印」）；
        # 重推既有 RO + ACK ok（幂等）。仅 ALLOW_REROLL 时才重 roll。
        already = _already_unsealed(source)
        reroll = env_bool("USDOF_GAME_UNSEAL_ALLOW_REROLL", False)
        if already and not reroll:
            options = inventory_row_random_options(source)
            if not options:
                return self._fail(
                    base, extra="reject=already_unsealed_empty_ro"
                )
            extra_note = "already_unsealed_repush"
        else:
            try:
                index = self.index or get_magic_seal_random_option_index()
                options = [
                    candidate.as_entry()
                    for candidate in index.roll_unseal_options(
                        meta,
                        rng=self.rng,
                    )
                ]
            except (RuntimeError, TypeError, ValueError) as exc:
                return self._fail(
                    base,
                    extra="reject=roll_failed:%s" % exc,
                )
            if not options:
                return self._fail(base, extra="reject=roll_empty")
            extra_note = ""

        opt_note = format_options_brief(options)

        # 耐久：DB 为 0 时用 PVF 默认
        dura = max(0, int(source.get("durability", 0) or 0)) & 0xFFFF
        if dura <= 0:
            dura = max(0, int(getattr(meta, "durability", 0) or 0)) & 0xFFFF
        iv = max(0, int(source.get("instance_value", 0) or 0)) & 0xFFFFFFFF
        if iv < EQUIP_TOP_QUALITY_SEED:
            iv = EQUIP_TOP_QUALITY_SEED
        item_kind = "equipment" if (meta.is_equipment or kind == "equipment") else kind

        # 1) 原地写 extra_json（不删不建；已解封仅 re-push 时跳过写库）
        if not (already and not reroll):
            extra_note = _write_extra_json(
                repo,
                int(cid),
                0,
                int(target_slot),
                {
                    "unsealed": True,
                    "source_slot": int(target_slot),
                    "random_options": options,
                },
            )
            if not extra_note.startswith("extra_json=ok"):
                return self._fail(base, extra="reject=persist_failed %s" % extra_note)

        packets: list[bytes] = []
        notes: list[str] = [
            base,
            "OK inplace_unseal item=%s slot=%s options=%s %s"
            % (item_id, target_slot, opt_note, extra_note),
        ]

        # 2) S0/000E 单槽 + 3) 全量 000D（都带 RO）
        #    客户端无 0191 S→C handler；必须靠 ITEMLIST 把 RO 写进槽，否则仍显示「魔法封印」
        refreshed = repo.get_inventory_item_at(
            int(cid), list_type=0, slot_index=int(target_slot)
        )
        entry_row = dict(refreshed or source)
        entry_row.update(
            durability=dura,
            instance_value=iv,
            item_kind=item_kind,
            seal_flag=0,
            marker_16=0xFFFFFFFF,
            random_options=options,
        )
        entry = InventoryItemView(entry_row)
        body_e = build_item_list_update_000e([entry], list_type=0)
        pkt_e, mode_e = self._emit(0, 0x000E, body_e)
        packets.append(pkt_e)
        notes.append(
            "000E/%s list0 slot=%s ro=%s plain_len=%s"
            % (mode_e, target_slot, opt_note, len(body_e))
        )

        # 全量 000D 重建 list0，保证悬停 tooltip 与后续移动读取同一 RO 状态。
        inv_rows = repo.list_inventory_items(int(cid), list_type=0)
        items = [InventoryItemView(row) for row in inv_rows]
        list_param16 = int(repo.get_container_list_param(int(cid), list_type=0))
        body_d = build_item_list_mode0(
            items, list_type=0, list_param16=list_param16
        )
        pkt_d, mode_d = self._emit(0, 0x000D, body_d)
        packets.append(pkt_d)
        notes.append(
            "000D/%s entries=%d place_ro=%s" % (mode_d, len(items), opt_note)
        )

        pkt_ack, mode_ack = self._emit(1, 0x0191, build_unseal_ack_ok())
        packets.append(pkt_ack)
        notes.append("ACK0191_ok/%s" % mode_ack)

        return packets, "; ".join(notes)


__all__ = [
    "UnsealRandomOption0191Service",
    "parse_unseal_body",
    "build_unseal_ack_ok",
    "build_unseal_ack_fail",
    "ERROR_INVALID",
]
