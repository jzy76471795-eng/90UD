"""Handle C1/0x0025 FINISH_LOADING.

Stage3 uses opcode 0x0025 in both directions for unrelated operations:

* C1/0x0025 is the client's map-loading completion signal.
* S0/0x0025 is the native experience notification.

Actor-publishing paths precompute the visible room's ground-drop outcomes
before their actor packet is emitted.  This handler acknowledges C1/0x0025,
emits S0/0x001E, and preserves the existing USERINFO-bound character object.

For an ordinary run entered with weapon-avatar slot 10 populated, the SELECT
path stages the native S0/0x0321 alternate USERINFO render-state body.  Stage3's
map installer calls sub_1D07810 before its equipment-object loop; the 0321
reader calls the same render-state owner after the actor and its slot objects
exist.  The staged body is sent once after FINISH_LOADING.  Its conversion
prefix and slot-10 compact auxiliary object are encoded by the dungeon helper.
"""
from __future__ import annotations

import os
import struct
from typing import Any

from game.chain_loader import load_chain

_weapon_avatar_render = load_chain("helper-dungeon_weapon_avatar_render.py")
take_pending_weapon_avatar_render = _weapon_avatar_render.take_pending_render
_equip_itemlist = load_chain("helper-equip_itemlist.py")
build_guardian_gem_refresh_000e_body = (
    _equip_itemlist.build_guardian_gem_refresh_000e_body
)
_tower_progress = load_chain("helper-hundred_floor_tower_progress.py")


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "off", "no", ""}


