"""Dungeon ally companions — 会话跟踪（**默认不跨房注入 mon**）。

## 权威结论（2026-07-22 live 再钉）

1. 官方无「过图带 APC」包；下一房要见 APC 只能 001D mon 再有该行。
2. **type5 跨房克隆闪退**：22:38 图2 只 ``injected=31509``（**1 个**，mon 22→23）即崩；
   不是「生成了多个 APC」。INJECT 必须 **OFF**。
3. 本模块只负责服务端同伴登记与可选的 mon 编排；客户端控制权由原生路径处理。
4. 进房 001D 以 map 原生行为准（例 59260 mon=9 含 31509 type5）；本模块只 harvest，inject 仅 A/B。

Env:
  USDOF_GAME_DUNGEON_ALLY_COMPANIONS=1
  USDOF_GAME_DUNGEON_ALLY_COMPANIONS_QUEST_SEED=1
  USDOF_GAME_DUNGEON_ALLY_COMPANIONS_INJECT=0    # 硬关
  USDOF_GAME_DUNGEON_ALLY_COMPANIONS_INJECT_AS_FACE=0
"""
from __future__ import annotations

import os
import re
from typing import Any, Iterable, Sequence

from game.chain_loader import load_chain

_map_mons = load_chain("helper-dungeon_map_monsters.py")

actor_is_ally = _map_mons.actor_is_ally
actor_is_hostile = _map_mons.actor_is_hostile
resolve_ai_character_level = _map_mons.resolve_ai_character_level
resolve_map_path = _map_mons.resolve_map_path
parse_event_monster_positions = _map_mons.parse_event_monster_positions

_APC_DIALOG_RE = re.compile(r"<apc::\s*(\d+)\s*>", re.IGNORECASE)
_INT_TOKEN_RE = re.compile(r"\d+")

# 会话字段
_SESSION_KEY = "dungeon_ally_companions"

# 不参与 wire 克隆的会话/运行时键（克隆时剥掉）
_SKIP_CLONE_KEYS = {
    "killed",
    "companion_dead",
    "alive",
    "defected",
    "source",
    "seen_on_map",
    "wire_row",
    "is_companion",
    "companion_source",
    "companion_carried",
}


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return bool(default)
    return str(raw).strip().lower() not in {"0", "false", "off", "no", "n", ""}


def companions_enabled() -> bool:
    return _env_bool("USDOF_GAME_DUNGEON_ALLY_COMPANIONS", True)


def quest_seed_enabled() -> bool:
    return _env_bool("USDOF_GAME_DUNGEON_ALLY_COMPANIONS_QUEST_SEED", True)


def inject_enabled() -> bool:
    """过图是否把已见友方克隆进 mon 表。

    **默认 OFF**（2026-07-22 live 再证伪）：图2 仅 inject **1 个** 31509
    （mon 22→23）即闪退；不是「生成了多个 APC」。type5 非本图 mon 行
    客户端不接受。日常只 harvest；跟身另议。
    """
    return _env_bool("USDOF_GAME_DUNGEON_ALLY_COMPANIONS_INJECT", False)


def inject_as_face_enabled() -> bool:
    """把友方 APC 按剧情 face 形态加载（type=0 / kind=monster / team=0 绿名）。

    与 ``INJECT=1`` 联用：过图每房再塞一行 → 观感「自动跟随」。
    同时把 **本房已有** 的 type5 原生 APC 改写成 type0（避免可控/扣 MP）。
    """
    return _env_bool("USDOF_GAME_DUNGEON_ALLY_COMPANIONS_INJECT_AS_FACE", False)


def _rewrite_row_as_face(mon: dict[str, Any]) -> dict[str, Any]:
    """单行：对齐 build_story_face_preload_row 的绿名友方 wire。"""
    row = dict(mon or {})
    row["kind"] = "monster"
    row["type"] = 0
    row["team"] = 0
    row["story_face_preload"] = True
    row["companion_as_face"] = True
    row["is_ally"] = True
    row["is_hostile"] = False
    row["role"] = "ally"
    row["faction"] = "character"
    row["is_blocking"] = False
    row["grants_exp"] = False
    row["killable"] = False
    return row


