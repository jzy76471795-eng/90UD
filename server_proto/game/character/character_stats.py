"""Combat stats for USERINFO subtype1 (from offline .chr tables).

Game 侧只读离线 JSON，不读 PVF。JSON 由
``pytools/_extract_initial_stats_json.py`` 从各职业 ``.chr`` 抽出：

  [initial value]     -> base（1 级锚点，全职业共用该 job 的初始）
  [growtype 1]        -> **转职前** 每级成长（未转职 / 1~15 级）
  [growtype 2..6]     -> **转职后** 各一转分支每级成长（16~50 级）
  [awakening 1/2]     -> **觉醒后** 在对应 growtype 下的每级成长（51+）

Wire layout (Stage3 ``sub_18CC850`` / S4A12 ``CharacterStatComputer``):
  u32 length = 83
  82B combat vector + 1B stat_level
"""
from __future__ import annotations

import json
import os
import struct
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Mapping

from config import BASE_DIR

DEFAULT_INITIAL_STATS_JSON = BASE_DIR / "data" / "resources" / "initial_stats_by_job.json"

# S4A12 CharacterStatComputer.BuildAdditionalInfo 在成长后叠加的 premium。
# Stage3 本地默认先关；需要时设 USDOF_STAT_PREMIUM_BONUS=1。
DEFAULT_PREMIUM_HP_BONUS = 9800
DEFAULT_PREMIUM_MP_BONUS = 10500
GUILD_MEMBERSHIP_AUTHORITATIVE_ATTRIBUTE_BONUS = 60

# Stage3 renders the USERINFO1 inventory limit in 1/10000 kg units.
DEFAULT_CHARACTER_INVENTORY_LIMIT_BONUS_KG = 90000
DEFAULT_CHARACTER_INVENTORY_LIMIT_BONUS = (
    DEFAULT_CHARACTER_INVENTORY_LIMIT_BONUS_KG * 10000
)

# 等级分段（与 S4A12 ComputeStat 循环变量 i 对齐：i 从 1 累加到 level-1）
#   i=1..14  -> 升到 2..15：转职前表 growtype[1]
#   i=15..49 -> 升到 16..50：转职后表 growtype[first+1]（未转职 first=0 仍用 growtype[1]）
#   i>=50    -> 升到 51+：觉醒表 awakening[first+1][second]（second=0 则继续转职后表）
PRE_CLASS_I_MAX = 14  # inclusive: levels 2..15
POST_CLASS_I_MAX = 49  # inclusive: levels 16..50

STAT_FIELDS = (
    "hp_max",
    "mp_max",
    "phys_atk",
    "phys_def",
    "mag_atk",
    "mag_def",
    "fire_res",
    "water_res",
    "dark_res",
    "light_res",
    "inventory_limit",
    "hp_regen",
    "mp_regen",
    "move_speed",
    "attack_speed",
    "cast_speed",
    "hit_recovery",
    "jump_power",
    "weight",
)

# Stage3 ``char_apply_combat_blob_1CBA240`` consumes exactly 17 signed i16
# values from combat-blob +24 and divides each by ten.  Personality rewards
# use the subset below; the remaining slots stay neutral until their client
# enum has an explicit PVF producer.
STATUS_SLOT_COUNT = 17
STATUS_SLOT_INDEPENDENT_ATTACK = 0
STATUS_SLOT_FIRE_ATTACK = 1
STATUS_SLOT_WATER_ATTACK = 2
STATUS_SLOT_DARK_ATTACK = 3
STATUS_SLOT_LIGHT_ATTACK = 4
STATUS_SLOT_HIT_RATE = 5
STATUS_SLOT_EVASION = 6
STATUS_SLOT_PHYSICAL_CRITICAL = 7
STATUS_SLOT_MAGICAL_CRITICAL = 8


def _status_slots(values: tuple[int, ...] | list[int] | None = None) -> tuple[int, ...]:
    raw = tuple(int(value) for value in (values or ()))
    if len(raw) > STATUS_SLOT_COUNT:
        raise ValueError("combat status slot count exceeds %s" % STATUS_SLOT_COUNT)
    return raw + (0,) * (STATUS_SLOT_COUNT - len(raw))


