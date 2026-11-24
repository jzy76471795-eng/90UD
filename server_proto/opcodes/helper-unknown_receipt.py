"""未知 C->S opcode 统一「收到」兜底回执。

设计目标（用户 2026-07-19）：
  - 业务 handler 未覆盖的 C->S，**可选兜底**回 S1/<same_opcode> body=``0x01``。
  - **默认关闭**：假 ACK 曾引入未知错误 / 打乱时序；仅显式排查时再开。
  - **禁止提前**：角色页 / 冷启动握手 / 控制面门闩阶段一律 silent，
    不得用假 ACK 抢答，否则会打乱既有时序（已回归：角色页回执后
    客户端不再发 SELECT_CHARACTER 0x0004）。

分层规则（全部满足才回执）：
  1. flag==1，且调用方已确认 **不在** ``ROUTED_OPCODES``（非业务 handler）
  2. 环境开关 ``USDOF_GAME_UNKNOWN_RECEIPT`` **默认关**；设 1 才开兜底
  3. **阶段门闩**：session 已绑定角色（``selected_character_id`` 非空）
     —— 未选角 = 角色页握手，禁止提前回执
  4. **控制面 / 静默观测** opcode 永不回执（见 ``SKIP_AUTO_RECEIPT_OPCODES``）

明确不做：
  - 不替代已有 opcode handler
  - 不拼业务体、不改 session / DB
  - 不对 PIV / check-connection / overflow gate / enter-game followup 回执
  - 不对角色页冷启动握手包回执（0x00AB/0x008C/0x01DD/0x01F8/0x0286 等）

环境开关：
  USDOF_GAME_UNKNOWN_RECEIPT=0  默认关；设 1 开启兜底回执
"""
from __future__ import annotations

import os
from typing import Any

from game.game_handlers import ENTER_GAME_FOLLOWUP_OPCODES


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


# ---------------------------------------------------------------------------
# 永不兜底：控制面 / 门闩 / 角色页握手（成功基线为 silent no-handler）
# ---------------------------------------------------------------------------
# 角色页冷启动握手（日志 13:17 成功 vs 13:45 失败对照）：
# 对这些 opcode 回 S1 body=01 会导致客户端停在角色页、点开始也不发 0x0004。
# 进房后若再次出现，同样保持 silent（客户端多为内部观测/回声，非业务请求）。
CHARSELECT_HANDSHAKE_SILENT_OPCODES: frozenset[int] = frozenset(
    {
        0x00AB,
        0x008C,
        0x01DD,
        0x01F8,
        0x0286,
    }
)

SKIP_AUTO_RECEIPT_OPCODES: frozenset[int] = frozenset(
    {
        0x0000,  # CHECK_CONNECTION → channelinfo 特殊路径
        0x00D9,  # OVERFLOW_INFO / ACK gate
        0x013E,  # PIV
        *CHARSELECT_HANDSHAKE_SILENT_OPCODES,
        *ENTER_GAME_FOLLOWUP_OPCODES,  # Exit/进房 followup：只观测，不抢答
    }
)


def session_has_selected_character(session: Any | None) -> bool:
    """是否已过「选角」阶段（禁止提前的阶段门闩）。"""
    if session is None:
        return False
    selected = getattr(session, "selected_character_id", None)
    return selected is not None


def should_auto_receipt(
    *,
    flag: int,
    opcode: int,
    session: Any | None = None,
) -> bool:
    """是否允许对未知 C->S 发兜底回执。

    仅兜底、禁止提前：未选角 / 控制面 / 握手静默集 → False。
    """
    if int(flag) != 1:
        return False
    if not _env_bool("USDOF_GAME_UNKNOWN_RECEIPT", False):
        return False
    op = int(opcode) & 0xFFFF
    if op in SKIP_AUTO_RECEIPT_OPCODES:
        return False
    # 禁止提前：角色页（尚未 SELECT_CHARACTER 绑定）一律 silent
    if not session_has_selected_character(session):
        return False
    return True


def receipt_deny_reason(
    *,
    flag: int,
    opcode: int,
    session: Any | None = None,
) -> str | None:
    """调试用：返回拒绝兜底的原因；允许时返回 None。"""
    if int(flag) != 1:
        return "flag!=1"
    if not _env_bool("USDOF_GAME_UNKNOWN_RECEIPT", False):
        return "env-off"
    op = int(opcode) & 0xFFFF
    if op in SKIP_AUTO_RECEIPT_OPCODES:
        if op in CHARSELECT_HANDSHAKE_SILENT_OPCODES:
            return "handshake-silent"
        if op in ENTER_GAME_FOLLOWUP_OPCODES:
            return "enter-followup-silent"
        return "control-plane-skip"
    if not session_has_selected_character(session):
        return "premature-no-selected-character"
    return None


def build_unknown_receipt_body() -> bytes:
    """兜底收到语义：单字节 success=1，无业务字段。"""
    return b"\x01"


def handle_unknown_receipt(
    server: Any,
    *,
    opcode: int,
    plain: bytes = b"",
) -> tuple[list[bytes], str]:
    """对未知 flag1 opcode 回 S1/same body=01（纯兜底，无业务）。"""
    op = int(opcode) & 0xFFFF
    body = build_unknown_receipt_body()
    packet = server._codec_response(1, op, body)
    note = (
        "C1/0x%04x unknown-receipt(fallback) plain_len=%s; "
        "S1/0x%04x body=01 (no business); wire=%s"
        % (op, len(plain or b""), op, len(packet))
    )
    return [packet], note
