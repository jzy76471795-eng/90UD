"""Channel script 响应判定。

00 09 是否出现决定服务端是否能发送 7C09 script。该判断被首次进入频道链路
和单元测试共用，独立成文件避免入口服务继续堆逻辑。

A/B env:
- ``USDOF_CHANNEL_SCRIPT_FORCE_OFF``:
  2026-07-13 Stage3 实测发现大区仍灰 + 频道列表空；client cold-start
  发 ``00 0B -> 00 09 -> 00 01`` 后服务端发 ``7C 09`` 仍然让 ``sub_15AC610``
  走错位解析（推断 client 在 cold-start 也命中 ``sub_1221F10()->sub_1210E40()==1``
  cached-script 路径）。打开此 env 让服务端**永远不发** ``7C 09``，只发
  ``7C 0C -> 7C 03``，验证是否解除大区灰。
"""
from __future__ import annotations

import os


def channel_script_response_reason(
    followups: list[int] | tuple[int, ...],
    *,
    implicit_on_channelinfo: bool | None = None,
) -> str | None:
    if os.environ.get("USDOF_CHANNEL_SCRIPT_FORCE_OFF", "").strip().lower() in {"1", "true", "yes", "on"}:
        return None
    opcodes = set(followups)
    if 0x09 in opcodes:
        return "requested-00-09"
    if implicit_on_channelinfo is None:
        implicit_on_channelinfo = False
    if implicit_on_channelinfo and 0x01 in opcodes:
        return "implicit-before-00-01"
    return None