def rewrite_ally_rows_as_face(
    mons: Iterable[dict] | None,
    session: Any = None,
) -> list[dict[str, Any]]:
    """把本房友方 APC / 已登记同伴改写成 face 绿名形态（type=0）。

    仅 ``INJECT_AS_FACE=1`` 时生效。先 harvest 再调用，避免 kind 改写后采不到。
    """
    base = [dict(m) for m in list(mons or [])]
    if not inject_as_face_enabled():
        return base
    store = _companion_store(session) if session is not None else {}
    out: list[dict[str, Any]] = []
    for mon in base:
        row = dict(mon)
        try:
            code = int(row.get("code") or row.get("script_id") or 0)
        except Exception:
            code = 0
        mon_type = int(row.get("type") or row.get("mon_type") or 0) & 0xFF
        kind = str(row.get("kind") or "").lower()
        is_target = False
        if row.get("companion_as_face") or row.get("companion_carried") or row.get("is_companion"):
            is_target = True
        elif is_follow_companion_actor(row):
            is_target = True
        elif kind == "ai_character" and 5 <= mon_type <= 8 and actor_is_ally(row):
            is_target = True
        elif code > 0 and code in store and bool((store.get(code) or {}).get("seen_on_map")):
            if kind == "ai_character" or 5 <= mon_type <= 8:
                is_target = True
        if is_target:
            row = _rewrite_row_as_face(row)
        out.append(row)
    return out


def _ai_lst() -> dict[int, str]:
    """AICharacter.lst code → path（经 map mon helper 缓存）。"""
    try:
        return dict(_map_mons._ai_character_lst())  # noqa: SLF001 — 同包运行时真源
    except Exception:
        return {}


def is_ai_character_code(code: int) -> bool:
    cid = int(code or 0)
    if cid <= 0:
        return False
    return cid in _ai_lst()


def is_follow_companion_actor(mon: dict[str, Any] | None) -> bool:
    """是否应作为跨房跟随同伴采集。

    仅 **友方 AI character**（faction=character / role=ally）。
    不携带：敌对 APC、中立、story_face_preload 假怪、team=0 普通怪
    （后者常是房内站桩，不是跟随同伴）。
    """
    if not mon:
        return False
    if mon.get("story_face_preload"):
        return False
    if mon.get("killed"):
        return False
    kind = str(mon.get("kind") or "").lower()
    if kind != "ai_character":
        return False
    if actor_is_hostile(mon):
        return False
    # 友方 / 非敌对 AI：is_ally 或 role=ally 或 faction=character
    if actor_is_ally(mon):
        return True
    fac = str(mon.get("faction") or "").lower()
    role = str(mon.get("role") or "").lower()
    if fac in {"character", "friend", "ally", "team", "party", "0"}:
        return True
    if role == "ally":
        return True
    # 中立 AI 不跟
    if fac == "neutral" or role == "neutral":
        return False
    return False


def _is_passive_kind(mon: dict[str, Any] | None) -> bool:
    kind = str((mon or {}).get("kind") or "").lower()
    return kind in {
        "special_passive",
        "passive_object",
        "passive",
        "map_passive",
    }


def _snapshot_wire_row(mon: dict[str, Any]) -> dict[str, Any]:
    """从 map load 产出的 mon 行抽出可克隆的 wire 字段快照。"""
    row: dict[str, Any] = {}
    for k, v in dict(mon or {}).items():
        if k in _SKIP_CLONE_KEYS:
            continue
        row[k] = v
    # 强制友方战斗语义（不挡门、不给经验）；保留 map 的 type/level/flag
    row["kind"] = "ai_character"
    row["faction"] = str(row.get("faction") or "character")
    row["role"] = "ally"
    row["is_hostile"] = False
    row["is_ally"] = True
    row["is_blocking"] = False
    row["grants_exp"] = False
    mon_type = int(row.get("type") or row.get("mon_type") or 5) & 0xFF
    if not (5 <= mon_type <= 8):
        mon_type = 5
    row["type"] = mon_type
    code = int(row.get("code") or row.get("script_id") or 0) & 0xFFFFFFFF
    row["code"] = code
    level = int(row.get("level") or 0) & 0xFF
    if level <= 0:
        lv = resolve_ai_character_level(code)
        level = int(lv if lv is not None else 1) & 0xFF
    row["level"] = max(1, level)
    # killable 以 map 为准（任务文案「同伴死亡无法完成」）
    if "killable" not in row:
        row["killable"] = False
    return row


