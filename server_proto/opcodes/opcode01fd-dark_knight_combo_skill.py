"""C1/01FD Dark Knight combo-skill page persistence.

The exact Stage3 writer is ``sub_1E72CA0``.  It sends one complete combo page
after a drag operation.  The packet is intentionally silent: the pre-fix live
client already applies the drag locally while the server sends no response,
and no Stage3 S1/01FD reader/wait state is established.  Persistence and the
later S0/01C0 restore are the only responsibilities of this handler.
"""
from __future__ import annotations

from typing import Any

from game.chain_loader import load_chain
from game.quest.quest_session import resolve_selected_character_detail


_combo = load_chain("helper-dark_knight_combo_skill.py")
DARK_KNIGHT_JOB = _combo.DARK_KNIGHT_JOB
parse_dark_knight_combo_save_body = _combo.parse_dark_knight_combo_save_body


def _resolve_character(server: Any, session: Any) -> tuple[int, int, str]:
    cid = 0
    job = -1
    source = "none"
    try:
        detail = resolve_selected_character_detail(server, session)
        cid = int(detail.get("character_id") or 0)
        job = int(detail.get("job") or 0)
        source = "db"
    except Exception:
        detail = None

    if cid <= 0 and session is not None:
        cid = int(getattr(session, "selected_character_id", 0) or 0)
        source = "session"
    if job < 0 and session is not None:
        job = int(getattr(session, "character_job", -1) or 0)
        source = "session"
    return cid, job, source


class DarkKnightComboSkill01FDService:
    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_dark_knight_combo_skill_01fd(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        base = "C1/01FD DARK_KNIGHT_COMBO_SKILL plain_len=%s" % len(body or b"")
        try:
            parsed = parse_dark_knight_combo_save_body(body)
        except (TypeError, ValueError) as exc:
            return [], "%s; reject=%s" % (base, str(exc))

        cid, job, source = _resolve_character(self.server, session)
        if cid <= 0:
            return [], "%s; reject=no_character page=%s" % (
                base,
                parsed.page_index,
            )
        if job != DARK_KNIGHT_JOB:
            return [], "%s; reject=job_gate cid=%s job=%s source=%s" % (
                base,
                cid,
                job,
                source,
            )

        try:
            repo = self.server._repo()
            changed = repo.upsert_dark_knight_combo_skill_page(
                cid,
                page_index=parsed.page_index,
                body=parsed.page_body,
                trailing_mode=int(parsed.trailing_mode or 0),
            )
        except Exception as exc:
            return [], "%s; persist=fail:%s cid=%s page=%s" % (
                base,
                type(exc).__name__,
                cid,
                parsed.page_index,
            )

        return [], (
            "%s; persist=%s cid=%s job=%s source=%s page=%s columns=%s "
            "mode=%s stored_len=%s response=silent"
            % (
                base,
                "ok" if changed else "no_change",
                cid,
                job,
                source,
                parsed.page_index,
                len(parsed.columns),
                parsed.trailing_mode,
                len(parsed.page_body),
            )
        )


__all__ = [
    "DarkKnightComboSkill01FDService",
    "parse_dark_knight_combo_save_body",
]
