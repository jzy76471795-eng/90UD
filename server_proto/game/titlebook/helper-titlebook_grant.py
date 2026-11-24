"""称号奖励写入称号簿 + 组包（交任务 / 进房回填）。

基线：``server_proto/docs/baseline_titlebook_20260719.md``（tip de78ad321）。

规则（Stage3 IDA + live 用户确认）:
- reward type=[title]：不进背包，写 titlebook.etc 固定槽

默认策略:
- 交任务 FINISH: DB 入簿 + **仅 0168**（获取动画；不叠 0166）
- 进房 ENTER: delayed **带条目 0166×5**，**不发 0168**
- 进房 **默认不做** finished-quest backfill
- 选角 SELECT（默认关）

开关（调试）:
- ``USDOF_GAME_TITLEBOOK_SEND_ON_ENTER=0`` 关进房 0166
- ``USDOF_GAME_TITLEBOOK_ENTER_0166_ENTRIES=0`` 进房只发空 0166
- ``USDOF_GAME_TITLEBOOK_ENTER_0168=1`` 进房额外 0168（会动画）
- ``USDOF_GAME_TITLEBOOK_BACKFILL_FINISHED=1`` 开进房 backfill（慎用）
- ``USDOF_GAME_TITLEBOOK_SEND_ON_SELECT=1`` 开选角即时 0166
- ``USDOF_GAME_TITLEBOOK_SEND_ON_FINISH=0`` 关交任务 0168
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

from game.chain_loader import load_chain
from game.quest.quest_common import env_bool
from game.titlebook.titlebook_index import (
    CATEGORY_NAMES,
    get_titlebook_index,
    resolve_title_items_for_quest,
)

_payloads = load_chain("helper-titlebook_payloads.py")
build_titlebook_list_0166 = _payloads.build_titlebook_list_0166
build_achievement_complete_0168 = _payloads.build_achievement_complete_0168
entries_from_repo_rows = _payloads.entries_from_repo_rows


def grant_title_rewards(
    repo: Any,
    *,
    character_id: int,
    quest_id: int,
    title_item_ids: Sequence[int],
) -> tuple[list[dict[str, int]], str]:
    """把称号物品写入 **titlebook.etc 指定槽**。返回 (granted_rows, note)。"""
    cid = int(character_id)
    qid = int(quest_id) & 0xFFFF
    pairs = resolve_title_items_for_quest(qid, title_item_ids)
    if not pairs:
        # 无 titlebook 槽映射时禁止乱塞；记 skip 等 etc 补映射
        return [], "title_skip no_slot quest=%s items=%s" % (
            qid,
            list(title_item_ids),
        )

    granted: list[dict[str, int]] = []
    notes: list[str] = []
    for slot, item_id in pairs:
        # 硬校验：槽必须 open 且允许该 item（每称号独立槽）
        if not slot.is_open:
            notes.append(
                "skip_closed cat=%s idx=%s item=%s" % (slot.category, slot.index, item_id)
            )
            continue
        if not slot.allows_item(int(item_id)):
            notes.append(
                "skip_not_allowed cat=%s idx=%s item=%s allowed=%s"
                % (slot.category, slot.index, item_id, list(slot.allowed_item_ids))
            )
            continue
        # S4A12 TriggerAchievement: Value = titleItemId；Slot = book_index
        ok, note, row = repo.upsert_titlebook_item(
            cid,
            category=int(slot.category),
            book_index=int(slot.index),
            item_id=int(item_id),
            value=int(item_id),
            source_quest_id=qid,
        )
        detail = "%s etc_q=%s" % (note, int(slot.quest_id))
        notes.append(detail if ok else "fail:%s" % detail)
        if ok and row:
            # 附带 etc 槽信息，便于日志核对「每称号一槽」
            row = dict(row)
            row["etc_quest_id"] = int(slot.quest_id)
            row["slot_type"] = int(slot.slot_type)
            granted.append(row)
    return granted, "title_grant " + ";".join(notes)


def pack_title_grant_packets(
    server: Any,
    *,
    character_id: int,
    quest_id: int,
    granted_rows: Sequence[dict[str, int]],
) -> tuple[list[bytes], str]:
    """交任务成功后的称号簿通知（默认 0168 获取动画）。

    - 默认 ``USDOF_GAME_TITLEBOOK_SEND_ON_FINISH=1``：发 0168（动画）
    - 默认 **不** 发 0166（live 交任务叠 0166 曾闪退）
    - 槽位以 granted_rows 的 category/book_index 为准（etc 固定槽）
    """
    if not granted_rows:
        return [], "title_pkts=0"
    slot_notes = [
        "cat=%s idx=%s item=%s"
        % (
            int(r.get("category", 0) or 0),
            int(r.get("book_index", 0) or 0),
            int(r.get("item_id", 0) or 0),
        )
        for r in granted_rows
        if int(r.get("item_id", 0) or 0) > 0
    ]
    if not env_bool("USDOF_GAME_TITLEBOOK_SEND_ON_FINISH", True):
        return [], "title_pkts=0 finish_notify_off granted=%s slots=[%s]" % (
            len(granted_rows),
            ",".join(slot_notes) if slot_notes else "-",
        )

    packets: list[bytes] = []
    cats: set[int] = set()
    n_0168 = 0
    # 0168 = 获取/解锁动画真源（handler 无静默开关）
    send_0168 = env_bool("USDOF_GAME_TITLEBOOK_SEND_0168", True)
    for row in granted_rows:
        cat = int(row.get("category", 0) or 0)
        idx = int(row.get("book_index", 0) or 0)
        iid = int(row.get("item_id", 0) or 0)
        if iid <= 0:
            continue
        if send_0168:
            body = build_achievement_complete_0168(
                int(quest_id), cat, idx, iid
            )
            packets.append(server._codec_response(0, 0x0168, body))
            n_0168 += 1
        cats.add(cat)

    # 0166 分类刷新：默认关（交任务叠 0166 live 曾闪）
    n_0166 = 0
    if env_bool("USDOF_GAME_TITLEBOOK_SEND_0166_ON_FINISH", False) and cats:
        repo = server._repo()
        for cat in sorted(cats):
            rows = repo.list_titlebook_items(int(character_id), category=cat)
            body = build_titlebook_list_0166(cat, entries_from_repo_rows(rows))
            packets.append(server._codec_response(0, 0x0166, body))
            n_0166 += 1
    return packets, "title_pkts=%s 0168=%s 0166=%s cats=%s slots=[%s]" % (
        len(packets),
        n_0168,
        n_0166,
        sorted(cats),
        ",".join(slot_notes) if slot_notes else "-",
    )


def pack_titlebook_sync_packets(
    server: Any,
    *,
    character_id: int,
    backfill_finished: bool = True,
    phase: str = "enter",
) -> tuple[list[bytes], str]:
    """称号簿同步组包。

    phase:
      - ``enter``（默认）: 进房 delayed —— **带条目 0166×5**，默认无 0168
      - ``select``: 选角即时 —— 默认关（曾闪退）；opt-in 带条目 0166
    """
    phase_key = str(phase or "enter").strip().lower()
    if phase_key not in ("select", "enter"):
        phase_key = "enter"

    if phase_key == "select":
        if not env_bool("USDOF_GAME_TITLEBOOK_SEND_ON_SELECT", False):
            return [], "titlebook_select_off"
    else:
        if not env_bool("USDOF_GAME_TITLEBOOK_SEND_ON_ENTER", True):
            return [], "titlebook_enter_off"

    repo = server._repo()
    cid = int(character_id)
    notes: list[str] = ["phase=%s" % phase_key]

    # 默认关 backfill：live 曾把 finished 1028→26598 写回，冲掉「仅 26596」对照。
    if backfill_finished and env_bool("USDOF_GAME_TITLEBOOK_BACKFILL_FINISHED", False):
        n, bnote = backfill_finished_title_quests(repo, character_id=cid)
        notes.append("backfill=%s %s" % (n, bnote))
    elif backfill_finished:
        notes.append("backfill=off")

    packets: list[bytes] = []
    idx = get_titlebook_index()
    cat_n = max(len(CATEGORY_NAMES), len(idx.capacities), 5)

    rows_by_cat: dict[int, list[dict[str, int]]] = {c: [] for c in range(cat_n)}
    all_rows: list[dict[str, int]] = []
    for cat in range(cat_n):
        for row in repo.list_titlebook_items(cid, category=cat):
            if int(row.get("item_id", 0) or 0) > 0:
                d = dict(row)
                rows_by_cat[cat].append(d)
                all_rows.append(d)

    # enter/select 默认都带条目（enter 是主路径；select 仅 opt-in）
    if phase_key == "select":
        use_entries = env_bool("USDOF_GAME_TITLEBOOK_SELECT_0166_ENTRIES", True)
        if env_bool("USDOF_GAME_TITLEBOOK_SELECT_0166_EMPTY_ONLY", False):
            use_entries = False
    else:
        use_entries = env_bool("USDOF_GAME_TITLEBOOK_ENTER_0166_ENTRIES", True)
        if env_bool("USDOF_GAME_TITLEBOOK_ENTER_0166_EMPTY_ONLY", False):
            use_entries = False

    n_entries = 0
    for cat in range(cat_n):
        if use_entries:
            ents = entries_from_repo_rows(rows_by_cat.get(cat) or ())
            n_entries += len(ents)
            body = build_titlebook_list_0166(cat, ents)
        else:
            body = build_titlebook_list_0166(cat, ())
        packets.append(server._codec_response(0, 0x0166, body))
    if use_entries:
        notes.append("0166_entries cats=%s entries=%s" % (cat_n, n_entries))
    else:
        notes.append("0166_empty_cats=%s" % cat_n)

    # 默认都不发 0168（无进房全量获取动画）；opt-in 才开
    if phase_key == "select":
        send_0168 = env_bool("USDOF_GAME_TITLEBOOK_SELECT_0168", False)
    else:
        send_0168 = env_bool("USDOF_GAME_TITLEBOOK_ENTER_0168", False)

    n_0168 = 0
    if send_0168 and all_rows:
        for row in all_rows:
            cat = int(row.get("category", 0) or 0)
            book = int(row.get("book_index", 0) or 0)
            iid = int(row.get("item_id", 0) or 0)
            if iid <= 0 or cat < 0 or cat >= cat_n or book < 0:
                continue
            qid = int(row.get("source_quest_id", 0) or 0)
            if qid <= 0:
                slot = idx.find_by_item_id(iid)
                if slot is not None and int(slot.quest_id) > 0:
                    qid = int(slot.quest_id)
            if qid <= 0:
                qid = 0
            body = build_achievement_complete_0168(qid, cat, book, iid)
            packets.append(server._codec_response(0, 0x0168, body))
            n_0168 += 1
        notes.append("0168_unlock=%s rows=%s" % (n_0168, len(all_rows)))
    elif not all_rows:
        notes.append("0168_unlock=0 no_db_rows")
    else:
        notes.append("0168_unlock=off db_rows=%s" % len(all_rows))

    return packets, "titlebook_%s %s" % (phase_key, ";".join(notes))


def pack_titlebook_batch_snapshot_packets(
    server: Any,
    *,
    character_id: int,
) -> tuple[list[bytes], str]:
    """Publish one bounded full title-book snapshot after an internal batch."""
    repo = server._repo()
    cid = int(character_id)
    idx = get_titlebook_index()
    category_count = max(len(CATEGORY_NAMES), len(idx.capacities), 5)
    packets: list[bytes] = []
    entry_count = 0
    for category in range(category_count):
        rows = repo.list_titlebook_items(cid, category=category)
        entries = entries_from_repo_rows(rows)
        entry_count += len(entries)
        packets.append(
            server._codec_response(
                0,
                0x0166,
                build_titlebook_list_0166(category, entries),
            )
        )
    return packets, "titlebook_batch categories=%s entries=%s" % (
        category_count,
        entry_count,
    )


def backfill_finished_title_quests(
    repo: Any,
    *,
    character_id: int,
) -> tuple[int, str]:
    """对已完成的 [title] 奖励任务，若称号簿仍空则补写。"""
    from game.quest.quest_rewards import resolve_quest_reward

    cid = int(character_id)
    finished = repo.list_finished_quests(cid)
    if not finished:
        return 0, "no_finished"
    granted_n = 0
    notes: list[str] = []
    for qid, _flag in finished:
        try:
            reward = resolve_quest_reward(int(qid), player_level=1)
        except Exception as exc:
            notes.append("q%s:err:%s" % (qid, type(exc).__name__))
            continue
        if not reward.is_title:
            continue
        item_ids = [it.item_id for it in (reward.title_items or ())]
        if not item_ids:
            # 兼容：若 resolve 仍清空，再读一次 raw 不走 is_title 清空路径
            item_ids = _peek_title_item_ids(int(qid))
        if not item_ids:
            notes.append("q%s:no_items" % qid)
            continue
        # 已有任一目标槽则跳过
        pairs = resolve_title_items_for_quest(int(qid), item_ids)
        already = False
        for slot, _iid in pairs:
            existing = repo.get_titlebook_item(
                cid, category=int(slot.category), book_index=int(slot.index)
            )
            if existing and int(existing.get("item_id", 0) or 0) > 0:
                already = True
                break
        if already:
            continue
        rows, gnote = grant_title_rewards(
            repo,
            character_id=cid,
            quest_id=int(qid),
            title_item_ids=item_ids,
        )
        if rows:
            granted_n += len(rows)
            notes.append("q%s:+%s" % (qid, len(rows)))
        else:
            notes.append("q%s:%s" % (qid, gnote))
    return granted_n, ";".join(notes) if notes else "noop"


def _peek_title_item_ids(quest_id: int) -> list[int]:
    """直接从 quest section 读 reward int data 的 title 物品（不经 is_title 清空）。"""
    try:
        from game.quest import quest_rewards as qr

        sections, _path = qr._read_quest_sections(int(quest_id) & 0xFFFF)
        if not sections:
            return []
        pairs = qr.parse_item_pairs(qr._section_text(sections, "reward int data"))
        return [int(p.item_id) for p in pairs if int(p.item_id) > 0]
    except Exception:
        return []