def _template_from_mon(mon: dict[str, Any]) -> dict[str, Any]:
    """会话模板：元数据 + 完整 wire 快照。"""
    wire = _snapshot_wire_row(mon)
    return {
        "code": int(wire.get("code") or 0),
        "kind": "ai_character",
        "type": int(wire.get("type") or 5),
        "level": int(wire.get("level") or 1),
        "faction": str(wire.get("faction") or "character"),
        "role": "ally",
        "is_hostile": False,
        "is_ally": True,
        "is_blocking": False,
        "grants_exp": False,
        "killable": bool(wire.get("killable", False)),
        "quest_enemy_type": int(wire.get("quest_enemy_type") or 0),
        "direction": int(wire.get("direction") or 0) & 0xFF,
        "source": "map",
        "seen_on_map": True,
        "alive": True,
        "defected": False,
        "wire_row": wire,
    }


def _template_from_code(code: int, *, dungeon_basic_lv: int = 0, source: str = "quest") -> dict[str, Any]:
    """任务预登记骨架：无 wire_row / seen_on_map=False → 禁止注入。"""
    cid = int(code or 0) & 0xFFFFFFFF
    lv = resolve_ai_character_level(cid)
    if lv is None:
        basic = max(1, min(255, int(dungeon_basic_lv or 0) if int(dungeon_basic_lv or 0) > 0 else 1))
        level = basic
    else:
        level = int(lv) & 0xFF
    return {
        "code": cid,
        "kind": "ai_character",
        "type": 5,
        "level": max(1, level),
        "faction": "character",
        "role": "ally",
        "is_hostile": False,
        "is_ally": True,
        "is_blocking": False,
        "grants_exp": False,
        "killable": False,
        "quest_enemy_type": 0,
        "direction": 0,
        "source": source,
        "seen_on_map": False,
        "alive": True,
        "defected": False,
        "wire_row": None,
    }


def _companion_store(session: Any) -> dict[int, dict[str, Any]]:
    if session is None:
        return {}
    raw = getattr(session, _SESSION_KEY, None)
    if isinstance(raw, dict):
        out: dict[int, dict[str, Any]] = {}
        for k, v in raw.items():
            try:
                out[int(k)] = dict(v) if isinstance(v, dict) else {}
            except Exception:
                continue
        return out
    out: dict[int, dict[str, Any]] = {}
    try:
        setattr(session, _SESSION_KEY, out)
    except Exception:
        pass
    return out


def _set_store(session: Any, store: dict[int, dict[str, Any]]) -> None:
    if session is None:
        return
    try:
        setattr(session, _SESSION_KEY, store)
    except Exception:
        pass




def clear_companions(session: Any) -> None:
    if session is None:
        return
    try:
        setattr(session, _SESSION_KEY, {})
    except Exception:
        pass


def companion_codes(session: Any, *, alive_only: bool = True) -> list[int]:
    store = _companion_store(session)
    out: list[int] = []
    for code, tpl in store.items():
        if alive_only and not bool(tpl.get("alive", True)):
            continue
        if int(code) > 0:
            out.append(int(code))
    return sorted(out)


def companion_progress_codes(session: Any) -> tuple[frozenset[int], frozenset[int]]:
    """Return map-proven and still-alive companion codes for quest progress."""
    seen: set[int] = set()
    alive: set[int] = set()
    for code, tpl in _companion_store(session).items():
        cid = int(code)
        if cid <= 0 or not bool(tpl.get("seen_on_map")):
            continue
        seen.add(cid)
        if bool(tpl.get("alive", True)) and not bool(tpl.get("defected")):
            alive.add(cid)
    return frozenset(seen), frozenset(alive)


def mark_companion_killed(session: Any, code: int) -> bool:
    """同伴死亡：不再跨房注入。返回是否命中会话同伴。"""
    if session is None:
        return False
    cid = int(code or 0)
    if cid <= 0:
        return False
    store = _companion_store(session)
    tpl = store.get(cid)
    if not tpl:
        return False
    if not bool(tpl.get("alive", True)):
        return True
    tpl = dict(tpl)
    tpl["alive"] = False
    store[cid] = tpl
    _set_store(session, store)
    return True