class FinishLoading0025Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _emit(
        self,
        flag: int,
        opcode: int,
        body: bytes,
        mode: str = "codec",
    ) -> tuple[bytes, str]:
        mode_norm = (mode or "codec").strip().lower()
        if mode_norm in {"raw", "plain"}:
            return self.server._raw_response(flag, opcode, body), "raw"
        return self.server._codec_response(flag, opcode, body), "codec"

    def handle(self, body: bytes, *, session: Any = None):
        if not _env_bool("USDOF_GAME_DUNGEON_FINISH_LOADING_HANDLE", True):
            return [], "FINISH_LOADING/0x0025 gate=OFF"

        in_run = bool(getattr(session, "in_dungeon_run", False)) if session else False
        map_index = getattr(session, "dungeon_map_index", None) if session else None
        dungeon_id = getattr(session, "selected_dungeon_id", None) if session else None
        cell = (
            (
                getattr(session, "dungeon_cell_x", None),
                getattr(session, "dungeon_cell_y", None),
            )
            if session
            else None
        )

        preload_note = (
            "drop_preload=already_prepared_before_visibility"
            if in_run and session is not None
            else "drop_preload=skip_not_in_run"
        )

        # Stage3 C1 ACK: one-byte success body.
        ack_body = b"\x01"
        ack_packet, ack_mode = self._emit(1, 0x0025, ack_body, "codec")

        # Stage3 sub_1638860 reads u32 result plus u8 state.
        finish_body = struct.pack("<IB", 0, 0)
        finish_packet, finish_mode = self._emit(0, 0x001E, finish_body, "codec")

        # USERINFO1 creates the worn medal object, but the guardian-gem socket
        # words live only in the 108-byte list3 equipment entry.  Reassert the
        # single worn medal after map installation so dungeon combat starts
        # with the same instance state as a real unequip/re-equip refresh.
        guardian_packet = None
        guardian_note = "guardian_gem_refresh=skip_not_in_run"
        if (
            in_run
            and session is not None
            and _env_bool("USDOF_GAME_DUNGEON_GUARDIAN_GEM_REFRESH", True)
            and not bool(
                getattr(session, "dungeon_guardian_gem_refresh_sent", False)
            )
        ):
            cid = getattr(session, "selected_character_id", None)
            if cid is None or int(cid) <= 0:
                guardian_note = "guardian_gem_refresh=skip_no_character"
            else:
                try:
                    guardian_body, guardian_count, guardian_note = (
                        build_guardian_gem_refresh_000e_body(
                            self.server._repo(),
                            int(cid),
                        )
                    )
                    if guardian_body and guardian_count:
                        guardian_packet, _guardian_mode = self._emit(
                            0,
                            0x000E,
                            guardian_body,
                            "codec",
                        )
                    # A successful lookup is enough to close this run's
                    # one-shot gate, including the no-gem case.  The helper
                    # reports repository failures as an empty result so the
                    # loading ACK stays intact; leave those retryable.
                    if "skip=repo_error:" not in str(guardian_note):
                        session.dungeon_guardian_gem_refresh_sent = True
                except Exception as exc:
                    guardian_note = "guardian_gem_refresh=skip_error:%s" % (
                        type(exc).__name__,
                    )

        # S0/0165 is an immediate whole-avatar detach/rebuild.  The town
        # snapshot already applies the persisted visibility bits before the
        # dungeon actor is installed; repeating it after START_MAP can detach
        # the slot-10 weapon-avatar proxy before its first combat commit.
        # Visibility changes made by their native option path still emit 0165.
        visibility_note = (
            "visibility_refresh=skip_initial_dungeon_rebuild"
            if in_run
            else "visibility_refresh=skip_not_in_dungeon"
        )

        render_packet = None
        render_note = "weapon_avatar_render=skip_gate_off"
        if _env_bool("USDOF_GAME_DUNGEON_WEAPON_AVATAR_RENDER", False):
            render_body, render_note = take_pending_weapon_avatar_render(
                session=session,
            )
            if render_body is not None:
                render_packet, _render_mode = self._emit(
                    0,
                    0x0321,
                    render_body,
                    "codec",
                )

        if session is not None:
            session.dungeon_finish_loading = True

        packets = [ack_packet, finish_packet]
        from game.character.fatigue_flow import finish_room
        fatigue_body = finish_room(self.server, session)
        if fatigue_body is not None:
            fatigue_packet, _ = self._emit(0, 0x0024, fatigue_body, "codec")
            packets.append(fatigue_packet)
        if guardian_packet is not None:
            packets.append(guardian_packet)
        if render_packet is not None:
            packets.append(render_packet)

        tower_kind = _tower_progress.tower_kind_for_dungeon(int(dungeon_id or 0)) if in_run else None
        if tower_kind is not None and session is not None:
            from game.quest.quest_index import daily_quest_day_key

            generation = session.dungeon_run_generation
            try:
                repo = self.server._repo()
                owner = repo.get_account_id_for_character(int(session.selected_character_id))
                if owner is not None and int(owner) == int(self.server.account_id):
                    entry_day = daily_quest_day_key()
                    if getattr(session, "tower_entry_display_generation", None) != generation:
                        repo.record_hundred_floor_tower_entry(int(owner), tower_kind, entry_day)
                        session.tower_entry_display_generation = generation
                    display = _tower_progress.tower_entry_display(repo, int(owner), entry_day)
                    packets.append(self.server._codec_response(0, 0x0510, bytes(display)))
            except Exception as error:
                self.server.log(f"[TowerEntry] HUD refresh deferred: {type(error).__name__}: {error}")

        note = (
            "C1/0025 FINISH_LOADING plain_len=%d in_run=%s dungeon_id=%s "
            "map_index=%s cell=%s reply=[ACK0025/%s/len=%d, "
            "001E/%s/len=%d] character_state=preserved_no_userinfo_rebind; "
            "%s; %s; %s; %s; "
            "ida=001C/001D reuse main object; "
            "chain_file=opcodes/opcode0025-finish_loading.py"
            % (
                len(body or b""),
                in_run,
                dungeon_id,
                map_index,
                cell,
                ack_mode,
                len(ack_body),
                finish_mode,
                len(finish_body),
                preload_note,
                visibility_note,
                guardian_note,
                render_note,
            )
        )
        return packets, note
