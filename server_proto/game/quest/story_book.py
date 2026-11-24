"""Use the observed Story Book C0204 vector through ordinary quest acceptance.

Frozen US 14b85f7e...: 01A858C0 reads the authored task ID and calls
01A85540(qid, 0), which writes i16 slot/u8 Main/u32 zero/u32 action=0x67/
u32 qid/u32 zero. The response reuses existing 001F/023F/0015/0164 and Main
000D readers; no new wire shape or client patch is introduced.
"""
from __future__ import annotations

from copy import copy
import struct
import time
from game.chain_loader import load_chain
from game.character.damage_font import build_main_inventory_refresh_packet
from game.character.item_metadata import job_label_for_id, resolve_item_metadata
from game.quest.quest_index import get_quest_index
from game.quest.daily_challenge_repository import DailyChallengeRepository
from game.quest.quest_session import bind_session_quest_state, resolve_selected_character_detail


class _NotAccepted(Exception):
    pass


def use_story_book(server, parsed, *, session=None):
    detail = resolve_selected_character_detail(server, session)
    cid = int(detail.get("character_id") or 0)
    base = "C1/0204 STORY_BOOK"
    if cid <= 0 or detail.get("record") is None:
        return [], base + " reject=no_selected_character"
    repo = server._repo()

    def reject(reason):
        packet, note = build_main_inventory_refresh_packet(server, repo, cid)
        return [packet], base + " reject=" + reason + "; action_did_not_consume_book; " + note

    if (parsed["list_type"] != 0 or parsed["instance"] != 0
            or parsed["action_type"] != 0x67 or parsed["argument2"] != 0):
        return reject("unsupported_layout")
    if (session is None or not getattr(session, "enter_seria_settled", False)
            or getattr(session, "in_dungeon_run", False)
            or getattr(session, "in_dungeon_select", False)
            or getattr(session, "another_arad_active", False)):
        return reject("town_required")
    slot = int(parsed["slot_index"])
    qid = int(parsed["argument1"])
    # The ordinary quest snapshot consults daily challenge claims. Its first
    # schema initialization uses SQLite executescript (which commits an open
    # transaction). Prepare it before the atomic accept/consume scope instead.
    DailyChallengeRepository(repo)
    shadow = copy(session)
    try:
        with repo.inventory_transaction_scope() as conn:
            actor = conn.execute(
                'SELECT account_id,job,level,grow_type FROM characters WHERE character_id=? AND delete_flag=0',
                (cid,),
            ).fetchone()
            if actor is None or tuple(int(v) for v in actor) != (
                    int(server.account_id), int(detail["job"]), int(detail["level"]), int(detail["grow_type"])):
                raise _NotAccepted("actor_changed")
            item = repo.get_inventory_item_at(cid, list_type=0, slot_index=slot)
            if item is None or item.get("item_kind") != "stackable":
                raise _NotAccepted("slot_empty_or_not_stackable")
            item_id = int(item["item_template_id"])
            metadata = resolve_item_metadata(item_id)
            if metadata.action_type != "[story book]" or tuple(metadata.action_values) != (qid,):
                raise _NotAccepted("pvf_quest_mismatch")
            jobs = {str(value).lower().strip('[] ') for value in metadata.usable_jobs}
            if int(detail["level"]) < int(metadata.minimum_level or 1):
                raise _NotAccepted("below_minimum_level")
            if jobs and 'all' not in jobs and job_label_for_id(int(detail["job"])) not in jobs:
                raise _NotAccepted("unusable_job")
            if int(item.get("stack_count") or 0) < 1:
                raise _NotAccepted("empty_stack")
            expiry = int(item.get("expire_time") or 0)
            if expiry and expiry <= int(time.time()):
                raise _NotAccepted("item_expired")
            meta = get_quest_index().get_meta(qid)
            if meta is None or not str(meta.path).replace("\\", "/").lower().startswith("n_quest/storybook/"):
                raise _NotAccepted("not_authored_story_quest")
            if repo.is_quest_finished(cid, qid):
                raise _NotAccepted("already_finished")
            if any(int(q) == qid for _, q, _ in repo.list_active_quests(cid)):
                raise _NotAccepted("already_active")
            # A failed transaction must not leave the real session showing
            # an uncommitted task. The normal accept service binds this copy.
            accept = load_chain("opcode001f-accept_quest.py").AcceptQuest001FService(server)
            packets, accept_note = accept.handle_accept_quest_001f(
                struct.pack("<H", qid), session=shadow, resolved_character=detail,
            )
            if not any(int(q) == qid for _, q, _ in repo.list_active_quests(cid)):
                raise _NotAccepted("quest_not_accepted; " + accept_note)
            ok, consume_note, _data = repo.consume_stackable_at(
                cid, list_type=0, slot_index=slot, expected_item_id=item_id, count=1,
            )
            if not ok:
                raise _NotAccepted("consume_failed; " + consume_note)
            inventory_packet, inventory_note = build_main_inventory_refresh_packet(server, repo, cid)
            packets.append(inventory_packet)
    except _NotAccepted as exc:
        return reject(str(exc))
    bind_session_quest_state(
        session,
        active_pairs=[(int(q), int(trigger)) for _, q, trigger in repo.list_active_quests(cid)],
        finished_ids=[int(q) for q, _flag in repo.list_finished_quests(cid)],
        last_quest_id=qid,
    )
    return packets, "%s success item=%d quest=%d; %s; %s" % (
        base, item_id, qid, accept_note, inventory_note,
    )
