"""COMPLETE_QUEST_AREA C->S 0x0315 (PVF area completion, no rewards).

Stage3 sends exactly ``u32 quest_id``.  Its S1/0315 registration has no
opcode-specific reader; the common flag-1 dispatcher consumes the one-byte
success marker.  After persistence, the server refreshes active, acceptable,
and finished quest snapshots in that order.
"""
from __future__ import annotations

import struct
from typing import Any

from game.chain_loader import load_chain
from game.quest.quest_common import env_bool
from game.quest.quest_session import (
    pack_quest_refresh_packets,
    resolve_selected_character_detail,
)

_epic_completion = load_chain("helper-epic_chain_complete.py")
complete_epic_area_without_rewards = _epic_completion.complete_epic_area_without_rewards


def parse_complete_quest_area_0315(body: bytes) -> dict[str, object]:
    """Parse the Stage3 C1/0315 ``u32 quest_id`` body exactly."""

    raw = bytes(body or b"")
    if len(raw) != 4:
        return {"layout": "invalid", "quest_id": None, "raw_len": len(raw)}
    quest_id = int(struct.unpack_from("<I", raw, 0)[0])
    if quest_id <= 0 or quest_id > 0xFFFF:
        return {"layout": "u32_quest_id", "quest_id": None, "raw_len": len(raw)}
    return {"layout": "u32_quest_id", "quest_id": quest_id, "raw_len": len(raw)}


class CompleteQuestArea0315Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_complete_quest_area_0315(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        parsed = parse_complete_quest_area_0315(body)
        handle_on = env_bool("USDOF_GAME_QUEST_HANDLE_0315", True)
        quest_id = parsed.get("quest_id")
        detail = resolve_selected_character_detail(self.server, session)
        cid = detail.get("character_id")
        job = detail.get("job")
        level = detail.get("level")
        grow = detail.get("grow_type")
        base = (
            "C1/0315 COMPLETE_QUEST_AREA layout=%s raw_len=%s quest_id=%s "
            "cid=%s level=%s handle=%s"
            % (
                parsed["layout"],
                parsed["raw_len"],
                quest_id,
                cid,
                level,
                int(handle_on),
            )
        )
        if not handle_on:
            return [], base + "; gate=OFF"
        if quest_id is None or cid is None or level is None:
            return [], base + "; reject=invalid"

        batch = complete_epic_area_without_rewards(
            self.server._repo(),
            character_id=int(cid),
            character_level=int(level),
            quest_id=int(quest_id),
        )
        if not batch.ok:
            return [], (
                "%s; reject=%s area=%s ids=%s"
                % (base, batch.note, batch.area_key or "none", len(batch.quest_ids))
            )

        packets = [self.server._codec_response(1, 0x0315, b"\x01")]
        refresh, refresh_note = pack_quest_refresh_packets(
            self.server,
            character_id=int(cid),
            job=int(job or 0),
            level=max(1, int(level)),
            grow_type=int(grow or 0),
            session=session,
            include_0015=True,
            include_023f=True,
            include_0123=False,
            include_0164=True,
        )
        packets.extend(refresh)
        return packets, (
            "%s; area=%s ids=%s added=%s no_reward=1 no_economy=1 "
            "no_items=1 no_consume=1; %s"
            % (
                base,
                batch.area_key,
                len(batch.quest_ids),
                batch.finished_added,
                refresh_note,
            )
        )
