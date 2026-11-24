"""Character / dungeon / quest exp helpers with PVF authority.

Sources (AGENTS: shared preloaded PVF only):
  - character/exptable.tbl          — level-up cumulative thresholds
  - n_quest/questparameter.etc      — [exp reward table] / [gold reward table]
    (already used by game.quest_rewards for quest finish; re-export here)
  - etc/(r)serverparameter.etc      — [dungeon difficulty exp bonusrate]
                                      [monster exp bonusrate]
  - dungeon/*.dgn                   — [experience increasing point]
                                      + 剧情 [story mode]/[increase exp rate] a b → (a+b)/100
                                        （叠到 weight；每图 PVF 读取，非固定 1.3）
  - Monster base exp table          — s4a12 MonsterRewardTable.BaseExp (1..100)
    (not present as a free-standing PVF table in Stage3; keep hardcoded parity)

Kill formula (default friend — 616ff337 / 本地完整路径):
  base      = MonsterRewardTable.BaseExp[mon_level]          # 1..100
  weight    = .dgn [experience increasing point]
  diff_rate = etc/(r)serverparameter [dungeon difficulty exp bonusrate][diff]
  type_rate = [monster exp bonusrate][mon_type]  # Stage3 表通常 1.0
  raw       = (int)(base * weight * diff_rate * type_rate)
  gained    = max(1, (int)(raw * BaseExpPenalty(char, mon)))  # base>0

Optional s4a12 CalcExp path (env USDOF_GAME_DUNGEON_KILL_EXP_MODE=calc):
  gained = (int)(BaseExp * weight * diff_rate)   # 无等级差惩罚

Clear settlement (default USDOF_GAME_DUNGEON_CLEAR_EXP_MODE=s4a12)::
  base       = GetExpRewardBase(basic_lv)  # questParameter [exp reward table]
  clear_base = floor(base * dgn_weight * difficulty_rate)
  score_bonus = floor(clear_base * clear_rank_bonus_rate(rank_index))
  grant      = clear_base + score_bonus   # no *10; *10 is friend/legacy mode

Level-up thresholds (hard): character/exptable.tbl via shared PVF preload.
Quest finish exp/gold: game.quest_rewards (questparameter) — not recomputed here.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Any

from pvf import pvf_runtime
from pvf.adventure_group_index import get_adventure_group_index

_NUM = re.compile(r"-?\d+(?:\.\d+)?")
_FLOAT = re.compile(r"-?\d+(?:\.\d+)?")

STAGE3_CHARACTER_MAX_LEVEL = 90
STAGE3_LEVEL90_EXP_CAP = 1_667_387_095
STAGE3_EXPERTISE_EXP_CAP = 2_092_039_480_482

# s4a12 MonsterRewardTable.BaseExp[1..100] — monster level base exp
_BASE_EXP_BY_LEVEL: tuple[int, ...] = (
    30, 40, 50, 60, 70, 80, 90, 100, 110, 120,
    130, 140, 150, 160, 170, 185, 201, 218, 235, 253,
    271, 290, 310, 330, 351, 372, 394, 417, 440, 464,
    488, 513, 539, 565, 592, 619, 647, 676, 705, 735,
    765, 796, 828, 860, 893, 926, 960, 995, 1030, 1066,
    1102, 1139, 1177, 1215, 1254, 1293, 1333, 1374, 1415, 1457,
    1499, 1542, 1586, 1630, 1675, 1720, 1766, 1813, 1860, 1908,
    1956, 2005, 2055, 2105, 2156, 2207, 2259, 2312, 2365, 2419,
    2473, 2528, 2584, 2640, 2697, 2754, 2812, 2871, 2930, 2990,
    3050, 3111, 3173, 3235, 3298, 3361, 3425, 3490, 3555, 3575,
)

_SERVER_PARAM_CANDIDATES = (
    "etc/(r)serverparameter.etc",
    "etc/(f)serverparameter.etc",
    "etc/serverparameter.etc",
    "Etc/ServerParameter.etc",
)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return int(default)
    try:
        return int(str(raw).strip(), 0)
    except ValueError:
        return int(default)


def _read_first_text(paths: tuple[str, ...]) -> str:
    for path in paths:
        try:
            text = pvf_runtime.read_text(path)
            if text:
                return str(text)
        except Exception:
            continue
    return ""


def _section_floats(text: str, tag: str) -> list[float]:
    """Extract float tokens under [tag] until next [section]."""
    if not text:
        return []
    low = text.lower()
    key = tag.lower().strip()
    if not key.startswith("["):
        key = "[" + key
    if not key.endswith("]"):
        key = key + "]"
    idx = low.find(key)
    if idx < 0:
        return []
    start = idx + len(key)
    end = low.find("[", start)
    if end < 0:
        end = len(text)
    chunk = text[start:end]
    out: list[float] = []
    for tok in _FLOAT.findall(chunk):
        try:
            out.append(float(tok))
        except ValueError:
            continue
    return out


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() not in {"0", "false", "off", "no", ""}


@lru_cache(maxsize=1)
def load_exp_thresholds() -> tuple[int, ...]:
    """Cumulative exp to *reach* level N+1 from PVF ``character/exptable.tbl``.

    s4a12: ``GetLevelThreshold(level)`` → thresholds[level-1];
    while level < cap and exp >= GetLevelThreshold(level): level++.
    Stage3 PVF (token-decoded): 1000, 2035, 3315, ... (L1→L2 needs 1000).
    """
    for path in ("character/exptable.tbl", "character/ExpTable.tbl"):
        try:
            text = pvf_runtime.read_text(path)
        except Exception:
            continue
        # PVF reader already expands script tokens to decimal lines.
        nums: list[int] = []
        for line in str(text or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("["):
                continue
            for tok in _NUM.findall(line):
                try:
                    v = int(float(tok))
                except ValueError:
                    continue
                if v > 0:
                    nums.append(v)
        if len(nums) >= 10:
            return tuple(nums)
        # fallback: any positive ints in file
        nums = [int(float(x)) for x in _NUM.findall(text or "") if float(x) > 0]
        if len(nums) >= 10:
            return tuple(nums)
    # last-resort synthetic (must not be used when PVF preload is healthy)
    return tuple(1000 * (i + 1) * (i + 2) // 2 for i in range(90))


def max_character_level() -> int:
    """Frozen Stage3 character cap; later rows in exptable.tbl are historical."""
    return STAGE3_CHARACTER_MAX_LEVEL


def level90_exp_cap() -> int:
    thresholds = load_exp_thresholds()
    index = STAGE3_CHARACTER_MAX_LEVEL - 2
    if index >= len(thresholds):
        raise ValueError(
            "character/exptable.tbl rows=%d below Lv.%d cap index"
            % (len(thresholds), STAGE3_CHARACTER_MAX_LEVEL)
        )
    value = int(thresholds[index])
    if value != STAGE3_LEVEL90_EXP_CAP:
        raise ValueError(
            "Stage3 Lv.90 EXP cap mismatch: expected=%d actual=%d"
            % (STAGE3_LEVEL90_EXP_CAP, value)
        )
    return value


def load_expertise_exp_requirements() -> tuple[int, ...]:
    requirements = tuple(
        int(value)
        for value in get_adventure_group_index().expertise_exp_requirements
    )
    if not requirements or any(value <= 0 for value in requirements):
        raise ValueError("etc/honorlevel.etc expertise EXP table is empty or invalid")
    return requirements


def expertise_exp_cap() -> int:
    value = sum(load_expertise_exp_requirements())
    if value != STAGE3_EXPERTISE_EXP_CAP:
        raise ValueError(
            "Stage3 Expertise EXP cap mismatch: expected=%d actual=%d"
            % (STAGE3_EXPERTISE_EXP_CAP, value)
        )
    return value


@lru_cache(maxsize=1)
def load_dungeon_difficulty_exp_rates() -> tuple[float, ...]:
    """PVF ``etc/(r)serverparameter.etc`` ``[dungeon difficulty exp bonusrate]``.

    Stage3 client PVF (observed): 1.3, 2.0, 2.5, 3.0, 4.0 for difficulty 0..4.
    s4a12 indexes with ``rates[difficulty]`` — same. Empty table → all 1.0.
    """
    text = _read_first_text(_SERVER_PARAM_CANDIDATES)
    rates = _section_floats(text, "[dungeon difficulty exp bonusrate]")
    if rates:
        return tuple(float(x) for x in rates)
    return (1.0, 1.0, 1.0, 1.0, 1.0)


@lru_cache(maxsize=1)
def load_monster_type_exp_rates() -> tuple[float, ...]:
    """PVF ``[monster exp bonusrate]`` — Stage3 only has ``1.0`` (unused by s4a12 CalcExp)."""
    text = _read_first_text(_SERVER_PARAM_CANDIDATES)
    rates = _section_floats(text, "[monster exp bonusrate]")
    if rates:
        return tuple(float(x) for x in rates)
    return (1.0,)


def monster_base_exp(mon_level: int) -> int:
    """Base exp by monster level (s4a12 MonsterRewardTable.BaseExp).

    Not present as a free-standing PVF table on Stage3; keep s4a12 parity table.
    """
    lv = int(mon_level) or 1
    if lv < 1:
        return 0
    if lv > len(_BASE_EXP_BY_LEVEL):
        return int(_BASE_EXP_BY_LEVEL[-1])
    return int(_BASE_EXP_BY_LEVEL[lv - 1])


def difficulty_exp_rate(difficulty: int) -> float:
    """PVF difficulty multiplier; missing table → 1.0."""
    rates = load_dungeon_difficulty_exp_rates()
    d = int(difficulty) if difficulty is not None else 0
    if d < 0:
        d = 0
    if not rates:
        return 1.0
    if d >= len(rates):
        return float(rates[-1])
    return float(rates[d])


def monster_type_exp_rate(mon_type: int) -> float:
    rates = load_monster_type_exp_rates()
    t = int(mon_type) if mon_type is not None else 0
    if not rates:
        return 1.0
    if t < 0:
        return float(rates[0])
    if t >= len(rates):
        return float(rates[0])
    return float(rates[t])


def exp_penalty_rate(char_level: int, mon_level: int) -> float:
    """Optional BaseExpPenalty (friend doc). Default OFF — s4a12 kill path skips it."""
    diff = int(mon_level) - int(char_level)
    if diff <= -7:
        return 0.05
    if diff == -6:
        return 0.20
    if diff == -5:
        return 0.50
    if diff == -4:
        return 0.75
    if -3 <= diff <= 0:
        return 1.00
    if 1 <= diff <= 3:
        return 1.12
    if 4 <= diff <= 5:
        return 1.00
    if diff == 6:
        return 0.75
    if diff == 7:
        return 0.70
    if diff == 8:
        return 0.60
    if diff == 9:
        return 0.50
    return 0.05


def resolve_experience_weight(
    dungeon_id: int | None = None,
    *,
    session: Any = None,
    explicit: float | None = None,
) -> float:
    """PVF ``.dgn [experience increasing point]`` with session/cache fallback."""
    if explicit is not None:
        try:
            w = float(explicit)
            return w if w >= 0 else 1.0
        except Exception:
            return 1.0
    if session is not None:
        for attr in ("dungeon_experience_weight", "experience_increasing_point"):
            val = getattr(session, attr, None)
            if val is not None:
                try:
                    w = float(val)
                    if w >= 0:
                        return w
                except Exception:
                    pass
    did = dungeon_id
    if did is None and session is not None:
        did = getattr(session, "selected_dungeon_id", None) or getattr(
            session, "cur_dungeon_id", None
        )
    if did is not None and int(did) > 0:
        try:
            from game.quest.quest_index import get_quest_index

            dgn = get_quest_index().get_dungeon(int(did))
            w = float(getattr(dgn, "experience_increasing_point", 1.0) or 1.0)
            return w if w >= 0 else 1.0
        except Exception:
            pass
    return 1.0


def compute_kill_exp_raw(
    mon_level: int,
    *,
    experience_weight: float = 1.0,
    difficulty: int = 0,
    mon_type: int = 0,
    named: bool = False,
) -> int:
    """Unscaled kill exp before char-level penalty (for run totals / calc mode).

    ``raw = (int)(BaseExp * dgn_weight * difficulty_rate * mon_type_rate)``
    ``named`` multiplies type_rate by 3（616ff337 原设定；普通小怪为 False）。
    """
    base = monster_base_exp(mon_level)
    if base <= 0:
        return 0
    weight = float(experience_weight) if float(experience_weight) >= 0 else 1.0
    diff_rate = difficulty_exp_rate(difficulty)
    type_rate = monster_type_exp_rate(mon_type)
    if named:
        type_rate *= 3.0
    # C# (int) cast truncates toward zero
    return max(0, int(base * weight * diff_rate * type_rate))


def compute_kill_exp(
    char_level: int,
    mon_level: int,
    *,
    experience_weight: float = 1.0,
    difficulty: int = 0,
    mon_type: int = 0,
    named: bool = False,
) -> int:
    """Kill exp persisted to DB.

    Default (friend / 616ff337)::
        raw    = BaseExp * weight * difficulty_rate * mon_type_rate
        gained = max(1, int(raw * BaseExpPenalty(char, mon)))

    Modes (``USDOF_GAME_DUNGEON_KILL_EXP_MODE``)::
      - friend (default): raw * 等级差惩罚
      - calc / s4a12: raw only（无惩罚，对齐 s4a12 CalcExp）
      - fixed: constant ``USDOF_GAME_DUNGEON_KILL_EXP_FIXED`` (test only)
    """
    mode = (
        str(os.environ.get("USDOF_GAME_DUNGEON_KILL_EXP_MODE") or "friend")
        .strip()
        .lower()
    )
    if mode in {"fixed", "const", "constant", "test"}:
        fixed = _env_int("USDOF_GAME_DUNGEON_KILL_EXP_FIXED", 20)
        return max(0, int(fixed))
    raw = compute_kill_exp_raw(
        mon_level,
        experience_weight=experience_weight,
        difficulty=difficulty,
        mon_type=mon_type,
        named=named,
    )
    if raw <= 0:
        return 0
    if mode in {"calc", "s4a12", "weight", "pvf_weight"}:
        return max(0, int(raw))
    # friend path: apply BaseExpPenalty
    rate = exp_penalty_rate(char_level, mon_level)
    awarded = int(float(raw) * float(rate))
    return max(1, awarded)


@lru_cache(maxsize=1)
def load_clear_exp_reward_base_table() -> tuple[int, ...]:
    """s4a12 ExpTableProvider: n_quest/questParameter.etc ``[exp reward table]``.

    Token stream is ``value, -1`` pairs; keep only ``>= 0`` entries so
    index ``level-1`` matches GetExpRewardBase(level).
    """
    text = _read_first_text(
        (
            "n_quest/questparameter.etc",
            "n_quest/questParameter.etc",
            "N_Quest/questParameter.etc",
        )
    )
    if not text:
        return ()
    low = text.lower()
    start_tag = "[exp reward table]"
    end_tag = "[gold reward table]"
    idx = low.find(start_tag)
    if idx < 0:
        return ()
    start = idx + len(start_tag)
    end = low.find(end_tag, start)
    if end < 0:
        end = len(text)
    nums: list[int] = []
    for tok in _NUM.findall(text[start:end]):
        try:
            v = int(float(tok))
        except ValueError:
            continue
        if v >= 0:
            nums.append(v)
    return tuple(nums)


def clear_exp_reward_base(dungeon_basic_lv: int) -> int:
    """s4a12 ExpTableProvider.GetExpRewardBase(basic_lv)."""
    table = load_clear_exp_reward_base_table()
    lv = int(dungeon_basic_lv) or 1
    if lv < 1 or not table:
        return 0
    if lv > len(table):
        return int(table[-1])
    return int(table[lv - 1])


@lru_cache(maxsize=1)
def load_clear_rank_exp_bonus_rates() -> tuple[float, ...]:
    """PVF ``[clear rank exp bonusrate]``; s4a12 fallback 0/1/2/3/5%."""
    text = _read_first_text(_SERVER_PARAM_CANDIDATES)
    rates = _section_floats(text, "[clear rank exp bonusrate]")
    if rates:
        return tuple(float(x) for x in rates)
    return (0.0, 0.01, 0.02, 0.03, 0.05)


@lru_cache(maxsize=1)
def load_clear_rank_grades() -> tuple[int, ...]:
    """PVF RankSystemInfo ``[rank grade]``; s4a12 fallback 99..10."""
    text = _read_first_text(
        (
            "etc/ranksysteminfo.etc",
            "Etc/RankSystemInfo.etc",
            "etc/RankSystemInfo.etc",
        )
    )
    if text:
        rates = _section_floats(text, "[rank grade]")
        if rates:
            return tuple(int(x) for x in rates)
    return (99, 90, 80, 60, 50, 30, 20, 10)


def clear_rank_bonus_index(rank_point: int) -> int:
    """s4a12 MonsterRewardTable.GetClearRankBonusIndex — 4..-1."""
    grades = load_clear_rank_grades()
    score = max(0, min(255, int(rank_point)))
    if len(grades) > 0 and score >= grades[0]:
        return 4
    if len(grades) > 1 and score >= grades[1]:
        return 3
    if len(grades) > 2 and score >= grades[2]:
        return 2
    if len(grades) > 3 and score >= grades[3]:
        return 1
    if len(grades) > 4 and score >= grades[4]:
        return 0
    return -1


def clear_rank_exp_bonus_rate(rank_bonus_index: int) -> float:
    rates = load_clear_rank_exp_bonus_rates()
    idx = int(rank_bonus_index)
    if idx < 0 or idx >= len(rates):
        return 0.0
    return float(rates[idx])


def compute_clear_base_exp(
    dungeon_basic_lv: int,
    *,
    experience_weight: float = 1.0,
    difficulty: int = 0,
) -> int:
    """Clear settlement base exp.

    Default **s4a12** (``USDOF_GAME_DUNGEON_CLEAR_EXP_MODE=s4a12``)::

      base = GetExpRewardBase(basic_lv)   # questParameter [exp reward table]
      clear_base = floor(base * dgn_weight * difficulty_rate)

    Legacy friend / 616ff337 (``mode=friend``)::

      clear_base = floor(MonsterBaseExp(basic_lv) * 10 * weight * difficulty_rate)
    """
    weight = float(experience_weight) if float(experience_weight) >= 0 else 1.0
    diff_rate = difficulty_exp_rate(difficulty)
    mode = (
        str(os.environ.get("USDOF_GAME_DUNGEON_CLEAR_EXP_MODE") or "s4a12")
        .strip()
        .lower()
    )
    if mode in {"friend", "legacy", "x10", "base10", "616ff337"}:
        base = monster_base_exp(int(dungeon_basic_lv) or 1)
        return max(0, int(base * 10 * weight * diff_rate))
    # s4a12 / default
    base = clear_exp_reward_base(int(dungeon_basic_lv) or 1)
    if base <= 0:
        # warm/PVF missing: fall back to monster table without *10
        base = monster_base_exp(int(dungeon_basic_lv) or 1)
    return max(0, int(base * weight * diff_rate))


def compute_clear_reward_exp(
    dungeon_basic_lv: int,
    *,
    experience_weight: float = 1.0,
    difficulty: int = 0,
    client_rank_point: int = 0,
) -> dict[str, int]:
    """s4a12 CalculateClearRewardExp (base + score bonus; no premium).

    Returns keys: clear_base, score_bonus, total, rank_point, rank_bonus_index.
    """
    clear_base = compute_clear_base_exp(
        dungeon_basic_lv,
        experience_weight=experience_weight,
        difficulty=difficulty,
    )
    rank_point = max(0, min(255, int(client_rank_point)))
    rank_idx = clear_rank_bonus_index(rank_point)
    score_bonus = max(0, int(float(clear_base) * clear_rank_exp_bonus_rate(rank_idx)))
    clear_base_i = max(0, int(clear_base))
    score_bonus_i = max(0, int(score_bonus))
    total = int(clear_base_i) + int(score_bonus_i)
    if total > 0xFFFFFFFF:
        total = 0xFFFFFFFF
    return {
        "clear_base": int(clear_base_i),
        "score_bonus": int(score_bonus_i),
        "total": int(total),
        "rank_point": int(rank_point),
        "rank_bonus_index": int(rank_idx),
    }


def grant_exp_to_character(
    repo: Any,
    character_id: int,
    exp_delta: int,
    *,
    gold_delta: int = 0,
    session: Any = None,
    expected_level: int | None = None,
    expected_exp: int | None = None,
    consume_stackable: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Persist one reward through the Stage3 ordinary/Expertise transaction."""
    cid = int(character_id)
    delta = max(0, int(exp_delta))
    gold = max(0, int(gold_delta))
    if delta <= 0 and gold <= 0:
        lv0, exp0, expertise0 = repo.get_character_progression(cid)
        expertise_level0 = get_adventure_group_index().expertise_level_for_exp(
            expertise0
        )
        return {
            "ok": True,
            "old_level": lv0,
            "new_level": lv0,
            "levels_gained": 0,
            "old_exp": exp0,
            "exp": exp0,
            "ordinary_exp_added": 0,
            "old_expertise_exp": expertise0,
            "expertise_exp": expertise0,
            "expertise_exp_added": 0,
            "old_expertise_level": expertise_level0,
            "expertise_level": expertise_level0,
            "expertise_levels_gained": 0,
            "gold": 0,
            "exp_delta": 0,
            "note": "noop",
        }
    result = repo.apply_exp_and_level(
        cid,
        exp_delta=delta,
        gold_delta=gold,
        max_level=max_character_level(),
        thresholds=list(load_exp_thresholds()),
        normal_exp_cap=level90_exp_cap(),
        expertise_exp_cap=expertise_exp_cap(),
        expertise_exp_requirements=list(load_expertise_exp_requirements()),
        wallet_gold=True,
        expected_level=expected_level,
        expected_exp=expected_exp,
        consume_stackable=consume_stackable,
    )
    result["exp_delta"] = delta
    if session is not None and result.get("ok"):
        try:
            session.character_level = int(result.get("new_level") or 1)
            session.character_exp = int(result.get("exp") or 0)
            session.character_expertise_exp = int(
                result.get("expertise_exp") or 0
            )
        except Exception:
            pass
    return result