@dataclass(frozen=True)
class CombatStats:
    hp_max: int = 0
    mp_max: int = 0
    phys_atk: int = 0
    phys_def: int = 0
    mag_atk: int = 0
    mag_def: int = 0
    fire_res: int = 0
    water_res: int = 0
    dark_res: int = 0
    light_res: int = 0
    inventory_limit: int = 0
    hp_regen: int = 0
    mp_regen: int = 0
    move_speed: int = 0
    attack_speed: int = 0
    cast_speed: int = 0
    hit_recovery: int = 0
    jump_power: int = 0
    weight: int = 0
    status_slots: tuple[int, ...] = (0,) * STATUS_SLOT_COUNT

    def add(self, other: "CombatStats") -> "CombatStats":
        values = {
            field: int(getattr(self, field)) + int(getattr(other, field))
            for field in STAT_FIELDS
        }
        values["status_slots"] = tuple(
            int(left) + int(right)
            for left, right in zip(_status_slots(self.status_slots), _status_slots(other.status_slots))
        )
        return CombatStats(**values)

    def with_bonus(self, *, hp: int = 0, mp: int = 0) -> "CombatStats":
        return CombatStats(
            hp_max=int(self.hp_max) + int(hp),
            mp_max=int(self.mp_max) + int(mp),
            phys_atk=self.phys_atk,
            phys_def=self.phys_def,
            mag_atk=self.mag_atk,
            mag_def=self.mag_def,
            fire_res=self.fire_res,
            water_res=self.water_res,
            dark_res=self.dark_res,
            light_res=self.light_res,
            inventory_limit=self.inventory_limit,
            hp_regen=self.hp_regen,
            mp_regen=self.mp_regen,
            move_speed=self.move_speed,
            attack_speed=self.attack_speed,
            cast_speed=self.cast_speed,
            hit_recovery=self.hit_recovery,
            jump_power=self.jump_power,
            weight=self.weight,
            status_slots=_status_slots(self.status_slots),
        )

    def to_dict(self) -> dict[str, int]:
        out = {field: int(getattr(self, field)) for field in STAT_FIELDS}
        for index, value in enumerate(_status_slots(self.status_slots)):
            out["status_slot_%02d" % index] = int(value)
        return out


def apply_personality_statuses(
    stats: CombatStats,
    statuses: Mapping[str, int] | None,
) -> CombatStats:
    """Apply aggregated PVF ``[special reward status]`` values to wire stats.

    Quest PVF values are player-facing units whereas USERINFO1's signed stats
    are tenths.  The four historical ``phys_*``/``mag_*`` fields name their
    old .chr source tags but Stage3 maps them to strength, vitality,
    intelligence, and spirit respectively.
    """
    logical = {str(name).strip().lower(): int(value) for name, value in (statuses or {}).items()}
    known = {
        "physical attack",
        "physical defense",
        "magical attack",
        "magical defense",
        "all elemental resistance",
        "separate attack",
        "all elemental attack",
        "stuck",
        "stuck resistance",
        "physical critical hit",
        "magical critical hit",
        "move speed",
        "hp regen rate",
        "mp regen rate",
    }
    unknown = sorted(name for name in logical if name and name not in known)
    if unknown:
        raise ValueError("unmapped personality status: %s" % ", ".join(unknown))

    result = stats
    slots = list(_status_slots(result.status_slots))

    def add_field(field: str, value: int) -> None:
        nonlocal result
        values = result.to_dict()
        values[field] = int(values[field]) + int(value) * 10
        values["status_slots"] = tuple(slots)
        result = CombatStats(**{name: values[name] for name in STAT_FIELDS}, status_slots=values["status_slots"])

    add_field("phys_atk", logical.get("physical attack", 0))
    add_field("phys_def", logical.get("physical defense", 0))
    add_field("mag_atk", logical.get("magical attack", 0))
    add_field("mag_def", logical.get("magical defense", 0))
    for field in ("fire_res", "water_res", "dark_res", "light_res"):
        add_field(field, logical.get("all elemental resistance", 0))
    add_field("hp_regen", logical.get("hp regen rate", 0))
    add_field("mp_regen", logical.get("mp regen rate", 0))
    add_field("move_speed", logical.get("move speed", 0))

    slots[STATUS_SLOT_INDEPENDENT_ATTACK] += logical.get("separate attack", 0) * 10
    elemental_attack = logical.get("all elemental attack", 0) * 10
    for index in (
        STATUS_SLOT_FIRE_ATTACK,
        STATUS_SLOT_WATER_ATTACK,
        STATUS_SLOT_DARK_ATTACK,
        STATUS_SLOT_LIGHT_ATTACK,
    ):
        slots[index] += elemental_attack
    slots[STATUS_SLOT_HIT_RATE] += logical.get("stuck", 0) * 10
    slots[STATUS_SLOT_EVASION] += logical.get("stuck resistance", 0) * 10
    slots[STATUS_SLOT_PHYSICAL_CRITICAL] += logical.get("physical critical hit", 0) * 10
    slots[STATUS_SLOT_MAGICAL_CRITICAL] += logical.get("magical critical hit", 0) * 10
    return CombatStats(
        **{field: int(getattr(result, field)) for field in STAT_FIELDS},
        status_slots=tuple(slots),
    )


