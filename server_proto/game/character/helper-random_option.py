# -*- coding: utf-8 -*-
"""魔法封印 / random option 只读 helper（共享 PVF 预加载）。

Stage3：装备脚本 ``[random option] N`` 表示可开启魔法封印。
本模块：模板判定、extra_json 解析、108B RO 打包。
业务解封写库在 opcode0191；进房/刷新 000D 必须带 RO 否则仍显示「魔法封印」。
"""
from __future__ import annotations

import json
from typing import Any

from game.character.item_metadata import (
    _extract_int_field,
    resolve_item_metadata,
)


def item_random_option_flag(item_id: int) -> int:
    """读 PVF ``[random option]`` 整数值；无则 0。"""
    meta = resolve_item_metadata(int(item_id))
    path = str(getattr(meta, "source_path", "") or "")
    if not path:
        return 0
    try:
        from pvf import pvf_runtime

        text = pvf_runtime.read_text(path)
    except Exception:
        return 0
    raw = _extract_int_field(text, "random option")
    if raw is None:
        return 0
    return max(0, int(raw))


def item_has_random_option(item_id: int) -> bool:
    """模板是否带魔法封印槽（可 UNSEAL）。"""
    meta = resolve_item_metadata(int(item_id))
    if not meta.is_equipment:
        return False
    return item_random_option_flag(item_id) > 0


def format_options_brief(entries: list[dict[str, Any]] | None) -> str:
    if not entries:
        return "[]"
    parts = []
    for e in entries:
        parts.append(
            "%02X/%s/%s"
            % (
                int(e.get("type", 0) or 0) & 0xFF,
                int(e.get("value1", 0) or 0),
                int(e.get("value2", 0) or 0),
            )
        )
    return "[" + ",".join(parts) + "]"


def parse_random_options_from_extra_json(
    extra_json: Any,
) -> list[dict[str, int]]:
    """从 character_items.extra_json 解析已解封 random_options。

    兼容::
      {"unsealed": true, "random_options": [{"type":..,"value1":..,"value2":..}, ...]}
    """
    if extra_json is None:
        return []
    try:
        if isinstance(extra_json, (bytes, bytearray)):
            text = bytes(extra_json).decode("utf-8", errors="ignore")
        elif isinstance(extra_json, str):
            text = extra_json
        elif isinstance(extra_json, dict):
            parsed = extra_json
            text = None
        else:
            return []
        if text is not None:
            text = text.strip()
            if not text:
                return []
            parsed = json.loads(text)
        if not isinstance(parsed, dict):
            return []
        raw = parsed.get("random_options")
        if not isinstance(raw, list) or not raw:
            return []
        out: list[dict[str, int]] = []
        for e in raw[:3]:
            if not isinstance(e, dict):
                continue
            t = int(e.get("type", 0) or 0) & 0xFF
            if t <= 0:
                continue
            out.append(
                {
                    "type": t,
                    "value1": int(e.get("value1", 0) or 0) & 0xFF,
                    "value2": int(e.get("value2", 0) or 0) & 0xFF,
                }
            )
        return out
    except Exception:
        return []


def inventory_row_random_options(row: dict[str, Any] | None) -> list[dict[str, int]]:
    """从 list_inventory_items 行取 RO（优先 extra_json）。"""
    if not row:
        return []
    if row.get("random_options"):
        try:
            return list(row.get("random_options") or [])[:3]
        except Exception:
            pass
    return parse_random_options_from_extra_json(row.get("extra_json"))


# Stage3 108B 条目 = 客户端 clear_entry 结构（IDA sub_160F990 + 000D sub_2C74B80(dst,108)）。
# 魔法封印字段（对象/线包绝对偏移，**不是** S4 TailData2F@0x2F）：
#   +60 count
#   +61..63 types
#   +64..66 value1
#   +67..69 value2
#   +70 state(0=已解封)  +71 changed(0xFF)
# 证据：sub_160F990 清 this+60 与 61/64/67 三元组；000D 整块 108B 读入同结构。
_ENTRY108_RO_COUNT = 60
_ENTRY108_RO_TYPE0 = 61
_ENTRY108_RO_VAL1_0 = 64
_ENTRY108_RO_VAL2_0 = 67
_ENTRY108_RO_STATE = 70
_ENTRY108_RO_CHANGED = 71
_ENTRY108_MIN = 72


def pack_random_options_tail(entries: list[dict[str, Any]] | None) -> bytes:
    """兼容旧名：返回从 offset 60 起的 RO 片段（12B：count+3type+3v1+3v2+state+changed）。"""
    blob = bytearray(12)
    if not entries:
        return bytes(blob)
    count = min(3, len(entries))
    if count <= 0:
        return bytes(blob)
    blob[0] = count & 0xFF
    for i in range(count):
        e = entries[i] or {}
        blob[1 + i] = int(e.get("type", 0) or 0) & 0xFF
        blob[4 + i] = int(e.get("value1", 0) or 0) & 0xFF
        blob[7 + i] = int(e.get("value2", 0) or 0) & 0xFF
    blob[10] = 0x00  # unsealed
    blob[11] = 0xFF
    return bytes(blob)


def apply_random_options_to_entry108(
    row: bytearray | memoryview, entries: list[dict[str, Any]] | None
) -> bool:
    """把解封后的 random option 写入 Stage3 108B 条目（offset 60..71）。"""
    if row is None or len(row) < _ENTRY108_MIN:
        return False
    if not entries:
        return False
    count = min(3, len(entries))
    if count <= 0:
        return False
    # 先清 RO 区，避免残留密制态
    for off in range(_ENTRY108_RO_COUNT, _ENTRY108_RO_CHANGED + 1):
        row[off] = 0
    row[_ENTRY108_RO_COUNT] = count & 0xFF
    for i in range(count):
        e = entries[i] or {}
        row[_ENTRY108_RO_TYPE0 + i] = int(e.get("type", 0) or 0) & 0xFF
        row[_ENTRY108_RO_VAL1_0 + i] = int(e.get("value1", 0) or 0) & 0xFF
        row[_ENTRY108_RO_VAL2_0 + i] = int(e.get("value2", 0) or 0) & 0xFF
    row[_ENTRY108_RO_STATE] = 0x00
    row[_ENTRY108_RO_CHANGED] = 0xFF
    return True


__all__ = [
    "item_random_option_flag",
    "item_has_random_option",
    "format_options_brief",
    "parse_random_options_from_extra_json",
    "inventory_row_random_options",
    "pack_random_options_tail",
    "apply_random_options_to_entry108",
]
