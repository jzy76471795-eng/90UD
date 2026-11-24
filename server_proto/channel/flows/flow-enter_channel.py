"""Channel 首次进入频道请求链。

本文件覆盖 00 0B -> [00 09] -> [00 01] 的进入频道链路：
1. 读取客户端请求序列；
2. 发送服务器 key；
3. 仅当客户端显式请求 00 09 时发送 script；
4. 当客户端请求 00 01 时发送 ChannelInfo；
5. 若同连接出现 00 11，转交刷新频道列表链路文件处理。

ChannelServer 仍是 socket 入口和日志能力持有者，本文件只负责该请求链。
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import struct
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from config import CHANNEL_SESSION_KEY, LOG_DIR, SCRIPT_RESPONSE_OPCODE
from channel.channel_crypto import build_encrypted_packed_payload
from channel.channel_script import build_script_plaintext
from channel.channel_wire import build_channel_packet, describe_header, read_channel_packet
from game.chain_loader import load_chain
send_refresh_channel_list_response = load_chain('flow-refresh_channel_list.py', area="channel").send_refresh_channel_list_response
channel_script_response_reason = load_chain('flow-script_decision.py', area="channel").channel_script_response_reason


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


def _env_int(name: str, default: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return int(default)
    return int(raw.strip(), 0)


def _path_from_env(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return Path(raw.strip())


def read_enter_channel_requests(server: Any, client: Any, prefix: str, diary: Any) -> list[int]:
    """读取进入频道链路请求，返回后续 opcode 列表。"""
    server._recv_expected(client, prefix, 0x0B, "00_0B", diary=diary)

    # 当前客户端首次进入通常是 00 0B,00 09,00 01；角色页返回频道可能只有
    # 00 0B,00 01。这里不强制等待 00 09，避免破坏 cached-script 路径。
    followups: list[int] = []
    original_timeout = client.gettimeout()
    client.settimeout(float(os.environ.get("USDOF_CHANNEL_FOLLOWUP_TIMEOUT", "0.35")))
    try:
        max_followups = int(os.environ.get("USDOF_CHANNEL_MAX_FOLLOWUPS", "4"))
        for index in range(max_followups):
            try:
                header, body = read_channel_packet(client)
            except socket.timeout:
                diary.add_note(f"followup read timeout after {index} packet(s)")
                break
            label = f"00_{header.opcode:02X}_{index}"
            server._log_channel_sequence(
                direction="C->S",
                connection_id=prefix,
                header=header,
                wire_body=body,
                semantic=f"followup_request_{label}",
            )
            diary.record_request(label, header, body)
            followups.append(header.opcode)
            body_sha = hashlib.sha256(body).hexdigest()[:16]
            if header.opcode == 0x09:
                server.log_traffic(f"[Channel] recv 00 09: {describe_header(header)} body_sha256={body_sha}")
            elif header.opcode == 0x01:
                server.log_traffic(f"[Channel] recv 00 01: {describe_header(header)} body_sha256={body_sha}")
            elif header.opcode == 0x11:
                server.log_traffic(f"[Channel] recv 00 11 new-channel-info request: {describe_header(header)} body_sha256={body_sha}")
            else:
                server.log(f"[Channel] recv unexpected followup opcode: {describe_header(header)} body_sha256={body_sha}")
    finally:
        client.settimeout(original_timeout)
    return followups


def maybe_wait_channel_response_pre_send_gate(
    server: Any,
    *,
    prefix: str,
    diary: Any,
    followups: list[int],
) -> str:
    """调试 gate：收到 Channel 请求后、发送 7C0C/7C09/7C03 前等待。

    维护说明：
    - 默认关闭，只用于 x32dbg 动态验证 ChannelInfo parser/UI commit。
    - 它不改任何 Channel 回包内容，只在发送前写 ready 文件并等 release。
    - 若要抓冷启动 7C03，先让客户端连到 Channel，服务端在这里停住；
      此时附加 x32dbg、种好 0x015AE960/0x0121F950 等断点，再写 release。
    """

    if not _env_bool("USDOF_CHANNEL_RESPONSE_PRE_SEND_WAIT", False):
        return ""

    ready_path = _path_from_env(
        "USDOF_CHANNEL_RESPONSE_PRE_SEND_READY_FILE",
        LOG_DIR / "channel_response_pre_send_ready.json",
    )
    release_path = _path_from_env(
        "USDOF_CHANNEL_RESPONSE_PRE_SEND_RELEASE_FILE",
        LOG_DIR / "channel_response_pre_send_release.flag",
    )
    timeout_ms = max(0, _env_int("USDOF_CHANNEL_RESPONSE_PRE_SEND_TIMEOUT_MS", 60000))
    poll_seconds = max(0.05, _env_int("USDOF_CHANNEL_RESPONSE_PRE_SEND_POLL_MS", 100) / 1000.0)
    clear_release = _env_bool("USDOF_CHANNEL_RESPONSE_PRE_SEND_CLEAR_RELEASE", True)
    continue_on_timeout = _env_bool("USDOF_CHANNEL_RESPONSE_PRE_SEND_CONTINUE_ON_TIMEOUT", True)
    nonce = f"{int(time.time() * 1000)}-{os.getpid()}"
    payload = {
        "nonce": nonce,
        "stage": "channel-response-pre-send",
        "time": datetime.now().isoformat(timespec="milliseconds"),
        "pid": os.getpid(),
        "connection_id": prefix,
        "request_sequence": diary.request_sequence(),
        "followups": [f"0x{opcode:02x}" for opcode in followups],
        "release_file": str(release_path),
        "timeout_ms": timeout_ms,
        "note": "service has received Channel requests and is waiting before S->C Channel responses",
    }

    try:
        ready_path.parent.mkdir(parents=True, exist_ok=True)
        release_path.parent.mkdir(parents=True, exist_ok=True)
        if clear_release and release_path.exists():
            release_path.unlink()
        ready_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        return f" channel_response_pre_send_gate write_failed={type(exc).__name__}:{exc}"

    server.log(
        "[Channel/Gate] response pre-send wait "
        f"nonce={nonce} ready={ready_path} release={release_path} timeout_ms={timeout_ms}"
    )
    deadline = time.time() + timeout_ms / 1000.0
    while True:
        if release_path.exists():
            try:
                release_text = release_path.read_text(encoding="utf-8", errors="replace").strip()
            except Exception:
                release_text = ""
            note = (
                " channel_response_pre_send_gate released "
                f"nonce={nonce} ready={ready_path} release={release_path}"
                f"{' release_note=' + release_text[:120] if release_text else ''}"
            )
            server.log(f"[Channel/Gate] response pre-send released nonce={nonce}")
            return note
        if timeout_ms == 0 or time.time() >= deadline:
            note = (
                " channel_response_pre_send_gate timeout "
                f"nonce={nonce} timeout_ms={timeout_ms} ready={ready_path} release={release_path}"
            )
            server.log("[Channel/Gate]" + note)
            if continue_on_timeout:
                return note + " continue_on_timeout=1"
            while not release_path.exists():
                time.sleep(poll_seconds)
            return note + " late_release_after_timeout"
        time.sleep(poll_seconds)


def send_enter_channel_responses(server: Any, client: Any, prefix: str, diary: Any, followups: list[int]) -> None:
    """按已收到请求发送进入频道链路响应。"""
    server.log_traffic(f"[Channel] request sequence {diary.request_sequence()}")
    gate_note = maybe_wait_channel_response_pre_send_gate(
        server,
        prefix=prefix,
        diary=diary,
        followups=followups,
    )
    if gate_note:
        diary.add_note(gate_note.strip())

    key_packet = server.build_server_key_packet(response_flag=server.response_flag)
    key_plain = struct.pack("<I", 0) + CHANNEL_SESSION_KEY
    key_summary = diary.record_response(
        f"resp_{server.response_flag:02X}_0C_server_key",
        key_packet,
        semantic="server_key",
    )
    server._log_channel_sequence_packet(
        direction="S->C",
        connection_id=prefix,
        packet=key_packet,
        plain_body=key_plain,
        semantic="server_key",
    )
    client.sendall(key_packet)
    server.log_traffic(
        f"[Channel] send {server.response_flag:02X} 0C server key response "
        f"total=0x{key_summary['total_length']:X} payload=0x{key_summary['payload_len']:X} "
        f"body_sha256={key_summary['body_sha256'][:16]}"
    )

    script_reason = channel_script_response_reason(followups)
    if script_reason is not None:
        script_plain, script_source = build_script_plaintext(
            prefer_pvf=server.prefer_pvf_script,
            pvf_path=server.pvf_path,
            profile=server.script_profile,
        )
        script_diary = diary.record_script_plaintext(script_plain, source=script_source)
        script_payload = build_encrypted_packed_payload(script_plain)
        script_packet = build_channel_packet(SCRIPT_RESPONSE_OPCODE, script_payload, flag=server.response_flag)
        script_summary = diary.record_response(
            f"resp_{server.response_flag:02X}_{SCRIPT_RESPONSE_OPCODE:02X}_script",
            script_packet,
            semantic="script",
        )
        server._log_channel_sequence_packet(
            direction="S->C",
            connection_id=prefix,
            packet=script_packet,
            plain_body=script_plain,
            semantic="script_plaintext_before_encrypted_packing",
            note=f"reason={script_reason}; source={script_source}; raw packet body is encrypted packed payload",
        )
        client.sendall(script_packet)
        server.log_traffic(
            f"[Channel] send script response flag={server.response_flag:02X} "
            f"reason={script_reason} "
            f"source={script_source} plain={script_diary['plaintext_len']} "
            f"total=0x{script_summary['total_length']:X} payload=0x{script_summary['payload_len']:X} "
            f"body_sha256={script_summary['body_sha256'][:16]}"
        )
        if script_reason != "requested-00-09":
            diary.add_note(
                "script response sent implicitly before ChannelInfo because request 00 01 "
                "arrived without 00 09 on this TCP connection"
            )
    else:
        server.log_traffic("[Channel] skip script response because request 00 09 was not present")
        diary.add_note("script response skipped: request 00 09 was not present on this TCP connection")

    if 0x01 in followups:
        info_started = time.perf_counter()
        info_plain = server._build_channelinfo_plain()
        info_diary = diary.record_channelinfo_plaintext(info_plain, envelope=server.channelinfo_envelope)
        info_payload = build_encrypted_packed_payload(info_plain)
        info_packet = build_channel_packet(0x03, info_payload, flag=server.response_flag)
        info_summary = diary.record_response(
            f"resp_{server.response_flag:02X}_03_channelinfo",
            info_packet,
            semantic="channel_info",
        )
        server._log_channel_sequence_packet(
            direction="S->C",
            connection_id=prefix,
            packet=info_packet,
            plain_body=info_plain,
            semantic="channel_info_plaintext_before_encrypted_packing",
            note=(
                f"profile={server.channelinfo_profile} envelope={server.channelinfo_envelope or 'none'}; "
                "raw packet body is encrypted packed payload"
            ),
        )
        client.sendall(info_packet)
        parsed = (info_diary.get("parsed") or {}) if isinstance(info_diary, dict) else {}
        first_channel = ""
        try:
            groups = parsed.get("groups") or []
            channels = groups[0].get("channels") or []
            if channels:
                ch0 = channels[0]
                first_channel = (
                    f" first_channel={ch0.get('name')} "
                    f"{ch0.get('ip')}:{ch0.get('port')} "
                    f"users={ch0.get('cur_users')}/{ch0.get('max_users')}"
                )
        except Exception:
            first_channel = ""
        server.log_traffic(
            f"[Channel] send channel info response opcode={server.response_flag:02X} 03 "
            f"profile={server.channelinfo_profile} envelope={server.channelinfo_envelope or 'none'} "
            f"plain={info_diary.get('plaintext_len')} "
            f"groups={parsed.get('group_count')} "
            f"total=0x{info_summary['total_length']:X} payload=0x{info_summary['payload_len']:X} "
            f"body_sha256={info_summary['body_sha256'][:16]}"
            f" build_elapsed_ms={int((time.perf_counter() - info_started) * 1000)}"
            f"{first_channel} game={server.effective_game_host()}:{server.game_port}"
        )
    else:
        server.log_traffic("[Channel] skip channel info response because request 00 01 was not present")
        diary.add_note("channel info response skipped: request 00 01 was not present on this TCP connection")

    send_refresh_channel_list_response(server, client, prefix, diary, followups)


def process_enter_channel_chain(server: Any, client: Any, prefix: str, diary: Any) -> None:
    """完整执行进入频道请求链。"""
    followups = read_enter_channel_requests(server, client, prefix, diary)
    send_enter_channel_responses(server, client, prefix, diary, followups)