def apply_guild_membership_authoritative_attributes(
    stats: CombatStats,
    membership_active: bool,
) -> CombatStats:
    """Project the server-owned half of the two-layer guild status benefit."""

    if not membership_active:
        return stats
    wire_bonus = GUILD_MEMBERSHIP_AUTHORITATIVE_ATTRIBUTE_BONUS * 10
    return stats.add(
        CombatStats(
            phys_atk=wire_bonus,
            phys_def=wire_bonus,
            mag_atk=wire_bonus,
            mag_def=wire_bonus,
        )
    )


@dataclass(frozen=True)
class GrowthPhaseInfo:
    """一次属性结算使用的成长档位说明（便于日志/调试）。"""

    first: int
    second: int
    pre_class_table: str  # 永远是 growtype[1]
    post_class_table: str  # growtype[first+1]
    awakening_table: str | None  # awakening[first+1][second] 或 None
    classed: bool
    awakened: bool


def resolve_initial_stats_json_path() -> Path | None:
    raw = (os.environ.get("USDOF_INITIAL_STATS_JSON") or "").strip()
    candidates: list[Path] = []
    if raw:
        candidates.append(Path(raw))
    candidates.append(DEFAULT_INITIAL_STATS_JSON)
    for path in candidates:
        if path.exists():
            return path
    return None


def _vector_from_mapping(raw: Mapping[str, object] | None) -> CombatStats | None:
    if not raw:
        return None
    values: dict[str, int] = {}
    for field in STAT_FIELDS:
        try:
            values[field] = int(raw.get(field, 0) or 0)
        except Exception:
            values[field] = 0
    return CombatStats(**values)


@lru_cache(maxsize=1)
def _load_jobs_table() -> dict[str, dict]:
    path = resolve_initial_stats_json_path()
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    jobs_raw = payload.get("jobs") or {}
    out: dict[str, dict] = {}
    for job_key, entry in jobs_raw.items():
        if not isinstance(entry, dict):
            continue
        out[str(int(job_key) & 0xFF)] = entry
    return out


def decode_grow_type(grow_type: int) -> tuple[int, int]:
    """拆 DB/包内 grow 字节。

    S4A12 ``DecodeGrowType``：
      first  = low 4 bit  -> 转职分支索引 0=未转职, 1..5=一转 1..5
      second = high 4 bit -> 觉醒阶段 0=未觉醒, 1=一次觉醒, 2=二次觉醒

    与 ``character_db.patch_userinfo_entry_prefix`` 的
    ``packed = grow_type | (sub_grow_type << 4)`` 一致：
    若 DB 列 ``grow_type`` 只存分支、觉醒在 ``sub_grow_type``，
    请用 ``compute_combat_stats(..., sub_grow_type=...)`` 显式传入。
    """
    value = int(grow_type) & 0xFF
    first = value & 0xF
    second = (value >> 4) & 0xF
    return first, second


def pack_grow_type(first: int, second: int = 0) -> int:
    """打包转职分支 + 觉醒阶段为单字节。"""
    return (int(first) & 0xF) | ((int(second) & 0xF) << 4)


def describe_growth_phase(grow_type: int = 0, *, sub_grow_type: int | None = None) -> GrowthPhaseInfo:
    first, second = _normalize_grow_indices(grow_type, sub_grow_type)
    pre = "growtype[1]"
    post = f"growtype[{first + 1}]"
    awk = f"awakening[{first + 1}][{second}]" if second > 0 else None
    return GrowthPhaseInfo(
        first=first,
        second=second,
        pre_class_table=pre,
        post_class_table=post,
        awakening_table=awk,
        classed=first > 0,
        awakened=second > 0,
    )


def _normalize_grow_indices(
    grow_type: int,
    sub_grow_type: int | None,
) -> tuple[int, int]:
    if sub_grow_type is None:
        first, second = decode_grow_type(grow_type)
    else:
        # DB 分列：grow_type=分支, sub_grow_type=觉醒
        first = int(grow_type) & 0xF
        second = int(sub_grow_type) & 0xF
    if first < 0 or first > 5:
        first = 0
    if second < 0 or second > 2:
        second = 0
    return first, second


def _fallback_stats() -> CombatStats:
    # Mirrors S4A12 BuildFallback (already *10 scaled).
    return CombatStats(
        hp_max=1800,
        mp_max=1400,
        phys_atk=75,
        phys_def=75,
        mag_atk=45,
        mag_def=45,
        inventory_limit=480000,
        mp_regen=500,
        move_speed=8500,
        attack_speed=8500,
        cast_speed=7000,
        hit_recovery=6000,
        jump_power=4300,
        weight=500000,
    )


