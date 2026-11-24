# -*- coding: utf-8 -*-
"""系统分解（DISJOINT）PVF 配置与产物计算。

真源：``etc/disjoint.etc``（共享预加载 PVF）。
对照 S4A12：
  - ``DisjointConfigProvider`` / ``DisjointFile`` / ``DisjointResultCalculator``

产物组成（按装备 rarity / level / sell_gold）：
  1. 无属性魔能碎片 cube（``[cube index][no element]`` + ``[cube creation const]``）
  2. 品质附加材料（``[additional result]`` + ``[additional result const]``）
  3. 扩展材料（``[additional result expand]`` + const，按概率）

运行时只读共享 reader；boot warmup 预解析配置。
"""
from __future__ import annotations

import math
import random
import re
import threading
from dataclasses import dataclass, field
from typing import Any

from pvf import pvf_runtime

DISJOINT_ETC_PATH = "etc/disjoint.etc"

_LOCK = threading.RLock()
_CONFIG: "DisjointConfig | None" = None
_CONFIG_SOURCE: str = ""


@dataclass
class DisjointAdditionalConst:
    great_count_divisor: float = 0.0
    normal_count_divisor: float = 0.0
    great_chance_percent: float = 0.0
    normal_chance_percent: float = 0.0


@dataclass
class DisjointExpandResult:
    enabled: bool = False
    item_template_id: int = 0
    level_divisor: float = 0.0
    great_chance_percent: float = 0.0
    normal_chance_percent: float = 0.0


@dataclass
class DisjointConfig:
    cube_indexes: dict[str, list[int]] = field(default_factory=dict)
    cube_creation_base: float = 150.0
    cube_creation_multipliers: list[float] = field(default_factory=list)
    additional_results: list[list[int]] = field(default_factory=list)
    additional_result_consts: list[DisjointAdditionalConst] = field(default_factory=list)
    expand_results: list[DisjointExpandResult] = field(default_factory=list)
    source_path: str = DISJOINT_ETC_PATH
    note: str = ""

    def get_no_element_cube_item_id(self) -> int:
        ids = self.cube_indexes.get("[no element]") or self.cube_indexes.get("no element")
        if ids:
            return int(ids[0])
        return 3037

    def get_cube_creation_multiplier(self, rarity: int) -> float:
        if not self.cube_creation_multipliers:
            return 1.0
        idx = max(0, int(rarity))
        if idx >= len(self.cube_creation_multipliers):
            idx = 0
        return float(self.cube_creation_multipliers[idx])

    def get_additional_items(self, rarity: int) -> list[int]:
        idx = max(0, int(rarity))
        if idx >= len(self.additional_results):
            return []
        return list(self.additional_results[idx])

    def get_additional_const(self, rarity: int) -> DisjointAdditionalConst | None:
        idx = max(0, int(rarity))
        if idx >= len(self.additional_result_consts):
            return None
        return self.additional_result_consts[idx]

    def get_expand_result(self, rarity: int) -> DisjointExpandResult | None:
        # The current PVF includes all seven rarity rows, including a disabled
        # chronicle placeholder at 5. Legacy six-row tables omit that slot.
        r = int(rarity)
        if len(self.expand_results) >= 7 and 0 <= r <= 6:
            idx = r
        elif r == 6:
            idx = 5
        elif 0 <= r <= 4:
            idx = r
        else:
            return None
        if idx < 0 or idx >= len(self.expand_results):
            return None
        return self.expand_results[idx]


@dataclass
class DisjointMaterial:
    item_template_id: int
    count: int
    slot_index: int = -1


