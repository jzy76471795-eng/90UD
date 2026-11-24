"""Retain the actual connection's C0002 claim without authorizing UDP peers."""
from game.raid.native_network_protocol import parse_udp_address_report
from game.raid.native_udp import UdpAddressClaim


def handle_udp_address_report(server, plain, session):
    # 027630A0 can run before SELECT_CHARACTER. Bind the report to this
    # authenticated TCP account, never to a guessed UID or group member.
    account = getattr(session, 'account_id', None)
    if type(account) is not int or account <= 0 or account != server.account_id:
        return [], 'C0002 rejected: authenticated account unavailable'
    try:
        report = parse_udp_address_report(plain)
        # Registered ACK reader 015FE210 consumes no extra success body.
        ack = server._codec_response(1, 0x0002, b'\x01')
    except ValueError as exc:
        return [], 'C0002 rejected: ' + str(exc)
    probe = getattr(server, 'native_udp_probe_service', None)
    if probe is not None:
        if not probe.record_claim(session, report):
            return [], 'C0002 rejected: current authenticated UDP connection unavailable'
    else:
        claim = UdpAddressClaim(account, report)
        session._native_udp_latest_address_claim = claim
        session._native_udp_address_claim = claim
    effective = getattr(session, '_native_udp_address_claim', None)
    if effective is not None and effective.report is not report:
        return [ack], 'C0002 latest report retained; previous effective UDP address preserved'
    return [ack], 'C0002 client address claim retained; UDP endpoint and combat readiness unverified'
