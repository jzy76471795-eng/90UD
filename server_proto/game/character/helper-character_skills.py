"""角色技能栏 / 技能树 DB 读写（character_skills）。

职责：
  - 首次进房：若无行则用 initial_skills JSON 灌 page0
  - 进房 / skillinfo：从 DB 读 slot/skill_id/level
  - C1/001C CHANGE_SKILLSLOT：三步交换槽位并落库

SP 余额不在本表权威存储（由 helper-skill_points 按等级派生）；
page_header 仅作可选镜像，方便抓包对照。
"""
from __future__ import annotations

from typing import Any, Sequence

from game.character.initial_skills import InitialSkill, get_initial_skills_for_job
from game.chain_loader import load_chain


def rows_to_initial_skills(rows: Sequence[dict[str, Any]]) -> list[InitialSkill]:
    """DB 行 → InitialSkill 列表（过滤 skill_id/level 无效项）。

    保留 ``extra_values`` 为 command_bytes，供进房 skillinfo field4 回写。
    """
    out: list[InitialSkill] = []
    for row in rows or ():
        try:
            skill_id = int(row.get("skill_id", 0) or 0) & 0xFFFF
            level = max(0, int(row.get("level", 0) or 0)) & 0xFF
            slot = int(row.get("slot", 0) or 0) & 0xFF
        except Exception:
            continue
        if skill_id <= 0 or level <= 0:
            continue
        extra = row.get("extra_values")
        if extra is None:
            cmd = b""
        elif isinstance(extra, (bytes, bytearray, memoryview)):
            cmd = bytes(extra)
        else:
            try:
                cmd = bytes(extra)
            except Exception:
                cmd = b""
        out.append(
            InitialSkill(
                slot=slot,
                skill_id=skill_id,
                level=level,
                command_bytes=cmd,
            )
        )
    return out


def _load_character_growth_state(repo: Any, character_id: int) -> tuple[int, int]:
    """Read the persisted growth branch and level for skill reconciliation."""
    try:
        with repo.open() as conn:
            row = conn.execute(
                "SELECT grow_type, level FROM characters "
                "WHERE character_id = ? AND delete_flag = 0",
                (int(character_id),),
            ).fetchone()
        if row is None:
            return 0, 1
        return int(row[0] or 0) & 0xFF, max(1, int(row[1] or 1))
    except Exception:
        return 0, 1


def _reconcile_growth_baseline_skills(
    repo: Any,
    character_id: int,
    job: int,
    grow_type: int,
    level: int,
    *,
    page_header: int = 0,
) -> tuple[int, str]:
    """Restore missing free class skills without resetting player allocations.

    Older local characters can have ``grow_type`` set to a promoted class while
    ``character_skills`` still contains only the creation snapshot.  The client
    uses the skill page while validating equipped items, so a missing class
    license (notably Swordman's skill 33, EquipLightSword) makes a light sword
    disappear on character entry.  Merge only absent/free baseline rows and
    preserve all existing rows and slots.
    """
    first = int(grow_type) & 0x0F
    second = (int(grow_type) >> 4) & 0x0F
    if first <= 0 and second <= 0:
        return 0, "grow_baseline=skip_unpromoted"

    try:
        from game.chain_loader import load_chain

        skill_init = load_chain("helper-skill_init.py")
        baseline, kind = skill_init.build_skill_init_snapshot(
            int(job) & 0xFF,
            int(grow_type) & 0xFF,
            level=max(1, int(level or 1)),
        )
    except Exception as exc:
        return 0, "grow_baseline=load_fail:%s" % type(exc).__name__

    try:
        rows = repo.load_character_skills(int(character_id), page=0, expected_job=job)
    except Exception as exc:
        return 0, "grow_baseline=read_fail:%s" % type(exc).__name__

    existing_by_skill = {
        int(row.get("skill_id", 0) or 0) & 0xFFFF: row for row in rows or ()
    }
    occupied = {
        int(row.get("slot", 0) or 0) & 0xFF
        for row in rows or ()
        if int(row.get("skill_id", 0) or 0) > 0
    }
    added: list[int] = []
    lowered: list[int] = []
    skill_data = None
    allocate_slot = None

    for item in baseline or ():
        sid = int(getattr(item, "skill_id", 0) or 0) & 0xFFFF
        want_level = max(1, int(getattr(item, "level", 0) or 0)) & 0xFF
        if sid <= 0 or want_level <= 0:
            continue
        old = existing_by_skill.get(sid)
        if old is not None:
            old_level = max(0, int(old.get("level", 0) or 0))
            if old_level >= want_level:
                continue
            # A free class grant is never supposed to be below its baseline.
            try:
                repo.upsert_character_skill(
                    int(character_id),
                    page=0,
                    slot=int(old.get("slot", 0) or 0),
                    skill_id=sid,
                    level=want_level,
                    page_header=int(page_header) & 0xFFFF,
                )
                existing_by_skill[sid]["level"] = want_level
                lowered.append(sid)
            except Exception:
                continue
            continue

        preferred = int(getattr(item, "slot", 0) or 0) & 0xFF
        slot = preferred
        if slot in occupied:
            try:
                if skill_data is None:
                    from game.chain_loader import load_chain

                    skill_data = load_chain("helper-skill_data.py")
                    allocate_slot = skill_data.allocate_skill_slot
                static = skill_data.get_skill_static(int(job) & 0xFF, sid)
                slot = int(
                    allocate_slot(
                        is_active=bool(getattr(static, "is_active", False))
                        if static is not None
                        else sid < 100,
                        skill_class=int(getattr(static, "skill_class", 0) or 0)
                        if static is not None
                        else 0,
                        grow_type_count=1,
                        occupied=occupied,
                        tree=0,
                    )
                )
            except Exception:
                slot = -1
        if slot < 0 or slot in occupied:
            continue
        try:
            ok = repo.upsert_character_skill(
                int(character_id),
                page=0,
                slot=int(slot) & 0xFF,
                skill_id=sid,
                level=want_level,
                page_header=int(page_header) & 0xFFFF,
            )
        except Exception:
            ok = False
        if ok:
            occupied.add(int(slot) & 0xFF)
            existing_by_skill[sid] = {
                "slot": int(slot) & 0xFF,
                "level": want_level,
            }
            added.append(sid)

    note = "grow_baseline=%s kind=%s added=%s raised=%s" % (
        len(added) + len(lowered),
        kind,
        ",".join(str(s) for s in added) or "-",
        ",".join(str(s) for s in lowered) or "-",
    )
    return len(added) + len(lowered), note


