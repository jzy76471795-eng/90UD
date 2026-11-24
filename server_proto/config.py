# -*- coding: utf-8 -*-
"""端口 / 协议常量。

路径类常量委托 ``data`` 包，避免双真源。
"""
from __future__ import annotations

import os

from data import (
    BASE_DIR,
    CAPTURE_DIR,
    DATA_DIR,
    DOC_DIR,
    LOG_DIR,
    PVF_PATH as _DATA_PVF_PATH,
    RESOURCE_DIR,
    ensure_dirs,
)

# 再导出路径，兼容历史 ``from config import BASE_DIR, LOG_DIR, ...``
PVF_PATH = os.environ.get("USDOF_PVF_PATH", str(_DATA_PVF_PATH))

LOCAL_HOST = "127.0.0.1"
SERVER_HOST = "0.0.0.0"
CHANNEL_PORT = 7001
GAME_PORT = 10052
# The control API keeps Bearer-token authentication, but its listener is also
# reachable by external GM tools. The endpoint descriptor still exposes
# 127.0.0.1 as the local forwarding address.
CONTROL_HOST = "0.0.0.0"
CONTROL_PORT = 17600
LOGIN_HOST = "0.0.0.0"
LOGIN_PORT = 17602

# Ports observed in DXF/ServerS4A12 login bootstrap packets.  They are not yet
# proven as required by USDOF Stage3, but keeping generic dump listeners ready
# lets the unattended tracer catch/record any later DOF.exe attempts instead of
# losing them to "connection refused".
DEFAULT_EXTRA_TCP_PORTS = (10128, 17101, 17200)
DEFAULT_EXTRA_UDP_PORTS = (12311, 12312)

REGION_NAME = "cain"
CHANNEL_NAME = "Channel 1"

# Standard local single-player launch defaults (2026-07-13):
# multi-channel PVF list (cain-pvf-paired) + START_MAP enter-Seria path.
DEFAULT_SCRIPT_PROFILE = "cain-pvf-paired"
DEFAULT_CHANNELINFO_PROFILE = "cain-pvf-paired"
DEFAULT_CHANNELINFO_MAX_CHANNELS = 64
DEFAULT_CHANNELINFO_REGION_NAME = "cain"
DEFAULT_CHANNELINFO_FIRST_CHANNEL_ID = 10  # Start Area A
DEFAULT_CHANNELINFO_CUR_USERS = 1
DEFAULT_CHANNELINFO_MAX_USERS = 500
DEFAULT_GAME_CLIENT_TIMEOUT = 0  # 0 = no idle kick
DEFAULT_GAME_ENTER_SERIA_START_MAP = True

# PVF 资源归属纪律：
# - 运行时 Script.pvf：默认 ../USDOF/Script.pvf（与 server_proto 同级的客户端目录）
# - 解包树 data/pvf/unpack/ 仅开发期离线查找，运行时禁读
# - server_proto 内禁止再放整包 .pvf 归档

# 32 bytes; returned by Channel 00 0C and used by the prototype AES-CBC layer.
CHANNEL_SESSION_KEY = b"USDOF_LOCAL_STAGE3_CHANNEL_KEY!!"
CHANNEL_AES_IV = b"\x00" * 16

SCRIPT_RESPONSE_OPCODE = int(os.environ.get("USDOF_SCRIPT_RESPONSE_OPCODE", "0x09"), 0) & 0xFF
CHANNEL_RESPONSE_FLAG = int(os.environ.get("USDOF_CHANNEL_RESPONSE_FLAG", "0x7C"), 0) & 0xFF

CHANNEL_HEADER_SIZE = 0x0B
GAME_HEADER_SIZE = 0x10

MAX_CHANNEL_PAYLOAD = 0x20000
MAX_GAME_PACKET = 4 * 1024 * 1024

__all__ = [
    "BASE_DIR",
    "CAPTURE_DIR",
    "CHANNEL_AES_IV",
    "CHANNEL_HEADER_SIZE",
    "CHANNEL_NAME",
    "CHANNEL_PORT",
    "CHANNEL_RESPONSE_FLAG",
    "CHANNEL_SESSION_KEY",
    "CONTROL_HOST",
    "CONTROL_PORT",
    "DATA_DIR",
    "DEFAULT_CHANNELINFO_CUR_USERS",
    "DEFAULT_CHANNELINFO_FIRST_CHANNEL_ID",
    "DEFAULT_CHANNELINFO_MAX_CHANNELS",
    "DEFAULT_CHANNELINFO_MAX_USERS",
    "DEFAULT_CHANNELINFO_PROFILE",
    "DEFAULT_CHANNELINFO_REGION_NAME",
    "DEFAULT_EXTRA_TCP_PORTS",
    "DEFAULT_EXTRA_UDP_PORTS",
    "DEFAULT_GAME_CLIENT_TIMEOUT",
    "DEFAULT_GAME_ENTER_SERIA_START_MAP",
    "DEFAULT_SCRIPT_PROFILE",
    "DOC_DIR",
    "GAME_HEADER_SIZE",
    "GAME_PORT",
    "LOCAL_HOST",
    "LOG_DIR",
    "MAX_CHANNEL_PAYLOAD",
    "MAX_GAME_PACKET",
    "PVF_PATH",
    "REGION_NAME",
    "RESOURCE_DIR",
    "SCRIPT_RESPONSE_OPCODE",
    "ensure_dirs",
]
