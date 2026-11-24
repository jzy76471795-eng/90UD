"""Versioned, idempotent initialization contract for new and legacy roles.

Creation callers run the strict form before sending the success ACK. Boot
backfill runs the non-strict form for existing roles, preserving player-owned
equipment, learned skills, hotkeys, and game-option values.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence, Tuple

from game.character.initial_skills import get_initial_skills_for_job
from game.chain_loader import load_chain
from game.quest import default_unlocks


NEW_CHARACTER_CONTRACT_VERSION = 1


class NewCharacterContractError(RuntimeError):
    pass


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise NewCharacterContractError(reason)


def _find_character(repo: Any, character_id: int) -> Any:
    cid = int(character_id)
    account_id = repo.get_account_id_for_character(cid)
    if account_id is not None:
        for record in repo.list_by_account(account_id):
            if int(record.character_id) == cid:
                return record
    raise NewCharacterContractError("character_missing cid=%s" % cid)


def _required_unlock_quest_ids() -> Tuple[int, ...]:
    index = default_unlocks.get_default_unlock_index()
    return index.default_quest_ids(include_ep1=True)


def _expected_equipment_set(
    entries: Optional[Sequence[Tuple[int, int]]],
) -> set:
    return {
        (int(slot) & 0xFF, int(item_id) & 0xFFFFFFFF)
        for slot, item_id in (entries or ())
        if int(item_id) > 0
    }


def apply_new_character_contract(
    repo: Any,
    character_id: int,
    *,
    strict_new: bool = False,
    expected_equipment: Optional[Sequence[Tuple[int, int]]] = None,
) -> Dict[str, Any]:
    """Apply and verify every required initialization invariant.

    The version marker is written last. A raised error therefore means the
    role is not contract-complete; the create handler removes that partial
    role, while boot aborts and retries the idempotent steps next launch.
    """
    cid = int(character_id)
    record = _find_character(repo, cid)
    account_id = int(record.account_id)
    level = max(1, int(record.level or 1))
    job = int(record.job) & 0xFF
    previous_version = int(repo.get_initialization_contract_version(cid) or 0)

    ok_container, container_note = repo.ensure_container_state(
        cid,
        account_id=account_id,
        main_list_param16=int(repo.MAX_MAIN_LIST_PARAM16),
        avatar_list_param16=int(repo.MAX_AVATAR_LIST_PARAM16),
        personal_cargo_list_param16=int(repo.MAX_PERSONAL_CARGO_LIST_PARAM16),
        character_level=level,
    )
    _require(bool(ok_container), "container_init_failed %s" % container_note)
    ok_cargo, cargo_note, _selection_key = repo.ensure_account_cargo_state(
        account_id,
        selection_key=int(repo.MAX_ACCOUNT_CARGO_SELECTION_KEY),
        character_level=level,
    )
    _require(bool(ok_cargo), "account_cargo_init_failed %s" % cargo_note)

    capacities = {
        0: int(repo.get_container_list_param(cid, repo.INVENTORY_LIST_MAIN)),
        1: int(repo.get_container_list_param(cid, repo.INVENTORY_LIST_AVATAR)),
        2: int(repo.get_container_list_param(cid, repo.INVENTORY_LIST_PERSONAL_CARGO)),
    }
    expected_capacities = {
        0: int(repo.MAX_MAIN_LIST_PARAM16),
        1: int(repo.MAX_AVATAR_LIST_PARAM16),
        2: int(repo.MAX_PERSONAL_CARGO_LIST_PARAM16),
    }
    _require(
        capacities == expected_capacities,
        "container_verify_failed got=%r expected=%r"
        % (capacities, expected_capacities),
    )
    account_cargo = repo.get_account_cargo_state(
        account_id,
        ensure_open=False,
        character_level=level,
    )
    _require(
        int(account_cargo.get("selection_key") or 0)
        == int(repo.MAX_ACCOUNT_CARGO_SELECTION_KEY),
        "account_cargo_verify_failed got=%r" % account_cargo,
    )

    quest_ids = _required_unlock_quest_ids()
    unlock_ok, unlock_result = repo.grant_default_unlock_progress(
        cid,
        quest_ids=quest_ids,
        ex_equip_slot_state=default_unlocks.EX_EQUIP_ALL_OPEN_STATE,
    )
    _require(bool(unlock_ok), "default_unlock_failed %r" % unlock_result)
    finished = {int(quest_id) for quest_id, _flag in repo.list_finished_quests(cid)}
    missing_quests = sorted(set(quest_ids) - finished)
    _require(not missing_quests, "default_unlock_missing quests=%r" % missing_quests)
    refreshed = _find_character(repo, cid)
    _require(
        (int(refreshed.ex_equip_slot_stat) & default_unlocks.EX_EQUIP_ALL_OPEN_STATE)
        == default_unlocks.EX_EQUIP_ALL_OPEN_STATE,
        "extended_equipment_slots_closed state=0x%02X"
        % (int(refreshed.ex_equip_slot_stat) & 0xFF),
    )

    initial_skills = list(get_initial_skills_for_job(job))
    _require(bool(initial_skills), "initial_skill_fixture_missing job=%s" % job)
    seeded_skills = int(
        repo.seed_character_skills_if_empty(
            cid,
            initial_skills,
            page=0,
            page_header=0,
        )
        or 0
    )
    skill_rows = list(repo.load_character_skills(cid, page=0) or ())
    _require(bool(skill_rows), "initial_skill_persist_failed job=%s" % job)
    if strict_new:
        expected_skills = {
            (int(skill.skill_id) & 0xFFFF, int(skill.level) & 0xFF)
            for skill in initial_skills
        }
        actual_skills = {
            (int(row.get("skill_id") or 0), int(row.get("level") or 0))
            for row in skill_rows
        }
        _require(
            expected_skills.issubset(actual_skills),
            "initial_skill_verify_failed missing=%r"
            % sorted(expected_skills - actual_skills),
        )

    skill_points = load_chain("helper-skill_points.py").ensure_skill_points_ledger(
        repo,
        character_id=cid,
        job=job,
        level=level,
    )
    persisted_skill_points = repo.get_character_skill_points(cid)
    _require(
        persisted_skill_points is not None,
        "skill_point_ledger_missing cid=%s" % cid,
    )
    _require(
        int(persisted_skill_points.get("synced_level") or 0) >= level,
        "skill_point_ledger_stale got=%r" % persisted_skill_points,
    )

    option_defaults = load_chain("helper-00ad_defaults.py")
    stored_game_option = repo.get_main_game_option(account_id)
    normalized_game_option = option_defaults.normalize_main_game_option_blob(
        stored_game_option
    )
    if stored_game_option != normalized_game_option:
        repo.save_main_game_option(account_id, normalized_game_option)
    verified_game_option = repo.get_main_game_option(account_id)
    _require(
        verified_game_option == normalized_game_option
        and len(verified_game_option or b"") == option_defaults.PRIMARY_LEN,
        "game_option_verify_failed",
    )

    hotkeys = load_chain("opcode00c6-save_hotkey_option.py")
    desired_hotkey_slots, desired_hotkey_text, _hotkey_source = (
        hotkeys.ensure_account_hotkey_profile(
            repo,
            account_id,
            character_id=cid,
            character_job=job,
        )
    )
    hotkey_profile_type = hotkeys.hotkey_profile_type_for_job(job)
    stored_hotkey_profile = repo.load_account_hotkey_profile(
        account_id,
        hotkey_profile_type,
    )
    _require(
        stored_hotkey_profile is not None,
        "account_hotkey_profile_missing account_id=%s profile=%s"
        % (account_id, hotkey_profile_type),
    )
    stored_hotkey_blob, stored_hotkey_text = stored_hotkey_profile
    _require(
        hotkeys.blob_to_slots(stored_hotkey_blob) == desired_hotkey_slots,
        "account_hotkey_slot_verify_failed account_id=%s profile=%s"
        % (account_id, hotkey_profile_type),
    )
    _require(
        stored_hotkey_text == desired_hotkey_text,
        "account_hotkey_text_verify_failed account_id=%s profile=%s"
        % (account_id, hotkey_profile_type),
    )

    expected_equipment_set = _expected_equipment_set(expected_equipment)
    if strict_new:
        _require(
            bool(expected_equipment_set),
            "initial_equipment_fixture_missing job=%s" % job,
        )
        actual_equipment = {
            (int(entry.slot) & 0xFF, int(entry.item_id) & 0xFFFFFFFF)
            for entry in refreshed.equipped_entries
        }
        _require(
            expected_equipment_set.issubset(actual_equipment),
            "initial_equipment_verify_failed missing=%r"
            % sorted(expected_equipment_set - actual_equipment),
        )

    _require(
        bool(
            repo.mark_initialization_contract_complete(
                cid,
                NEW_CHARACTER_CONTRACT_VERSION,
            )
        ),
        "contract_marker_write_failed cid=%s" % cid,
    )
    _require(
        int(repo.get_initialization_contract_version(cid))
        >= NEW_CHARACTER_CONTRACT_VERSION,
        "contract_marker_verify_failed cid=%s" % cid,
    )

    return {
        "ok": True,
        "character_id": cid,
        "account_id": account_id,
        "contract_version": NEW_CHARACTER_CONTRACT_VERSION,
        "previous_version": previous_version,
        "container_main": capacities[0],
        "container_avatar": capacities[1],
        "container_personal_cargo": capacities[2],
        "account_cargo": int(account_cargo.get("selection_key") or 0),
        "quest_count": len(quest_ids),
        "ex_equip_slot_stat": int(refreshed.ex_equip_slot_stat) & 0xFF,
        "skill_count": len(skill_rows),
        "skills_seeded": seeded_skills,
        "remaining_sp": int(skill_points.get("remaining_sp") or 0),
        "hotkey_count": len(desired_hotkey_slots),
        "game_option_len": len(normalized_game_option),
    }


def format_new_character_contract_report(report: Dict[str, Any]) -> str:
    return (
        "contract=v%(contract_version)s previous=%(previous_version)s "
        "containers=%(container_main)s/%(container_avatar)s/"
        "%(container_personal_cargo)s account_cargo=%(account_cargo)s "
        "quests=%(quest_count)s ex_equip=0x%(ex_equip_slot_stat)02X "
        "skills=%(skill_count)s seeded=%(skills_seeded)s sp=%(remaining_sp)s "
        "hotkeys=%(hotkey_count)s game_option=%(game_option_len)s"
    ) % report


def backfill_new_character_contracts(repo: Any) -> Dict[str, int]:
    """Repair every live role before listeners open; fail boot on any error."""
    records = list(repo.list_all_alive())
    upgraded = 0
    for record in records:
        report = apply_new_character_contract(repo, int(record.character_id))
        if int(report.get("previous_version") or 0) < NEW_CHARACTER_CONTRACT_VERSION:
            upgraded += 1
    return {"characters": len(records), "contracts_upgraded": upgraded}