def _repair_dark_knight_skill_layout(
    repo: Any,
    character_id: int,
    rows: Sequence[dict[str, Any]],
    *,
    seeded_now: bool = False,
) -> tuple[list[dict[str, Any]], str]:
    """Repair only a fresh or structurally legacy job-9 skill snapshot.

    ``C1/001C`` is the player's authoritative normal/extension-bar layout.
    The old blanket normalizer ran during every read and therefore moved a
    newly dragged extension skill back before the next character entry.  A
    fresh JSON seed still needs normalization, and the historical broken
    layout is unambiguous because all six combo roots were displaced from
    slots ``0..5``.  Other complete root layouts are player-owned.
    """
    cid = int(character_id)
    before = rows_to_initial_skills(rows)
    if not before:
        return list(rows or ()), "dark_knight_layout=skip_empty"
    try:
        combo_helper = load_chain("helper-dark_knight_combo_skill.py")
        root_slot_by_skill = dict(
            combo_helper.DARK_KNIGHT_COMBO_ROOT_SLOT_BY_SKILL
        )
        skill_init = load_chain("helper-skill_init.py")
        expected = skill_init.ensure_active_skills_on_quickbar(
            9,
            before,
            preserve_tp=True,
        )
    except Exception as exc:
        return list(rows or ()), (
            "dark_knight_layout=plan_fail:%s" % type(exc).__name__
        )

    root_entries = []
    for row in rows or ():
        skill_id = int(row.get("skill_id", 0) or 0) & 0xFFFF
        if skill_id in root_slot_by_skill:
            root_entries.append(
                (skill_id, int(row.get("slot", 0) or 0) & 0xFF)
            )
    root_slots = {skill_id: slot for skill_id, slot in root_entries}
    roots_complete = (
        len(root_entries) == len(root_slot_by_skill)
        and len(root_slots) == len(root_slot_by_skill)
        and set(root_slots) == set(root_slot_by_skill)
    )
    roots_aligned = roots_complete and all(
        root_slots[skill_id] == slot
        for skill_id, slot in root_slot_by_skill.items()
    )

    current_slot_by_skill = {
        int(row.get("skill_id", 0) or 0) & 0xFFFF: int(row.get("slot", 0) or 0) & 0xFF
        for row in rows or ()
        if int(row.get("skill_id", 0) or 0) > 0
    }
    expected_slot_by_skill = {
        int(skill.skill_id) & 0xFFFF: int(skill.slot) & 0xFF for skill in expected
    }
    if set(expected_slot_by_skill) != set(current_slot_by_skill):
        return list(rows or ()), "dark_knight_layout=skip_skill_set_mismatch"
    if not roots_complete:
        return list(rows or ()), "dark_knight_layout=skip_incomplete_roots"
    assignments = {
        skill_id: target_slot
        for skill_id, target_slot in expected_slot_by_skill.items()
        if current_slot_by_skill.get(skill_id) != target_slot
    }
    if not assignments:
        return list(rows or ()), "dark_knight_layout=aligned"
    if roots_aligned and not seeded_now:
        return list(rows or ()), (
            "dark_knight_layout=preserve_player_layout moved=%s"
            % len(assignments)
        )

    try:
        result = repo.relayout_character_skill_slots(cid, 0, expected_slot_by_skill)
    except Exception as exc:
        return list(rows or ()), (
            "dark_knight_layout=write_fail:%s" % type(exc).__name__
        )
    if not bool(result.get("ok")):
        return list(rows or ()), "dark_knight_layout=reject:%s" % result.get("detail")
    try:
        repaired_rows = repo.load_character_skills(cid, page=0)
    except Exception as exc:
        return list(rows or ()), (
            "dark_knight_layout=reload_fail:%s" % type(exc).__name__
        )
    return repaired_rows, (
        "dark_knight_layout=repaired moved=%s"
        % int(result.get("affected", 0) or 0)
    )


