"""新角色首次进游戏：职业剧情教程地下城（对照 90qiyuan / PVF Tutorial/*.dgn）。

## 产品语义（一次性，硬规则）

  - tutorial_state==0 且 level<=1 → **最多触发一次** 强制进教程本
  - 一旦触发（grant 包已发出）→ **立刻** 持久化 tutorial_state=2
  - tutorial_state!=0 → **永不** 再触发（含中断重进、重登、换槽）
  - 禁止 state=1 重入：旧「进行中可重进」会让全角色反复 001C+001D，叠在 START 城镇态上卡死

## Stage3 IDA 结论（2026-07-19，权威 .i64）

  1. S1/0004 START = ``sub_1603250``：**永远城镇向**
     - ``map_helper_sub_2BF6980_town_area`` + ``map_helper_loc_2BB38A0_gate_enter``
     - **没有** 90qiyuan / S4 那套 variable tutorial-index 列表
     - START body 内最接近 tutorial 的 u8（``sub_1AB4FC0`` / mgr+3896）是 skipable 布尔，不是进本开关

  2. ``onStartTutorialDungeon``（``sub_2AF9430`` / 字符串 0x3CB25B0）：
     - 是 **脚本回调**，不是 START 自动分支
     - 仅当 script slot 已装载（this+1457!=0）才查表调用；否则 case24 DEFAULT
     - 注释：Not auto-triggered by daily START

  3. Guide tick ``sub_106ED40``：
     - mode==13 → **城镇** 引导路径（不调 onStartTutorialDungeon）
     - mode==14 & state==1 & !UI(0x26A) → ``sub_1BDAAD0`` → onStartTutorialDungeon
     - 即：客户端必须 **先已在 dungeon mode(14)**，教程脚本才会跑

  4. **禁止** 在 START 城镇链（USERINFO/0016/0017…）之后硬塞 S0/001C+001D：
     - 城镇态 + 副本包 = 混合态 → 卡死赛丽亚 / 海量 UI 异常
     - 日常默认 **关闭** 强制 grant；真要做首进教程需另开「非城镇 START 首场景」方案，不可叠包

## PVF dungeon.lst（USDOF Script）

  10000 Tutorial/Tutorial.dgn           通用 Cali Forest（job 0–8 等）
  10001 Tutorial/Tutorial_timegate.dgn  外传 Time Gate 本1（job 9 黑暗武士）
  10002 Tutorial/Tutorial_timegate2.dgn 外传 Time Gate 本2（job 10 缔造者）
  10003 Tutorial/Tutorial_atslayer.dgn  job=11
  10004 Tutorial/Tutorial_knight.dgn    job=12
  10005 Tutorial/Tutorial_Demonic.dgn   job=13
  10006 Cataclysm/NewTutorial/Priest_F  job=14

Env：
  USDOF_GAME_NEWBIE_FIRST_ENTER=1     标准 profile 总开关（仅 state==0 可 arm）
  USDOF_GAME_TUTORIAL_AFTER_0023=1    标准 profile Phase-T1 开关（名历史遗留）
  USDOF_GAME_TUTORIAL_SKIP_SERIA_ROOM=1  跳过 0016/0017/0018 房间 paint
  USDOF_GAME_TUTORIAL_GRANT_AFTER_SETUP=1  START 角色包后直接 Phase-T1
  USDOF_GAME_TUTORIAL_DUNGEON_GRANT=0 旧 hybrid：与房间 paint 同批 001C+001D（禁止日常）
  USDOF_GAME_TUTORIAL_DUNGEON_ID=0    >0 时覆盖职业映射（调试）

安全路径（2026-07-19）：
  Phase-T0 arm（START，跳过房间 paint，预置回城赛丽亚）
  → Phase-T1（角色 setup delayed 末尾 000F+grant；0023 仅 fallback）
  → 离开教程本强制银色村庄赛丽亚房间
  禁止与 0016/0017/0018 同批叠 001C+001D。
"""
from __future__ import annotations

import os
import struct
from typing import Any

# job_id -> dungeon_id（character.lst 真源）
# 未列出的职业走 DEFAULT_TUTORIAL_DUNGEON_ID
JOB_TUTORIAL_DUNGEON: dict[int, int] = {
    9: 10001,   # DSSwordman 黑暗武士 → Tutorial_timegate（外传专属）
    10: 10002,  # CreatorMage 缔造者  → Tutorial_timegate2（外传专属）
    11: 10003,  # ATSwordman
    12: 10004,  # Knight
    13: 10005,  # DemonicLancer
    14: 10006,  # ATPriest
}

DEFAULT_TUTORIAL_DUNGEON_ID = 10000