def _require_growtype(entry: dict, table_index: int) -> CombatStats:
    """取 [growtype N]，N=table_index（1=转职前，2..=转职后分支）。"""
    growtype = entry.get("growtype") or {}
    key = str(int(table_index))
    vec = _vector_from_mapping(growtype.get(key))
    if vec is None:
        raise KeyError(f"missing .chr [growtype {table_index}]")
    return vec


def _require_awakening(entry: dict, table_index: int, awakening: int) -> CombatStats:
    """取 [growtype N] 下的 [awakening K]。"""
    awakening_map = entry.get("awakening") or {}
    by_grow = awakening_map.get(str(int(table_index))) or {}
    vec = _vector_from_mapping(by_grow.get(str(int(awakening))))
    if vec is None:
        raise KeyError(f"missing .chr [growtype {table_index}] [awakening {awakening}]")
    return vec


def _select_growth_tables(
    entry: dict,
    first: int,
    second: int,
) -> tuple[CombatStats, CombatStats, CombatStats]:
    """按转职/觉醒状态选出三段每级成长向量。

    返回 (pre_class, post_class, post_awakening)：
      pre_class      = growtype[1]           # 未转职档，1~15
      post_class     = growtype[first+1]     # 转职档；first=0 时等同 growtype[1]
      post_awakening = awakening[...] 或 post_class
    """
    # 转职前：永远用 growtype 1（.chr 注释/结构上的“基础成长”）
    pre_class = _require_growtype(entry, 1)

    # 转职后：用对应分支 growtype[first+1]
    # first=0 未转职 -> growtype[1]，与 pre 相同（15 级后仍按未转职成长）
    post_index = int(first) + 1
    try:
        post_class = _require_growtype(entry, post_index)
    except KeyError:
        # 缺分支表时严禁静默全 0；回退到转职前表并保证能算出基础面板
        post_class = pre_class

    # 觉醒后：仅 second>0 时切 awakening；否则继续转职后表
    if second > 0:
        try:
            post_awakening = _require_awakening(entry, post_index, second)
        except KeyError:
            post_awakening = post_class
    else:
        post_awakening = post_class

    return pre_class, post_class, post_awakening


def compute_combat_stats(
    job: int,
    level: int = 1,
    grow_type: int = 0,
    *,
    sub_grow_type: int | None = None,
    apply_premium_bonus: bool | None = None,
) -> CombatStats:
    """按 .chr 规则累计战斗属性。

    参数
    ----
    job:
        character.lst 职业 id（决定读哪份 .chr / JSON 条目）。
    level:
        角色等级。
    grow_type:
        - 若 ``sub_grow_type is None``：按打包字节解码
          ``first=low4, second=high4``（兼容 S4A12 / 已打包缓存）。
        - 若传入 ``sub_grow_type``：``grow_type`` 只表示转职分支，
          ``sub_grow_type`` 表示觉醒阶段（匹配 DB 分列语义）。
    sub_grow_type:
        可选觉醒阶段 0/1/2。

    累计规则（S4A12 ``ComputeStat``，与 .chr 段一一对应）
    ------------------------------------------------
    level == 1:
        仅 ``[initial value]``（base），**不加**任何成长。

    升到 2..15（循环 i=1..14）:
        **转职前** 每级 + ``[growtype 1]``
        无论是否已转职，这一段都用基础成长表。

    升到 16..50（循环 i=15..49）:
        **转职后** 每级 + ``[growtype first+1]``
        - 未转职 first=0 -> 仍用 ``[growtype 1]``
        - 一转分支 first=1..5 -> ``[growtype 2..6]``

    升到 51+（循环 i>=50）:
        **觉醒后** 每级 + ``[growtype first+1][awakening second]``
        - second=0 未觉醒 -> 继续用转职后 ``[growtype first+1]``
        - second=1/2 -> 对应一次/二次觉醒成长表
    """
    job_u8 = int(job) & 0xFF
    level_i = max(1, int(level))
    first, second = _normalize_grow_indices(grow_type, sub_grow_type)

    entry = _load_jobs_table().get(str(job_u8))
    if not entry:
        stats = _fallback_stats()
    else:
        base = _vector_from_mapping(entry.get("base")) or _fallback_stats()
        stats = base
        if level_i > 1:
            pre_class, post_class, post_awakening = _select_growth_tables(entry, first, second)
            for i in range(1, level_i):
                if i <= PRE_CLASS_I_MAX:
                    # 2..15：转职前
                    stats = stats.add(pre_class)
                elif i <= POST_CLASS_I_MAX:
                    # 16..50：转职后（未转职则仍是 growtype1）
                    stats = stats.add(post_class)
                else:
                    # 51+：觉醒后（未觉醒则继续转职后表）
                    stats = stats.add(post_awakening)

    if apply_premium_bonus is None:
        raw = (os.environ.get("USDOF_STAT_PREMIUM_BONUS") or "").strip().lower()
        apply_premium_bonus = raw in {"1", "true", "yes", "on", "y"}
    if apply_premium_bonus:
        stats = stats.with_bonus(hp=DEFAULT_PREMIUM_HP_BONUS, mp=DEFAULT_PREMIUM_MP_BONUS)
    return replace(
        stats,
        inventory_limit=int(stats.inventory_limit)
        + DEFAULT_CHARACTER_INVENTORY_LIMIT_BONUS,
    )


