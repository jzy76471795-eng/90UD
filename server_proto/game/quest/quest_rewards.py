"""PVF-backed quest reward resolver for Game finish/accept paths.

Reads n_quest/quest.lst + *.qst + questParameter.etc via the shared
preloaded PVF reader only. Game chains must not open raw PVF themselves;
call resolve_* helpers here. Index tables are warmed at server boot.

Reward rules follow Stage3-compatible S4A12 QuestData.GetRewardExp:
- exp from questParameter table * difficulty weight * level penalty
- gold from reward int data itemId=0 + gold multiple
- item pairs from reward int data / reward selection int data / depend give
- title reward type skips bag item inserts
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import BASE_DIR, PVF_PATH
from game.quest.quest_reward_cache import REWARD_SECTION_TAGS
from pvf import pvf_runtime
from pvf.nkpi_reader import PvfReader


DEFAULT_QUEST_PVF = BASE_DIR / "data" / "pvf" / "USDOF_client_script_readable_20260705.pvf"

_LOCK = threading.RLock()
_READER: PvfReader | None = None
_READER_PATH: str | None = None
_QUEST_PATHS: dict[int, str] | None = None
_QUEST_SECTIONS: dict[int, tuple[dict[str, list[str]], str]] = {}
_QUEST_SECTIONS_FULLY_LOADED = False
_PARAM: "QuestParameterTable | None" = None


@dataclass(frozen=True)
class QuestRewardItem:
    item_id: int
    count: int


@dataclass(frozen=True)
class QuestReward:
    quest_id: int
    exp: int = 0
    gold: int = 0
    chain_type: int = 0
    # chain_type∈{1,2,20,21,10,25} 时来自 [reward int data] 首个 int
    # 一转=[grow type] → grow_number=新 first grow；觉醒=second grow
    grow_number: int = 0
    reward_type: str = "[item]"
    is_title: bool = False
    is_repeatable: bool = False
    ignore_level_for_exp: bool = False
    quest_min_level: int = 1
    difficulty: str = "G"
    gold_multiple: int = -1
    items: tuple[QuestRewardItem, ...] = ()
    # reward type=[title]：不进背包，写入称号簿（见 game/titlebook）
    title_items: tuple[QuestRewardItem, ...] = ()
    event_items: tuple[QuestRewardItem, ...] = ()
    source_path: str = ""
    note: str = ""


@dataclass
class QuestParameterTable:
    difficulty_weight: dict[str, int] = field(default_factory=dict)
    exp_table: list[int] = field(default_factory=list)
    gold_table: list[int] = field(default_factory=list)
    green_penalty: int = 80
    grey_penalty: int = 30

    def compute_exp(self, player_level: int, quest_min_level: int, difficulty: str, ignore_level: bool) -> int:
        level_diff = int(player_level) - int(quest_min_level)
        penalty = 100 if ignore_level else self._level_penalty(level_diff)
        lookup = int(player_level) if ignore_level else int(quest_min_level)
        base = self.exp_table[lookup - 1] if 1 <= lookup <= len(self.exp_table) else 0
        key = (difficulty or "G")[:1].upper()
        weight = int(self.difficulty_weight.get(key, 10))
        return max(0, int(penalty * ((int(weight) * int(base)) // 100) // 100)) & 0xFFFFFFFF

    def compute_gold(self, player_level: int, quest_min_level: int, gold_multiple: int, ignore_level: bool) -> int:
        mult = int(gold_multiple) if int(gold_multiple) > 0 else 100
        level_diff = int(player_level) - int(quest_min_level)
        penalty = 100 if ignore_level else self._level_penalty(level_diff)
        lookup = int(player_level) if ignore_level else int(quest_min_level)
        base = self.gold_table[lookup - 1] if 1 <= lookup <= len(self.gold_table) else 0
        return max(0, int(int(mult) * ((int(penalty) * int(base)) // 100) // 100)) & 0xFFFFFFFF

    def _level_penalty(self, level_diff: int) -> int:
        if 6 < int(level_diff) <= 11:
            return int(self.green_penalty)
        if int(level_diff) <= 11:
            return 100
        return int(self.grey_penalty)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return bool(default)
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "n"}


def _candidate_pvf_paths() -> list[Path]:
    raws = [
        os.environ.get("USDOF_QUEST_PVF"),
        os.environ.get("USDOF_CLIENT_PVF_READABLE"),
        str(DEFAULT_QUEST_PVF),
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
    raise FileNotFoundError("quest reward PVF not found in candidates")


def _get_reader() -> PvfReader:
    """Return the process-shared preloaded PVF reader only.

    Runtime hard rule (AGENTS 4.1.2): never open a private PvfReader for quest
    rewards. Server start must preload Script.pvf and boot-warm indexes first.
    """
    global _READER, _READER_PATH
    target = str(_resolve_pvf_path())
    with _LOCK:
        shared = pvf_runtime.get_shared_reader(target)
        if shared is None or not pvf_runtime.is_memory_prepared(target):
            shared = pvf_runtime.ensure_shared_reader(target)
        if shared is None or not pvf_runtime.is_memory_prepared(target):
            raise RuntimeError(
                f"shared memory PVF unavailable for quest rewards: {target}"
            )
        _READER = shared
        _READER_PATH = str(Path(target).resolve())
        return _READER


def _strip_tick(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("`") and text.endswith("`") and len(text) >= 2:
        return text[1:-1].strip()
    return text


def _parse_sections(
    text: str,
    *,
    allowed_tags: frozenset[str] | None = None,
) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("[/") and line.endswith("]"):
            current = None
            continue
        if line.startswith("[") and line.endswith("]"):
            tag = line[1:-1].strip().lower()
            if allowed_tags is None or tag in allowed_tags:
                current = tag
                sections.setdefault(tag, [])
            else:
                current = None
            continue
        if current is None:
            continue
        sections[current].append(line)
    return sections


def _section_text(sections: dict[str, list[str]], tag: str) -> str:
    return "\n".join(sections.get(tag.lower(), ()))


def _section_first(sections: dict[str, list[str]], tag: str, default: str = "") -> str:
    rows = sections.get(tag.lower(), ())
    return rows[0] if rows else default


def _parse_int_list(text: str) -> list[int]:
    out: list[int] = []
    for tok in re.split(r"[\s,]+", str(text or "").strip()):
        if not tok:
            continue
        tok = _strip_tick(tok)
        try:
            out.append(int(tok, 0))
        except Exception:
            continue
    return out


def parse_item_pairs(
    data: str,
    *,
    player_job: int = -1,
    player_grow_type: int = -1,
) -> list[QuestRewardItem]:
    tokens: list[str] = []
    for raw in re.split(r"[\s]+", str(data or "").strip()):
        if raw:
            tokens.append(raw)
    result: list[QuestRewardItem] = []
    i = 0
    while i < len(tokens):
        tok = _strip_tick(tokens[i])
        try:
            item_id = int(tok, 0)
        except Exception:
            i += 1
            continue
        i += 1
        if i < len(tokens) and "[job]" in tokens[i].lower():
            i += 1
            job_id = -1
            grow_type = -1
            count = 1
            if i < len(tokens):
                try:
                    job_id = int(_strip_tick(tokens[i]), 0)
                except Exception:
                    job_id = -1
                i += 1
            if i < len(tokens):
                try:
                    grow_type = int(_strip_tick(tokens[i]), 0)
                except Exception:
                    grow_type = -1
                i += 1
            if i < len(tokens):
                try:
                    count = int(_strip_tick(tokens[i]), 0)
                except Exception:
                    count = 1
                i += 1
            job_match = player_job < 0 or job_id == int(player_job)
            grow_match = (
                player_grow_type < 0
                or grow_type == -1
                or grow_type == (int(player_grow_type) & 0xF)
            )
            if item_id > 0 and job_match and grow_match:
                result.append(QuestRewardItem(item_id=item_id, count=max(0, int(count))))
        else:
            count = 0
            if i < len(tokens):
                try:
                    count = int(_strip_tick(tokens[i]), 0)
                except Exception:
                    count = 0
                i += 1
            if item_id >= 0:
                result.append(QuestRewardItem(item_id=item_id, count=max(0, int(count))))
    return result


def map_reward_type(reward_type: str) -> int:
    tag = _strip_tick(reward_type).lower()
    if not tag:
        return 0
    if not tag.startswith("["):
        tag = f"[{tag}]"
    mapping = {
        "[item]": 0,
        "[grow type]": 1,
        "[awakening type]": 2,
        "[creature evolution]": 10,
        "[expert job]": 20,
        "[slot expansion]": 21,
        "[event creature evolution]": 25,
        "[title]": 0,
    }
    return int(mapping.get(tag, 0))


def _load_quest_paths(reader: PvfReader) -> dict[int, str]:
    text = reader.read_text("n_quest/quest.lst")
    paths: dict[int, str] = {}
    lines_local = [ln.strip() for ln in text.splitlines() if ln.strip()]
    i = 0
    while i < len(lines_local):
        line = lines_local[i]
        if re.fullmatch(r"-?\d+", line):
            qid = int(line)
            rel = None
            if i + 1 < len(lines_local):
                nxt = lines_local[i + 1]
                m = re.search(r"`([^`]+)`", nxt)
                if m:
                    rel = m.group(1)
                    i += 2
                else:
                    i += 1
            else:
                i += 1
            if rel and qid not in paths:
                paths[qid] = ("n_quest/" + rel.replace("\\", "/").lstrip("/")).lower()
            continue
        i += 1
    return paths


def _parse_quest_parameter(text: str) -> QuestParameterTable:
    """Parse questParameter.etc.

    Token-decoded PVF usually emits one token per line, so difficulty is:
      `A`
      400
      `B`
      300
    and exp table may contain sentinel -1 rows that S4A12 skips (v >= 0 only).
    """
    table = QuestParameterTable()
    section: str | None = None
    pending_diff_key: str | None = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("[") and low.endswith("]"):
            if low == "[difficulty]":
                section = "diff"
                pending_diff_key = None
            elif low == "[/difficulty]":
                section = None
                pending_diff_key = None
            elif low == "[exp reward table]":
                section = "exp"
            elif low == "[gold reward table]":
                section = "gold"
            elif low.startswith("[green level penalty]"):
                section = "green"
            elif low.startswith("[grey level penalty]"):
                section = "grey"
            elif low.startswith("[/") or low.startswith("["):
                section = None
                pending_diff_key = None
            continue
        if section == "green":
            nums = _parse_int_list(line)
            if nums:
                table.green_penalty = int(nums[0])
            section = None
        elif section == "grey":
            nums = _parse_int_list(line)
            if nums:
                table.grey_penalty = int(nums[0])
            section = None
        elif section == "diff":
            tokens = [tok for tok in re.split(r"\s+", line) if tok]
            for tok in tokens:
                key = _strip_tick(tok)
                # numeric value
                try:
                    val = int(key, 0)
                    is_num = True
                except Exception:
                    is_num = False
                    val = 0
                if pending_diff_key is not None and is_num:
                    k = pending_diff_key
                    table.difficulty_weight[k] = val
                    if len(k) == 1:
                        table.difficulty_weight[k.upper()] = val
                    pending_diff_key = None
                elif not is_num and key:
                    # accept `A` / A / 10 (digit difficulties stay as full key)
                    pending_diff_key = key.upper()
                elif is_num and pending_diff_key is None:
                    # stray number; ignore
                    continue
        elif section == "exp":
            for n in _parse_int_list(line):
                if int(n) >= 0:
                    table.exp_table.append(int(n))
        elif section == "gold":
            for n in _parse_int_list(line):
                if int(n) >= 0:
                    table.gold_table.append(int(n))
    return table


def _ensure_index() -> tuple[PvfReader, dict[int, str], QuestParameterTable]:
    global _QUEST_PATHS, _PARAM
    reader = _get_reader()
    with _LOCK:
        if _QUEST_PATHS is None:
            _QUEST_PATHS = _load_quest_paths(reader)
        if _PARAM is None:
            _PARAM = _parse_quest_parameter(reader.read_text("n_quest/questParameter.etc"))
        return reader, _QUEST_PATHS, _PARAM


def _read_quest_sections(quest_id: int) -> tuple[dict[str, list[str]], str]:
    qid = int(quest_id)
    cached = _QUEST_SECTIONS.get(qid)
    if cached is not None:
        return cached
    reader, paths, _param = _ensure_index()
    rel = paths.get(qid)
    if not rel:
        return {}, ""
    text_local = reader.read_text(rel)
    parsed = (
        _parse_sections(text_local, allowed_tags=REWARD_SECTION_TAGS),
        rel,
    )
    with _LOCK:
        _QUEST_SECTIONS[qid] = parsed
    return parsed


def _is_title_quest(sections: dict[str, list[str]], path: str) -> bool:
    """仅 reward type=[title] 视为称号奖励（对齐 S4A12 IsTitleRewardQuest）。

    grade=[title] / path 含 /title/ 的称号簿槽位任务仍可能发 [item]，
    不得误清空背包奖励。
    """
    del path  # 保留签名兼容，不再用路径判定
    reward_type = _strip_tick(_section_first(sections, "reward type")).lower()
    tag = reward_type if reward_type.startswith("[") else f"[{reward_type}]"
    return tag in {"[title]", "title"} or reward_type == "title"


def _is_repeatable(sections: dict[str, list[str]]) -> bool:
    grade = _strip_tick(_section_first(sections, "grade")).lower()
    qtype = _strip_tick(_section_first(sections, "type")).lower()
    if grade in {
        "[daily]",
        "[daily random]",
        "[normaly repeat]",
        "[special daily]",
        "daily",
        "daily random",
        "normaly repeat",
        "special daily",
    }:
        return True
    if qtype in {"[repeat]", "repeat"}:
        return True
    return False


def resolve_quest_reward(
    quest_id: int,
    *,
    player_level: int = 1,
    player_job: int = -1,
    player_grow_type: int = -1,
    reward_select_idx: int = -1,
    multiplier: int = 1,
) -> QuestReward:
    if not _env_bool("USDOF_GAME_QUEST_REWARD_FROM_PVF", True):
        return QuestReward(quest_id=int(quest_id) & 0xFFFF, note="pvf_reward_off")

    qid = int(quest_id) & 0xFFFF
    try:
        sections, path = _read_quest_sections(qid)
    except Exception as exc:
        return QuestReward(quest_id=qid, note=f"pvf_read_fail:{exc}")
    if not sections:
        return QuestReward(quest_id=qid, note="quest_missing")

    _reader, _paths, param = _ensure_index()

    reward_type_raw = _section_first(sections, "reward type", "[item]")
    chain_type = map_reward_type(reward_type_raw)
    levels = _parse_int_list(_section_text(sections, "level"))
    quest_min_level = levels[0] if levels else 1
    difficulty = _strip_tick(_section_first(sections, "difficulty", "G")) or "G"
    gold_multiple_vals = _parse_int_list(_section_text(sections, "gold multiple"))
    gold_multiple = gold_multiple_vals[0] if gold_multiple_vals else -1
    ignore_level = bool(sections.get("ignore quest level 4 exp"))
    is_repeatable = _is_repeatable(sections)
    is_title = _is_title_quest(sections, path)

    exp = 0
    if not is_repeatable:
        exp = param.compute_exp(int(player_level), quest_min_level, difficulty, ignore_level)

    items: list[QuestRewardItem] = []
    gold = 0
    grow_number = 0
    if chain_type == 0:
        fixed = parse_item_pairs(
            _section_text(sections, "reward int data"),
            player_job=int(player_job),
            player_grow_type=int(player_grow_type),
        )
        for fr in fixed:
            if int(fr.item_id) == 0:
                if int(fr.count) > 0 or int(gold_multiple) > 0:
                    gold = param.compute_gold(
                        int(player_level),
                        quest_min_level,
                        gold_multiple if gold_multiple > 0 else 100,
                        ignore_level,
                    )
            elif int(fr.item_id) > 0 and int(fr.count) > 0:
                items.append(fr)

        if int(reward_select_idx) >= 0:
            selectable = parse_item_pairs(
                _section_text(sections, "reward selection int data"),
                player_job=int(player_job),
                player_grow_type=int(player_grow_type),
            )
            if int(reward_select_idx) < len(selectable):
                chosen = selectable[int(reward_select_idx)]
                if chosen.item_id > 0 and chosen.count > 0:
                    items.append(chosen)
    elif chain_type in (1, 2, 10, 20, 21, 25):
        # 转职/觉醒/专家职等：reward int data 首 int = growNumber（或 expert type）
        reward_vals = _parse_int_list(_section_text(sections, "reward int data"))
        if reward_vals:
            grow_number = int(reward_vals[0])

    mult = max(1, int(multiplier or 1))
    exp = (int(exp) * mult) & 0xFFFFFFFF
    gold = (int(gold) * mult) & 0xFFFFFFFF
    # 称号奖励：保留 title_items，items 清空以免误入背包
    title_items: list[QuestRewardItem] = []
    if is_title:
        title_items = [it for it in items if int(it.item_id) > 0]
        items = []

    event_items = [
        it
        for it in parse_item_pairs(
            _section_text(sections, "depend give item"),
            player_job=int(player_job),
            player_grow_type=int(player_grow_type),
        )
        if it.item_id > 0 and it.count > 0
    ]
    clear_items = [
        it
        for it in parse_item_pairs(
            _section_text(sections, "clear reward item"),
            player_job=int(player_job),
            player_grow_type=int(player_grow_type),
        )
        if it.item_id > 0 and it.count > 0
    ]
    note = "ok" if not clear_items else ("ok clear=%s" % len(clear_items))

    if title_items:
        note = (note + " title=%s" % len(title_items)).strip()
    if chain_type in (1, 2, 20) and grow_number:
        note = (note + " grow=%s" % grow_number).strip()

    return QuestReward(
        quest_id=qid,
        exp=int(exp),
        gold=int(gold),
        chain_type=int(chain_type),
        grow_number=int(grow_number),
        reward_type=_strip_tick(reward_type_raw) or "[item]",
        is_title=bool(is_title),
        is_repeatable=bool(is_repeatable),
        ignore_level_for_exp=bool(ignore_level),
        quest_min_level=int(quest_min_level),
        difficulty=str(difficulty)[:1].upper(),
        gold_multiple=int(gold_multiple),
        items=tuple(items),
        title_items=tuple(title_items),
        event_items=tuple(event_items),
        source_path=path,
        note=note,
    )


def resolve_accept_event_items(
    quest_id: int,
    *,
    player_job: int = -1,
    player_grow_type: int = -1,
) -> list[QuestRewardItem]:
    reward = resolve_quest_reward(
        quest_id,
        player_level=1,
        player_job=player_job,
        player_grow_type=player_grow_type,
    )
    return list(reward.event_items)


def warmup_quest_reward_index(
    *,
    full_sections: bool = True,
    cache: Any = None,
) -> dict[str, int | str]:
    """Boot-time quest reward index warmup (shared PVF only).

    By default parse every quest reward script into section cache so finish/
    accept paths do not pay first-touch PVF text parse cost.
    """
    global _QUEST_PATHS, _QUEST_SECTIONS, _QUEST_SECTIONS_FULLY_LOADED, _PARAM
    reader = _get_reader()
    cache_status: str | None = None
    if cache is not None and full_sections:
        from game.quest.quest_reward_cache import (
            CACHE_DOMAIN,
            CACHE_SEMANTIC_VERSION,
            restore,
        )

        payload = cache.read(CACHE_DOMAIN, CACHE_SEMANTIC_VERSION)
        if payload is not None:
            try:
                paths, sections, param = restore(payload, QuestParameterTable)
                with _LOCK:
                    _QUEST_PATHS = paths
                    _QUEST_SECTIONS = sections
                    _PARAM = param
                    _QUEST_SECTIONS_FULLY_LOADED = True
                cache_status = "hit"
            except Exception:
                cache.discard(CACHE_DOMAIN)
                cache_status = "rebuild"

    if cache_status != "hit":
        reader, paths, param = _ensure_index()
    else:
        paths = _QUEST_PATHS or {}
        param = _PARAM or QuestParameterTable()
    sections_loaded = 0
    if full_sections and not _QUEST_SECTIONS_FULLY_LOADED:
        with _LOCK:
            if not _QUEST_SECTIONS_FULLY_LOADED:
                for qid, rel in paths.items():
                    if int(qid) in _QUEST_SECTIONS:
                        continue
                    try:
                        text_local = reader.read_text(rel)
                        _QUEST_SECTIONS[int(qid)] = (
                            _parse_sections(
                                text_local,
                                allowed_tags=REWARD_SECTION_TAGS,
                            ),
                            rel,
                        )
                        sections_loaded += 1
                    except Exception:
                        continue
                _QUEST_SECTIONS_FULLY_LOADED = True
    if (
        cache is not None
        and cache.enabled
        and full_sections
        and cache_status != "hit"
    ):
        from game.quest.quest_reward_cache import (
            CACHE_DOMAIN,
            CACHE_SEMANTIC_VERSION,
            to_payload,
        )

        cache.write(
            CACHE_DOMAIN,
            CACHE_SEMANTIC_VERSION,
            to_payload(
                paths,
                _QUEST_SECTIONS,
                param,
                fully_loaded=_QUEST_SECTIONS_FULLY_LOADED,
            ),
            record_count=len(_QUEST_SECTIONS),
        )
        cache_status = str(cache.last_status.get(CACHE_DOMAIN) or "miss")
    elif cache is not None and full_sections and not cache.enabled:
        cache_status = "disabled"
    personality_count = 0
    personality_status_count = 0
    time_fragment_count = 0
    time_fragment_job_count = 0
    time_fragment_effect_row_count = 0
    try:
        from game.quest.personality_rewards import warmup_personality_reward_index
        from game.quest.time_fragment_rewards import warmup_time_fragment_reward_index

        personality_report = warmup_personality_reward_index()
        personality_count = int(personality_report.get("personality_quest_count", 0) or 0)
        personality_status_count = int(personality_report.get("personality_status_count", 0) or 0)
        time_fragment_report = warmup_time_fragment_reward_index()
        time_fragment_count = int(time_fragment_report.get("time_fragment_quest_count", 0) or 0)
        time_fragment_job_count = int(time_fragment_report.get("time_fragment_job_count", 0) or 0)
        time_fragment_effect_row_count = int(
            time_fragment_report.get("time_fragment_effect_row_count", 0) or 0
        )
    except Exception:
        # This remains a hard PVF warmup domain.  Do not hide an index failure
        # behind a partial quest-reward report.
        raise
    result: dict[str, int | str] = {
        "ok": 1,
        "pvf": _READER_PATH or "",
        "quest_count": len(paths),
        "quest_paths": len(paths),
        "sections_cached": len(_QUEST_SECTIONS),
        "sections_loaded_now": sections_loaded,
        "sections_fully_loaded": int(bool(_QUEST_SECTIONS_FULLY_LOADED)),
        "exp_rows": len(param.exp_table),
        "gold_rows": len(param.gold_table),
        "diff_keys": len(param.difficulty_weight),
        "personality_quest_count": personality_count,
        "personality_status_count": personality_status_count,
        "time_fragment_quest_count": time_fragment_count,
        "time_fragment_job_count": time_fragment_job_count,
        "time_fragment_effect_row_count": time_fragment_effect_row_count,
        "reader": type(reader).__name__,
    }
    if cache is not None and full_sections:
        result["cache"] = cache_status or "miss"
    return result


def parse_finish_extra_fields(body: bytes, *, quest_id_layout: str) -> dict[str, int]:
    raw = bytes(body or b"")
    out = {"reward_select_idx": -1, "multiplier": 1, "has_reward_select": 0}
    base = 4 if quest_id_layout == "u16_opcode_echo+u16_quest" else 2
    if len(raw) >= base + 2:
        sel = int.from_bytes(raw[base : base + 2], "little")
        if sel != 0xFFFF:
            out["reward_select_idx"] = sel
            out["has_reward_select"] = 1
    if len(raw) >= base + 4:
        mult = int.from_bytes(raw[base + 2 : base + 4], "little")
        out["multiplier"] = mult if mult > 0 else 1
    return out