def _section_bodies(text: str, key: str) -> list[str]:
    """收集全部 ``[key] ... [/key]`` 或到下一同级 tag 的 body。"""
    # 优先闭合标签
    closed = re.compile(
        rf"\[{re.escape(key)}\]\s*(.*?)\s*\[/{re.escape(key)}\]",
        re.IGNORECASE | re.DOTALL,
    )
    bodies = [m.group(1).strip() for m in closed.finditer(text or "")]
    if bodies:
        return bodies
    # 退化：无闭合标签，取到下一 [tag]
    open_pat = re.compile(
        rf"\[{re.escape(key)}\]\s*(.*?)(?=\n\s*\[[^\]]+\]|\Z)",
        re.IGNORECASE | re.DOTALL,
    )
    return [m.group(1).strip() for m in open_pat.finditer(text or "")]


def _tokens(body: str) -> list[str]:
    if not body:
        return []
    # 保留 `...` 内空格
    out: list[str] = []
    for m in re.finditer(r"`([^`]*)`|(\S+)", body):
        if m.group(1) is not None:
            out.append(m.group(1).strip())
        else:
            out.append(m.group(2).strip())
    return out


def _parse_ints(tokens: list[str]) -> list[int]:
    out: list[int] = []
    for t in tokens:
        try:
            # 浮点 token 里若是整型字面也接受
            if re.fullmatch(r"-?\d+", t):
                out.append(int(t))
            elif re.fullmatch(r"-?\d+\.\d+", t):
                # 不把 float 当 int（留给 doubles）
                continue
        except Exception:
            continue
    return out


def _parse_doubles(tokens: list[str]) -> list[float]:
    out: list[float] = []
    for t in tokens:
        try:
            out.append(float(t))
        except Exception:
            continue
    return out


def _normalize_key(token: str) -> str:
    s = (token or "").strip().strip("`").strip().lower()
    if s.startswith("[") and s.endswith("]") and len(s) >= 2:
        return s
    if s:
        return f"[{s}]"
    return s


def _bind_expand_constants(
    rows: list[DisjointExpandResult], values: list[float],
) -> None:
    """Enabled rows have three constants; disabled rows may have 1..3.

    Resolve the complete shape before assigning rarity rows. Accept alternative
    disabled widths only when they give the same enabled-row constants.
    """
    from functools import lru_cache

    @lru_cache(maxsize=None)
    def parse(row_index: int, offset: int) -> tuple:
        if row_index == len(rows):
            return ((),) if offset == len(values) else ()
        row = rows[row_index]
        results = set()
        for width in ([3] if row.enabled else [1, 2, 3]):
            if offset + width > len(values):
                continue
            constants = tuple(float(v) for v in values[offset:offset + 3]) if row.enabled else (0.0, 0.0, 0.0)
            if row.enabled and (constants[0] < 0 or not 0 <= constants[1] <= 100 or not 0 <= constants[2] <= 100):
                continue
            for tail in parse(row_index + 1, offset + width):
                results.add((constants,) + tail)
                if len(results) > 1:
                    return tuple(results)
        return tuple(results)

    matches = parse(0, 0)
    if len(matches) != 1:
        raise ValueError("disjoint expand constants have invalid or ambiguous row shape")
    for row, constants in zip(rows, matches[0]):
        row.level_divisor, row.great_chance_percent, row.normal_chance_percent = constants