def build_additional_info_blob(
    stats: CombatStats,
    *,
    stat_level: int = 100,
    include_stat_level: bool = True,
) -> bytes:
    """82B combat fields, optional trailing u8 stat_level -> 83B body for length prefix.

    Layout (little-endian), offsets match S4A12 BuildAdditionalInfo:
      +0  u32 hp_max
      +4  u32 mp_max
      +8  i16 phys_atk
      +10 i16 phys_def
      +12 i16 mag_atk
      +14 i16 mag_def
      +16 i16 fire_res
      +18 i16 water_res
      +20 i16 dark_res
      +22 i16 light_res
      +24 17 * i16 permanent/status stats
      +58 u32 inventory_limit
      +62 u16 hp_regen
      +64 u16 mp_regen
      +66 u32 move_speed
      +70 u16 attack_speed
      +72 u16 cast_speed
      +74 u16 hit_recovery
      +76 u16 jump_power
      +78 u32 weight
      (=82B)
      +82 u8  stat_level  (when include_stat_level；面板锚，非角色等级)
    """
    body = bytearray()
    body += struct.pack("<I", int(stats.hp_max) & 0xFFFFFFFF)
    body += struct.pack("<I", int(stats.mp_max) & 0xFFFFFFFF)
    body += struct.pack("<h", int(stats.phys_atk))
    body += struct.pack("<h", int(stats.phys_def))
    body += struct.pack("<h", int(stats.mag_atk))
    body += struct.pack("<h", int(stats.mag_def))
    body += struct.pack("<h", int(stats.fire_res))
    body += struct.pack("<h", int(stats.water_res))
    body += struct.pack("<h", int(stats.dark_res))
    body += struct.pack("<h", int(stats.light_res))
    for value in _status_slots(stats.status_slots):
        body += struct.pack("<h", int(value))
    body += struct.pack("<I", int(stats.inventory_limit) & 0xFFFFFFFF)
    body += struct.pack("<H", int(stats.hp_regen) & 0xFFFF)
    body += struct.pack("<H", int(stats.mp_regen) & 0xFFFF)
    body += struct.pack("<I", int(stats.move_speed) & 0xFFFFFFFF)
    body += struct.pack("<H", int(stats.attack_speed) & 0xFFFF)
    body += struct.pack("<H", int(stats.cast_speed) & 0xFFFF)
    body += struct.pack("<H", int(stats.hit_recovery) & 0xFFFF)
    body += struct.pack("<H", int(stats.jump_power) & 0xFFFF)
    body += struct.pack("<I", int(stats.weight) & 0xFFFFFFFF)
    if len(body) != 82:
        raise ValueError(f"combat blob must be 82B, got {len(body)}")
    if include_stat_level:
        body += bytes((int(stat_level) & 0xFF,))
    return bytes(body)


def build_additional_info_for_character(
    job: int,
    level: int = 1,
    grow_type: int = 0,
    *,
    sub_grow_type: int | None = None,
    stat_level: int = 100,
    apply_premium_bonus: bool | None = None,
    personality_statuses: Mapping[str, int] | None = None,
    guild_membership_active: bool = False,
) -> tuple[CombatStats, bytes]:
    stats = compute_combat_stats(
        job,
        level,
        grow_type,
        sub_grow_type=sub_grow_type,
        apply_premium_bonus=apply_premium_bonus,
    )
    stats = apply_personality_statuses(stats, personality_statuses)
    stats = apply_guild_membership_authoritative_attributes(
        stats,
        guild_membership_active,
    )
    blob = build_additional_info_blob(stats, stat_level=stat_level, include_stat_level=True)
    return stats, blob
