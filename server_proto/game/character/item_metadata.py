"""PVF-backed item metadata for inventory grant slot/stack rules.

Game grant paths need equipment vs stackable classification and the Stage3
main-bag slot bands.  This module is a focused metadata helper (not a full
inventory system): resolve kind + stackable type + stack limit, then map to
slot ranges used by the client UI tabs.
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from config import BASE_DIR, PVF_PATH
from pvf import pvf_runtime
from pvf.nkpi_reader import PvfReader


DEFAULT_ITEM_PVF = BASE_DIR / "data" / "pvf" / "USDOF_client_script_readable_20260705.pvf"

# Main bag (list_type=0) physical slot bands.
# Stage3 IDA sub_1B676B0 / sub_1B67800（2026-07-19）真值；S4 旧值仅作对照。
# 快捷消耗栏（角色 HUD）：QuickSlotStart/End = 3..8
SLOT_RANGE_QUICK = (3, 8)
SLOT_RANGE_EQUIPMENT = (9, 64)
SLOT_RANGE_CONSUMABLE = (65, 120)
SLOT_RANGE_MATERIAL = (121, 176)
SLOT_RANGE_QUEST = (177, 232)
SLOT_RANGE_MATERIAL_EXPERT = (233, 288)
# Stage3：徽章带扩到 353（S4 仅 289..344）
SLOT_RANGE_AVATAR_EMBLEM = (289, 353)
# Stage3 特殊材料区 354..366（S4 旧 material_ext 345..359）
# 区内：354..359 小晶体固定槽；360..365 六级账号灵魂投影。
SLOT_RANGE_MATERIAL_EXT = (354, 366)
SLOT_RANGE_MATERIAL_SPECIAL = SLOT_RANGE_MATERIAL_EXT
SLOT_RANGE_CUBE_FRAGMENT = (354, 359)
SLOT_RANGE_SOUL_MATERIAL = (360, 365)

# Server inventory policy for an explicit PVF [stack limit] other than 1.
SERVER_STACK_LIMIT_OVERRIDE = 99_999
GOLD_CUBE_FRAGMENT_ID = 3262

# Pet inventory (list_type=7) physical slot bands.  Stage3 creates one
# 238-slot container; the three UI pages share it without a list_param16.
SLOT_RANGE_PET_CREATURE = (0, 139)
SLOT_RANGE_PET_ARTIFACT = (140, 188)
SLOT_RANGE_PET_CONSUMABLE = (189, 237)

# Guild medal inventory. Stage3 stores both pages in one internal list36
# container, while S0/000D selects its full-list reader with discriminator 39.
GUILD_MEDAL_INVENTORY_LIST_TYPE = 36
GUILD_MEDAL_FULL_WIRE_LIST_TYPE = 39
GUILD_MEDAL_EQUIP_SLOT = 31
GUILD_MEDAL_INVENTORY_CAPACITY = 98
SLOT_RANGE_GUILD_MEDAL = (0, 48)
SLOT_RANGE_GUARDIAN_GEM = (49, 97)

CREATURE_ARTIFACT_COLOR_BY_TYPE: dict[str, str] = {
    "[artifact red]": "red",
    "[artifact blue]": "blue",
    "[artifact green]": "green",
}

AVATAR_SOCKET_TYPE_BY_LABEL: dict[str, int] = {
    "A": 0x0001,
    "B": 0x0002,
    "C": 0x0004,
    "D": 0x0008,
    "S": 0x0010,
    "M": 0xFFEF,
}

# Older ordinary-avatar templates omit both socket sections. Their equipment
# position still determines the native Stage3 socket family unambiguously.
AVATAR_SOCKET_FALLBACK_BY_EQUIPMENT_TYPE: dict[str, tuple[int, ...]] = {
    "[hat avatar]": (0x0001, 0x0001),
    "[hair avatar]": (0x0001, 0x0001),
    "[face avatar]": (0x0002, 0x0002),
    "[breast avatar]": (0x0002, 0x0002),
    "[coat avatar]": (0x0004, 0x0004),
    "[pants avatar]": (0x0004, 0x0004),
    "[shoes avatar]": (0x0008, 0x0008),
    "[waist avatar]": (0x0008, 0x0008),
    "[skin avatar]": (0xFFEF, 0xFFEF),
}

# Wear slots used by Stage3 USERINFO / create-equipment list.
AURA_AVATAR_EQUIPMENT_TYPE = "[aurora avatar]"
AURA_ATTRIBUTE_EQUIP_SLOT = 9
AURA_SKIN_EQUIP_SLOT = 11

EQUIPMENT_TYPE_TO_SLOT: dict[str, int] = {
    # Stage3 avatar appearance slots.  C1/0013 live confirms the client sends
    # Avatar(list1) -> Equipment(list3) with these 0..10 destination slots.
    "[hat avatar]": 0,
    "[hair avatar]": 1,
    "[face avatar]": 2,
    "[coat avatar]": 3,
    "[pants avatar]": 4,
    "[shoes avatar]": 5,
    "[breast avatar]": 6,
    "[waist avatar]": 7,
    "[skin avatar]": 8,
    AURA_AVATAR_EQUIPMENT_TYPE: AURA_ATTRIBUTE_EQUIP_SLOT,
    "[weapon avatar]": 10,
    "[weapon]": 12,
    "[title]": 13,
    # Stage3 / S4A12 脚本真值多为 [title name]
    "[title name]": 13,
    "[coat]": 14,
    "[shoulder]": 15,
    "[pants]": 16,
    "[shoes]": 17,
    "[waist]": 18,
    "[amulet]": 19,
    "[wrist]": 20,
    "[ring]": 21,
    "[support]": 22,
    "[magic stone]": 23,
    # 副手/盾：仅守护者(job12)与女鬼剑剑豪等有 [support weapon] 的角色使用。
    # Stage3 槽序与 90qiyuan current-EXE 一致：support=22 / magic stone=23 /
    # support weapon=24 / earring=25。禁止与 [support] 共用 22（2026-07-20
    # 误写 slot22 → 进赛丽亚 USERINFO0/000E 闪退）。
    "[support weapon]": 24,
    "[earring]": 25,
    # Stage3 live C1/0013 (2026-07-25): pet inventory equipment uses the
    # dedicated list3 tail slots.  Classification remains PVF-type based, so
    # newly added pets/artifacts inherit the correct slot without item-id maps.
    "[creature]": 26,
    "[artifact red]": 27,
    "[artifact blue]": 28,
    "[artifact green]": 29,
    # Stage3 guild medal equipment slot. The unequipped medal lives in the
    # dedicated list36 page rather than Main/list0.
    "[flag]": GUILD_MEDAL_EQUIP_SLOT,
}

# The aura-skin slot accepts the same PVF equipment type as the ordinary aura
# slot. The destination slot, rather than a second template type, selects
# attribute-bearing aura (9) versus display-only aura skin (11).
EQUIPMENT_TYPE_TO_COMPATIBLE_SLOTS: dict[str, tuple[int, ...]] = {
    AURA_AVATAR_EQUIPMENT_TYPE: (
        AURA_ATTRIBUTE_EQUIP_SLOT,
        AURA_SKIN_EQUIP_SLOT,
    ),
}

# Character job labels (index == characters.job).
# 与 character/character.lst 0..14 对齐；缺项会导致 can_equip 对武器
# 报 job_blocked（衣服常为 [all] 不受影响）。2026-07-19：补 13/14。
CHARACTER_JOB_LABELS: tuple[str, ...] = (
    "swordman",          # 0  Swordman
    "fighter",           # 1  Fighter
    "gunner",            # 2  Gunner
    "mage",              # 3  Mage
    "priest",            # 4  Priest
    "at gunner",         # 5  ATGunner
    "thief",             # 6  Thief
    "at fighter",        # 7  ATFighter
    "at mage",           # 8  ATMage
    "demonic swordman",  # 9  Dark Knight / DSSwordman
    "creator mage",      # 10 CreatorMage
    "at swordman",       # 11 ATSwordman
    "knight",            # 12 Knight
    "demonic lancer",    # 13 DemonicLancer
    "at priest",         # 14 ATPriest
)

_LOCK = threading.RLock()
_READER: PvfReader | None = None
_READER_PATH: str | None = None
_EQUIP_MAP: dict[int, str] | None = None
_STACK_MAP: dict[int, str] | None = None
_EQUIP_MAP_PATH: str | None = None
_STACK_MAP_PATH: str | None = None
_PRESET_METADATA: dict[int, "ItemMetadata"] = {}
_RESOLVED_METADATA: dict[int, "ItemMetadata"] = {}


# PVF [attach type] → 模板交易规则。普通装备另按原生角色绑定枚举处理。
_TRADABLE_ATTACH_TYPES: frozenset[str] = frozenset(
    {
        "free",
        "trade",
        "trade limit",
    }
)
_NON_TRADABLE_ATTACH_TYPES: frozenset[str] = frozenset(
    {
        "sealing",
        "account",
        "character",
        "trade delete",
    }
)


@dataclass(frozen=True)
class ItemMetadata:
    item_id: int
    item_kind: str  # equipment | stackable | special | unknown
    stackable_type: str = ""
    stack_limit: int = 1
    equipment_type: str = ""
    is_avatar: bool = False
    durability: int = 0
    minimum_level: int = 0
    usable_jobs: tuple[str, ...] = ()
    suitable_jobs: tuple[str, ...] = ()
    usable_period_days: int = 0
    expiration_timestamp: int = 0
    daily_delete: bool = False
    attach_type: str = ""  # PVF [attach type] 去括号小写：free/trade/sealing/...
    name: str = ""
    source_path: str = ""
    note: str = ""
    # PVF [price] / [value]；NPC 出售用 sell_gold（对照 S4A12 ItemMetadataResolver）
    buy_price: int = 0
    sell_value: int = 0
    # PVF [need material] id count — 材料兑换商店
    need_material_id: int = 0
    need_material_count: int = 0
    # PVF [rarity]：0 普通 … 4 史诗 …（分解 cube/附加材料表索引）
    rarity: int = 0
    # PVF [set item]：装备/装扮所属套装关联 ID。空元组表示散件。
    set_item_ids: tuple[int, ...] = ()
    # PVF [impossible contents] 标签列表（小写去括号，如 disjoint）
    impossible_contents: tuple[str, ...] = ()
    # PVF [item category] 标签列表。克隆装扮以 clear avatar 动态识别，
    # 不依赖模板 ID、名称或当前 PVF 中已有的克隆套装。
    item_categories: tuple[str, ...] = ()
    # PVF [grade] is distinct from [minimum level]. Reinforcement destruction
    # compensation uses this corrected equipment grade.
    grade: int = 0
    # PVF [avatar select ability change]: target job/type, target grade, mode.
    # Empty scope means the stackable is not an avatar-option adjustment item.
    avatar_option_change_scope: tuple[str, str] = ()
    avatar_option_change_grade: int = 0
    avatar_option_change_mode: int = 0
    # PVF [avatar emblem target type] projected as Stage3 socket bit masks.
    avatar_emblem_target_types: tuple[int, ...] = ()
    # Frozen Stage3 avatar instance blob contains five packed {u16 type,u32 id}
    # slots. Empty means this template has no proven socket eligibility.
    avatar_socket_types: tuple[int, ...] = ()
    avatar_socket_source: str = ""
    # Aiken equipment instance identity and shape. The option pool itself is
    # parsed once by customability_index during hard warmup.
    custom_ability_type: str = ""
    custom_ability_count: int = 0
    custom_ability_part_set_index: int = 0
    # Armor material/family discriminator used by Aiken part-set assignment.
    # Jewelry and special equipment omit the field and remain -1.
    equipment_sub_type: int = -1
    # Creature eggs point at the resulting creature through PVF
    # ``[output index]``.  Zero means this is an ordinary creature.
    output_index: int = 0
    # Stackable [action type]. The token is normalized but keeps brackets;
    # following integers remain ordered so consumers can honor PVF parameters.
    action_type: str = ""
    action_values: tuple[int, ...] = ()
    # An explicit template contribution, not total character exorcism.
    # Missing/unreadable/malformed tags stay unknown; never fill a join profile.
    anti_evil: int | None = None

    @property
    def is_equipment(self) -> bool:
        return self.item_kind == "equipment"

    @property
    def is_stackable(self) -> bool:
        return self.item_kind == "stackable"

    @property
    def is_customability_equipment(self) -> bool:
        return bool(
            self.is_equipment
            and _normalize_type_token(self.custom_ability_type).strip("[]")
            == "warriors dungeon"
        )

    @property
    def is_clone_avatar(self) -> bool:
        return bool(self.is_avatar and "clear avatar" in self.item_categories)

    @property
    def is_creature(self) -> bool:
        return bool(
            self.is_equipment
            and _normalize_equipment_type(self.equipment_type) == "[creature]"
        )

    @property
    def creature_artifact_color(self) -> str:
        if not self.is_equipment:
            return ""
        equipment_type = _normalize_equipment_type(self.equipment_type)
        return CREATURE_ARTIFACT_COLOR_BY_TYPE.get(equipment_type, "")

    @property
    def is_creature_artifact(self) -> bool:
        return bool(self.creature_artifact_color)

    @property
    def is_creature_consumable(self) -> bool:
        if not self.is_stackable:
            return False
        stackable_type = _normalize_type_token(self.stackable_type)
        return stackable_type.startswith("[creature]") or stackable_type.startswith(
            "[feed]"
        )

    @property
    def is_pet_inventory_item(self) -> bool:
        return bool(
            self.is_creature
            or self.is_creature_artifact
            or self.is_creature_consumable
        )

    @property
    def is_guild_medal(self) -> bool:
        return bool(
            self.is_equipment
            and _normalize_equipment_type(self.equipment_type) == "[flag]"
        )

    @property
    def is_guardian_gem(self) -> bool:
        return bool(
            self.is_stackable
            and _normalize_type_token(self.stackable_type).startswith("[flag gem]")
        )

    @property
    def is_guild_medal_inventory_item(self) -> bool:
        return bool(self.is_guild_medal or self.is_guardian_gem)

    def guild_medal_slot_range(self) -> tuple[int, int] | None:
        if self.is_guild_medal:
            return SLOT_RANGE_GUILD_MEDAL
        if self.is_guardian_gem:
            return SLOT_RANGE_GUARDIAN_GEM
        return None

    def inventory_list_type(self) -> int:
        """Return the persisted Stage3 inventory container for this template."""
        if self.is_pet_inventory_item:
            return 7
        if self.is_guild_medal_inventory_item:
            return GUILD_MEDAL_INVENTORY_LIST_TYPE
        if self.is_avatar:
            return 1
        return 0

    def pet_slot_range(self) -> tuple[int, int] | None:
        if self.is_creature:
            return SLOT_RANGE_PET_CREATURE
        if self.is_creature_artifact:
            return SLOT_RANGE_PET_ARTIFACT
        if self.is_creature_consumable:
            return SLOT_RANGE_PET_CONSUMABLE
        return None

    @property
    def buy_gold(self) -> int:
        """NPC金币单价：兑换配方仅取[price]，普通金币商品可回退[value]。"""
        price = max(0, int(self.buy_price or 0))
        value = max(0, int(self.sell_value or 0))
        # [value] is the item's value, not an additional hidden gold cost for
        # a [need material] recipe. An explicit [price] still charges gold.
        if price > 0 or self.is_material_exchange:
            return price
        return value

    @property
    def is_material_exchange(self) -> bool:
        return int(self.need_material_id or 0) > 0 and int(self.need_material_count or 0) > 0

    @property
    def sell_gold(self) -> int:
        """NPC 出售单价（金币）。0 表示脚本无价也按 1 金保底，避免卖了不加钱。"""
        value = max(0, int(self.sell_value or 0))
        price = max(0, int(self.buy_price or 0))
        if self.is_equipment:
            base = value if value > 0 else price
            # S4: base * SellRates.Equipment / 1000；缺表时用 20% 近似
            if base <= 0:
                return 1
            return max(1, (base * 200) // 1000)
        # stackable / special：S4 用 value/5 或 price/5
        if value > 0:
            return max(0, value // 5)
        if price > 0:
            return max(0, price // 5)
        return 1

    @property
    def allows_stack_merge(self) -> bool:
        # True when multiple counts may share one bag slot.
        if self.is_equipment:
            return False
        return int(self.effective_stack_limit) != 1

    @property
    def effective_stack_limit(self) -> int:
        """Return 0 for unlimited stacks or the normalized numeric cap."""
        return max(0, int(self.stack_limit or 0))

    @property
    def is_quest_item(self) -> bool:
        st = _normalize_type_token(self.stackable_type)
        return st.startswith("[quest")

    def normalized_attach_type(self) -> str:
        text = _strip_tick(str(self.attach_type or "")).strip().lower()
        if text.startswith("[") and text.endswith("]") and len(text) >= 2:
            text = text[1:-1].strip()
        return " ".join(text.split())

    def is_tradable_template(self) -> bool:
        """模板层是否可交易（不含实例封印）。

        规则：
        - 任务道具 → 不可交易
        - 装备的原生 trade 表示角色绑定；堆叠物品继续按既有交易规则处理
        - attach type ∈ sealing/account/character/trade delete → 不可交易
        - 空/未知：非任务 equipment/stackable 默认按可交易（常见掉落物）
        """
        if self.is_quest_item:
            return False
        attach = self.normalized_attach_type()
        if self.is_equipment and attach == "trade":
            return False
        if attach in _NON_TRADABLE_ATTACH_TYPES:
            return False
        if attach in _TRADABLE_ATTACH_TYPES:
            return True
        # 空 attach：装备/材料默认允许落地；unknown/special 保守销毁
        if not attach and self.item_kind in {"equipment", "stackable"}:
            return True
        return False

    def is_tradable_instance(self, seal_flag: int = 0) -> bool:
        """Return whether the current instance state permits trading."""
        attach = self.normalized_attach_type()
        if attach == "sealing":
            return bool(self.is_equipment and int(seal_flag or 0) != 0)
        if int(seal_flag or 0) != 0:
            return False
        return self.is_tradable_template()

    def equipment_slot(self) -> int | None:
        if not self.is_equipment:
            return None
        key = _normalize_equipment_type(self.equipment_type)
        return EQUIPMENT_TYPE_TO_SLOT.get(key)

    def compatible_equipment_slots(self) -> tuple[int, ...]:
        """Return every Stage3 wear slot that accepts this template type."""
        primary = self.equipment_slot()
        if primary is None:
            return ()
        key = _normalize_equipment_type(self.equipment_type)
        return EQUIPMENT_TYPE_TO_COMPATIBLE_SLOTS.get(key, (int(primary),))

    def is_job_allowed(self, job: int) -> bool:
        labels = tuple(_normalize_job_label(x) for x in self.usable_jobs if str(x).strip())
        if not labels:
            return True
        if any(label == "all" for label in labels):
            return True
        job_label = job_label_for_id(job)
        if not job_label:
            return False
        return job_label in labels

    def is_purchase_job_allowed(self, job: int) -> bool:
        """Apply both hard usable-job and cash-item suitable-job filters."""
        if not self.is_job_allowed(int(job)):
            return False
        labels = tuple(
            _normalize_job_label(x)
            for x in self.suitable_jobs
            if str(x).strip()
        )
        if not labels or any(label == "all" for label in labels):
            return True
        job_label = job_label_for_id(int(job))
        return bool(job_label and job_label in labels)

    def is_level_allowed(self, level: int) -> bool:
        need = max(0, int(self.minimum_level or 0))
        if need <= 0:
            return True
        return int(level) >= need

    def can_equip(self, *, job: int, level: int, equip_slot: int | None = None) -> tuple[bool, str]:
        if not self.is_equipment:
            return False, "not_equipment"
        if not self.is_job_allowed(int(job)):
            return False, f"job_blocked job={int(job)} allow={list(self.usable_jobs)}"
        if not self.is_level_allowed(int(level)):
            return False, f"level_blocked level={int(level)} need>={int(self.minimum_level)}"
        expected = self.equipment_slot()
        compatible = self.compatible_equipment_slots()
        if equip_slot is not None and expected is not None and int(equip_slot) not in compatible:
            # Allow dual-ring style only when expected is ring and slot is ring.
            if not (expected == 21 and int(equip_slot) == 21):
                expected_text = ",".join(str(int(slot)) for slot in compatible)
                return False, f"slot_mismatch slot={int(equip_slot)} expect={expected_text}"
        return True, "ok"

    def slot_range(self) -> tuple[int, int]:
        guild_range = self.guild_medal_slot_range()
        if guild_range is not None:
            return guild_range
        pet_range = self.pet_slot_range()
        if pet_range is not None:
            return pet_range
        if self.is_equipment:
            return SLOT_RANGE_EQUIPMENT
        # Stage3 小晶体 / 灵魂固定带（优先于 stackable type 文本）
        try:
            from game.chain_loader import load_chain

            _hs = load_chain("helper-cube_soul_slots.py")
            if _hs.is_cube_fragment(int(self.item_id)):
                special = _hs.material_special_slot_range_for_item(
                    int(self.item_id), meta=self
                )
                if special is not None:
                    return special
            if _hs.is_soul_material(int(self.item_id), meta=self):
                return SLOT_RANGE_SOUL_MATERIAL
        except Exception:
            pass
        st = _normalize_type_token(self.stackable_type)
        if st.startswith("[material expert job]"):
            return SLOT_RANGE_MATERIAL_EXPERT
        if st.startswith("[material]"):
            # The special band is now virtual-account-only.  Other material
            # extensions use the regular material tab instead of colliding
            # with account cube/Soul projections.
            if st.endswith("4") or st.rstrip().endswith("]4"):
                return SLOT_RANGE_MATERIAL
            trailing = st[len("[material]") :].strip()
            if trailing.startswith("4"):
                return SLOT_RANGE_MATERIAL_EXT
            return SLOT_RANGE_MATERIAL
        if st.startswith("[quest"):
            return SLOT_RANGE_QUEST
        if st.startswith("[avatar emblem]"):
            return SLOT_RANGE_AVATAR_EMBLEM
        return SLOT_RANGE_CONSUMABLE

    def db_item_kind(self) -> str:
        if self.is_pet_inventory_item:
            return "pet"
        if self.item_kind in {"equipment", "stackable", "avatar", "pet", "special"}:
            return self.item_kind
        if self.is_equipment:
            return "equipment"
        if self.is_stackable:
            return "stackable"
        return "special"


def _candidate_pvf_paths() -> list[Path]:
    raws = [
        os.environ.get("USDOF_ITEM_PVF"),
        os.environ.get("USDOF_QUEST_PVF"),
        os.environ.get("USDOF_CLIENT_PVF_READABLE"),
        str(DEFAULT_ITEM_PVF),
        os.environ.get("USDOF_PVF_PATH"),
        PVF_PATH,
    ]
    out: list[Path] = []
    for raw in raws:
        if not raw:
            continue
        for part in str(raw).split(";"):
            p = Path(part.strip())
            if str(p) and p not in out:
                out.append(p)
    return out


def _resolve_pvf_path() -> Path:
    for path in _candidate_pvf_paths():
        if path.exists():
            return path.resolve()
    raise FileNotFoundError("item metadata PVF not found in candidates")


def _get_reader() -> PvfReader:
    """Return the process-shared preloaded PVF reader only.

    Runtime hard rule (AGENTS 4.1.2): never open a private PvfReader for
    item metadata. Server start must preload Script.pvf first.
    """
    global _READER, _READER_PATH
    target = str(_resolve_pvf_path())
    with _LOCK:
        shared = pvf_runtime.get_shared_reader(target)
        if shared is None or not pvf_runtime.is_memory_prepared(target):
            # Force the shared memory path; do not fall back to a private open.
            shared = pvf_runtime.ensure_shared_reader(target)
        if shared is None or not pvf_runtime.is_memory_prepared(target):
            raise RuntimeError(f"shared memory PVF unavailable: {target}")
        _READER = shared
        _READER_PATH = str(Path(target).resolve())
        return _READER


def _strip_tick(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("`") and text.endswith("`") and len(text) >= 2:
        return text[1:-1].strip()
    return text


def _normalize_type_token(value: str) -> str:
    text = _strip_tick(value).strip().lower()
    text = text.replace("`", "")
    return " ".join(text.split())


def _parse_lst_id_path_map(text: str) -> dict[int, str]:
    # Parse PVF *.lst id/`path` pairs (id and path often on consecutive lines).
    pairs = re.findall(r"(?m)^(\d+)\s*(?:\r?\n|\s+)`([^`]+)`", text)
    out: dict[int, str] = {}
    for raw_id, rel in pairs:
        try:
            iid = int(raw_id)
        except ValueError:
            continue
        path = rel.replace("\\", "/").strip().lstrip("/")
        out[iid] = path
    return out


def _load_equipment_map(reader: PvfReader) -> dict[int, str]:
    global _EQUIP_MAP, _EQUIP_MAP_PATH
    target = str(getattr(reader, "path", "") or _READER_PATH or "")
    with _LOCK:
        if _EQUIP_MAP is not None and _EQUIP_MAP_PATH == target:
            return _EQUIP_MAP
        text = reader.read_text("equipment/equipment.lst")
        _EQUIP_MAP = _parse_lst_id_path_map(text)
        _EQUIP_MAP_PATH = target
        return _EQUIP_MAP


def _load_stackable_map(reader: PvfReader) -> dict[int, str]:
    global _STACK_MAP, _STACK_MAP_PATH
    target = str(getattr(reader, "path", "") or _READER_PATH or "")
    with _LOCK:
        if _STACK_MAP is not None and _STACK_MAP_PATH == target:
            return _STACK_MAP
        text = reader.read_text("stackable/stackable.lst")
        _STACK_MAP = _parse_lst_id_path_map(text)
        _STACK_MAP_PATH = target
        return _STACK_MAP


def _section_body(text: str, key: str) -> str:
    # Return raw body text after [key] until next [tag].
    pattern = re.compile(
        rf"\[{re.escape(key)}\]\s*(.*?)(?=\n\s*\[[^\]]+\]|\Z)",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        return ""
    return match.group(1).strip()


def _first_tick_or_token(body: str) -> str:
    if not body:
        return ""
    tick = re.search(r"`([^`]*)`", body)
    if tick:
        return tick.group(1).strip()
    for line in body.splitlines():
        token = line.strip()
        if not token:
            continue
        if token.startswith("["):
            return token.strip("`")
        return token.strip("`")
    return ""


def _extract_stackable_type(text: str) -> str:
    body = _section_body(text, "stackable type")
    if not body:
        return ""
    # Common form:
    #   [stackable type]
    #   `[waste]`
    #   4
    tick = re.search(r"`([^`]*)`", body)
    if tick:
        base = tick.group(1).strip()
        rest = body[tick.end() :]
        num = re.search(r"(?m)^\s*(\d+)\s*$", rest)
        if num:
            return f"{base} {num.group(1)}".strip()
        return base
    return _first_tick_or_token(body)


def _extract_anti_evil(text: str) -> int | None:
    tags = re.findall(r"(?mi)^\s*\[anti evil\]\s*$", text)
    if len(tags) != 1:
        return None
    value = _section_body(text, "anti evil")
    if not re.fullmatch(r"\d+", value):
        return None
    return int(value)


def _extract_int_field(text: str, key: str) -> int | None:
    body = _section_body(text, key)
    if not body:
        return None
    match = re.search(r"(?m)^\s*(-?\d+)\s*$", body)
    if match:
        return int(match.group(1))
    match = re.search(r"(-?\d+)", body)
    if match:
        return int(match.group(1))
    return None


def _extract_name(text: str) -> str:
    body = _section_body(text, "name")
    return _first_tick_or_token(body)


def _extract_action_rule(text: str) -> tuple[str, tuple[int, ...]]:
    body = _section_body(text, "action type")
    if not body:
        return "", ()
    tokens = tuple(
        _normalize_type_token(value)
        for value in re.findall(r"`([^`]*)`", body)
        if _normalize_type_token(value)
    )
    action_type = tokens[0] if len(tokens) == 1 else ""
    values = tuple(int(value) for value in re.findall(r"-?\d+", body))
    return action_type, values


def job_label_for_id(job: int) -> str:
    idx = int(job)
    if 0 <= idx < len(CHARACTER_JOB_LABELS):
        return CHARACTER_JOB_LABELS[idx]
    return ""


def _normalize_job_label(value: str) -> str:
    text = _strip_tick(str(value or "")).strip().lower()
    text = text.replace("`", "")
    if text.startswith("[") and text.endswith("]") and len(text) >= 2:
        text = text[1:-1].strip()
    text = " ".join(text.split())
    # Common PVF variants.
    aliases = {
        "atgunner": "at gunner",
        "atfighter": "at fighter",
        "atmage": "at mage",
        "atswordman": "at swordman",
        "atpriest": "at priest",
        "demonicswordman": "demonic swordman",
        "demoniclancer": "demonic lancer",
        "creatormage": "creator mage",
        "darkknight": "demonic swordman",
        "dark knight": "demonic swordman",
    }
    compact = text.replace(" ", "")
    if text in aliases:
        return aliases[text]
    if compact in aliases:
        return aliases[compact]
    return text


def _extract_job_labels(text: str, key: str) -> tuple[str, ...]:
    body = _section_body(text, key)
    if not body:
        return ()
    labels: list[str] = []
    for match in re.finditer(r"`([^`]*)`", body):
        token = _normalize_job_label(match.group(1))
        if token and token not in labels:
            labels.append(token)
    if labels:
        return tuple(labels)
    # Fallback plain tokens like [all]
    for line in body.splitlines():
        token = _normalize_job_label(line.strip().strip("[]"))
        if not token:
            continue
        # restore bracket form if original had it
        raw = line.strip().strip("`")
        if raw.startswith("[") and raw.endswith("]"):
            token = _normalize_job_label(raw[1:-1])
        if token and token not in labels:
            labels.append(token)
    return tuple(labels)


def _extract_usable_jobs(text: str) -> tuple[str, ...]:
    return _extract_job_labels(text, "usable job")


def _extract_suitable_jobs(text: str) -> tuple[str, ...]:
    return _extract_job_labels(text, "suitable job")


def _extract_minimum_level(text: str) -> int:
    raw = _extract_int_field(text, "minimum level")
    if raw is None:
        return 0
    return max(0, int(raw))

def _extract_equipment_type(text: str) -> str:
    body = _section_body(text, "equipment type")
    return _first_tick_or_token(body)


def _extract_avatar_option_change_rule(
    text: str,
) -> tuple[tuple[str, str], int, int]:
    body = _section_body(text, "avatar select ability change")
    if not body:
        return (), 0, 0
    labels = []
    for raw in re.findall(r"`([^`]*)`", body):
        normalized = _normalize_type_token(raw).strip("[]").strip()
        if normalized:
            labels.append(normalized)
    values = [int(raw) for raw in re.findall(r"(?m)^\s*(-?\d+)\s*$", body)]
    if len(labels) < 2 or len(values) < 2:
        return (), 0, 0
    return (labels[0], labels[1]), max(0, values[0]), values[1]


def _is_avatar_equipment_type(equipment_type: str) -> bool:
    normalized = _normalize_equipment_type(equipment_type)
    words = normalized.strip("[]").replace("_", " ").split()
    return "avatar" in words


def _socket_types_from_section(text: str, key: str) -> tuple[int, ...]:
    body = _section_body(text, key)
    if not body:
        return ()
    labels = re.findall(r"\[\s*([^\]]+?)\s+socket\s*\]", body, re.IGNORECASE)
    if not labels:
        return ()
    result: list[int] = []
    for raw in labels:
        label = str(raw).strip().upper()
        if label not in AVATAR_SOCKET_TYPE_BY_LABEL:
            raise ValueError("unknown avatar socket label %r in [%s]" % (raw, key))
        result.append(AVATAR_SOCKET_TYPE_BY_LABEL[label])
    if len(result) > 5:
        raise ValueError("avatar socket count %d exceeds Stage3 limit" % len(result))
    return tuple(result)


def _extract_avatar_socket_types(
    text: str,
    equipment_type: str,
) -> tuple[tuple[int, ...], str]:
    normalized = _normalize_equipment_type(equipment_type)
    if not _is_avatar_equipment_type(normalized):
        return (), ""
    for section, source in (
        ("emblem socket default", "emblem_socket_default"),
        ("avatar type select", "avatar_type_select"),
    ):
        socket_types = _socket_types_from_section(text, section)
        if socket_types:
            return socket_types, source
    fallback = AVATAR_SOCKET_FALLBACK_BY_EQUIPMENT_TYPE.get(normalized, ())
    if fallback:
        return fallback, "equipment_type_fallback"
    return (), ""


def _extract_usable_period_days(text: str) -> int:
    raw = _extract_int_field(text, "usable period")
    return max(0, int(raw or 0))


def _extract_expiration_timestamp(text: str) -> int:
    body = _section_body(text, "expiration date")
    value = _strip_tick(_first_tick_or_token(body))
    if not value:
        return 0
    try:
        numeric = int(value, 0)
    except ValueError:
        numeric = 0
    if numeric > 0:
        return min(numeric, 0xFFFFFFFF)
    try:
        local_tz = timezone(timedelta(hours=8))
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=local_tz
        )
        return min(max(0, int(parsed.timestamp())), 0xFFFFFFFF)
    except (OverflowError, ValueError):
        return 0


def _extract_attach_type(text: str) -> str:
    """解析 PVF ``[attach type]`` → 去括号小写 token（free/trade/sealing/...）。"""
    body = _section_body(text, "attach type")
    raw = _first_tick_or_token(body)
    if not raw:
        return ""
    text_n = _strip_tick(raw).strip().lower()
    if text_n.startswith("[") and text_n.endswith("]") and len(text_n) >= 2:
        text_n = text_n[1:-1].strip()
    return " ".join(text_n.split())


def _extract_rarity(text: str) -> int:
    """解析 PVF ``[rarity]``，缺省 0（普通）。"""
    raw = _extract_int_field(text, "rarity")
    if raw is None:
        return 0
    return max(0, int(raw))


def _extract_set_item_ids(text: str) -> tuple[int, ...]:
    """Parse the ordered positive IDs from an equipment ``[set item]`` block."""
    body = _section_body(text, "set item")
    if not body:
        return ()
    out: list[int] = []
    seen: set[int] = set()
    for raw in re.findall(r"(?m)^\s*(-?\d+)\s*$", body):
        value = int(raw)
        if value <= 0 or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return tuple(out)


def _extract_impossible_contents(text: str) -> tuple[str, ...]:
    """解析 PVF ``[impossible contents]`` 为小写 token 元组。"""
    body = _section_body(text, "impossible contents")
    if not body:
        return ()
    out: list[str] = []
    for line in body.splitlines():
        token = line.strip().strip("`").strip()
        if not token:
            continue
        if token.startswith("[") and token.endswith("]") and len(token) >= 2:
            token = token[1:-1].strip()
        token = " ".join(token.lower().split())
        if token:
            out.append(token)
    return tuple(out)


def _extract_item_categories(text: str) -> tuple[str, ...]:
    """解析所有 PVF ``[item category]`` 段为规范化的小写标签。"""
    pattern = re.compile(
        r"\[item\s+category\]\s*(.*?)(?=\n\s*\[[^\]]+\]|\Z)",
        re.IGNORECASE | re.DOTALL,
    )
    out: list[str] = []
    for match in pattern.finditer(text):
        body = match.group(1)
        tokens = re.findall(r"`([^`]*)`", body)
        if not tokens:
            tokens = [line.strip() for line in body.splitlines() if line.strip()]
        for raw in tokens:
            token = _strip_tick(raw).strip()
            if token.startswith("[") and token.endswith("]") and len(token) >= 2:
                token = token[1:-1].strip()
            token = " ".join(token.lower().split())
            if token and token not in out:
                out.append(token)
    return tuple(out)


def normalize_stack_limit(explicit_limit) -> int:
    """Apply the server policy to a parsed PVF [stack limit] value.

    A missing field means unlimited stacking. An explicit value of 1 remains
    single-count; every other explicit value uses the server cap.
    """
    if explicit_limit is None:
        return 0
    if int(explicit_limit) == 1:
        return 1
    return SERVER_STACK_LIMIT_OVERRIDE


def _relative_script_path(prefix: str, rel: str) -> str:
    path = rel.replace("\\", "/").strip().lstrip("/")
    low = path.lower()
    pref = prefix.lower().rstrip("/") + "/"
    if low.startswith(pref):
        return path
    return f"{prefix.rstrip('/')}/{path}"



def _normalize_equipment_type(value: str) -> str:
    text = _strip_tick(value).strip().lower()
    start = text.find("[")
    end = text.find("]", start + 1) if start >= 0 else -1
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


def _has_durability_by_type(normalized_type: str) -> bool:
    # Weapon / armor only. Jewelry / magic stone / title / avatar have no durability.
    return normalized_type in {
        "[weapon]",
        "[support weapon]",
        "[coat]",
        "[pants]",
        "[shoulder]",
        "[shoes]",
        "[waist]",
    }


def _extract_equipment_durability(text: str, equipment_type: str) -> int:
    raw = _extract_int_field(text, "durability")
    if raw is None or int(raw) <= 0:
        return 0
    eq_type = _normalize_equipment_type(equipment_type)
    if not _has_durability_by_type(eq_type):
        return 0
    return max(0, int(raw)) & 0xFFFF


def _extract_equipment_sub_type(text: str) -> int:
    raw = _extract_int_field(text, "sub type")
    return int(raw) if raw is not None else -1


def _extract_price_value(text: str) -> tuple[int, int]:
    """从 PVF 脚本读 ``[price]`` / ``[value]``，供 NPC 买卖计价。"""
    price_raw = _extract_int_field(text, "price")
    value_raw = _extract_int_field(text, "value")
    price = max(0, int(price_raw)) if price_raw is not None else 0
    value = max(0, int(value_raw)) if value_raw is not None else 0
    return price, value


def _extract_need_material(text: str) -> tuple[int, int]:
    """从 PVF ``[need material]`` 读材料 id + 数量（S4 NeedMaterial）。"""
    body = _section_body(text, "need material")
    if not body:
        return 0, 0
    nums = re.findall(r"(?m)^\s*(-?\d+)\s*$", body)
    if len(nums) < 2:
        nums = re.findall(r"(-?\d+)", body)
    if len(nums) < 2:
        return 0, 0
    mat_id = max(0, int(nums[0]))
    mat_count = max(0, int(nums[1]))
    return mat_id, mat_count

def _resolve_from_pvf(item_id: int) -> ItemMetadata:
    iid = int(item_id) & 0xFFFFFFFF
    if iid <= 0:
        return ItemMetadata(item_id=iid, item_kind="unknown", stack_limit=1, note="invalid_id")

    try:
        reader = _get_reader()
    except Exception as exc:
        return ItemMetadata(
            item_id=iid,
            item_kind="unknown",
            stack_limit=1,
            note=f"pvf_open_fail:{exc}",
        )

    try:
        equip_map = _load_equipment_map(reader)
        equip_err = ""
    except Exception as exc:
        equip_map = {}
        equip_err = f"equip_lst_fail:{exc}"

    if iid in equip_map:
        rel = equip_map[iid]
        path = _relative_script_path("equipment", rel)
        try:
            text = reader.read_text(path)
        except Exception as exc:
            return ItemMetadata(
                item_id=iid,
                item_kind="equipment",
                stack_limit=1,
                durability=0,
                source_path=path,
                note=f"equip_read_fail:{exc}",
            )
        equipment_type = _extract_equipment_type(text)
        avatar_socket_types, avatar_socket_source = _extract_avatar_socket_types(
            text,
            equipment_type,
        )
        durability = _extract_equipment_durability(text, equipment_type)
        buy_price, sell_value = _extract_price_value(text)
        need_mat_id, need_mat_count = _extract_need_material(text)
        return ItemMetadata(
            item_id=iid,
            item_kind="equipment",
            stack_limit=1,
            equipment_type=equipment_type,
            is_avatar=_is_avatar_equipment_type(equipment_type),
            durability=int(durability),
            minimum_level=_extract_minimum_level(text),
            usable_jobs=_extract_usable_jobs(text),
            suitable_jobs=_extract_suitable_jobs(text),
            usable_period_days=_extract_usable_period_days(text),
            expiration_timestamp=_extract_expiration_timestamp(text),
            daily_delete=bool(_extract_int_field(text, "daily delete item")),
            attach_type=_extract_attach_type(text),
            name=_extract_name(text),
            source_path=path,
            note="ok",
            buy_price=int(buy_price),
            sell_value=int(sell_value),
            need_material_id=int(need_mat_id),
            need_material_count=int(need_mat_count),
            rarity=_extract_rarity(text),
            set_item_ids=_extract_set_item_ids(text),
            impossible_contents=_extract_impossible_contents(text),
            item_categories=_extract_item_categories(text),
            grade=max(0, int(_extract_int_field(text, "grade") or 0)),
            avatar_socket_types=avatar_socket_types,
            avatar_socket_source=avatar_socket_source,
            custom_ability_type=_normalize_type_token(
                _first_tick_or_token(_section_body(text, "custom ability type"))
            ).strip("[]"),
            custom_ability_count=max(
                0, int(_extract_int_field(text, "custom ability count") or 0)
            ),
            custom_ability_part_set_index=max(
                0,
                int(_extract_int_field(text, "custom ability part set index") or 0),
            ),
            equipment_sub_type=_extract_equipment_sub_type(text),
            output_index=max(0, int(_extract_int_field(text, "output index") or 0)),
            anti_evil=_extract_anti_evil(text),
        )

    try:
        stack_map = _load_stackable_map(reader)
    except Exception as exc:
        return ItemMetadata(
            item_id=iid,
            item_kind="unknown",
            stack_limit=1,
            note=equip_err or f"stack_lst_fail:{exc}",
        )

    if iid in stack_map:
        rel = stack_map[iid]
        path = _relative_script_path("stackable", rel)
        try:
            text = reader.read_text(path)
        except Exception as exc:
            return ItemMetadata(
                item_id=iid,
                item_kind="stackable",
                stackable_type="[waste]",
                stack_limit=0,
                source_path=path,
                note=f"stack_read_fail:{exc}",
            )
        stackable_type = _extract_stackable_type(text)
        explicit_limit = _extract_int_field(text, "stack limit")
        stack_limit = normalize_stack_limit(explicit_limit)
        limit_note = "unlimited_no_field" if explicit_limit is None else "explicit_limit"
        buy_price, sell_value = _extract_price_value(text)
        need_mat_id, need_mat_count = _extract_need_material(text)
        option_scope, option_grade, option_mode = _extract_avatar_option_change_rule(
            text
        )
        action_type, action_values = _extract_action_rule(text)
        emblem_target_types = _socket_types_from_section(
            text,
            "avatar emblem target type",
        )
        return ItemMetadata(
            item_id=iid,
            item_kind="stackable",
            stackable_type=stackable_type or "[waste]",
            stack_limit=int(stack_limit),
            minimum_level=_extract_minimum_level(text),
            usable_jobs=_extract_usable_jobs(text),
            suitable_jobs=_extract_suitable_jobs(text),
            usable_period_days=_extract_usable_period_days(text),
            expiration_timestamp=_extract_expiration_timestamp(text),
            daily_delete=bool(_extract_int_field(text, "daily delete item")),
            attach_type=_extract_attach_type(text),
            name=_extract_name(text),
            source_path=path,
            note=f"ok:{limit_note}",
            buy_price=int(buy_price),
            sell_value=int(sell_value),
            need_material_id=int(need_mat_id),
            need_material_count=int(need_mat_count),
            rarity=_extract_rarity(text),
            impossible_contents=_extract_impossible_contents(text),
            grade=max(0, int(_extract_int_field(text, "grade") or 0)),
            avatar_option_change_scope=option_scope,
            avatar_option_change_grade=option_grade,
            avatar_option_change_mode=option_mode,
            avatar_emblem_target_types=emblem_target_types,
            action_type=action_type,
            action_values=action_values,
        )

    return ItemMetadata(
        item_id=iid,
        item_kind="special",
        stack_limit=1,
        note=equip_err or "not_in_lst",
    )


@lru_cache(maxsize=32768)
def _resolve_item_metadata_from_pvf(item_id: int) -> ItemMetadata:
    return _resolve_from_pvf(int(item_id))


def resolve_item_metadata(item_id: int) -> ItemMetadata:
    """Resolve one item template, preferring a validated boot cache record."""
    iid = int(item_id) & 0xFFFFFFFF
    with _LOCK:
        preset = _PRESET_METADATA.get(iid)
    metadata = preset if preset is not None else _resolve_item_metadata_from_pvf(iid)
    with _LOCK:
        _RESOLVED_METADATA[iid] = metadata
    return metadata


def install_preset_item_metadata(records: dict[int, ItemMetadata]) -> None:
    """Install PVF-identity-bound records restored by the shop bundle cache."""
    normalized: dict[int, ItemMetadata] = {}
    for raw_id, metadata in records.items():
        iid = int(raw_id) & 0xFFFFFFFF
        if iid <= 0 or not isinstance(metadata, ItemMetadata) or int(metadata.item_id) != iid:
            raise ValueError("invalid preset item metadata")
        normalized[iid] = metadata
    with _LOCK:
        _PRESET_METADATA.clear()
        _PRESET_METADATA.update(normalized)
        _RESOLVED_METADATA.update(normalized)


def snapshot_resolved_item_metadata() -> dict[int, ItemMetadata]:
    """Return all records resolved during current shop bundle construction."""
    with _LOCK:
        return dict(_RESOLVED_METADATA)


def resolve_item_slot_range(item_id: int) -> tuple[int, int]:
    return resolve_item_metadata(int(item_id)).slot_range()


def resolve_item_stack_limit(item_id: int) -> int:
    return int(resolve_item_metadata(int(item_id)).stack_limit)


def is_item_tradable(item_id: int, *, seal_flag: int = 0) -> bool:
    """当前实例是否可交易（丢弃落地再捡用）。"""
    return resolve_item_metadata(int(item_id)).is_tradable_instance(int(seal_flag or 0))


def resolve_item_expire_time(
    metadata: ItemMetadata,
    *,
    now_unix: int | None = None,
) -> int:
    """Materialize one item's PVF lifetime as an absolute Unix timestamp.

    ``[usable period]`` begins when an instance is granted. A future
    ``[expiration date]`` is already absolute and is used only when no
    relative period is present. The rule belongs to item metadata rather than
    to one delivery path, so quest, drop, GM, package, and shop grants create
    equivalent timed instances.
    """
    if now_unix is None:
        now_value = int(datetime.now(timezone.utc).timestamp())
    else:
        now_value = max(0, int(now_unix))
    if getattr(metadata, "daily_delete", False):
        # UTC+8 06:00 is UTC 22:00. At the boundary a newly issued item
        # belongs to the new day and expires at the following boundary.
        return min(((now_value + 7200) // 86400 + 1) * 86400 - 7200, 0xFFFFFFFF)
    period_days = max(0, int(getattr(metadata, "usable_period_days", 0) or 0))
    if period_days > 0:
        return min(now_value + period_days * 24 * 60 * 60, 0xFFFFFFFF)
    absolute = max(0, int(getattr(metadata, "expiration_timestamp", 0) or 0))
    if absolute > now_value:
        return min(absolute, 0xFFFFFFFF)
    return 0


def clear_item_metadata_cache() -> None:
    _resolve_item_metadata_from_pvf.cache_clear()
    global _EQUIP_MAP, _STACK_MAP, _EQUIP_MAP_PATH, _STACK_MAP_PATH
    with _LOCK:
        _EQUIP_MAP = None
        _STACK_MAP = None
        _EQUIP_MAP_PATH = None
        _STACK_MAP_PATH = None
        _PRESET_METADATA.clear()
        _RESOLVED_METADATA.clear()


def warmup_item_metadata_index() -> dict[str, int | str]:
    # Boot-time required: load equipment/stackable lst maps from shared PVF.
    reader = _get_reader()
    equip = _load_equipment_map(reader)
    stack = _load_stackable_map(reader)
    return {
        "ok": 1,
        "equipment_count": len(equip),
        "stackable_count": len(stack),
        "pvf": str(_resolve_pvf_path()),
    }
