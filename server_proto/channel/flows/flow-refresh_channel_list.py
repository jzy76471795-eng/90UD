"""Channel 刷新频道列表请求链。

当前客户端在 Channel TCP 连接中发送 00 11 时，服务端返回 7C/0x12
形式的新频道列表。该链路独立于首次进入频道的 00 0B/00 09/00 01，
所以单独放一个文件，便于后续继续逆向刷新按钮的字段。
"""
from __future__ import annotations

import time
from typing import Any

from channel.channel_crypto import build_encrypted_packed_payload
from channel.channel_info import build_channelinfo_plaintext_by_profile
from channel.channel_wire import build_channel_packet


def send_refresh_channel_list_response(server: Any, client: Any, prefix: str, diary: Any, followups: list[int]) -> None:
    """如本连接收到了 00 11，则发送刷新频道列表响应。"""
    if 0x11 not in followups:
        return

    new_started = time.perf_counter()
    new_plain = build_channelinfo_plaintext_by_profile(
        profile="dxf-new",
        game_ip_text=server.game_host,
        game_port=server.game_port,
        envelope=None,
    )
    new_diary = diary.record_channelinfo_plaintext(
        new_plain,
        envelope=None,
        label="channelinfo_new_plain",
    )
    new_payload = build_encrypted_packed_payload(new_plain)
    new_packet = build_channel_packet(0x12, new_payload, flag=server.response_flag)
    new_summary = diary.record_response(
        f"resp_{server.response_flag:02X}_12_channelinfo_new",
        new_packet,
        semantic="channel_info_new",
    )
    server._log_channel_sequence_packet(
        direction="S->C",
        connection_id=prefix,
        packet=new_packet,
        plain_body=new_plain,
        semantic="channel_info_new_plaintext_before_encrypted_packing",
        note="profile=dxf-new; raw packet body is encrypted packed payload",
    )
    client.sendall(new_packet)
    server.log_traffic(
        f"[Channel] send NEW channel info response opcode={server.response_flag:02X} 12 "
        f"profile=dxf-new plain={new_diary.get('plaintext_len')} "
        f"total=0x{new_summary['total_length']:X} payload=0x{new_summary['payload_len']:X} "
        f"body_sha256={new_summary['body_sha256'][:16]} "
        f"build_elapsed_ms={int((time.perf_counter() - new_started) * 1000)}"
    )
