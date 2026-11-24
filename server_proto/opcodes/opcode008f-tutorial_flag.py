"""Tutorial flag C->S 0x008F (CHANGE_TUTORIAL_FLAG) — Step E.

Stage3 client send sites (IDA, sub_2B60950 / sub_2B60E30 / sub_2B61270):
  net_send_gate_check_1093_1094(writer, 0x8F)
  write_u8(0)            # sub_2BF0560(0)
  write_u32(flagIndex)   # sub_2BF05A0(writer, flag)
  write_u8(rewardFlag)   # sub_2BF0560(1) in live call sites (often 1)
  flush                   # sub_2BF3160

Semantic body (before codec pad):
  u8  prefix0     always 0 in observed builders
  u32 flagIndex   LE
  u8  rewardFlag

S4A12 (read-only contrast) uses u32+u8 without the leading 0; Stage3 wins.

Safety defaults:
  USDOF_GAME_TUTORIAL_HANDLE_008F=0   log/parse only
  USDOF_GAME_TUTORIAL_008F_SEND_ACK=0 no ACK unless explicitly enabled
  No item rewards. No flag=31 return-to-town. No enter-path change.
"""
from __future__ import annotations

import os
from typing import Any


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


def normalize_tutorial_flag_index(flag_index: int) -> int:
    """Normalize C->S / stored tutorial flag to Stage3 u8 index (0..101).

    Live Stage3 body is ``u8 prefix + u32 flag + u8 reward`` (often pad-aligned).
    Old parser treated prefix!=0 as S4 ``u32+u8`` and stored values like
    ``0x0D01`` (3329) instead of ``13``. START ``sub_2B66670`` only accepts
    per-byte indices into ``buf_1[0..0x65]``.
    """
    v = int(flag_index) & 0xFFFFFFFF
    if v <= 0x65:
        return int(v)
    # Misparsed Stage3: low byte was prefix (0/1), high byte was real index.
    lo = v & 0xFF
    hi = (v >> 8) & 0xFF
    if lo in (0, 1) and hi <= 0x65 and (v >> 16) == 0:
        return int(hi)
    return int(v & 0xFF)


def parse_008f_plain(plain: bytes) -> dict[str, Any]:
    """Parse Stage3-first; fall back to S4A12 5B layout for logging only."""
    raw = bytes(plain or b"")
    out: dict[str, Any] = {
        "raw_hex": raw.hex(" "),
        "raw_len": len(raw),
        "layout": "unknown",
        "flag_index": None,
        "reward_flag": None,
        "prefix0": None,
        "flag_index_raw": None,
    }
    # Stage3 live: always leading u8 (0 or 1 seen) + u32 flag + u8 reward (+ AES pad).
    # Do NOT require prefix==0 — that mis-parsed shop/map UI flags as huge ints.
    if len(raw) >= 6:
        out["layout"] = "stage3_u8_prefix+u32_flag+u8_reward"
        out["prefix0"] = int(raw[0]) & 0xFF
        raw_flag = int.from_bytes(raw[1:5], "little")
        out["flag_index_raw"] = raw_flag
        out["flag_index"] = normalize_tutorial_flag_index(raw_flag)
        out["reward_flag"] = int(raw[5]) & 0xFF
        return out
    if len(raw) >= 5:
        out["layout"] = "s4a12_u32_flag+u8_reward_fallback"
        raw_flag = int.from_bytes(raw[0:4], "little")
        out["flag_index_raw"] = raw_flag
        out["flag_index"] = normalize_tutorial_flag_index(raw_flag)
        out["reward_flag"] = int(raw[4]) & 0xFF
        return out
    if len(raw) >= 4:
        out["layout"] = "u32_flag_only"
        raw_flag = int.from_bytes(raw[0:4], "little")
        out["flag_index_raw"] = raw_flag
        out["flag_index"] = normalize_tutorial_flag_index(raw_flag)
        return out
    return out


def parse_001f_plain(plain: bytes) -> dict[str, Any]:
    """Log-only helper for ACCEPT_QUEST (Step F will own full handling)."""
    raw = bytes(plain or b"")
    out: dict[str, Any] = {
        "raw_hex": raw.hex(" "),
        "raw_len": len(raw),
        "layout": "unknown",
        "quest_id": None,
        "u16_0": None,
        "u16_1": None,
    }
    if len(raw) >= 2:
        u0 = int.from_bytes(raw[0:2], "little")
        out["u16_0"] = u0
        if len(raw) >= 4:
            u1 = int.from_bytes(raw[2:4], "little")
            out["u16_1"] = u1
            if u0 == 0x001F and 0 < u1 < 0xFFFF:
                out["layout"] = "u16_opcode_echo+u16_quest"
                out["quest_id"] = u1
            else:
                out["layout"] = "u16_quest(+pad)"
                out["quest_id"] = u0
        else:
            out["layout"] = "u16_quest"
            out["quest_id"] = u0
    return out


