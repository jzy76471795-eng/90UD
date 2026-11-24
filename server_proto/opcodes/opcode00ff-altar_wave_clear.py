"""C1/00FF altar death-list report and wave progression."""
from __future__ import annotations

from typing import Any

from game.chain_loader import load_chain

_payloads = load_chain("helper-altar-payloads.py")
AltarRandomFlow = load_chain("flow-altar-random.py").AltarRandomFlow


def handle_altar_wave_clear_00ff(
    server: Any,
    body: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    ack_body = _payloads.build_blood_wave_clear_ack_00ff(0)
    ack = server._codec_response(1, 0x00FF, ack_body)
    try:
        monster_seqs = _payloads.parse_blood_wave_clear_00ff(body)
    except ValueError as exc:
        return [ack], "altar 00FF ACK u32=0 reject=parse error=%s" % exc
    flow = AltarRandomFlow(server)
    accepted, validation_note = flow.validate_wave_deaths(
        monster_seqs,
        session=session,
    )
    if not accepted:
        return [ack], "altar 00FF ACK u32=0; " + validation_note

    death_packets: list[bytes] = []
    death_notes: list[str] = []
    room_mons = list(getattr(session, "dungeon_room_mons", None) or [])
    rows_by_seq = {
        int(row.get("seq") or 0) & 0xFFFF: row
        for row in room_mons
        if isinstance(row, dict)
    }
    die_handler = getattr(server, "_handle_die_monster_0027", None)
    for seq in monster_seqs:
        row = rows_by_seq.get(int(seq) & 0xFFFF)
        if row is not None and bool(row.get("killed")):
            death_notes.append("seq=%d already_killed" % int(seq))
            continue
        if callable(die_handler):
            packets, note = die_handler(
                (int(seq) & 0xFFFF).to_bytes(2, "little"),
                session=session,
                defer_altar_progression=True,
            )
            death_packets.extend(packets)
            death_notes.append("seq=%d %s" % (int(seq), note))

    progression_packets, progression_note = flow.handle_wave_clear(
        monster_seqs,
        session=session,
        validated=True,
    )
    return [ack] + death_packets + progression_packets, (
        "altar 00FF ACK u32=0; %s; deaths=[%s]; %s"
        % (
            validation_note,
            " | ".join(death_notes) if death_notes else "state_only",
            progression_note,
        )
    )
