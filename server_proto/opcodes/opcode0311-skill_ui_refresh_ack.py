# -*- coding: utf-8 -*-
"""C1/0x0311：技能窗 skill_ui_refresh 客户端回声 / 门闩包。

Stage3 live（2026-07-21）：
  - ``skill_ui_refresh_from_final_mode@0x18ADC30`` 在 001D final_mode 1|3 后
    会 ``net_send_gate_check(..., 0x311)`` + 发 28B blob
  - 服务端此前无 handler → 日志 ``no handler for 0x0311``
  - ``sub_18ADC30`` 在原生路径中检查并清掉 ``this+0x47C`` wait 标志

本 handler：**有把握的最小 ACK**（S1/0311 body=0x01），不改 DB / 不叠业务包。
与 unknown-receipt 分离登记，默认开启（技能窗路径需要）。
"""
from __future__ import annotations

import os
from typing import Any


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return bool(default)
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "n"}


def build_skill_ui_refresh_ack() -> bytes:
    """S1/0311 成功 ACK：单字节 0x01。"""
    return bytes([0x01])


class SkillUiRefresh0311Service:
    """C1/0x0311 静默 ACK（技能窗 refresh 回声）。"""

    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_skill_ui_refresh_0311(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        _ = session
        data = bytes(body or b"")
        if not _env_bool("USDOF_GAME_HANDLE_SKILL_UI_0311", True):
            return [], "C1/0311 plain_len=%s; gate=OFF" % (len(data),)

        ack = build_skill_ui_refresh_ack()
        packet = self.server._codec_response(1, 0x0311, ack)
        return [packet], (
            "C1/0311 skill_ui_refresh_echo plain_len=%s; ACK wire=%s"
            % (len(data), len(packet))
        )