class TutorialFlag008FService:
    """Gated CHANGE_TUTORIAL_FLAG handler (C->S 0x008F)."""

    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_tutorial_flag_008f(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        parsed = parse_008f_plain(body)
        handle_on = _env_bool("USDOF_GAME_TUTORIAL_HANDLE_008F", False)
        send_ack = _env_bool("USDOF_GAME_TUTORIAL_008F_SEND_ACK", False)

        cid = None
        if session is not None:
            cid = getattr(session, "selected_character_id", None)
        if cid is None:
            try:
                repo = self.server._repo()
                rows = repo.list_by_account(getattr(self.server, "account_id", 1))
                if rows:
                    cid = int(rows[0].character_id)
            except Exception:
                cid = None

        base = (
            f"C1/008F CHANGE_TUTORIAL_FLAG plain_len={parsed['raw_len']} "
            f"plain={parsed['raw_hex'] or '<empty>'} layout={parsed['layout']} "
            f"flag_index={parsed['flag_index']} reward_flag={parsed['reward_flag']} "
            f"cid={cid} handle={int(handle_on)} send_ack={int(send_ack)}"
        )

        if not handle_on:
            return [], base + "; gate=OFF store/ack skipped (log-only)"

        store_note = "store_skipped_no_cid"
        complete_note = "complete=skip"
        if cid is not None and parsed["flag_index"] is not None:
            try:
                repo = self.server._repo()
                flag_i = int(parsed["flag_index"]) & 0xFF
                # Stage3：prefix=1 为 UI guide id（0..15），START 只写 low bank，
                # 须同时存 map 后的 67..88（IDA sub_10771F0 / sub_1074920）。
                store_indices = [flag_i]
                try:
                    from game.character.payloads import (
                        STAGE3_UI_GUIDE_ID_TO_LOW_FLAG,
                    )

                    prefix0 = parsed.get("prefix0")
                    if prefix0 is None or int(prefix0) == 1:
                        mapped = STAGE3_UI_GUIDE_ID_TO_LOW_FLAG.get(flag_i)
                        if mapped is not None and mapped not in store_indices:
                            store_indices.append(int(mapped))
                except Exception:
                    pass
                ok = True
                for si in store_indices:
                    if not repo.add_tutorial_flag(int(cid), int(si)):
                        ok = False
                # 账号级也写一份：赛丽亚 UI 指引（商城/选图）按账号只弹一次
                account_id = int(
                    getattr(session, "account_id", None)
                    or getattr(self.server, "account_id", 1)
                    or 1
                )
                try:
                    aok = True
                    for si in store_indices:
                        if not repo.add_account_tutorial_flag(account_id, int(si)):
                            aok = False
                except Exception:
                    aok = False
                flags = repo.get_tutorial_flags(int(cid))
                try:
                    aflags = repo.get_account_tutorial_flags(account_id)
                except Exception:
                    aflags = []
                store_note = (
                    f"store={'ok' if ok else 'fail'} flags={flags} "
                    f"account_store={'ok' if aok else 'fail'} account_flags={aflags} "
                    f"last_flag={flag_i} store_idx={store_indices} "
                    f"raw_flag={parsed.get('flag_index_raw')} prefix={parsed.get('prefix0')}"
                )
                # S4A12 / df_game_r: flagIndex==31 + in dungeon → tutorial complete
                if flag_i == 31 and _env_bool(
                    "USDOF_GAME_TUTORIAL_008F_FLAG31_COMPLETE", True
                ):
                    from game.chain_loader import load_chain

                    _tut = load_chain("helper-tutorial_dungeon.py")
                    in_tut = bool(
                        session is not None
                        and getattr(session, "is_tutorial_dungeon_run", False)
                    )
                    if not in_tut and session is not None:
                        did = int(
                            getattr(session, "cur_dungeon_id", 0)
                            or getattr(session, "selected_dungeon_id", 0)
                            or 0
                        )
                        in_tut = bool(did and _tut.is_tutorial_dungeon_id(did))
                    if in_tut or _env_bool(
                        "USDOF_GAME_TUTORIAL_008F_FLAG31_COMPLETE_ALWAYS", False
                    ):
                        cok = _tut.mark_tutorial_complete(repo, int(cid))
                        if session is not None:
                            _tut.bind_session_tutorial_flags(
                                session, dungeon_id=0, active=False
                            )
                        complete_note = "complete_flag31=%s" % ("ok" if cok else "fail")
            except Exception as exc:
                store_note = f"store_error={exc!r}"

        packets: list[bytes] = []
        ack_note = "ack=off"
        if send_ack:
            packets.append(self.server._codec_response(1, 0x008F, bytes([1, 0])))
            ack_note = f"ack=S1/008F 01 00 wire={len(packets[0])}"

        safety = "no_reward no_return_town_auto"
        return packets, f"{base}; gate=ON {store_note}; {complete_note}; {ack_note}; {safety}"

    def handle_accept_quest_001f_log_only(self, body: bytes) -> tuple[list[bytes], str]:
        parsed = parse_001f_plain(body)
        return [], (
            f"C1/001F ACCEPT_QUEST log-only plain_len={parsed['raw_len']} "
            f"plain={parsed['raw_hex'] or '<empty>'} layout={parsed['layout']} "
            f"quest_id={parsed['quest_id']} u16_0={parsed['u16_0']} u16_1={parsed['u16_1']}; "
            f"Step F handler not enabled"
        )