def mark_companion_killed_from_die(
    session: Any,
    *,
    seq: int = 0,
    monster_code: int = 0,
) -> int:
    """Resolve a locally carried companion DIE by actor code or native seq.

    Cross-room companion actors are client-owned in Stage3 and are absent from
    the new room's 001D rows.  The client can still report their original actor
    sequence.  Match only a companion previously observed in a native map.
    """
    store = _companion_store(session)
    direct_code = int(monster_code or 0)
    if direct_code > 0:
        direct = store.get(direct_code)
        if direct and bool(direct.get("seen_on_map")):
            mark_companion_killed(session, direct_code)
            return direct_code

    target_seq = int(seq or 0)
    if target_seq <= 0:
        return 0
    for code, tpl in store.items():
        if not bool(tpl.get("seen_on_map")):
            continue
        wire = tpl.get("wire_row")
        if not isinstance(wire, dict):
            continue
        try:
            wire_seq = int(wire.get("seq") or 0)
        except Exception:
            wire_seq = 0
        if wire_seq != target_seq:
            continue
        cid = int(code)
        mark_companion_killed(session, cid)
        return cid
    return 0


def mark_companion_defected(session: Any, code: int) -> bool:
    """同伴叛变/变敌对：不再跨房注入。返回是否命中会话同伴。"""
    if session is None:
        return False
    cid = int(code or 0)
    if cid <= 0:
        return False
    store = _companion_store(session)
    tpl = store.get(cid)
    if not tpl:
        return False
    if bool(tpl.get("defected")):
        return True
    tpl = dict(tpl)
    tpl["defected"] = True
    tpl["alive"] = False  # 与死亡同效：后续 inject 跳过
    store[cid] = tpl
    _set_store(session, store)
    return True


def companion_is_carriable(tpl: dict[str, Any] | None) -> bool:
    """是否允许跨房克隆：存活、未叛变、本局 map 见过且有 wire 快照。"""
    if not isinstance(tpl, dict) or not tpl:
        return False
    if not bool(tpl.get("alive", True)):
        return False
    if bool(tpl.get("defected")):
        return False
    if not bool(tpl.get("seen_on_map")):
        return False
    wire = tpl.get("wire_row")
    return isinstance(wire, dict) and bool(wire)


def harvest_companions_from_mons(
    session: Any,
    mons: Iterable[dict] | None,
) -> list[int]:
    """从当前房 mon 表采集新友方 AI 进会话。返回新登记的 code 列表。

    若本房同 code 已是敌对/叛变形态，标记 defected，不再携带。
    """
    if not companions_enabled() or session is None:
        return []
    store = _companion_store(session)
    added: list[int] = []
    for mon in list(mons or []):
        code = int(mon.get("code") or mon.get("script_id") or 0)
        if code <= 0:
            continue
        # 已登记同伴在本房变成敌对 → 叛变，停止携带
        existing = store.get(code)
        if existing is not None and actor_is_hostile(mon):
            if bool(existing.get("seen_on_map")) or bool(existing.get("alive", True)):
                mark_companion_defected(session, code)
                store = _companion_store(session)
            continue
        if not is_follow_companion_actor(mon):
            continue
        existing = store.get(code)
        tpl = _template_from_mon(mon)
        if existing is not None:
            # 已死 / 已叛变：不复活
            if not bool(existing.get("alive", True)) or bool(existing.get("defected")):
                continue
            # map 行覆盖 wire 快照；保留 alive / 清 defected
            tpl["alive"] = True
            tpl["defected"] = False
            store[code] = tpl
            continue
        store[code] = tpl
        added.append(code)
    if added or store:
        _set_store(session, store)
    return added


def _parse_int_tokens(text: str) -> list[int]:
    out: list[int] = []
    for m in _INT_TOKEN_RE.findall(str(text or "")):
        try:
            v = int(m, 0)
        except ValueError:
            continue
        if v > 0:
            out.append(v)
    return out


