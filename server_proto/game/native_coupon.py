"""Native 4x4 Coupon submission; awards use the existing Go CDK transaction."""
from pathlib import Path
import hashlib
import http.client
import json
import os
import re
import ssl
import struct

_GROUPED = re.compile(rb'[A-Za-z0-9]{4}(?:-[A-Za-z0-9]{4}){3}')
_PLAIN = re.compile(rb'[A-Za-z0-9]{16}')


def parse_code(body):
    raw = bytes(body)
    if len(raw) < 4:
        raise ValueError('truncated Coupon string')
    size, = struct.unpack_from('<I', raw)
    end = 4 + size
    if size not in (16, 19) or end > len(raw):
        raise ValueError('invalid Coupon length')
    if len(raw) != end and not (any(len(raw) == (end + unit - 1) // unit * unit for unit in (8, 16)) and not any(raw[end:])):
        raise ValueError('invalid Coupon padding')
    code = raw[4:end]
    if not (_GROUPED.fullmatch(code) or _PLAIN.fullmatch(code)):
        raise ValueError('invalid Coupon characters')
    return code.replace(b'-', b'').decode('ascii').upper()


def result_body(error=0, points=0, mails=0):
    if type(points) is not int or not 0 <= points <= 99999999:
        raise ValueError('invalid Coupon award amount')
    if type(mails) is not int or not 0 <= mails <= 255:
        raise ValueError('invalid Coupon delivery count')
    # 00F90070 reads u32 amount and u8 count even in the error branch.
    if error:
        if error not in (1, 2, 3):
            raise ValueError('unknown native Coupon error')
        return bytes((0, error)) + struct.pack('<IB', 0, 0)
    # The native reader discards each pair; no unproved item semantics are used.
    return b'\x01' + struct.pack('<IB', points, mails) + b'\0' * (8 * mails)


def backend_redeem(server, account, character, code):
    db = Path(server._repo().db_path).resolve()
    root = Path(os.environ.get('USDOF_REPO_ROOT') or db.parents[2]).resolve()
    path = root / 'gm_manager/go-manager.json'
    if not path.is_file() or path.stat().st_size > 65536:
        raise RuntimeError('manager configuration unavailable')
    config = json.loads(path.read_text('utf-8-sig'))
    host, separator, port = str(config.get('listen', '')).rpartition(':')
    if not separator or host not in ('127.0.0.1', 'localhost', '0.0.0.0') or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise RuntimeError('unsupported local manager listener')
    token = config.get('token')
    if not isinstance(token, str) or len(token) < 32:
        raise RuntimeError('manager authentication unavailable')
    certificate = config.get('tls_certificate')
    if certificate:
        cert_path = Path(certificate)
        if not cert_path.is_absolute():
            cert_path = path.parent / cert_path
        pem = cert_path.read_text('ascii')
        context = ssl.create_default_context(cadata=pem)
        # The fixed loopback address is pinned to this exact configured leaf.
        context.check_hostname = False
        connection = http.client.HTTPSConnection('127.0.0.1', int(port), timeout=6, context=context)
        connection.connect()
        expected = ssl.PEM_cert_to_DER_cert(pem)
        if hashlib.sha256(connection.sock.getpeercert(binary_form=True)).digest() != hashlib.sha256(expected).digest():
            connection.close()
            raise RuntimeError('manager TLS identity mismatch')
    else:
        connection = http.client.HTTPConnection('127.0.0.1', int(port), timeout=6)
    try:
        payload = json.dumps(dict(account_id=account, character_id=character, code=code)).encode('utf-8')
        connection.request('POST', '/manage/v1/native/coupon', body=payload, headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        response = connection.getresponse()
        raw = response.read(65537)
        if response.status != 200 or len(raw) > 65536:
            raise RuntimeError('Coupon backend unavailable')
        doc = json.loads(raw)
        if doc.get('ok') is not True or not isinstance(doc.get('data'), dict):
            raise RuntimeError('invalid Coupon backend response')
        return doc['data']
    finally:
        connection.close()


def handle(server, body, *, session=None, redeem=None):
    emit = server._codec_response
    try:
        code = parse_code(body)
        account = getattr(session, 'account_id', None)
        character = getattr(session, 'selected_character_id', None)
        if type(account) is not int or type(character) is not int or min(account, character) <= 0:
            raise ValueError('Coupon requires an authenticated selected character')
        directory = getattr(server, 'native_game_connections', None)
        selection = directory.chat_selection(session, sender=True) if directory is not None else None
        if not selection or selection[2] != account or selection[4] != character:
            raise ValueError('Coupon selected owner is no longer current')
        data = (redeem or backend_redeem)(server, account, character, code)
        # The request belongs to this account-thread session. A replacement may
        # receive a fresh snapshot later, but must never receive this response.
        if directory.chat_selection(session, sender=True) != selection:
            return [], 'C1/0497 Coupon owner changed after backend completion; durable receipt retained'
        error = data.get('coupon_error', 0)
        if type(error) is not int or error not in (0, 1, 2, 3):
            raise ValueError('invalid Coupon backend result')
        if error:
            return [emit(1, 0x0497, result_body(error))], 'C1/0497 Coupon rejected error=%d' % error
        result = data.get('result')
        if not isinstance(result, dict):
            raise ValueError('missing durable Coupon receipt')
        points = result.get('points', 0)
        mails = result.get('mailIds', [])
        if not isinstance(mails, list) or len(mails) > 100 or any(type(mid) is not int or mid <= 0 for mid in mails):
            raise ValueError('invalid Coupon delivery receipt')
        deliveries = data.get('native_delivery_count')
        if type(deliveries) is not int or not 0 <= deliveries <= 100:
            raise ValueError('invalid Coupon delivery summary')
        packets = [emit(1, 0x0497, result_body(points=points, mails=deliveries))]
    except Exception as exc:
        # Never record the private configuration, token, input code or raw HTTP
        # exception text. No local fallback award is permitted.
        return [emit(1, 0x0497, result_body(3))], 'C1/0497 Coupon failed kind=%s' % type(exc).__name__
    try:
        from game.native_mailbox import snapshot_packets
        refresh, _ = snapshot_packets(server, session)
        packets.extend(refresh)
    except Exception:
        return packets, 'C1/0497 Coupon committed; mailbox refresh pending'
    return packets, 'C1/0497 Coupon committed; native mailbox refreshed'
