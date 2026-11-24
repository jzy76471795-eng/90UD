"""Game-thread GM commands for PVF-backed profession changes."""
from __future__ import annotations

from typing import Any

from control.contracts import ControlApiError
from game.chain_loader import load_chain
from game.character.adventure_group import calculate_adventure_group_for_character
from game.character.payloads import build_userinfo_subtype0_min, build_userinfo_subtype1_min
from game.guild.guild_benefits import (
    has_active_guild_membership,
    pack_guild_membership_status_rows,
)
from game.guild.guild_repository import GuildRepository
from game.quest.quest_session import pack_quest_refresh_packets, resolve_selected_character_detail


_JOB_CHANGE = load_chain("helper-job_change.py")
_EXP = load_chain("helper-character_exp.py")
_SKILL_POINTS = load_chain("helper-skill_points.py")
_CHARACTER_SKILLS = load_chain("helper-character_skills.py")
_USERINFO_EQUIPMENT = load_chain("helper-userinfo_equipment.py")
_DUNGEON_PAYLOADS = load_chain("helper-dungeon_payloads.py")
_QUEST_COMPLETION = load_chain("helper-quest_completion_snapshot.py")
_LEVEL_TITLES = load_chain("helper-level_milestone_titles.py")


class GmProfessionControlCommands:
    def __init__(self, server: Any) -> None:
        self.server = server

    @staticmethod
    def _character_id(session: Any) -> int:
        character_id = int(getattr(session, "selected_character_id", 0) or 0)
        if character_id <= 0:
            raise ControlApiError(
                "SESSION_NOT_READY",
                "current client has no selected character",
                status=409,
            )
        return character_id

    @staticmethod
    def _guild_identity(repo: Any, character_id: int) -> tuple[int, str]:
        db_path = getattr(repo, "db_path", None)
        if db_path is None:
            return 0, ""
        guild = GuildRepository(db_path).get_guild_for_character(character_id)
        if guild is None:
            return 0, ""
        return int(guild.guild_id), guild.display_name

    @staticmethod
    def _catalog_job(job_id: int) -> dict[str, Any] | None:
        return next(
            (
                entry
                for entry in _JOB_CHANGE.load_profession_catalog()
                if int(entry.get("job_id", -1)) == int(job_id)
            ),
            None,
        )

    @classmethod
    def _validate_job(cls, job_id: int) -> dict[str, Any]:
        entry = cls._catalog_job(job_id)
        if entry is None:
            raise ControlApiError("INVALID_PARAMS", "job_id is not in the PVF catalog", status=400)
        return entry

    @staticmethod
    def _set_session_snapshot(session: Any, detail: dict[str, Any], result: Any) -> None:
        if session is None:
            return
        try:
            session.character_job = int(detail.get("job") or 0) & 0xFF
            session.character_grow_type = int(result.new_grow_type) & 0xFF
            session.character_level = int(detail.get("level") or 1) & 0xFF
            session.skill_remain_sp = int(result.remaining_sp)
            session.skill_remain_tp = int(result.remaining_tp)
        except Exception:
            pass

    def list_professions(self, params: dict[str, Any], session: Any):
        if params:
            raise ControlApiError(
                "INVALID_PARAMS",
                "character.list_professions does not accept parameters",
                status=400,
            )
        self._character_id(session)
        jobs = list(_JOB_CHANGE.load_profession_catalog())
        detail = resolve_selected_character_detail(self.server, session)
        # The catalog is static, while these values must come from the selected
        # character row so the GM UI can restore the actual profession state.
        current_job = detail.get("job")
        current_grow_type = detail.get("grow_type")
        record = detail.get("record")
        return [], {
            "jobs": jobs,
            "total": len(jobs),
            "current_job_id": (
                int(current_job) & 0xFF if current_job is not None else None
            ),
            "current_grow_type": (
                int(current_grow_type) & 0xFF
                if current_grow_type is not None
                else None
            ),
            "current_level": (
                int(detail.get("level") or 1)
                if detail.get("level") is not None
                else None
            ),
            "current_exp": max(0, int(getattr(record, "exp", 0) or 0)),
            "current_expertise_exp": max(
                0, int(getattr(record, "expertise_exp", 0) or 0)
            ),
            "max_level": int(_EXP.max_character_level()),
        }, "GM profession catalog jobs=%d current_job=%s grow=0x%02X" % (
            len(jobs),
            current_job if current_job is not None else "unknown",
            int(current_grow_type or 0) & 0xFF,
        )

    @staticmethod
    def _target_exp_for_level(level: int) -> int:
        target = int(level)
        if target <= 1:
            return 0
        if target >= int(_EXP.max_character_level()):
            return int(_EXP.level90_exp_cap())
        thresholds = tuple(int(value) for value in _EXP.load_exp_thresholds())
        index = target - 2
        if not 0 <= index < len(thresholds):
            raise ControlApiError(
                "CHARACTER_LEVEL_CHANGE_FAILED",
                "PVF experience table does not contain the requested level",
                status=409,
            )
        return max(0, int(thresholds[index]))

    def pack_level_refresh(
        self,
        *,
        session: Any,
        character_id: int,
        level: int,
        exp: int,
        expertise_exp: int = 0,
        include_0164: bool = False,
    ) -> tuple[list[bytes], dict[str, Any], str]:
        """Build the complete live level snapshot used by ``character.set_level``."""
        target_level = max(1, int(level))
        target_exp = max(0, int(exp))
        target_expertise = max(0, int(expertise_exp))
        repo = self.server._repo()
        detail = resolve_selected_character_detail(self.server, session)
        record = detail.get("record")
        if record is None:
            raise ControlApiError(
                "SESSION_NOT_READY", "character record is unavailable", status=409
            )
        job = int(detail.get("job") or 0)
        grow_type = int(detail.get("grow_type") or 0)
        skill_state = _SKILL_POINTS.resolve_skill_point_protocol_state(
            level=target_level,
            character_id=int(character_id),
            repo=repo,
            session=session,
            job=job,
        )
        skills, skills_note = _CHARACTER_SKILLS.resolve_skills_for_character(
            repo,
            int(character_id),
            job,
            page_header=int(skill_state.page0_sp),
            seed_if_empty=True,
        )
        equipment, equipment_note = _USERINFO_EQUIPMENT.resolve_enter_room_equipment(
            record,
            enabled=True,
            backfill_from_pvf=False,
            repo=repo,
            prefer_live_db=True,
        )
        quest_snapshot = _QUEST_COMPLETION.load_finished_quest_snapshot(
            repo, int(character_id)
        )
        adventure_progression = calculate_adventure_group_for_character(
            repo, record
        )
        adventure_group_level = int(adventure_progression.adventure_level)
        guild_membership_active = has_active_guild_membership(repo, record)
        guild_id, guild_name = self._guild_identity(repo, int(character_id))
        packets = [
            self.server._codec_response(
                0,
                0x0002,
                build_userinfo_subtype0_min(
                    record,
                    equipment,
                    guild_name=guild_name,
                    guild_id=guild_id,
                ),
            ),
            _JOB_CHANGE.pack_job_change_avatar_visibility(
                self.server,
                record=record,
            ),
            self.server._codec_response(
                0,
                0x0002,
                build_userinfo_subtype1_min(
                    record,
                    exp=target_exp,
                    skills=skills,
                    equipment=equipment,
                    finished_quest_ids=quest_snapshot.finished_quest_ids,
                    adventure_group_level=adventure_group_level,
                    guild_membership_active=guild_membership_active,
                ),
            ),
        ]
        guild_status_rows = pack_guild_membership_status_rows(
            self.server,
            guild_membership_active,
        )
        packets.extend(packet for packet, _note in guild_status_rows)
        packets.extend(
            [
                _JOB_CHANGE.pack_job_change_skillinfo(
                    self.server,
                    skills=skills,
                    remaining_sp=int(skill_state.page0_sp),
                    remaining_tp=int(skill_state.page0_tp),
                ),
                self.server._codec_response(
                    0,
                    0x0025,
                    _DUNGEON_PAYLOADS.build_exp_notification_0025(
                        target_level,
                        target_exp,
                        expertise_exp=target_expertise,
                        page0_sp=int(skill_state.page0_sp),
                        page1_sp=int(skill_state.page1_sp),
                        page0_tp=int(skill_state.page0_tp),
                        page1_tp=int(skill_state.page1_tp),
                    ),
                ),
            ]
        )
        quest_packets, quest_note = pack_quest_refresh_packets(
            self.server,
            character_id=int(character_id),
            job=job,
            level=target_level,
            grow_type=grow_type,
            session=session,
            include_0015=True,
            include_023f=True,
            include_0164=bool(include_0164),
        )
        packets.extend(quest_packets)
        try:
            session.character_level = target_level
            session.character_exp = target_exp
            session.character_expertise_exp = target_expertise
            session.skill_remain_sp = int(skill_state.page0_sp)
            session.skill_remain_tp = int(skill_state.page0_tp)
            session.client_exp_primed = True
        except Exception:
            pass
        result = {
            "job_id": job & 0xFF,
            "grow_type": grow_type & 0xFF,
            "remaining_sp": int(skill_state.page0_sp),
            "remaining_tp": int(skill_state.page0_tp),
            "skill_count": len(skills),
            "packet_flow": [
                "S0/0002 USERINFO0",
                "S0/0165 AVATAR_VISIBILITY",
                "S0/0002 USERINFO1",
                "S0/01E8 or S0/01E9 GUILD_STATUS",
                "S0/01EA GUILD_ACTIVE_SET",
                "S0/0013",
                "S0/0025",
                "quest.refresh",
            ],
        }
        return packets, result, "%s; %s; %s" % (
            skills_note,
            equipment_note,
            quest_note,
        )

    def set_level(self, params: dict[str, Any], session: Any):
        if not isinstance(params, dict) or set(params) != {"level"}:
            raise ControlApiError(
                "INVALID_PARAMS",
                "character.set_level requires only level",
                status=400,
            )
        try:
            target_level = int(params["level"])
        except (TypeError, ValueError):
            raise ControlApiError("INVALID_PARAMS", "level must be an integer", status=400)
        max_level = int(_EXP.max_character_level())
        if not 1 <= target_level <= max_level:
            raise ControlApiError(
                "INVALID_PARAMS",
                "level must be between 1 and %d" % max_level,
                status=400,
            )
        if bool(getattr(session, "in_dungeon_run", False)):
            raise ControlApiError(
                "CHARACTER_STATE_CONFLICT",
                "character level can be changed after returning to town",
                status=409,
            )

        character_id = self._character_id(session)
        repo = self.server._repo()
        before = resolve_selected_character_detail(self.server, session)
        previous_level = int(before.get("level") or 1)
        previous_record = before.get("record")
        target_exp = self._target_exp_for_level(target_level)
        target_expertise = (
            max(0, int(getattr(previous_record, "expertise_exp", 0) or 0))
            if previous_level == target_level == max_level
            else 0
        )
        try:
            progression = repo.set_character_level_progression(
                character_id,
                level=target_level,
                exp=target_exp,
                expertise_exp=target_expertise,
            )
        except (TypeError, ValueError) as exc:
            raise ControlApiError(
                "CHARACTER_LEVEL_CHANGE_FAILED",
                str(exc),
                status=409,
            )

        level_title_grants = []
        level_title_note = "level_title skip=no_threshold"
        try:
            if _LEVEL_TITLES.has_crossed_level_title_threshold(
                previous_level,
                target_level,
            ):
                level_title_grants, level_title_note = (
                    _LEVEL_TITLES.claim_level_title_rewards(
                        repo,
                        character_id=character_id,
                        previous_level=previous_level,
                        current_level=target_level,
                    )
                )
        except Exception as exc:
            level_title_note = "level_title err=%s" % type(exc).__name__

        packets, refresh_result, refresh_note = self.pack_level_refresh(
            session=session,
            character_id=character_id,
            level=target_level,
            exp=target_exp,
            expertise_exp=target_expertise,
            include_0164=False,
        )
        if level_title_grants:
            try:
                title_packets, title_packet_note = _LEVEL_TITLES.pack_level_title_grant_packets(
                    self.server,
                    grants=level_title_grants,
                )
                packets.extend(title_packets)
                level_title_note = "%s;%s" % (level_title_note, title_packet_note)
            except Exception as exc:
                level_title_note = "%s;pack_err=%s" % (
                    level_title_note,
                    type(exc).__name__,
                )
        result = dict(progression)
        result.update(refresh_result)
        result["max_level"] = max_level
        if level_title_grants:
            result["packet_flow"].append("S0/0168 level-title")
        return packets, result, (
            "GM level %d->%d exp=%d expertise=%d; %s; %s"
            % (
                previous_level,
                target_level,
                target_exp,
                target_expertise,
                refresh_note,
                level_title_note,
            )
        )

    def _change_result(
        self,
        *,
        session: Any,
        detail: dict[str, Any],
        previous_job: int,
        result: Any,
        note: str,
    ):
        if not result.ok:
            raise ControlApiError("PROFESSION_CHANGE_FAILED", result.note, status=409)
        record = detail.get("record")
        if record is None:
            raise ControlApiError("SESSION_NOT_READY", "character record is unavailable", status=409)
        equipment = getattr(record, "equipped_entries", None) or ()
        repo = self.server._repo()
        guild_membership_active = has_active_guild_membership(repo, record)
        packets = [
            _JOB_CHANGE.pack_job_change_userinfo0(
                self.server,
                record=record,
                equipment=equipment,
            ),
            _JOB_CHANGE.pack_job_change_avatar_visibility(
                self.server,
                record=record,
            ),
            _JOB_CHANGE.pack_job_change_skillinfo(
                self.server,
                skills=result.skills,
                remaining_sp=result.remaining_sp,
                remaining_tp=result.remaining_tp,
            ),
            _JOB_CHANGE.pack_job_change_userinfo1(
                self.server,
                repo=repo,
                record=record,
                skills=result.skills,
            ),
        ]
        guild_status_rows = pack_guild_membership_status_rows(
            self.server,
            guild_membership_active,
        )
        packets.extend(packet for packet, _note in guild_status_rows)
        refresh, refresh_note = pack_quest_refresh_packets(
            self.server,
            character_id=int(detail["character_id"]),
            job=int(detail.get("job") or 0),
            level=int(detail.get("level") or 1),
            grow_type=int(result.new_grow_type),
            session=session,
            include_0015=True,
            include_023f=True,
            include_0164=False,
        )
        packets.extend(refresh)
        self._set_session_snapshot(session, detail, result)
        return packets, {
            "previous_job_id": int(previous_job) & 0xFF,
            "job_id": int(detail.get("job") or 0) & 0xFF,
            "previous_grow_type": int(result.previous_grow_type) & 0xFF,
            "grow_type": int(result.new_grow_type) & 0xFF,
            "chain_type": int(result.chain_type),
            "grow_number": int(result.grow_number),
            "skill_count": len(result.skills),
            "remaining_sp": int(result.remaining_sp),
            "remaining_tp": int(result.remaining_tp),
            "packet_flow": [
                "S0/0002 USERINFO0",
                "S0/0165 AVATAR_VISIBILITY",
                "S0/0013",
                "S0/0002 USERINFO1",
                "S0/01E8 or S0/01E9 GUILD_STATUS",
                "S0/01EA GUILD_ACTIVE_SET",
                "quest.refresh",
            ],
        }, "%s; %s; %s" % (note, result.note, refresh_note)

    def change_job(self, params: dict[str, Any], session: Any):
        if not isinstance(params, dict) or set(params) != {"job_id"}:
            raise ControlApiError(
                "INVALID_PARAMS",
                "character.change_job requires only job_id",
                status=400,
            )
        try:
            job_id = int(params["job_id"])
        except (TypeError, ValueError):
            raise ControlApiError("INVALID_PARAMS", "job_id must be an integer", status=400)
        entry = self._validate_job(job_id)
        character_id = self._character_id(session)
        detail = resolve_selected_character_detail(self.server, session)
        previous_job = int(detail.get("job") or 0)
        result = _JOB_CHANGE.apply_base_job_change(
            self.server._repo(),
            character_id=character_id,
            job=job_id,
            level=int(detail.get("level") or 1),
            current_grow_type=int(detail.get("grow_type") or 0),
        )
        # The record used by USERINFO0 must reflect the just-persisted job.
        updated = resolve_selected_character_detail(self.server, session)
        return self._change_result(
            session=session,
            detail=updated,
            previous_job=previous_job,
            result=result,
            note="GM base job %s" % entry.get("name", job_id),
        )

    def change_profession(self, params: dict[str, Any], session: Any):
        allowed = {"job_id", "chain_type", "grow_number"}
        if not isinstance(params, dict) or not set(params).issubset(allowed):
            raise ControlApiError("INVALID_PARAMS", "invalid character.change_profession parameters", status=400)
        if "chain_type" not in params or "grow_number" not in params:
            raise ControlApiError(
                "INVALID_PARAMS",
                "chain_type and grow_number are required",
                status=400,
            )
        try:
            chain_type = int(params["chain_type"])
            grow_number = int(params["grow_number"])
            job_id = int(params["job_id"]) if "job_id" in params else None
        except (TypeError, ValueError):
            raise ControlApiError(
                "INVALID_PARAMS",
                "job_id, chain_type, and grow_number must be integers",
                status=400,
            )
        if chain_type not in (1, 2) or not 1 <= grow_number <= 0x0F:
            raise ControlApiError("INVALID_PARAMS", "invalid profession branch", status=400)

        character_id = self._character_id(session)
        detail = resolve_selected_character_detail(self.server, session)
        if job_id is None:
            job_id = int(detail.get("job") or 0)
        elif int(job_id) != int(detail.get("job") or 0):
            raise ControlApiError(
                "INVALID_PARAMS",
                "switch the base job before applying its profession branch",
                status=409,
            )
        entry = self._validate_job(job_id)
        branches = list(entry.get("branches") or [])
        first_grow = int(detail.get("grow_type") or 0) & 0x0F
        if chain_type == 1 and grow_number not in {
            int(branch.get("grow_number", 0)) for branch in branches
        }:
            raise ControlApiError("INVALID_PARAMS", "grow_number is not a PVF transfer branch", status=400)
        if chain_type == 2:
            branch = next(
                (item for item in branches if int(item.get("grow_number", 0)) == first_grow),
                None,
            )
            if branch is None or grow_number not in {
                int(item.get("grow_number", 0)) for item in branch.get("awakenings", ())
            }:
                raise ControlApiError("INVALID_PARAMS", "grow_number is not a PVF awakening branch", status=400)

        previous_job = int(detail.get("job") or 0)
        result = _JOB_CHANGE.apply_profession_change(
            self.server._repo(),
            character_id=character_id,
            job=job_id,
            level=int(detail.get("level") or 1),
            current_grow_type=int(detail.get("grow_type") or 0),
            chain_type=chain_type,
            grow_number=grow_number,
        )
        updated = resolve_selected_character_detail(self.server, session)
        return self._change_result(
            session=session,
            detail=updated,
            previous_job=previous_job,
            result=result,
            note="GM profession chain=%d branch=%d" % (chain_type, grow_number),
        )


__all__ = ["GmProfessionControlCommands"]