def extract_quest_companion_codes(
    session: Any,
    *,
    extra_quest_ids: Sequence[int] | None = None,
) -> list[int]:
    """从进行中任务 int_data / dialog 提取 AICharacter code。"""
    if not quest_seed_enabled():
        return []
    qids: list[int] = []
    if session is not None:
        for raw in list(getattr(session, "active_quest_ids", None) or []):
            try:
                qids.append(int(raw))
            except Exception:
                continue
    for raw in list(extra_quest_ids or []):
        try:
            qids.append(int(raw))
        except Exception:
            continue
    # 去重保序
    seen: set[int] = set()
    ordered: list[int] = []
    for q in qids:
        if q > 0 and q not in seen:
            seen.add(q)
            ordered.append(q)
    if not ordered:
        return []

    try:
        from game.quest.quest_index import get_quest_index

        idx = get_quest_index()
    except Exception:
        return []

    ai_codes: set[int] = set()
    ai_table = _ai_lst()
    for qid in ordered:
        try:
            meta = idx.get_meta(int(qid))
        except Exception:
            meta = None
        if meta is None:
            continue
        int_data = getattr(meta, "int_data", "") or ""
        raw_text = getattr(meta, "raw_text", "") or ""
        candidates = set(_parse_int_tokens(int_data))
        for m in _APC_DIALOG_RE.finditer(raw_text):
            try:
                candidates.add(int(m.group(1), 0))
            except ValueError:
                continue
        for c in candidates:
            if c in ai_table:
                ai_codes.add(int(c))
    return sorted(ai_codes)


def seed_companions_from_quests(
    session: Any,
    *,
    dungeon_basic_lv: int = 0,
    extra_quest_ids: Sequence[int] | None = None,
) -> list[int]:
    """任务侧预登记同伴元数据（**不** 注入 mon；须 map 见过才跟）。"""
    if not companions_enabled() or session is None:
        return []
    codes = extract_quest_companion_codes(session, extra_quest_ids=extra_quest_ids)
    if not codes:
        return []
    store = _companion_store(session)
    added: list[int] = []
    for code in codes:
        if code in store:
            continue
        store[code] = _template_from_code(
            code, dungeon_basic_lv=dungeon_basic_lv, source="quest"
        )
        added.append(code)
    if added:
        _set_store(session, store)
    return added


def _insert_index_before_passives(mons: Sequence[dict[str, Any]]) -> int:
    """友方克隆插点：第一个 passive 之前；无 passive 则表尾。"""
    for i, mon in enumerate(list(mons or [])):
        if _is_passive_kind(mon):
            return i
    return len(list(mons or []))


def _assign_companion_indices(
    mons: Sequence[dict[str, Any]],
    *,
    first_seq: int = 0,
) -> list[dict[str, Any]]:
    """只给克隆同伴补 **seq**；slot/template_order 保持 map 原生快照。

    Stage3 真相（59260 毗奥拉 live 行）：
      - ``slot_index`` / ``template_order`` 来自 ``[ai character]`` **块内序号**
        （首个友方 APC 常为 0），**不是** mon 表全局下标；
      - 旧实现用 ``len(base)`` 写成 22 等大 slot → 过图 mon=22→23 后 ~2s 闪退。

    硬约束：
      - 不改 map 原生 passive 的 ``packet_index`` / ``slot_index``；
      - 克隆行保留 harvest 时 wire 快照的 slot/template_order；
      - 仅保证 seq 在本房 mon 表内唯一（接 max 原生 seq 之后）。
    """
    base = list(mons or [])
    max_seq = int(first_seq or 0)
    for mon in base:
        if mon.get("is_companion") or mon.get("companion_carried"):
            continue
        try:
            sq = int(mon.get("seq") or 0)
        except Exception:
            sq = 0
        if sq > max_seq:
            max_seq = sq

    next_seq = max_seq
    out: list[dict[str, Any]] = []
    for mon in base:
        row = dict(mon)
        if row.get("is_companion") or row.get("companion_carried"):
            # slot/template_order：优先已有（来自 wire 快照），缺则 0（与 map 首个 APC 一致）
            if row.get("slot_index") is None:
                row["slot_index"] = 0
            if row.get("template_order") is None:
                row["template_order"] = 0
            next_seq += 1
            row["seq"] = next_seq & 0xFFFF
        out.append(row)
    return out


