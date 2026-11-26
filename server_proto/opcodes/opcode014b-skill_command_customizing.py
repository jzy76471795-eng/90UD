#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C->S 0x014B SKILL_COMMAND_CUSTOMIZING：技能释放命令自定义。

PacketEnum: ENUM_CMDPACKET_SKILL_COMMAND_CUSTOMIZING = 331。

Stage3 2026-07-22::
  seed0 AES-ECB 解 0x014B 为乱码（与 00BF/0191 同类）→ **解密尝试上限 3 次后改 DLL**
  （AGENTS §0.1.1）。2026-07-24 已迁入 ``usdof_codec_passthrough`` 全局
  encode/decode，服务端 plain-wire 收明文再落库（勿再 seed0）。

明文布局（90qiyuan DecodeSkillCommandRequest / Stage3 live）::
  u8 tree_or_ctx   # 0/1=页镜像；Stage3 批量改键常见 tree=2，仍合法
  repeated:
    u16 skill_id LE
    u8  command_len
    command_len bytes
  （尾部可零 pad 到 16；解析时忽略）

S2C（S4A12 SkillHandler）::
  flag=1 opcode=0x014B body = {0x01} + echo(C2S body)

持久化：独立表 ``character_skill_commands``（权威）+ 镜像
``character_skills.extra_values``。进房 S0/0013 skillinfo SkillTree
field4 = repeated int32（命令字节逐字节）。01EC 清树不删命令库。
"""
from __future__ import annotations

import os
import struct
from typing import Any, List, Optional, Tuple

def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


def parse_skill_command_records(
    body: bytes,
) -> Tuple[int, List[Tuple[int, bytes]], str]:
    """解析 014B plain → (page, [(skill_id, cmd_bytes), ...], note)。

    wire（Stage3 live + 90qiyuan）::
      u8 tree_or_ctx
      repeated: u16 skill_id LE, u8 cmd_len, cmd_len bytes
      尾部可 0 pad

    page 镜像：tree==1 → 1，否则 0（独立命令库不按 page 分）。

    Stage3 2026-07-22 live 批量改键：
      global plain body = ``02 0d 00 03 01 01 08 29 00 03 03 03 08``
      tree_raw=2（>1）但仍是合法多记录明文；**禁止**把 tree>1 一律当 AES 乱码。
      旧逻辑 ``if tree>1: bad_tree`` → try_raw 失败 → seed0 再解 → 批量永不落库。
    """
    data = bytes(body or b"")
    if len(data) < 1:
        return 0, [], "empty_body"
    tree = int(data[0]) & 0xFF
    page = 1 if tree == 1 else 0
    records: List[Tuple[int, bytes]] = []
    offset = 1
    while offset < len(data):
        # 尾部全 0 pad（历史 codec buffer 常见）直接结束
        if data[offset] == 0 and not any(data[offset:]):
            break
        if len(data) - offset < 3:
            return page, [], "truncated_record_header"
        skill_id = struct.unpack_from("<H", data, offset)[0]
        cmd_len = int(data[offset + 2]) & 0xFF
        offset += 3
        if skill_id == 0:
            return page, [], "invalid_skill_id"
        if offset + cmd_len > len(data):
            return page, [], (
                "truncated tree=%s skill=%s cmd_len=%s offset=%s body_len=%s"
                % (tree, skill_id, cmd_len, offset, len(data))
            )
        cmd = bytes(data[offset : offset + cmd_len])
        offset += cmd_len
        records.append((skill_id, cmd))
    if not records:
        # 无记录：tree>1 更像密文；tree∈{0,1} 可能是空改键
        if tree > 1:
            return page, [], "bad_tree=%s body_len=%s" % (tree, len(data))
        return page, [], "tree=%s records=0 body_len=%s" % (tree, len(data))
    return page, records, "tree=%s records=%s" % (tree, len(records))


def try_raw_plain_014b(codec_body: bytes) -> Optional[bytes]:
    """识别历史 raw-plain 线上体；能解析出 ≥1 条记录则直接用，勿再 seed0。

    仅供迁移前二进制兼容。当前默认全局 plain-wire 不经过此门闩。
    返回 handler 用 plain；无法识别则 None。
    """
    raw = bytes(codec_body or b"")
    if len(raw) < 4:
        return None
    # 试整段 / 去掉尾零 pad 后的最短语义体
    candidates = [raw]
    trimmed = raw.rstrip(b"\x00")
    if trimmed and trimmed != raw:
        candidates.append(trimmed)
    # reserved 前缀 3B 有时被拼进 codec_body；若首字节 tree 不合理再试 [3:]
    if len(raw) > 4:
        candidates.append(raw[3:])
        t3 = raw[3:].rstrip(b"\x00")
        if t3 and t3 not in candidates:
            candidates.append(t3)
    best: Optional[bytes] = None
    best_n = 0
    for cand in candidates:
        _page, records, note = parse_skill_command_records(cand)
        if not records:
            continue
        if "truncated" in note and len(records) < 1:
            continue
        n = len(records)
        # 优先记录数最多的候选（整段 plain > 截掉 reserved 的误切）
        if n > best_n:
            best_n = n
            best = cand
    return best


def build_skill_command_ack(body: bytes) -> bytes:
    """S1/014B 成功 ACK：0x01 + 回显 C2S body。"""
    data = bytes(body or b"")
    return b"\x01" + data


class SkillCommandCustomizing014BService:
    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_skill_command_customizing_014b(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        if not _env_bool("USDOF_GAME_HANDLE_SKILL_COMMAND_014B", True):
            return [], "C1/014B SKILL_COMMAND_CUSTOMIZING gate=OFF"

        data = bytes(body or b"")
        base = "C1/014B SKILL_COMMAND_CUSTOMIZING plain_len=%s" % (len(data),)
        page, records, parse_note = parse_skill_command_records(data)
        if not records:
            # A genuine no-op may be acknowledged. Malformed records cannot
            # be echoed into the native client as a successful change.
            ack = build_skill_command_ack(data) if not any(data[1:]) else b"\x00"
            pkt = self.server._codec_response(1, 0x014B, ack)
            return [pkt], "%s; %s" % (base, parse_note)

        updated = None
        status = "invalid_owner_or_skill"
        try:
            account = int(getattr(self.server, "account_id", 0) or 0)
            cid = int(getattr(session, "selected_character_id", 0) or 0)
            if account > 0 and cid > 0 and int(getattr(session, "account_id", account) or 0) == account:
                updated = self.server._repo().save_owned_skill_commands(
                    account, cid, records, persist=_env_bool("USDOF_GAME_SKILL_COMMAND_PERSIST", True),
                )
        except Exception as exc:
            status = "storage:%s" % type(exc).__name__
        if updated is None:
            ack = b"\x00"
        else:
            ack = build_skill_command_ack(data)
            status = "accepted updated=%s" % updated
        pkt = self.server._codec_response(1, 0x014B, ack)
        return [pkt], "%s; %s; page=%s; %s" % (base, parse_note, page, status)