def read_character_level(repo: Any, character_id: int, *, session: Any = None) -> int:
    if session is not None:
        for attr in ("character_level", "level"):
            val = getattr(session, attr, None)
            if val is not None:
                try:
                    lv = int(val)
                    if lv > 0:
                        return lv
                except Exception:
                    pass
    _exp, level = repo.get_character_exp_level(int(character_id))
    return max(1, int(level))


def warmup_exp_tables() -> dict[str, Any]:
    """Boot-warm PVF-backed exp tables (call from boot_warmup)."""
    thr = load_exp_thresholds()
    expertise_requirements = load_expertise_exp_requirements()
    normal_cap = level90_exp_cap()
    expertise_cap_value = expertise_exp_cap()
    diff = load_dungeon_difficulty_exp_rates()
    mon = load_monster_type_exp_rates()
    clear_tbl = load_clear_exp_reward_base_table()
    rank_rates = load_clear_rank_exp_bonus_rates()
    return {
        "ok": 1,
        "exp_thresholds": len(thr),
        "max_character_level": max_character_level(),
        "level90_exp_cap": normal_cap,
        "expertise_exp_rows": len(expertise_requirements),
        "expertise_exp_cap": expertise_cap_value,
        "difficulty_rates": list(diff),
        "monster_type_rates": list(mon),
        "base_exp_levels": len(_BASE_EXP_BY_LEVEL),
        "clear_exp_reward_rows": len(clear_tbl),
        "clear_rank_bonus_rates": list(rank_rates),
        "sample_lv5_base": monster_base_exp(5),
        "sample_lv5_clear_reward_base": clear_exp_reward_base(5),
        "sample_lv5_clear_s4a12": compute_clear_base_exp(5, experience_weight=1.0, difficulty=0),
        "sample_diff0_rate": difficulty_exp_rate(0),
    }