def build_companion_mon_row(
    template: dict[str, Any],
    *,
    seq: int = 0,
    slot_index: Any = None,
    template_order: Any = None,
    x: Any = None,
    y: Any = None,
    as_face: bool = False,
) -> dict[str, Any]:
    """从会话模板构造注入行：优先整行克隆 map wire 快照（含原生 slot）。

    ``as_face=True``：改写成剧情 face 预载形态（type=0 / kind=monster / team=0），
    与 ``build_story_face_preload_row`` 同族。**仅 A/B**；APC code 不在 monster.lst。
    """
    wire = template.get("wire_row")
    if isinstance(wire, dict) and wire:
        row = dict(wire)
    else:
        # 无 map 快照：不该被 inject 调用；兜底最小友方行
        mon_type = int(template.get("type") or 5) & 0xFF
        if not (5 <= mon_type <= 8):
            mon_type = 5
        row = {
            "kind": "ai_character",
            "code": int(template.get("code") or 0) & 0xFFFFFFFF,
            "level": max(1, int(template.get("level") or 1) & 0xFF),
            "type": mon_type,
            "aux": 0,
            "aux2": 0,
            "flag0": 0,
            "flag1": 0,
            "extra_state": 0,
            "extra_flag": 0,
            "quest_enemy_type": 0,
            "faction": str(template.get("faction") or "character"),
            "role": "ally",
            "is_hostile": False,
            "is_ally": True,
            "is_blocking": False,
            "grants_exp": False,
            "killable": bool(template.get("killable", False)),
            "direction": int(template.get("direction") or 0) & 0xFF,
            "slot_index": 0,
            "template_order": 0,
        }
    if as_face:
        row = _rewrite_row_as_face(row)
    else:
        row["kind"] = "ai_character"
        mon_type = int(row.get("type") or 5) & 0xFF
        if not (5 <= mon_type <= 8):
            row["type"] = 5
    if seq:
        row["seq"] = int(seq) & 0xFFFF
    # 显式覆盖仅当调用方传入；默认保留 wire 快照 slot（map 块内序号）
    if slot_index is not None:
        row["slot_index"] = int(slot_index) & 0xFFFFFFFF
    elif row.get("slot_index") is None:
        row["slot_index"] = 0
    if template_order is not None:
        row["template_order"] = int(template_order) & 0xFFFF
    elif row.get("template_order") is None:
        row["template_order"] = 0
    row["is_hostile"] = False
    row["is_ally"] = True
    row["role"] = "ally"
    row["faction"] = str(row.get("faction") or "character")
    row["is_blocking"] = False
    row["grants_exp"] = False
    if x is not None:
        row["x"] = x
    if y is not None:
        row["y"] = y
    row["is_companion"] = True
    row["companion_source"] = str(template.get("source") or "map")
    row["companion_carried"] = True
    return row


def inject_companions_into_mons(
    session: Any,
    mons: Iterable[dict] | None,
    *,
    map_index: int = 0,
    first_seq: int = 0,
    dungeon_basic_lv: int = 0,
) -> tuple[list[dict[str, Any]], list[int]]:
    """把会话中 **已在本局 map 见过** 且存活、当前房缺失的同伴克隆进 mon 表。

    返回 ``(新 mon 表, 本次注入的 code 列表)``。
    调用前应先 harvest 当前 map 自带的 ally，避免重复。
    """
    base = list(mons or [])
    if not companions_enabled() or session is None:
        return base, []
    as_face = inject_as_face_enabled()
    if not inject_enabled():
        # 仍 harvest；as_face 时把本房原生 type5 改成绿名（不跨房克隆）
        harvest_companions_from_mons(session, base)
        if as_face:
            base = rewrite_ally_rows_as_face(base, session)
        return base, []

    # 先采集本房地图自带的友方 AI（必须在 type5→type0 改写之前）
    harvest_companions_from_mons(session, base)

    store = _companion_store(session)
    if not store:
        if as_face:
            base = rewrite_ally_rows_as_face(base, session)
        return base, []

    present: set[int] = set()
    for mon in base:
        try:
            c = int(mon.get("code") or mon.get("script_id") or 0)
        except Exception:
            continue
        if c > 0:
            present.add(c)

    injected: list[int] = []
    # 稳定顺序：code 升序；插在 passive 之前（每次插入后索引后移）
    # 累加语义：store 中所有 carriable 同伴，本房缺谁补谁（图1+图2 → 图2 两份）
    insert_at = _insert_index_before_passives(base)
    for code in sorted(store.keys()):
        tpl = store.get(code) or {}
        if int(code) in present:
            continue
        if not companion_is_carriable(tpl):
            continue
        # 克隆 map wire 快照；as_face=type0 绿名（A/B）
        row = build_companion_mon_row(tpl, as_face=as_face)
        base.insert(insert_at, row)
        insert_at += 1
        present.add(int(code))
        injected.append(int(code))

    if injected:
        # 只给克隆行补 slot/seq；map 原生 passive/怪的 slot 保持不动
        base = _assign_companion_indices(base, first_seq=int(first_seq or 0))
        _set_store(session, store)
    # 本房已有的 type5 原生 APC 也改成绿名（与注入行一致）
    if as_face:
        base = rewrite_ally_rows_as_face(base, session)
    return base, injected


