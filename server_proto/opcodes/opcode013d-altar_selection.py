"""C1/013D Ultimate Altar A/B selection and status-plus-value ACK."""
from __future__ import annotations

from typing import Any

from game.chain_loader import load_chain

_payloads = load_chain("helper-altar-payloads.py")
AltarRandomFlow = load_chain("flow-altar-random.py").AltarRandomFlow


def handle_altar_selection_013d(
    server: Any,
    body: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    try:
        selection = _payloads.parse_blood_selection_013d(body)
    except ValueError as exc:
        packet = server._codec_response(
            1,
            0x013D,
            _payloads.build_blood_selection_ack_013d(0),
        )
        return [packet], "altar 013D ACK=0 reject=parse error=%s" % exc
    accepted, response_packets, note = AltarRandomFlow(server).handle_selection(
        selection,
        session=session,
    )
    ack_value = selection if accepted else 0
    packet = server._codec_response(
        1,
        0x013D,
        _payloads.build_blood_selection_ack_013d(ack_value),
    )
    return [packet] + response_packets, "altar 013D ACK=%d; %s" % (ack_value, note)
