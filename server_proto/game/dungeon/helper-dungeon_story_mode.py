"""Dungeon .dgn [story mode] — exp rate + mon HP/DEF scales.

PVF 权威（非固定 130%）：

```text
[story mode]
[difficulty size] 1
[increase exp rate]
100          # 与下一行相加后 /100 → 经验倍率
30
[MAX_HP]
[ALL]
70
0
[/MAX_HP]
[DEFENSE]
[NORMAL] 70 5
...
[/DEFENSE]
[quest list] ...
[/story mode]
```

经验倍率：``(rate_a + rate_b) / 100.0``
  - 例：100 30 → 1.30；300 30 → 3.30
  - 每图从对应 `.dgn` 读取，禁止写死 1.3

属性缩放：同公式 ``(a + b) / 100.0``，键 ALL/NORMAL/NAMED/BOSS。
  - mon type 0→NORMAL，1/2→NAMED，3→BOSS，缺省回落 ALL→1.0

仅剧情迷宫（quest connection / quest_connected）叠加；自由迷宫只吃
``[experience increasing point]``。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping


def _strip_tick(value: str) -> str:
    s = str(value or "").strip()
    if len(s) >= 2 and s[0] == "`" and s[-1] == "`":
        return s[1:-1].strip()
    return s


def _pair_rate(a: float, b: float) -> float:
    """PVF 两段参数 → 倍率。100 30 → 1.30；70 0 → 0.70。"""
    try:
        rate = (float(a) + float(b)) / 100.0
    except Exception:
        return 1.0
    if rate < 0:
        return 0.0
    return float(rate)


@dataclass
class StoryModeInfo:
    """Parsed [story mode] block from one .dgn."""

    present: bool = False
    difficulty_size: int = 0
    # raw pair from [increase exp rate]; None = tag missing
    exp_rate_a: float | None = None
    exp_rate_b: float | None = None
    # key -> (a, b) for MAX_HP / DEFENSE
    max_hp: dict[str, tuple[float, float]] = field(default_factory=dict)
    defense: dict[str, tuple[float, float]] = field(default_factory=dict)
    quest_list: list[int] = field(default_factory=list)

    @property
    def exp_multiplier(self) -> float:
        if self.exp_rate_a is None:
            return 1.0
        b = 0.0 if self.exp_rate_b is None else float(self.exp_rate_b)
        return _pair_rate(float(self.exp_rate_a), b)

    def _lookup_pair(
        self, table: Mapping[str, tuple[float, float]], mon_type: int
    ) -> tuple[float, float] | None:
        key = mon_type_to_story_key(int(mon_type))
        if key in table:
            return table[key]
        if "ALL" in table:
            return table["ALL"]
        return None

    def hp_multiplier(self, mon_type: int = 0) -> float:
        pair = self._lookup_pair(self.max_hp, mon_type)
        if pair is None:
            return 1.0
        return _pair_rate(pair[0], pair[1])

    def defense_multiplier(self, mon_type: int = 0) -> float:
        pair = self._lookup_pair(self.defense, mon_type)
        if pair is None:
            return 1.0
        return _pair_rate(pair[0], pair[1])


def mon_type_to_story_key(mon_type: int) -> str:
    t = int(mon_type) & 0xFF
    if t == 3:
        return "BOSS"
    if t in (1, 2):
        return "NAMED"
    return "NORMAL"


def _parse_stat_block(body: str) -> dict[str, tuple[float, float]]:
    """Parse [MAX_HP]/[DEFENSE]-style: [KEY] a b ... until next [tag] or end."""
    out: dict[str, tuple[float, float]] = {}
    if not body:
        return out
    # Split on [KEY] lines
    parts = re.split(r"(?is)\[([A-Za-z_ ]+)\]", body)
    # parts[0]=preamble, then key, data, key, data...
    i = 1
    while i + 1 < len(parts):
        key = _strip_tick(parts[i]).upper().replace(" ", "")
        data = parts[i + 1]
        i += 2
        if not key or key.startswith("/"):
            continue
        # stop data at nested close tags leftovers
        data = re.split(r"(?is)\[/", data, maxsplit=1)[0]
        nums = re.findall(r"-?\d+(?:\.\d+)?", data)
        if len(nums) < 1:
            continue
        try:
            a = float(nums[0])
            b = float(nums[1]) if len(nums) >= 2 else 0.0
        except ValueError:
            continue
        out[key] = (a, b)
    return out


def parse_story_mode_text(text: str) -> StoryModeInfo:
    """Extract [story mode]...[/story mode] from decoded .dgn text."""
    src = str(text or "")
    m = re.search(r"(?is)\[story\s*mode\](.*?)\[/story\s*mode\]", src)
    if not m:
        return StoryModeInfo(present=False)
    block = m.group(1)
    info = StoryModeInfo(present=True)

    ds = re.search(r"(?is)\[difficulty\s*size\]\s*(-?\d+)", block)
    if ds:
        try:
            info.difficulty_size = int(ds.group(1))
        except ValueError:
            pass

    er = re.search(
        r"(?is)\[increase\s*exp\s*rate\](.*?)(?=\[[a-zA-Z_/ ]|\Z)",
        block,
    )
    if er:
        nums = re.findall(r"-?\d+(?:\.\d+)?", er.group(1))
        if nums:
            try:
                info.exp_rate_a = float(nums[0])
                info.exp_rate_b = float(nums[1]) if len(nums) >= 2 else 0.0
            except ValueError:
                pass

    hp_m = re.search(r"(?is)\[MAX_HP\](.*?)\[/MAX_HP\]", block)
    if hp_m:
        info.max_hp = _parse_stat_block(hp_m.group(1))

    def_m = re.search(r"(?is)\[DEFENSE\](.*?)\[/DEFENSE\]", block)
    if def_m:
        info.defense = _parse_stat_block(def_m.group(1))

    ql = re.search(r"(?is)\[quest\s*list\](.*?)\[/quest\s*list\]", block)
    if ql:
        for n in re.findall(r"-?\d+", ql.group(1)):
            try:
                qid = int(n)
            except ValueError:
                continue
            if qid > 0:
                info.quest_list.append(qid)
    return info


def is_story_run(
    *,
    maze_index: int = 0,
    quest_connection: Any = None,
    reason: str = "",
    quest_connected: bool = False,
    session: Any = None,
) -> bool:
    """剧情迷宫判定：有 quest connection / 会话已标记连接。"""
    if quest_connected:
        return True
    if session is not None and bool(getattr(session, "quest_connected", False)):
        return True
    if session is not None and bool(getattr(session, "dungeon_story_mode", False)):
        return True
    reason_s = str(reason or "").strip().lower()
    if reason_s in {
        "active_connection",
        "finished_connection",
        "active_connection_mirror",
        "finished_connection_mirror",
    }:
        return True
    if quest_connection:
        return True
    # maze_index>0 且会话已记 story 时由上面覆盖；裸 maze_index 不单独当 story
    # （避免非任务变体迷宫误叠倍率）
    _ = maze_index
    return False


def resolve_story_exp_multiplier(
    story: StoryModeInfo | None,
    *,
    story_active: bool,
) -> float:
    """剧情激活时返回 PVF 倍率，否则 1.0。"""
    if not story_active or story is None or not story.present:
        return 1.0
    return float(story.exp_multiplier)


def combine_experience_weight(
    base_weight: float,
    story: StoryModeInfo | None,
    *,
    story_active: bool,
) -> float:
    """最终杀怪/通关经验权重 = [experience increasing point] × story_exp_rate。"""
    try:
        base = float(base_weight)
    except Exception:
        base = 1.0
    if base < 0:
        base = 1.0
    mult = resolve_story_exp_multiplier(story, story_active=story_active)
    return float(base) * float(mult)


def apply_story_scales_to_monsters(
    mons: list[dict[str, Any]] | None,
    story: StoryModeInfo | None,
    *,
    story_active: bool,
) -> list[dict[str, Any]]:
    """给 mon dict 写入 story_hp_rate / story_def_rate（wire 无 HP 字段，供会话/日志/后续战斗）。"""
    rows = list(mons or [])
    if not story_active or story is None or not story.present:
        for mon in rows:
            mon.setdefault("story_hp_rate", 1.0)
            mon.setdefault("story_def_rate", 1.0)
        return rows
    for mon in rows:
        mt = int(mon.get("type") or mon.get("mon_type") or 0)
        mon["story_hp_rate"] = float(story.hp_multiplier(mt))
        mon["story_def_rate"] = float(story.defense_multiplier(mt))
        # 可选：把缩放百分比写入 mon 备注字段，不改 wire 布局
        mon["story_stat_key"] = mon_type_to_story_key(mt)
    return rows


def bind_story_to_session(
    session: Any,
    story: StoryModeInfo | None,
    *,
    story_active: bool,
    base_weight: float,
) -> dict[str, Any]:
    """写入会话字段，返回摘要 dict 便于 enter/MOVE 日志。"""
    mult = resolve_story_exp_multiplier(story, story_active=story_active)
    combined = combine_experience_weight(base_weight, story, story_active=story_active)
    summary: dict[str, Any] = {
        "story_active": bool(story_active and story is not None and story.present),
        "story_exp_mult": float(mult),
        "base_weight": float(base_weight),
        "combined_weight": float(combined),
        "exp_rate_pair": None,
        "hp_keys": [],
        "def_keys": [],
    }
    if session is None:
        return summary
    try:
        session.dungeon_story_mode = bool(summary["story_active"])
        session.dungeon_story_exp_mult = float(mult)
        session.dungeon_experience_weight = float(combined)
        session.dungeon_experience_weight_base = float(base_weight)
        if story is not None and story.present and story_active:
            if story.exp_rate_a is not None:
                summary["exp_rate_pair"] = (
                    float(story.exp_rate_a),
                    float(0.0 if story.exp_rate_b is None else story.exp_rate_b),
                )
            session.dungeon_story_exp_rate_pair = summary["exp_rate_pair"]
            session.dungeon_story_max_hp = dict(story.max_hp)
            session.dungeon_story_defense = dict(story.defense)
            session.dungeon_story_quest_list = list(story.quest_list)
            summary["hp_keys"] = sorted(story.max_hp.keys())
            summary["def_keys"] = sorted(story.defense.keys())
        else:
            session.dungeon_story_exp_rate_pair = None
            session.dungeon_story_max_hp = {}
            session.dungeon_story_defense = {}
            session.dungeon_story_quest_list = []
    except Exception:
        pass
    return summary


def story_info_from_dungeon(dgn: Any) -> StoryModeInfo | None:
    """从 quest_index.DungeonFileInfo 取 story_mode。"""
    if dgn is None:
        return None
    sm = getattr(dgn, "story_mode", None)
    if isinstance(sm, StoryModeInfo):
        return sm
    if sm is None:
        return None
    # 兼容 dict 快照
    if isinstance(sm, dict):
        info = StoryModeInfo(present=bool(sm.get("present")))
        info.difficulty_size = int(sm.get("difficulty_size") or 0)
        info.exp_rate_a = sm.get("exp_rate_a")
        info.exp_rate_b = sm.get("exp_rate_b")
        info.max_hp = dict(sm.get("max_hp") or {})
        info.defense = dict(sm.get("defense") or {})
        info.quest_list = list(sm.get("quest_list") or [])
        return info
    return None