def parse_disjoint_etc(text: str) -> DisjointConfig:
    """解析 ``etc/disjoint.etc`` 文本（token 解码后）。"""
    cfg = DisjointConfig(note="parsed")
    raw = text or ""

    # cube index 可出现多次（按元素）
    for body in _section_bodies(raw, "cube index"):
        toks = _tokens(body)
        if not toks:
            continue
        key = _normalize_key(toks[0])
        ids = _parse_ints(toks[1:])
        if key and ids:
            cfg.cube_indexes[key] = ids

    for body in _section_bodies(raw, "cube creation const"):
        vals = _parse_doubles(_tokens(body))
        if not vals:
            continue
        cfg.cube_creation_base = vals[0] if vals[0] > 0 else 150.0
        cfg.cube_creation_multipliers = list(vals[1:])
        break

    for body in _section_bodies(raw, "additional result"):
        numbers = _parse_ints(_tokens(body))
        rows: list[list[int]] = []
        i = 0
        while i < len(numbers):
            count = max(0, int(numbers[i]))
            i += 1
            items: list[int] = []
            for _ in range(count):
                if i >= len(numbers):
                    break
                if numbers[i] > 0:
                    items.append(int(numbers[i]))
                i += 1
            rows.append(items)
        cfg.additional_results = rows
        break

    for body in _section_bodies(raw, "additional result const"):
        values = _parse_doubles(_tokens(body))
        if not values:
            break
        row_count = (
            len(cfg.additional_results)
            if cfg.additional_results
            else max(1, (len(values) + 3) // 4)
        )
        consts: list[DisjointAdditionalConst] = []
        index = 0
        for row in range(row_count):
            if index >= len(values):
                break
            remaining_rows = row_count - row - 1
            # S4：若剩余刚好 1 + remaining*4，本行只有 great divisor
            if len(values) - index == 1 + remaining_rows * 4:
                consts.append(
                    DisjointAdditionalConst(great_count_divisor=float(values[index]))
                )
                index += 1
                continue
            great = float(values[index]) if index < len(values) else 0.0
            normal = float(values[index + 1]) if index + 1 < len(values) else 0.0
            gchance = float(values[index + 2]) if index + 2 < len(values) else 0.0
            nchance = float(values[index + 3]) if index + 3 < len(values) else 0.0
            consts.append(
                DisjointAdditionalConst(
                    great_count_divisor=great,
                    normal_count_divisor=normal,
                    great_chance_percent=gchance,
                    normal_chance_percent=nchance,
                )
            )
            index += 4
        cfg.additional_result_consts = consts
        break

    for body in _section_bodies(raw, "additional result expand"):
        numbers = _parse_ints(_tokens(body))
        expands: list[DisjointExpandResult] = []
        i = 0
        while i < len(numbers):
            enabled_flag = int(numbers[i])
            i += 1
            item_id = 0
            if enabled_flag > 0:
                if i >= len(numbers):
                    raise ValueError("disjoint expand enabled row is missing item id")
                item_id = int(numbers[i])
                i += 1
            expands.append(
                DisjointExpandResult(
                    enabled=enabled_flag > 0 and item_id > 0,
                    item_template_id=item_id,
                )
            )
        cfg.expand_results = expands
        break

    for body in _section_bodies(raw, "additional result expand const"):
        values = _parse_doubles(_tokens(body))
        _bind_expand_constants(cfg.expand_results, values)
        break

    return cfg


def _load_config_text() -> str:
    return pvf_runtime.read_text(DISJOINT_ETC_PATH)


def load_system_disjoint(*, force_reload: bool = False) -> DisjointConfig:
    """加载/缓存系统分解配置（共享 PVF）。"""
    global _CONFIG, _CONFIG_SOURCE
    with _LOCK:
        if _CONFIG is not None and not force_reload:
            return _CONFIG
        try:
            text = _load_config_text()
            cfg = parse_disjoint_etc(text)
            cfg.source_path = DISJOINT_ETC_PATH
            cfg.note = "ok"
            _CONFIG = cfg
            _CONFIG_SOURCE = DISJOINT_ETC_PATH
            return cfg
        except Exception as exc:
            cfg = DisjointConfig(
                note=f"load_fail:{type(exc).__name__}:{exc}",
                source_path=DISJOINT_ETC_PATH,
            )
            # 保底 cube 表，避免完全无产物
            cfg.cube_indexes["[no element]"] = [3037]
            cfg.cube_creation_base = 150.0
            cfg.cube_creation_multipliers = [1.0]
            _CONFIG = cfg
            _CONFIG_SOURCE = DISJOINT_ETC_PATH
            return cfg


def warmup_disjoint_config() -> dict[str, Any]:
    """boot warmup 入口。"""
    cfg = load_system_disjoint(force_reload=True)
    return {
        "ok": 1 if cfg.note == "ok" else 0,
        "path": cfg.source_path,
        "note": cfg.note,
        "cube_keys": len(cfg.cube_indexes),
        "cube_base": cfg.cube_creation_base,
        "cube_mul": len(cfg.cube_creation_multipliers),
        "additional_rows": len(cfg.additional_results),
        "additional_const_rows": len(cfg.additional_result_consts),
        "expand_rows": len(cfg.expand_results),
        "no_element_cube": cfg.get_no_element_cube_item_id(),
    }


def clear_disjoint_config_cache() -> None:
    global _CONFIG, _CONFIG_SOURCE
    with _LOCK:
        _CONFIG = None
        _CONFIG_SOURCE = ""


def _clamp_percent(value: float) -> float:
    if value < 0:
        return 0.0
    if value > 100:
        return 100.0
    return float(value)


def _level_divisor_count(level: int, divisor: float, rng: random.Random) -> int:
    if divisor <= 0:
        return 1
    value = max(1, int(level)) / float(divisor)
    count = int(math.floor(value))
    fraction = value - count
    if rng.random() < fraction:
        count += 1
    return max(1, count)


def _add_material(result: list[DisjointMaterial], item_id: int, count: int) -> None:
    if item_id <= 0 or count <= 0:
        return
    for existing in result:
        if existing.item_template_id == item_id:
            existing.count += int(count)
            return
    result.append(
        DisjointMaterial(item_template_id=int(item_id), count=int(count), slot_index=-1)
    )


def calculate_disjoint_materials(
    *,
    sell_gold: int,
    rarity: int,
    minimum_level: int,
    rng: random.Random | None = None,
    config: DisjointConfig | None = None,
) -> list[DisjointMaterial]:
    """按 S4A12 DisjointResultCalculator 计算分解产物。"""
    cfg = config or load_system_disjoint()
    rnd = rng or random.Random()
    result: list[DisjointMaterial] = []

    # 1) cube 数量
    base = cfg.cube_creation_base if cfg.cube_creation_base > 0 else 150.0
    gold = max(1, int(sell_gold or 1))
    mul = cfg.get_cube_creation_multiplier(int(rarity))
    cube_count = int(math.floor(gold * mul / base))
    cube_count = max(1, cube_count)
    _add_material(result, cfg.get_no_element_cube_item_id(), cube_count)

    # 2) additional
    candidates = cfg.get_additional_items(int(rarity))
    if candidates:
        item_id = candidates[0] if len(candidates) == 1 else candidates[rnd.randrange(len(candidates))]
        if item_id > 0:
            const = cfg.get_additional_const(int(rarity))
            if const is None:
                add_count = 1
            else:
                roll = rnd.random() * 100.0
                great = _clamp_percent(const.great_chance_percent)
                divisor = (
                    const.great_count_divisor
                    if roll < great and const.great_count_divisor > 0
                    else const.normal_count_divisor
                )
                if divisor <= 0:
                    add_count = 1
                else:
                    add_count = _level_divisor_count(int(minimum_level), divisor, rnd)
            _add_material(result, int(item_id), int(add_count))

    # 3) expand
    expand = cfg.get_expand_result(int(rarity))
    if expand is not None and expand.enabled and expand.item_template_id > 0:
        if rnd.random() * 100.0 < _clamp_percent(expand.normal_chance_percent):
            exp_count = _level_divisor_count(
                int(minimum_level), expand.level_divisor, rnd
            )
            if rnd.random() * 100.0 < _clamp_percent(expand.great_chance_percent):
                exp_count += 1
            _add_material(result, int(expand.item_template_id), int(exp_count))

    return result


__all__ = [
    "DISJOINT_ETC_PATH",
    "DisjointConfig",
    "DisjointMaterial",
    "DisjointAdditionalConst",
    "DisjointExpandResult",
    "parse_disjoint_etc",
    "load_system_disjoint",
    "warmup_disjoint_config",
    "clear_disjoint_config_cache",
    "calculate_disjoint_materials",
]