# tutorial_state 约定（characters.tutorial_state）
# 0 = 从未触发；非 0 = 已触发过（永不重进）。历史 1=in_progress 也视为已触发。
TUTORIAL_STATE_PENDING = 0
TUTORIAL_STATE_IN_PROGRESS = 1  # 仅兼容旧库；should_start 对非 0 一律拒绝
TUTORIAL_STATE_COMPLETE = 2


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


def tutorial_dungeon_id_for_job(job: int) -> int:
    """职业 → 教程地下城 id。"""
    override = _env_int("USDOF_GAME_TUTORIAL_DUNGEON_ID", 0)
    if override > 0:
        return int(override) & 0xFFFFFFFF
    job_i = int(job) & 0xFF
    return int(JOB_TUTORIAL_DUNGEON.get(job_i, DEFAULT_TUTORIAL_DUNGEON_ID)) & 0xFFFFFFFF


def should_start_tutorial_dungeon(
    *,
    tutorial_state: int,
    level: int,
    env_first_enter: bool | None = None,
) -> bool:
    """是否在本次 SELECT/START **arm** 教程首进（Phase-T0）。

    硬规则：仅 NEWBIE 开 + state==0 + level<=1；**任何非 0 状态永不 arm**。

    真正进本由：
      - ``USDOF_GAME_TUTORIAL_AFTER_0023=1`` → Phase-T1（推荐）
      - 或 ``USDOF_GAME_TUTORIAL_DUNGEON_GRANT=1`` → 旧 hybrid（危险，日常关）
    二者都不开时仅 arm 无 grant（等同未启用进本）。
    """
    if env_first_enter is None:
        env_first_enter = _env_bool("USDOF_GAME_NEWBIE_FIRST_ENTER", False)
    if not env_first_enter:
        return False
    # 至少一种进本路径打开才 arm；避免空 arm 污染 session
    after_0023 = _env_bool("USDOF_GAME_TUTORIAL_AFTER_0023", False)
    hybrid = _env_bool("USDOF_GAME_TUTORIAL_DUNGEON_GRANT", False)
    if not after_0023 and not hybrid:
        return False
    state = int(tutorial_state) & 0xFF
    # 已触发 / 进行中 / 完成 → 永不重进
    if state != TUTORIAL_STATE_PENDING:
        return False
    # pending：仅 1 级新品（老号无标记但高等级不强制）
    if max(1, int(level or 1)) > 1:
        return False
    return True


def should_use_hybrid_grant() -> bool:
    """旧 hybrid：START delayed 硬塞 001C+001D。与 AFTER_0023 互斥（后者优先）。"""
    if _env_bool("USDOF_GAME_TUTORIAL_AFTER_0023", False):
        return False
    return _env_bool("USDOF_GAME_TUTORIAL_DUNGEON_GRANT", False)


def build_select_dungeon_0010_plain(dungeon_id: int, difficulty: int = 0) -> bytes:
    """合成 Stage3 17B SELECT_DUNGEON plain，供内部 grant 复用 handle_0010。"""
    body = bytearray(17)
    struct.pack_into("<I", body, 0, int(dungeon_id) & 0xFFFFFFFF)
    body[4] = int(difficulty) & 0xFF
    # pad / mode2 / sso / extra 与 live 空字段一致
    struct.pack_into("<H", body, 9, 0xFFFF)
    return bytes(body)


def mark_tutorial_in_progress(repo: Any, character_id: int) -> bool:
    """兼容旧名：现统一写 COMPLETE（触发即永久完成，禁止重入）。"""
    return mark_tutorial_triggered(repo, int(character_id))


def mark_tutorial_triggered(repo: Any, character_id: int) -> bool:
    """grant 成功后立刻落库 state=2，保证本角色永不第二次触发。"""
    try:
        return bool(repo.set_tutorial_state(int(character_id), TUTORIAL_STATE_COMPLETE))
    except Exception:
        return False


def mark_tutorial_complete(repo: Any, character_id: int) -> bool:
    try:
        return bool(repo.set_tutorial_state(int(character_id), TUTORIAL_STATE_COMPLETE))
    except Exception:
        return False


def is_tutorial_dungeon_id(dungeon_id: int) -> bool:
    did = int(dungeon_id) & 0xFFFFFFFF
    if did == DEFAULT_TUTORIAL_DUNGEON_ID:
        return True
    if did in JOB_TUTORIAL_DUNGEON.values():
        return True
    # 10000-10006 段 + 调试覆盖
    if 10000 <= did <= 10006:
        return True
    override = _env_int("USDOF_GAME_TUTORIAL_DUNGEON_ID", 0)
    return override > 0 and did == (override & 0xFFFFFFFF)


def bind_session_tutorial_flags(session: Any, *, dungeon_id: int, active: bool = True) -> None:
    if session is None:
        return
    try:
        session.is_tutorial_dungeon_run = bool(active)
        session.tutorial_dungeon_id = int(dungeon_id) & 0xFFFFFFFF if active else 0
    except Exception:
        pass
