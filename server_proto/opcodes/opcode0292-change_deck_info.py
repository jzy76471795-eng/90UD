# -*- coding: utf-8 -*-
"""C1/0x0292 CHANGE_DECK_INFO — 骑士盾 1 主 4 副整 deck 上报。

Stage3 live 2026-07-22：
  - 客户端拖动盾牌仓库底部槽位时发本包（plain=5×u32 LE，codec0/XTEA）
  - IDA ``sub_15FE950`` @ 0x015FE950：成功路径读 5 个 u32 写本地 deck
  - 服务端权威：``character_knight_shield_deck`` + 主盾 slot24

回复：
  - S1/0292 ACK：u8 status=1 + u8 reserved=0 + 5×u32
  - S0/0245 SEND_DECK_INFO：5×u32（UI 同步）
  - 主盾变更：S0/000E list3 + USERINFO0（副本仅 U0）
"""
from __future__ import annotations

from typing import Any

from game.chain_loader import load_chain
from game.quest.quest_common import env_bool
from game.quest.quest_session import resolve_selected_character_detail


class ChangeDeckInfo0292Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_change_deck_info_0292(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        handle_on = env_bool("USDOF_GAME_HANDLE_CHANGE_DECK_0292", True)
        ks = load_chain("helper-knight_shield.py")
        detail = resolve_selected_character_detail(self.server, session)
        cid = detail.get("character_id")
        job = int(detail.get("job") or 0)
        level = int(detail.get("level") or 1)
        base = (
            "C1/0292 CHANGE_DECK_INFO cid=%s job=%s level=%s plain_len=%s"
            % (cid, job, level, len(body or b""))
        )
        if not handle_on:
            return [], base + "; gate=OFF"

        parsed = ks.parse_change_deck_request(body)
        if parsed is None:
            # 畸形包：若有角色则回权威 deck ACK，避免客户端卡等待
            packets: list[bytes] = []
            if cid is not None:
                try:
                    repo = self.server._repo()
                    deck = repo.load_knight_shield_deck(int(cid))
                    packets.append(
                        self.server._codec_response(
                            1,
                            int(ks.CMD_CHANGE_DECK_INFO),
                            ks.build_change_deck_ack_body(deck),
                        )
                    )
                except Exception:
                    pass
            return packets, base + "; reject=short_body"

        if cid is None:
            return [], base + "; reject=no_character"

        repo = self.server._repo()
        ok, note, result = repo.try_save_knight_shield_deck(
            int(cid),
            parsed,
            job=int(job),
        )
        deck = repo.load_knight_shield_deck(int(cid))
        packets = [
            self.server._codec_response(
                1,
                int(ks.CMD_CHANGE_DECK_INFO),
                ks.build_change_deck_ack_body(deck),
            )
        ]
        if not ok:
            return packets, "%s; FAIL %s; %s" % (base, note, ks.describe_deck(deck))

        list_notes: list[str] = []
        # 始终回 S0/0245，保证 UI 底部槽与 DB 一致
        if ks.should_send_deck_notification():
            packets.append(
                self.server._codec_response(
                    0,
                    int(ks.NOTI_SEND_DECK_INFO),
                    ks.build_send_deck_info_body(deck),
                )
            )
            list_notes.append("S0/0245 %s" % ks.describe_deck(deck))
        else:
            list_notes.append("%s (deck_noti_off)" % ks.describe_deck(deck))

        equip_changed = int(result.get("equipment_changed", 0) or 0) == 1
        if equip_changed and env_bool(
            "USDOF_GAME_MOVE_ITEMSPACE_SEND_EQUIP_ITEMLIST", True
        ):
            try:
                equip_helper = load_chain("helper-equip_itemlist.py")
                equip_body, _n, equip_note = equip_helper.build_equipment_bar_update_000e_body(
                    repo,
                    int(cid),
                )
                packets.append(self.server._codec_response(0, 0x000E, equip_body))
                list_notes.append(equip_note)
            except Exception as equip_exc:
                list_notes.append("equip_bar_000e_fail:%s" % equip_exc)

        if equip_changed and env_bool(
            "USDOF_GAME_MOVE_ITEMSPACE_SEND_USERINFO0", True
        ):
            try:
                u0_helper = load_chain("helper-userinfo_equipment.py")
                u0_packets, appearance_note = u0_helper.build_userinfo0_appearance_packets(
                    self.server,
                    character_id=int(cid),
                    record=detail.get("record"),
                    repo=repo,
                    session=session,
                    allow_dungeon_userinfo0=True,
                )
                packets.extend(u0_packets)
                if appearance_note:
                    list_notes.append(appearance_note)
            except Exception as u0_exc:
                list_notes.append("USERINFO0_fail:%s" % type(u0_exc).__name__)

        return packets, "%s; OK %s; %s" % (base, note, "; ".join(list_notes))