def resolve_skills_for_character(
    repo: Any,
    character_id: int,
    job: int,
    *,
    page_header: int = 0,
    seed_if_empty: bool = True,
    grow_type: int | None = None,
    level: int = 1,
) -> tuple[list[InitialSkill], str]:
    """确保 page0 有技能行并返回列表。

    返回 (skills, note)。skills 可能为空（无 JSON / 无 DB）。
    """
    cid = int(character_id or 0)
    job_i = int(job or 0) & 0xFF
    if repo is None or cid <= 0:
        fallback = list(get_initial_skills_for_job(job_i))
        return fallback, "skills_resolve skip=no_repo_or_cid fallback_json n=%s" % (
            len(fallback),
        )

    seeded = 0
    repair_note = "grow_baseline=not_checked"
    if seed_if_empty:
        try:
            existing = int(repo.count_character_skills(cid) or 0)
        except Exception as exc:
            fallback = list(get_initial_skills_for_job(job_i))
            return fallback, "skills_resolve count_fail=%r fallback_json n=%s" % (
                exc,
                len(fallback),
            )
        if existing <= 0:
            initial = get_initial_skills_for_job(job_i)
            try:
                seeded = int(
                    repo.seed_character_skills_if_empty(
                        cid,
                        initial,
                        page=0,
                        page_header=int(page_header) & 0xFFFF,
                    )
                    or 0
                )
            except Exception as exc:
                fallback = list(initial)
                return fallback, "skills_resolve seed_fail=%r fallback_json n=%s" % (
                    exc,
                    len(fallback),
                )

    if grow_type is None:
        grow_type, stored_level = _load_character_growth_state(repo, cid)
        level = max(int(level or 1), int(stored_level or 1))
    if int(grow_type or 0) & 0xFF:
        _, repair_note = _reconcile_growth_baseline_skills(
            repo,
            cid,
            job_i,
            int(grow_type) & 0xFF,
            max(1, int(level or 1)),
            page_header=int(page_header) & 0xFFFF,
        )

    try:
        rows = repo.load_character_skills(cid, page=0, expected_job=job_i)
    except Exception as exc:
        fallback = list(get_initial_skills_for_job(job_i))
        return fallback, "skills_resolve load_fail=%r fallback_json n=%s" % (
            exc,
            len(fallback),
        )

    layout_note = "dark_knight_layout=not_applicable"
    if job_i == 9:
        rows, layout_note = _repair_dark_knight_skill_layout(
            repo,
            cid,
            rows,
            seeded_now=bool(seeded),
        )

    skills = rows_to_initial_skills(rows)
    if not skills:
        fallback = list(get_initial_skills_for_job(job_i))
        return fallback, (
            "skills_resolve empty_db seeded=%s fallback_json n=%s job=%s"
            % (seeded, len(fallback), job_i)
        )
    combo_note = ""
    if job_i == 9:
        try:
            combo_helper = load_chain("helper-dark_knight_combo_skill.py")
            _combo_changed, combo_note = combo_helper.ensure_dark_knight_combo_skill_pages(
                repo,
                cid,
                job_i,
                skills,
            )
        except Exception as exc:
            combo_note = "dark_knight_combo_seed_fail:%s" % type(exc).__name__
    return skills, (
        "skills_resolve db_page0 n=%s seeded=%s job=%s cid=%s %s %s%s"
        % (
            len(skills),
            seeded,
            job_i,
            cid,
            repair_note,
            layout_note,
            (" " + combo_note) if combo_note else "",
        )
    )


def persist_skill_slot_swap(
    repo: Any,
    character_id: int,
    page: int,
    slot1: int,
    slot2: int,
    *,
    job: int = 0,
    page_header: int = 0,
) -> tuple[bool, str]:
    """先 seed（如空）再交换槽位。返回 (ok, note)。"""
    cid = int(character_id or 0)
    if repo is None or cid <= 0:
        return False, "persist_swap skip=no_repo_or_cid"
    # 保证有行可换（新号首次拖技能）
    _skills, seed_note = resolve_skills_for_character(
        repo,
        cid,
        int(job or 0),
        page_header=int(page_header) & 0xFFFF,
        seed_if_empty=True,
    )
    try:
        result = repo.swap_character_skill_slot(
            cid,
            int(page),
            int(slot1),
            int(slot2),
        )
    except Exception as exc:
        return False, "persist_swap err=%r; %s" % (exc, seed_note)
    ok = bool(result.get("ok"))
    return ok, (
        "persist_swap ok=%s affected=%s page=%s slot1=%s slot2=%s detail=%s; %s"
        % (
            int(ok),
            result.get("affected"),
            result.get("page"),
            result.get("slot1"),
            result.get("slot2"),
            result.get("detail"),
            seed_note,
        )
    )