def bind_companions_on_enter(
    session: Any,
    mons: Iterable[dict] | None,
    *,
    map_index: int = 0,
    dungeon_basic_lv: int = 0,
    first_seq: int = 0,
    extra_quest_ids: Sequence[int] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """进本：清旧集合 → 任务预登记 → 采集起点 map → 仅补缺（通常 map 已有）。

    返回 ``(mons, note)``。
    """
    if session is None:
        return list(mons or []), "ally_companions=no_session"
    if not companions_enabled():
        return list(mons or []), "ally_companions=off"

    clear_companions(session)
    seeded = seed_companions_from_quests(
        session,
        dungeon_basic_lv=dungeon_basic_lv,
        extra_quest_ids=extra_quest_ids,
    )
    base = list(mons or [])
    harvested = harvest_companions_from_mons(session, base)
    base, injected = inject_companions_into_mons(
        session,
        base,
        map_index=map_index,
        first_seq=first_seq,
        dungeon_basic_lv=dungeon_basic_lv,
    )
    codes = companion_codes(session, alive_only=True)
    map_seen = [
        c
        for c, t in _companion_store(session).items()
        if bool(t.get("seen_on_map")) and bool(t.get("alive", True))
    ]
    note = (
        "ally_companions enter seeded=%s harvested=%s injected=%s map_seen=%s alive=%s "
        "inject=%s as_face=%s"
        % (
            ",".join(str(x) for x in seeded) or "-",
            ",".join(str(x) for x in harvested) or "-",
            ",".join(str(x) for x in injected) or "-",
            ",".join(str(x) for x in sorted(map_seen)) or "-",
            ",".join(str(x) for x in codes) or "-",
            "on" if inject_enabled() else "off",
            "on" if inject_as_face_enabled() else "off",
        )
    )
    return base, note


def apply_companions_on_move_map(
    session: Any,
    mons: Iterable[dict] | None,
    *,
    map_index: int = 0,
    dungeon_basic_lv: int = 0,
    first_seq: int = 0,
    freeze_mons: bool = False,
) -> tuple[list[dict[str, Any]], str]:
    """过图：采集新房 map 友方 + 克隆本局已见同伴。

    ``freeze_mons``（通关结算后）跳过注入，避免结算 UI 多 actor。
    已清空房（mons=[]）仍注入，保证回房/进下一房同伴跟身直至 boss。
    """
    if session is None:
        return list(mons or []), "ally_companions=no_session"
    if not companions_enabled():
        return list(mons or []), "ally_companions=off"
    if freeze_mons:
        return list(mons or []), "ally_companions=skip_freeze"

    base = list(mons or [])
    harvested = harvest_companions_from_mons(session, base)
    base, injected = inject_companions_into_mons(
        session,
        base,
        map_index=map_index,
        first_seq=first_seq,
        dungeon_basic_lv=dungeon_basic_lv,
    )
    codes = companion_codes(session, alive_only=True)
    note = (
        "ally_companions move harvested=%s injected=%s alive=%s inject=%s as_face=%s"
        % (
            ",".join(str(x) for x in harvested) or "-",
            ",".join(str(x) for x in injected) or "-",
            ",".join(str(x) for x in codes) or "-",
            "on" if inject_enabled() else "off",
            "on" if inject_as_face_enabled() else "off",
        )
    )
    return base, note
