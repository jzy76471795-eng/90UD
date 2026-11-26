"""USDOF 服务端会话：起标准服务端，在当前控制台显示进度并保存日志。

关控制台（X）/ Ctrl+C 会停：
  - start.py（Channel 7001 + Game 10052；兼容旧名 server_main.py）

默认入口不启动客户端或 GM，也不清理其它会话的进程。

日常入口：``启动游戏.cmd`` → 本脚本（``server_proto/start/launch_game_session.py``）。

**Python 硬锁 3.12.10 x64**（与 Win10/Win11 分享包一致）；
其它版本入口会 re-exec；子进程 ``start.py`` 也强制用同一解释器。

客户端注入 DLL（CREATE_SUSPENDED + LoadLibraryW，不改磁盘 EXE；顺序即注入顺序）：
  - 客户端 us_extend_dll/usdof_cn_fix.dll（第三方汉化，可选）— USDOF_CN_FIX=1
  - usdof_func_extend.dll     （PVF/toast/图片窗/原生视频）— USDOF_FUNC_EXTEND=1
  - usdof_codec_passthrough.dll（C2S 明文 / S2C 原生 codec）— USDOF_CODEC_PASSTHROUGH=1
  - usdof_debug_window.dll    （数据流 / PVF / NPK 实时调试窗口）— USDOF_DEBUG_WINDOW=1 时启用，默认关闭
  - usdof_desktop_focus_guard.dll（窄范围本地桌面命令屏蔽）— USDOF_DESKTOP_FOCUS_GUARD=1
  - forbidden-func.dll        （屏蔽邮箱入口）— USDOF_FORBIDDEN_FUNC=1
  - usdof_weapon_avatar_dungeon.dll（首次进副本武器装扮渲染提交）— USDOF_WEAPON_AVATAR_DUNGEON=1
  - usdof_native_loadout.dll  （副本快捷换装原生逐件移动）— USDOF_NATIVE_LOADOUT=1
  - debug/usdof_random_box_trace.dll（魔盒崩溃只读追踪）— USDOF_RANDOM_BOX_TRACE=1
  - debug/usdof_manual_item_move_trace.dll（手动移动单次观测，本地专用）— USDOF_MANUAL_ITEM_MOVE_TRACE=1
  - debug/usdof_skill_point_book_trace.dll（技能点书分类单次观测，本地专用）— USDOF_SKILL_POINT_BOOK_TRACE=1
  - 客户端 us_extend_dll/*.dll（自定义补丁，按文件名排序追加到内置 DLL 之后）

本文件属于服务端启动链路（server_proto/start/），不属于 pytools 小工具。
"""

from __future__ import annotations

import atexit
import contextlib
import ipaddress
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import TextIO

# server_proto/start/launch_game_session.py → parents[1]=server_proto, parents[2]=repo root
_START_DIR = Path(__file__).resolve().parent
SERVER_DIR = _START_DIR.parent
ROOT = SERVER_DIR.parent
PACK_DIR = ROOT / "server-pack-win10"
LOG_DIR = SERVER_DIR / "log"
CLIENT_DIR = Path(os.environ.get("USDOF_CLIENT_DIR", str(ROOT / "USDOF")))
NATIVE_LAUNCHER = _START_DIR / "usdof_session_launcher.exe"
CLIENT_EXE = Path(os.environ.get("USDOF_CLIENT_EXE", str(CLIENT_DIR / "DFO.exe")))
LOCAL_GM_CONSOLE = ROOT / "USDOF控制台.exe"
LAUNCH_ARG = (
    "9?127.0.0.1?7001?3012345?e3210f9193c9558c3638523332a864a2d93222fc94a85a9d966ab135c0125814"
    "?0?0?0?0?0?2?0?0?0?0?0?0?0?0?0?0"
)
STAGE3_LAUNCH_TAIL = "0?0?0?0?0?2?0?0?0?0?0?0?0?0?0?0"
PORT_READY_TIMEOUT_SECONDS = 3600.0
PORT_RELEASE_TIMEOUT_SECONDS = 5.0
SERVER_TCP_PORTS = (7001, 10052, 10128, 17101, 17200, 17600, 17602)


def client_launch_argument(source: dict[str, str] | None = None) -> str:
    """Build the frozen Stage3 argument from launcher-selected HTTP metadata."""

    values = os.environ if source is None else source
    raw_host = str(values.get("USDOF_CLIENT_CHANNEL_HOST", "127.0.0.1")).strip()
    try:
        host = str(ipaddress.IPv4Address(raw_host))
    except ValueError as exc:
        raise ValueError("USDOF_CLIENT_CHANNEL_HOST must be an IPv4 address") from exc
    try:
        port = int(str(values.get("USDOF_CLIENT_CHANNEL_PORT", "7001")), 10)
    except ValueError as exc:
        raise ValueError("USDOF_CLIENT_CHANNEL_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("USDOF_CLIENT_CHANNEL_PORT must be in 1..65535")

    opaque_account = str(
        values.get("USDOF_CLIENT_OPAQUE_ACCOUNT", "3012345")
    ).strip()
    opaque_token = str(
        values.get(
            "USDOF_CLIENT_OPAQUE_TOKEN",
            "e3210f9193c9558c3638523332a864a2d93222fc94a85a9d966ab135c0125814",
        )
    ).strip()
    for name, value in (
        ("USDOF_CLIENT_OPAQUE_ACCOUNT", opaque_account),
        ("USDOF_CLIENT_OPAQUE_TOKEN", opaque_token),
    ):
        if not value or "?" in value or any(char in value for char in "\r\n\x00"):
            raise ValueError("%s contains an invalid value" % name)
    return "9?%s?%s?%s?%s?%s" % (
        host,
        port,
        opaque_account,
        opaque_token,
        STAGE3_LAUNCH_TAIL,
    )

sys.path.insert(0, str(SERVER_DIR))
sys.path.insert(0, str(PACK_DIR))
from python_runtime import (  # noqa: E402
    REQUIRED_PY_VERSION_TEXT,
    describe_runtime,
    ensure_server_python,
    server_python_executable,
)

# 会话编排进程本身也必须是 3.12.10（与开服子进程一致）。
ensure_server_python(reexec=True)

from standard_local_flow import (  # noqa: E402
    apply_standard_local_flow_env,
    describe_standard_local_flow,
    standard_local_flow_env,
)
from startup_log import (  # noqa: E402
    format_init_log,
    service_ready_lines,
    startup_footer_lines,
)
from pvf.nkpi_reader import PvfDetectionResult, detect_pvf_profile  # noqa: E402
from pvf.town_structure_preprocessor import (  # noqa: E402
    format_town_preprocess_result,
    preprocess_town_structure,
)
from launcher.external_control import (  # noqa: E402
    ensure_external_control_config,
)
from launcher.external_dlls import resolve_managed_external_dll_paths  # noqa: E402
from dll_catalog import require_hardened_dll  # noqa: E402

_server_proc: subprocess.Popen | None = None
_client_proc: subprocess.Popen | None = None
_cleaned = False
_session_pvf_detection: PvfDetectionResult | None = None
_server_output_threads: list[threading.Thread] = []
_server_output_log_paths: tuple[Path, Path] | None = None
_server_console_lock = threading.Lock()
_session_control_port = 17600

# Required local runtime components.  The command/native launchers and the
# Python compatibility path all force these values to 1 before constructing
# server/client environments; they are not user-selectable startup switches.
_FIXED_RUNTIME_FEATURES = {
    "USDOF_CODEC_PASSTHROUGH": "1",
    "USDOF_FUNC_EXTEND": "1",
}

_PRESERVED_SESSION_ENV = (
    "USDOF_CN2018_SHOP",
    "USDOF_GAME_WEEKLY_ATTENDANCE_START",
    "USDOF_GAME_ADVENTURE_CN_START",
    "USDOF_REPO_ROOT",
    "USDOF_SERVER_HOME",
    "USDOF_CLIENT_DIR",
    "USDOF_CLIENT_EXE",
    "USDOF_PVF_PATH",
    "USDOF_SQLITE_DB",
    "USDOF_LOGIN_ADVERTISE_HOST",
    "USDOF_CHANNELINFO_GAME_IP_TEXT",
    "USDOF_GAME_SERVER_IP",
    "USDOF_CONTROL_HOST",
    "USDOF_CONTROL_PORT",
    "USDOF_PARTY_RELAY_CONFIG",
    "USDOF_PVF_BOOT_MODE",
    "USDOF_PVF_INDEX_CACHE",
    "USDOF_PVF_INDEX_CACHE_CLEAR",
    "USDOF_SQLITE_JOURNAL_MODE",
    "USDOF_SQLITE_SYNCHRONOUS",
    "USDOF_PROTOCOL_TRAFFIC_LOG",
    "USDOF_GAME_DIAGNOSTIC_TRACE",
    "USDOF_PACKET_SEQUENCE_LOG",
    "USDOF_CODEC_TRACE_LIMIT",
    "USDOF_GAME_HOTPATH_METRICS",
    "USDOF_GAME_TCP_NODELAY",
    "USDOF_GAME_MAP_TEMPLATE_CACHE",
    "USDOF_RANDOM_BOX_TRACE",
    "USDOF_MANUAL_ITEM_MOVE_TRACE",
    "USDOF_MANUAL_ITEM_MOVE_TRACE_LOG_PATH",
    "USDOF_SKILL_POINT_BOOK_TRACE",
    "USDOF_SKILL_POINT_BOOK_TRACE_LOG_PATH",
    # The native loadout DLL and the server must agree on this opt-in and its
    # two local sidechannel paths after standard_local_flow rebuilds USDOF_*.
    "USDOF_NATIVE_LOADOUT",
    "USDOF_NATIVE_LOADOUT_PLAN_PATH",
    "USDOF_NATIVE_LOADOUT_ACK_PATH",
    # Unified extension: one UI-thread owner for toast, item popup, and video.
    "USDOF_FUNC_EXTEND",
    "USDOF_FUNC_EXTEND_TOAST_REQUEST_PATH",
    "USDOF_FUNC_EXTEND_THREAD_PATH",
    "USDOF_FUNC_EXTEND_TOAST_LOG_PATH",
    "USDOF_FUNC_EXTEND_VIDEO",
    "USDOF_FUNC_EXTEND_VIDEO_HOOK",
    "USDOF_FUNC_EXTEND_VIDEO_SOURCE",
    "USDOF_FUNC_EXTEND_VIDEO_MATCH",
    "USDOF_FUNC_EXTEND_VIDEO_COMMAND_PATH",
    "USDOF_ITEM_OF_MONTH_ICON_CONFIG_PATH",
    "USDOF_ITEM_OF_MONTH_ICON_IMAGE_PATH",
    "USDOF_ITEM_OF_MONTH_CONFIG_PATH",
    "USDOF_ITEM_OF_MONTH_IMAGE_PATH",
    "USDOF_ITEM_OF_MONTH_POPUP_REQUEST_PATH",
    "USDOF_DEBUG_WINDOW",
    "USDOF_DEBUG_WINDOW_LOG_DIR",
    "USDOF_DESKTOP_FOCUS_GUARD",
    "USDOF_DESKTOP_FOCUS_GUARD_LOG_PATH",
    # Preserve the explicitly enabled, verified restoration features.
    "USDOF_RESTORE_QUEST_HYDRATION_ORDER",
    "USDOF_RESTORE_RAID_USERINFO0_ORDER",
    "USDOF_RESTORE_ANTON_WAITING_SPAWN",
    "USDOF_LOCAL_RAID_FEATURES",
    "USDOF_LOCAL_RAID_CHANNELS",
    "USDOF_RAID_LOCAL",
    "USDOF_RAID_LOBBY",
    "USDOF_RAID_BATTLE",
    "USDOF_RAID_START_CLOCK",
    "USDOF_GAME_STATE_INIT_0001_MODE",
    "USDOF_GAME_STATE_INIT_0001_STATE",
    "USDOF_GAME_CHANNELINFO_MODE",
    "USDOF_CHANNELINFO_MAX_CHANNELS",
    "USDOF_CHANNELINFO_FIRST_CHANNEL_ID",
    "USDOF_GAME_CHANNEL_INDEX",
    "USDOF_CHANNELINFO_UNIQUE_PORTS",
    "USDOF_CHANNELINFO_PORT_BASE",
    "USDOF_RESTORE_RAID_WAIT_NOTICE_04CE",
    "USDOF_RESTORE_RAID_EMPTY_LIST_02F2",
    "USDOF_EXPERIMENT_RAID_CREATE_ACK_0299",
    "USDOF_EXPERIMENT_RAID_CREATE_ACK_0299_VALUE",
    "USDOF_EXPERIMENT_RAID_CREATE_OBJECT_0250",
    "USDOF_EXPERIMENT_RAID_CREATOR_ROW_0250",
    "USDOF_EXPERIMENT_RAID_MEMBER_REFRESH_054E",
)
_NATIVE_LOADOUT_PLAN_FILENAME = "USDOFLoadoutNativePlan.bin"
_NATIVE_LOADOUT_ACK_FILENAME = "USDOFLoadoutNativeAck.bin"
_NATIVE_LOADOUT_THREAD_FILENAME = "USDOFNativeLoadoutThread.bin"
_FUNC_EXTEND_TOAST_REQUEST_FILENAME = "USDOFFuncExtendToastRequest.bin"
_FUNC_EXTEND_THREAD_FILENAME = "USDOFFuncExtendThread.bin"
_FUNC_EXTEND_VIDEO_COMMAND_FILENAME = "USDOFFuncExtendVideoCommand.bin"
_ITEM_OF_MONTH_ICON_CONFIG_FILENAME = "USDOFItemOfMonthIconConfig.bin"
_ITEM_OF_MONTH_ICON_IMAGE_FILENAME = "USDOFItemOfMonthIconImage.bin"
_ITEM_OF_MONTH_CONFIG_FILENAME = "USDOFItemOfMonthConfig.bin"
_ITEM_OF_MONTH_IMAGE_FILENAME = "USDOFItemOfMonthImage.bin"
_ITEM_OF_MONTH_POPUP_REQUEST_FILENAME = "USDOFItemOfMonthPopupRequest.bin"


def _apply_fixed_runtime_features(env: dict[str, str] | None = None) -> dict[str, str]:
    """Force the required local components on for the active launch path."""

    target = os.environ if env is None else env
    target.update(_FIXED_RUNTIME_FEATURES)
    return target


def _setdefault_native_loadout_sidechannel_paths(env: dict[str, str]) -> None:
    """Make the server and injected client use the same absolute sidechannel files."""

    env.setdefault(
        "USDOF_NATIVE_LOADOUT_PLAN_PATH",
        str(CLIENT_DIR / _NATIVE_LOADOUT_PLAN_FILENAME),
    )
    env.setdefault(
        "USDOF_NATIVE_LOADOUT_ACK_PATH",
        str(CLIENT_DIR / _NATIVE_LOADOUT_ACK_FILENAME),
    )


def _setdefault_native_loadout_thread_path(env: dict[str, str]) -> None:
    """Give the injector and native DLL one absolute UI-thread bootstrap path."""

    disabled = env.get("USDOF_NATIVE_LOADOUT", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
        "n",
        "",
    }
    if not disabled:
        env.setdefault(
            "USDOF_NATIVE_LOADOUT_THREAD_PATH",
            str(CLIENT_DIR / _NATIVE_LOADOUT_THREAD_FILENAME),
        )


def _setdefault_func_extend_sidechannel_paths(env: dict[str, str]) -> None:
    """Share one non-client runtime directory with the unified extension."""

    root = SERVER_DIR / "data" / "runtime_cache" / "func_extend"
    root.mkdir(parents=True, exist_ok=True)
    env.setdefault(
        "USDOF_FUNC_EXTEND_TOAST_REQUEST_PATH",
        str(root / _FUNC_EXTEND_TOAST_REQUEST_FILENAME),
    )
    env.setdefault(
        "USDOF_FUNC_EXTEND_THREAD_PATH",
        str(root / _FUNC_EXTEND_THREAD_FILENAME),
    )
    env.setdefault(
        "USDOF_FUNC_EXTEND_VIDEO_COMMAND_PATH",
        str(root / _FUNC_EXTEND_VIDEO_COMMAND_FILENAME),
    )


def _setdefault_item_of_month_paths(env: dict[str, str]) -> None:
    """Separate the fixed icon artwork from mutable popup staging files."""

    asset_root = SERVER_DIR / "data" / "resources"
    env.setdefault(
        "USDOF_ITEM_OF_MONTH_ICON_CONFIG_PATH",
        str(asset_root / _ITEM_OF_MONTH_ICON_CONFIG_FILENAME),
    )
    env.setdefault(
        "USDOF_ITEM_OF_MONTH_ICON_IMAGE_PATH",
        str(asset_root / _ITEM_OF_MONTH_ICON_IMAGE_FILENAME),
    )
    root = SERVER_DIR / "data" / "runtime_cache" / "item_of_month"
    env.setdefault(
        "USDOF_ITEM_OF_MONTH_CONFIG_PATH",
        str(root / _ITEM_OF_MONTH_CONFIG_FILENAME),
    )
    env.setdefault(
        "USDOF_ITEM_OF_MONTH_IMAGE_PATH",
        str(root / _ITEM_OF_MONTH_IMAGE_FILENAME),
    )
    env.setdefault(
        "USDOF_ITEM_OF_MONTH_POPUP_REQUEST_PATH",
        str(root / _ITEM_OF_MONTH_POPUP_REQUEST_FILENAME),
    )


def _setdefault_debug_window_log_dir(env: dict[str, str]) -> None:
    """Give the read-only client diagnostic window the active server log tree."""

    env.setdefault("USDOF_DEBUG_WINDOW_LOG_DIR", str(LOG_DIR))


def _server_tcp_ports() -> tuple[int, ...]:
    return tuple(sorted(set(SERVER_TCP_PORTS + (int(_session_control_port),))))


def _apply_internal_control_env(env: dict[str, str]) -> int:
    """Configure the server-owned externally reachable control listener."""

    global _session_control_port
    raw_port = env.get("USDOF_CONTROL_PORT")
    if raw_port is None:
        raw_port = str(ensure_external_control_config(ROOT).internal_port)
    _session_control_port = int(raw_port)
    if not 1 <= _session_control_port <= 65535:
        raise ValueError("USDOF_CONTROL_PORT must be in 1..65535")
    for name in (
        "USDOF_GM_API_ENABLED",
        "USDOF_GM_API_HOST",
        "USDOF_GM_API_PORT",
        "USDOF_GM_API_TOKEN",
    ):
        env.pop(name, None)
    return _session_control_port


def _psutil():
    try:
        import psutil  # type: ignore

        return psutil
    except Exception:
        return None


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        try:
            return s.connect_ex(("127.0.0.1", port)) == 0
        except OSError:
            return False


def _local_gm_console_running() -> bool:
    psutil = _psutil()
    if psutil is None:
        return False
    expected = os.path.normcase(os.path.abspath(str(LOCAL_GM_CONSOLE)))
    try:
        for process in psutil.process_iter(["exe"]):
            try:
                executable = str(process.info.get("exe") or "")
            except Exception:
                continue
            if (
                executable
                and os.path.normcase(os.path.abspath(executable)) == expected
            ):
                return True
    except Exception:
        pass
    return False


def start_local_gm_console(timeout: float = 5.0) -> subprocess.Popen | None:
    """Open the packaged local GM after the loopback control owner is ready."""
    external = ensure_external_control_config(ROOT)
    process = None
    if _local_gm_console_running():
        print("[session] local GM console already running", flush=True)
    elif not LOCAL_GM_CONSOLE.is_file():
        print(
            "[session][warn] local GM console missing; run 打包GM.cmd: %s"
            % LOCAL_GM_CONSOLE,
            flush=True,
        )
        return None
    else:
        try:
            process = subprocess.Popen(
                [str(LOCAL_GM_CONSOLE)],
                cwd=str(ROOT),
                close_fds=True,
            )
        except OSError as exc:
            print(
                "[session][warn] local GM console start failed: %s: %s"
                % (type(exc).__name__, exc),
                flush=True,
            )
            return None
        print(
            "[session] local GM console started pid=%s" % int(process.pid or 0),
            flush=True,
        )

    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() < deadline:
        if _port_open(int(external.port)):
            print(
                "[session] online GM ready: 127.0.0.1:%s" % int(external.port),
                flush=True,
            )
            return process
        if process is not None and process.poll() is not None:
            break
        time.sleep(0.1)
    print(
        "[session][warn] online GM port %s is not ready; check the GM window log"
        % int(external.port),
        flush=True,
    )
    return process


def wait_listen(port: int, timeout: float = PORT_READY_TIMEOUT_SECONDS) -> bool:
    """Wait until TCP port accepts connections.

    PVF preload + boot warmup can be expensive on cold start; allow up to one hour.
    """
    end = time.time() + timeout
    while time.time() < end:
        if _port_open(port):
            return True
        time.sleep(0.2)
    return False


def _wait_ports_python(
    ports: list[int],
    timeout: float = PORT_READY_TIMEOUT_SECONDS,
    server_proc: subprocess.Popen | None = None,
) -> dict[int, bool]:
    """Compatibility implementation for hosts without the native batch waiter."""
    end = time.time() + timeout
    ready = {int(p): False for p in ports}
    started = time.time()
    next_log = started + 5.0
    while time.time() < end:
        if server_proc is not None:
            returncode = server_proc.poll()
            if returncode is not None:
                print(
                    "[session] server exited before ports were ready "
                    f"(rc={returncode})",
                    flush=True,
                )
                return ready
        for p in list(ready):
            if not ready[p] and _port_open(p):
                ready[p] = True
                print(
                    f"[session] port {p} ready (+{time.time() - started:.1f}s)",
                    flush=True,
                )
        if all(ready.values()):
            return ready
        now = time.time()
        if now >= next_log:
            pending = [p for p, ok in ready.items() if not ok]
            print(
                f"[session] waiting ports {pending} "
                f"(+{now - started:.0f}s / {timeout:.0f}s; PVF boot may take ~40s)",
                flush=True,
            )
            next_log = now + 5.0
        time.sleep(0.2)
    return ready


def wait_ports(
    ports: list[int],
    timeout: float = PORT_READY_TIMEOUT_SECONDS,
    server_proc: subprocess.Popen | None = None,
) -> dict[int, bool]:
    """Wait for all ports under one shared deadline, preferring the C++ batch loop."""
    unique_ports = list(dict.fromkeys(int(port) for port in ports))
    if not unique_ports:
        return {}
    server_pid = int(getattr(server_proc, "pid", 0) or 0)
    native_enabled = os.environ.get("USDOF_NATIVE_PORT_WAIT", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
        "n",
        "",
    }
    if os.name != "nt" or not native_enabled or not NATIVE_LAUNCHER.is_file() or server_pid <= 0:
        return _wait_ports_python(unique_ports, timeout=timeout, server_proc=server_proc)

    timeout_seconds = max(0.0, float(timeout))
    timeout_ms = int(timeout_seconds * 1000.0)
    command = [
        str(NATIVE_LAUNCHER),
        "--wait-ports",
        "--timeout-ms",
        str(timeout_ms),
        "--server-pid",
        str(server_pid),
    ]
    for port in unique_ports:
        command.extend(("--port", str(port)))

    print(
        "[session] port wait branch=cpp ports=%s timeout=%.0fs server_pid=%s"
        % (unique_ports, timeout_seconds, server_pid),
        flush=True,
    )
    started = time.monotonic()
    try:
        returncode = subprocess.call(command, cwd=str(ROOT))
    except OSError as exc:
        returncode = -1
        print(
            "[session] native port wait unavailable; Python compatibility branch "
            "will continue (%s: %s)" % (type(exc).__name__, exc),
            flush=True,
        )

    if returncode in {0, 10, 11}:
        if returncode == 0:
            return {port: True for port in unique_ports}
        return {port: _port_open(port) for port in unique_ports}

    elapsed = time.monotonic() - started
    remaining = max(0.0, timeout_seconds - elapsed)
    print(
        "[session] native port wait rc=%s; Python compatibility branch "
        "continues with %.1fs remaining" % (returncode, remaining),
        flush=True,
    )
    return _wait_ports_python(unique_ports, timeout=remaining, server_proc=server_proc)


def _pids_listening_on_port(port: int) -> list[int]:
    """Return LISTENING process IDs for one TCP port without a full psutil scan."""
    if os.name != "nt":
        return []
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    needle = ":%d" % int(port)
    pids: set[int] = set()
    for line in (result.stdout or "").splitlines():
        if "LISTEN" not in line.upper() or needle not in line:
            continue
        parts = line.split()
        if not any(part.endswith(needle) for part in parts):
            continue
        try:
            pid = int(parts[-1])
        except (IndexError, ValueError):
            continue
        if pid > 0:
            pids.add(pid)
    return sorted(pids)


def _terminate_process_tree(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=6,
            )
            return result.returncode == 0
        except Exception:
            return False
    try:
        os.kill(pid, 9)
        return True
    except OSError:
        return False


def kill_usdof_servers() -> list[int]:
    psutil = _psutil()
    killed: list[int] = []
    if psutil is not None:
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmd = " ".join(p.info.get("cmdline") or [])
            except Exception:
                continue
            if "start.py" in cmd or "server_main.py" in cmd:
                try:
                    p.kill()
                    killed.append(int(p.info["pid"]))
                except Exception:
                    pass
        for c in psutil.net_connections(kind="inet"):
            if (
                c.status == "LISTEN"
                and c.laddr
                and c.laddr.port in _server_tcp_ports()
                and c.pid
            ):
                try:
                    psutil.Process(c.pid).kill()
                    killed.append(int(c.pid))
                except Exception:
                    pass
    for port in _server_tcp_ports():
        for pid in _pids_listening_on_port(port):
            try:
                if _terminate_process_tree(pid):
                    killed.append(pid)
            except Exception:
                pass
    return sorted(set(killed))


def release_server_ports() -> list[int]:
    """Close listeners from a prior session before launching this server."""
    occupied = [port for port in _server_tcp_ports() if _port_open(port)]
    if not occupied:
        return []
    print(f"[session] releasing occupied server ports {occupied}", flush=True)
    killed = kill_usdof_servers()
    deadline = time.time() + PORT_RELEASE_TIMEOUT_SECONDS
    remaining = occupied
    while time.time() < deadline:
        remaining = [port for port in occupied if _port_open(port)]
        if not remaining:
            print("[session] server ports released", flush=True)
            return killed
        time.sleep(0.1)
    raise RuntimeError("server ports still occupied after cleanup: %s" % remaining)


def kill_usdof_clients() -> list[int]:
    psutil = _psutil()
    if psutil is None:
        return []
    killed: list[int] = []
    for p in psutil.process_iter(["pid", "name", "exe"]):
        name = (p.info.get("name") or "").lower()
        exe = (p.info.get("exe") or "").lower().replace("\\", "/")
        if (
            "dfo_stage3" in name
            or name.startswith("dfo_stage")
            or name == "dfo.exe"
            or "dfo_stage3" in exe
            or exe.endswith("/dfo.exe")
        ):
            try:
                p.kill()
                killed.append(int(p.info["pid"]))
            except Exception:
                pass
    return sorted(set(killed))


def cleanup(reason: str = "exit") -> None:
    global _cleaned, _server_proc
    if _cleaned:
        return
    _cleaned = True
    print(f"\n[session] cleanup ({reason}) ...", flush=True)
    if _server_proc is not None and _server_proc.poll() is None:
        _terminate_process_tree(int(_server_proc.pid))
        try:
            _server_proc.wait(timeout=2.0)
        except Exception:
            pass
    _join_server_output_threads(timeout=2.0)
    print("[session] owned server stopped", flush=True)


def _install_console_handlers() -> None:
    atexit.register(lambda: cleanup("atexit"))

    def _sig_handler(signum, frame):  # type: ignore[no-untyped-def]
        cleanup(f"signal:{signum}")
        raise SystemExit(0)

    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _sig_handler)
            except Exception:
                pass

    # Windows console close (X button) -> CTRL_CLOSE_EVENT
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            HandlerRoutine = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
            CTRL_C_EVENT = 0
            CTRL_BREAK_EVENT = 1
            CTRL_CLOSE_EVENT = 2
            CTRL_LOGOFF_EVENT = 5
            CTRL_SHUTDOWN_EVENT = 6

            @HandlerRoutine
            def _console_handler(ctrl_type):  # type: ignore[no-untyped-def]
                if ctrl_type in {
                    CTRL_C_EVENT,
                    CTRL_BREAK_EVENT,
                    CTRL_CLOSE_EVENT,
                    CTRL_LOGOFF_EVENT,
                    CTRL_SHUTDOWN_EVENT,
                }:
                    cleanup(f"console:{ctrl_type}")
                    # TRUE = we handled it (esp. CTRL_CLOSE_EVENT). Returning
                    # FALSE can race and kill this process before children die.
                    if ctrl_type in {CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT}:
                        try:
                            os._exit(0)
                        except Exception:
                            return True
                    return True
                return False

            # Keep reference so GC does not free the callback.
            _install_console_handlers._handler = _console_handler  # type: ignore[attr-defined]
            ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_handler, True)
        except Exception as exc:
            print(f"[session] warn: console handler not installed: {exc!r}", flush=True)


def prepare_client_cfg() -> None:
    """Do not rewrite client settings."""
    # Keep LocalLow/DNF/DFO_UNIV.cfg under client control.
    # prepare_usdof_client_cfg.py is only for manual emergency use.
    print(
        "[session] skip cfg rewrite; leave DFO_UNIV.cfg to the client",
        flush=True,
    )


def _current_pvf_path() -> Path:
    return Path(os.environ.get("USDOF_PVF_PATH", str(CLIENT_DIR / "Script.pvf")))


def _detect_current_pvf(context: str) -> PvfDetectionResult:
    pvf_path = _current_pvf_path()
    try:
        detected = detect_pvf_profile(pvf_path)
    except Exception as exc:
        raise RuntimeError(
            "%s PVF detection failed: path=%s detail=%s: %s"
            % (context, pvf_path, type(exc).__name__, exc)
        ) from exc
    print(
        "[session] PVF detected context=%s profile=%s guarded=%d "
        "bridge_required=%d size=%d sha256=%s path=%s"
        % (
            context,
            detected.profile_name,
            int(detected.guarded),
            int(detected.bridge_required),
            detected.file_size,
            detected.source_sha256,
            pvf_path,
        ),
        flush=True,
    )
    return detected


def _preprocess_current_pvf(context: str):
    pvf_path = _current_pvf_path()
    try:
        result = preprocess_town_structure(pvf_path)
    except Exception as exc:
        raise RuntimeError(
            "%s PVF town preprocessing failed: path=%s detail=%s: %s"
            % (context, pvf_path, type(exc).__name__, exc)
        ) from exc
    print(format_town_preprocess_result(result, context), flush=True)
    return result


def _require_preprocessed_pvf(preprocessed, detected: PvfDetectionResult) -> None:
    if preprocessed.source_sha256.lower() != detected.source_sha256.lower():
        raise RuntimeError(
            "Script.pvf changed between town preprocessing and profile detection: "
            "path=%s preprocessed_sha256=%s detected_sha256=%s"
            % (
                _current_pvf_path(),
                preprocessed.source_sha256,
                detected.source_sha256,
            )
        )


def _require_same_pvf(
    expected: PvfDetectionResult,
    actual: PvfDetectionResult,
    path: Path,
) -> None:
    if (
        expected.profile_name != actual.profile_name
        or expected.source_sha256.lower() != actual.source_sha256.lower()
        or expected.file_size != actual.file_size
    ):
        raise RuntimeError(
            "Script.pvf does not match this server session: path=%s "
            "expected_profile=%s actual_profile=%s expected_sha256=%s actual_sha256=%s"
            % (
                path,
                expected.profile_name,
                actual.profile_name,
                expected.source_sha256,
                actual.source_sha256,
            )
        )


def _require_remote_pvf_identity(detected: PvfDetectionResult) -> None:
    expected_profile = os.environ.get("USDOF_REMOTE_PVF_PROFILE", "").strip()
    expected_sha256 = os.environ.get("USDOF_REMOTE_PVF_SHA256", "").strip().lower()
    if expected_profile and detected.profile_name != expected_profile:
        raise RuntimeError(
            "Script.pvf profile does not match the remote server: "
            "expected=%s actual=%s"
            % (expected_profile, detected.profile_name)
        )
    if expected_sha256 and detected.source_sha256.lower() != expected_sha256:
        raise RuntimeError(
            "Script.pvf SHA-256 does not match the remote server: "
            "expected=%s actual=%s"
            % (expected_sha256, detected.source_sha256.lower())
        )


def _apply_pvf_detection_env(
    env: dict[str, str], detected: PvfDetectionResult
) -> None:
    env["USDOF_PVF_PATH"] = str(_current_pvf_path())
    env["USDOF_EXPECTED_PVF_PROFILE"] = detected.profile_name
    env["USDOF_EXPECTED_PVF_SHA256"] = detected.source_sha256
    env["USDOF_TOWN_STRUCTURE_PREPROCESSED_SHA256"] = detected.source_sha256
    env["USDOF_PVF_BRIDGE"] = "1" if detected.bridge_required else "0"


def _server_launch_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(base if base is not None else os.environ)
    preserved = {
        key: env[key]
        for key in _PRESERVED_SESSION_ENV
        if key in env
    }
    for key in list(env):
        if key.startswith("USDOF_"):
            env.pop(key, None)
    apply_standard_local_flow_env(env, force=True)
    env.update(preserved)
    _apply_fixed_runtime_features(env)
    # Keep the server-side plan publisher aligned with the default-injected
    # client bridge.  A caller can still preserve an explicit 0 for an A/B run.
    env.setdefault("USDOF_NATIVE_LOADOUT", "1")
    _setdefault_native_loadout_sidechannel_paths(env)
    env.setdefault("USDOF_FUNC_EXTEND_VIDEO", "1")
    env.setdefault("USDOF_FUNC_EXTEND_VIDEO_HOOK", "1")
    _setdefault_func_extend_sidechannel_paths(env)
    _setdefault_item_of_month_paths(env)
    if "USDOF_CODEC_TRACE_LIMIT" not in preserved:
        diagnostic = env.get("USDOF_GAME_DIAGNOSTIC_TRACE", "0").strip().lower()
        env["USDOF_CODEC_TRACE_LIMIT"] = (
            "512"
            if diagnostic not in {"0", "false", "no", "off", "n", ""}
            else "0"
        )
    return env


def _tee_server_stream(
    source: TextIO,
    log_file: TextIO,
    console: TextIO,
) -> None:
    """Drain one server pipe to its UTF-8 log and the owning console."""
    try:
        while True:
            line = source.readline()
            if line == "":
                break
            try:
                log_file.write(line)
                log_file.flush()
            except Exception:
                # Keep draining the child even if its diagnostic log becomes unwritable.
                pass
            try:
                with _server_console_lock:
                    console.write(line)
                    console.flush()
            except Exception:
                # The log remains authoritative when the console is closing.
                pass
    finally:
        with contextlib.suppress(Exception):
            source.close()
        with contextlib.suppress(Exception):
            log_file.close()


def _start_server_output_threads(
    process: subprocess.Popen,
    out_file: TextIO,
    err_file: TextIO,
) -> None:
    global _server_output_threads
    streams = (
        ("stdout", process.stdout, out_file, sys.stdout),
        ("stderr", process.stderr, err_file, sys.stderr),
    )
    threads: list[threading.Thread] = []
    for name, source, log_file, console in streams:
        if source is None:
            log_file.close()
            raise RuntimeError("server %s pipe was not created" % name)
        thread = threading.Thread(
            target=_tee_server_stream,
            args=(source, log_file, console),
            name="usdof-server-%s" % name,
            daemon=True,
        )
        thread.start()
        threads.append(thread)
    _server_output_threads = threads


def _join_server_output_threads(timeout: float = 2.0) -> None:
    deadline = time.monotonic() + max(0.0, float(timeout))
    for thread in tuple(_server_output_threads):
        remaining = max(0.0, deadline - time.monotonic())
        thread.join(remaining)


def _native_server_process_enabled() -> bool:
    return os.name == "nt" and NATIVE_LAUNCHER.is_file() and os.environ.get(
        "USDOF_NATIVE_SERVER_PROCESS", "1"
    ).strip().lower() not in {"0", "false", "no", "off", "n", ""}


def _native_server_process_command(command: list[str]) -> list[str]:
    if not _native_server_process_enabled():
        return command
    native_command = [
        str(NATIVE_LAUNCHER),
        "--server-process",
        "--application",
        command[0],
        "--workdir",
        str(SERVER_DIR),
    ]
    for argument in command:
        native_command.extend(("--arg", argument))
    return native_command


def start_server() -> subprocess.Popen:
    global _server_proc, _server_output_log_paths, _session_pvf_detection
    _apply_fixed_runtime_features()
    env = _server_launch_env()
    control_port = _apply_internal_control_env(env)
    occupied = [port for port in _server_tcp_ports() if _port_open(port)]
    if occupied:
        raise RuntimeError("server ports already occupied; stop the running server first: %s" % occupied)
    preprocessed = _preprocess_current_pvf("server-start")
    detected = _detect_current_pvf("server-start")
    _require_preprocessed_pvf(preprocessed, detected)
    _session_pvf_detection = detected
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_log = LOG_DIR / f"launch_game_session_{stamp}.out.log"
    err_log = LOG_DIR / f"launch_game_session_{stamp}.err.log"
    launch_log = LOG_DIR / f"launch_game_session_{stamp}.launch.log"

    print(
        "[session] control API bind: %s:%s"
        % (env.get("USDOF_CONTROL_HOST", "127.0.0.1"), control_port),
        flush=True,
    )
    _apply_pvf_detection_env(env, detected)
    codec_passthrough = _codec_passthrough_enabled()
    # The client encode hook and the server C2S parser are one atomic mode.
    # S2C always keeps the Stage3 native codec/decode contract.
    env["USDOF_CODEC_PASSTHROUGH"] = "1" if codec_passthrough else "0"
    env["USDOF_GAME_PLAIN_WIRE"] = "0"
    env["USDOF_GAME_C2S_PLAIN_WIRE"] = "1" if codec_passthrough else "0"
    env["USDOF_GAME_S2C_PLAIN_WIRE"] = "0"
    env["USDOF_GAME_PLAIN_WIRE_HEADER"] = "16"
    std = standard_local_flow_env()

    # 硬锁 3.12.10：服务端子进程与分享包 bytecode magic 保持一致。
    py_exe = str(server_python_executable())
    env["USDOF_PYTHON_EXE"] = py_exe
    cmd = [
        py_exe,
        "-u",
        "start.py",
        "--host",
        "0.0.0.0",
        "--advertise-host",
        env.get("USDOF_LOGIN_ADVERTISE_HOST", "127.0.0.1"),
        "--channel-port",
        "7001",
        "--game-port",
        "10052",
        "--login-host",
        "0.0.0.0",
        "--login-port",
        "17602",
        "--control-port",
        str(control_port),
        "--control-host",
        env.get("USDOF_CONTROL_HOST", "127.0.0.1"),
        "--script-profile",
        std["USDOF_SCRIPT_PROFILE"],
        "--channelinfo-profile",
        std["USDOF_CHANNELINFO_PROFILE"],
        "--channelinfo-envelope",
        "none",
        "--game-probes",
        "0x0000",
        "--game-flow-profile",
        "enter-game-p1",
        "--game-roster-profile",
        std.get("USDOF_USERINFO_ROSTER_PROFILE", "us-trace-dstr"),
        "--extra-tcp-ports",
        "10128,17101,17200",
        "--extra-udp-ports",
        "12311,12312",
    ]
    lines = [
        f"cmd={' '.join(cmd)}",
        f"cwd={SERVER_DIR}",
        f"python_required={REQUIRED_PY_VERSION_TEXT}",
        f"python_runtime={describe_runtime()}",
        f"standard={describe_standard_local_flow()}",
    ]
    for k in sorted(env):
        if k.startswith("USDOF_"):
            value = "<redacted>" if k == "USDOF_GM_API_TOKEN" else env[k]
            lines.append(f"{k}={value}")
    server_command = _native_server_process_command(cmd)
    if server_command != cmd:
        lines.append("native_server_process=cpp")
    launch_log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    out_f = open(out_log, "w", encoding="utf-8")
    err_f = open(err_log, "w", encoding="utf-8")
    # Keep as child of this console session (no DETACHED / NEW_CONSOLE).
    try:
        _server_proc = subprocess.Popen(
            server_command,
            cwd=str(SERVER_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except Exception:
        out_f.close()
        err_f.close()
        raise
    _server_output_log_paths = (out_log, err_log)
    _start_server_output_threads(_server_proc, out_f, err_f)
    print(
        "[session] server process branch=%s"
        % ("cpp" if server_command != cmd else "python"),
        flush=True,
    )
    print(f"[session] server pid={_server_proc.pid}", flush=True)
    print(f"[session] out={out_log}", flush=True)
    print(f"[session] err={err_log}", flush=True)
    print(f"[session] launch_log={launch_log}", flush=True)
    return _server_proc


def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _cn_fix_enabled() -> bool:
    """Optional third-party Chinese compatibility DLL gate."""
    return _env_flag("USDOF_CN_FIX", "0")


def _cn_fix_dll() -> Path:
    configured = (os.environ.get("USDOF_CN_FIX_DLL") or "").strip().strip('"')
    if configured:
        return Path(configured).expanduser().resolve()
    return CLIENT_DIR / "us_extend_dll" / "usdof_cn_fix.dll"


def _codec_passthrough_enabled() -> bool:
    """Stage3 Game codec encode/decode plaintext owner; default on."""
    return _env_flag("USDOF_CODEC_PASSTHROUGH", "1")


def _codec_passthrough_dll() -> Path:
    return (
        SERVER_DIR
        / "patches"
        / "usdof_codec_passthrough"
        / "build"
        / "Release-Win32"
        / "usdof_codec_passthrough.dll"
    )


def _debug_window_enabled() -> bool:
    """Standalone data-flow / PVF / NPK window; explicit local opt-in only."""
    return _env_flag("USDOF_DEBUG_WINDOW", "0")


def _debug_window_dll() -> Path:
    return (
        SERVER_DIR
        / "patches"
        / "usdof_debug_window"
        / "build"
        / "Release-Win32"
        / "usdof_debug_window.dll"
    )


def _desktop_focus_guard_enabled() -> bool:
    """Narrow Stage3 WM_COMMAND/0x19F source-caller guard; default on."""
    return _env_flag("USDOF_DESKTOP_FOCUS_GUARD", "1")


def _desktop_focus_guard_dll() -> Path:
    return (
        SERVER_DIR
        / "patches"
        / "usdof_desktop_focus_guard"
        / "build"
        / "Release-Win32"
        / "usdof_desktop_focus_guard.dll"
    )


def _forbidden_func_enabled() -> bool:
    """邮箱入口屏蔽 DLL；沿用已默认开启的独立开关。"""
    return _env_flag("USDOF_FORBIDDEN_FUNC", "1")


def _forbidden_func_dll() -> Path:
    """只使用用户指定主名 ``forbidden-func.dll``。"""
    return (
        SERVER_DIR
        / "patches"
        / "forbidden-func"
        / "build"
        / "Release-Win32"
        / "forbidden-func.dll"
    )


def _weapon_avatar_dungeon_enabled() -> bool:
    """首次进副本武器装扮原生渲染提交；默认注入。"""
    return _env_flag("USDOF_WEAPON_AVATAR_DUNGEON", "1")


def _weapon_avatar_dungeon_dll() -> Path:
    """只使用固定 001E reader hook 的唯一主名 DLL。"""
    return (
        SERVER_DIR
        / "patches"
        / "usdof_weapon_avatar_dungeon"
        / "build"
        / "Release-Win32"
        / "usdof_weapon_avatar_dungeon.dll"
    )


def _native_loadout_enabled() -> bool:
    """副本一键换装原生移动桥；默认注入，显式 0 可停用。"""
    return _env_flag("USDOF_NATIVE_LOADOUT", "1")


def _native_loadout_dll() -> Path:
    """只使用副本原生换装桥的唯一主名 DLL。"""
    return (
        SERVER_DIR
        / "patches"
        / "usdof_native_loadout"
        / "build"
        / "Release-Win32"
        / "usdof_native_loadout.dll"
    )


def _func_extend_enabled() -> bool:
    """Unified PVF/toast/item-popup/video extension owner; fixed on."""
    return _env_flag("USDOF_FUNC_EXTEND", "1")


def _func_extend_dll() -> Path:
    return (
        SERVER_DIR
        / "patches"
        / "usdof_func_extend"
        / "build"
        / "Release-Win32"
        / "usdof_func_extend.dll"
    )


def _client_launch_env(
    base: dict[str, str] | None = None,
    detected: PvfDetectionResult | None = None,
) -> dict[str, str]:
    """Environment for Stage3 so the window follows Windows display scaling.

    Stage3 has no dpiAware manifest but **self-upgrades to PER_MONITOR** during
    init, which disables classic DPI virtualization (logical 1600x900 stays
    physical pixels on a 200% desktop).

    ``__COMPAT_LAYER=DPIUNAWARE`` is the AppCompat switch that keeps the process
    DPI Unaware so Windows bitmap-scales the window (e.g. 1600x900 -> 3200x1800
    at 200%). Same env is set in client-only start cmd so both entry points match.

    Also set the creating thread to UNAWARE before CreateProcess (defense in
    depth when parent is python.exe with AppCompat HIGHDPIAWARE).
    """
    env = dict(base if base is not None else os.environ)
    _apply_fixed_runtime_features(env)
    _setdefault_native_loadout_sidechannel_paths(env)
    _setdefault_native_loadout_thread_path(env)
    env.setdefault("USDOF_FUNC_EXTEND_VIDEO", "1")
    env.setdefault("USDOF_FUNC_EXTEND_VIDEO_HOOK", "1")
    _setdefault_func_extend_sidechannel_paths(env)
    _setdefault_item_of_month_paths(env)
    _setdefault_debug_window_log_dir(env)
    if "USDOF_CODEC_TRACE_LIMIT" not in env:
        diagnostic = env.get("USDOF_GAME_DIAGNOSTIC_TRACE", "0").strip().lower()
        env["USDOF_CODEC_TRACE_LIMIT"] = (
            "512"
            if diagnostic not in {"0", "false", "no", "off", "n", ""}
            else "0"
        )
    env["__COMPAT_LAYER"] = "DPIUNAWARE"
    if detected is not None:
        _apply_pvf_detection_env(env, detected)
    return env


@contextlib.contextmanager
def _windows_createprocess_dpi_unaware():
    """Best-effort: creating thread DPI UNAWARE for CreateProcess inheritance."""
    if os.name != "nt":
        yield
        return

    prev = None
    set_fn = None
    try:
        import ctypes

        user32 = ctypes.windll.user32
        set_fn = user32.SetThreadDpiAwarenessContext
        set_fn.argtypes = [ctypes.c_void_p]
        set_fn.restype = ctypes.c_void_p
        # DPI_AWARENESS_CONTEXT_UNAWARE == (DPI_AWARENESS_CONTEXT)-1
        prev = set_fn(ctypes.c_void_p(-1))
    except Exception as exc:
        print(f"[session] warn: DPI UNAWARE thread set failed: {exc!r}", flush=True)
        set_fn = None
        prev = None

    try:
        yield
    finally:
        if set_fn is not None and prev is not None:
            try:
                set_fn(ctypes.c_void_p(prev))
            except Exception:
                pass


def _multi_inject_launcher() -> Path:
    """Generic multi-DLL CREATE_SUSPENDED injector."""
    return (
        SERVER_DIR
        / "patches"
        / "client_injector"
        / "build"
        / "Release-Win32"
        / "launch_usdof_client.exe"
    )


def _random_box_trace_enabled() -> bool:
    """魔盒崩溃只读 VEH 追踪；默认关闭，手动 opt-in 才注入。"""
    return _env_flag("USDOF_RANDOM_BOX_TRACE", "0")


def _random_box_trace_dll() -> Path:
    """只使用随机盒子追踪的唯一主名 DLL。"""
    return (
        SERVER_DIR
        / "patches"
        / "debug"
        / "usdof_random_box_trace"
        / "build"
        / "Release-Win32"
        / "usdof_random_box_trace.dll"
    )


def _manual_item_move_trace_enabled() -> bool:
    """Local-only one-shot manual item-move observer; default off."""
    return _env_flag("USDOF_MANUAL_ITEM_MOVE_TRACE", "0")


def _manual_item_move_trace_dll() -> Path:
    """Return the isolated debug DLL; this path is never used by share builds."""
    return (
        SERVER_DIR
        / "patches"
        / "debug"
        / "usdof_manual_item_move_trace"
        / "build"
        / "Release-Win32"
        / "usdof_manual_item_move_trace.dll"
    )


def _skill_point_book_trace_enabled() -> bool:
    """Local-only one-shot SP/TP-book classifier observer; default off."""
    return _env_flag("USDOF_SKILL_POINT_BOOK_TRACE", "0")


def _skill_point_book_trace_dll() -> Path:
    """Return the isolated SP/TP-book debug DLL, excluded from share builds."""
    return (
        SERVER_DIR
        / "patches"
        / "debug"
        / "usdof_skill_point_book_trace"
        / "build"
        / "Release-Win32"
        / "usdof_skill_point_book_trace.dll"
    )


def _collect_inject_dlls(
    detected: PvfDetectionResult | None = None,
) -> list[tuple[str, Path]]:
    """Ordered (label, path) list of runtime inject DLLs enabled by env.

    注入顺序：cn_fix → func_extend（内部按 profile 启用 PVF）→ codec_passthrough →
    debug_window（默认关闭的数据流/PVF/NPK 观测）→ desktop_focus_guard（窄范围本地命令）→
    forbidden-func（邮箱入口屏蔽）→ weapon_avatar_dungeon（固定 001E reader）→
    native_loadout → random_box_trace（默认关闭的纯观测 VEH）→
    manual_item_move_trace（本地默认关闭、单次恢复的 debug 观测）→
    skill_point_book_trace（本地默认关闭、仅四本技能点书的单次观测）。
    """
    out: list[tuple[str, Path]] = []
    if detected is None:
        detected = _detect_current_pvf("inject-list")
    if _cn_fix_enabled():
        cn_fix = _cn_fix_dll()
        if cn_fix.is_file():
            out.append(("cn_fix", cn_fix))
        else:
            print(f"[session] third-party cn_fix DLL not found, skip: {cn_fix}", flush=True)
    # PVF/toast/item-popup/video share one resident owner. Its PVF component
    # reads USDOF_PVF_BRIDGE written by the profile detector in the child env.
    if _func_extend_enabled():
        out.append(("func_extend", _func_extend_dll()))
    # Sole owner of the Game encode/decode call sites; must precede feature DLLs.
    if _codec_passthrough_enabled():
        out.append(("codec_passthrough", _codec_passthrough_dll()))
    # Read-only main-IAT resource observer plus server-log tail UI.
    if _debug_window_enabled():
        out.append(("debug_window", _debug_window_dll()))
    # One verified SendMessageW caller only; must be installed while suspended.
    if _desktop_focus_guard_enabled():
        out.append(("desktop_focus_guard", _desktop_focus_guard_dll()))
    # The mail-entry hook must be ready before the user reaches that UI.
    if _forbidden_func_enabled():
        out.append(("forbidden-func", _forbidden_func_dll()))
    # One fixed S0/001E reader hook; no inventory/network/render-loop ownership.
    if _weapon_avatar_dungeon_enabled():
        out.append(("weapon_avatar_dungeon", _weapon_avatar_dungeon_dll()))
    # Default-on native dungeon loadout bridge; USDOF_NATIVE_LOADOUT=0 disables it.
    if _native_loadout_enabled():
        out.append(("native_loadout", _native_loadout_dll()))
    # No hook/breakpoint: a temporary opt-in VEH crash recorder for magic boxes.
    if _random_box_trace_enabled():
        out.append(("random_box_trace", _random_box_trace_dll()))
    # Repository-local evidence probe. Share catalog/build paths exclude debug/.
    if _manual_item_move_trace_enabled():
        out.append(("manual_item_move_trace", _manual_item_move_trace_dll()))
    # Exact C1/002C predecessor only; remains absent unless explicitly enabled.
    if _skill_point_book_trace_enabled():
        out.append(("skill_point_book_trace", _skill_point_book_trace_dll()))
    return out


def _collect_runtime_inject_dlls(
    detected: PvfDetectionResult | None = None,
) -> list[tuple[str, Path]]:
    """Append client-managed custom DLLs after the built-in patch catalog."""
    out = _collect_inject_dlls(detected)
    seen = {
        os.path.normcase(str(path.expanduser().resolve()))
        for _label, path in out
    }
    for path in resolve_managed_external_dll_paths(CLIENT_DIR):
        resolved = path.expanduser().resolve()
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        seen.add(key)
        out.append((f"custom:{resolved.name}", resolved))
    return out


def _is_custom_inject_label(label: str) -> bool:
    return label.startswith("custom:")


def _attach_newest_stage3_client(
    expected_dll_needles: list[str],
) -> subprocess.Popen | None:
    """Best-effort attach to newest Stage3 PID; verify inject DLLs mapped."""
    psutil = _psutil()
    if psutil is None:
        return None

    expected_exe = str(CLIENT_EXE.resolve()).lower().replace("\\", "/")
    newest = None
    newest_create = 0.0
    for p in psutil.process_iter(["pid", "name", "create_time", "exe"]):
        exe = (p.info.get("exe") or "").lower().replace("\\", "/")
        if exe == expected_exe:
            ct = float(p.info.get("create_time") or 0.0)
            if ct >= newest_create:
                newest_create = ct
                newest = p
    if newest is None:
        return None

    client_pid = int(newest.info["pid"])

    def _dlls_loaded(pid: int) -> dict[str, bool]:
        found = {n: False for n in expected_dll_needles}
        if not expected_dll_needles:
            return found
        try:
            proc = psutil.Process(pid)
            for m in proc.memory_maps():
                path = (getattr(m, "path", None) or "").lower().replace("\\", "/")
                for n in expected_dll_needles:
                    if n in path:
                        found[n] = True
        except Exception:
            pass
        return found

    time.sleep(1.5)
    mapped = _dlls_loaded(client_pid)
    for needle, ok in mapped.items():
        if ok:
            print(f"[session] inject DLL mapped: {needle} pid={client_pid}", flush=True)
        else:
            print(
                f"[session] WARN inject DLL NOT mapped: {needle} pid={client_pid}; "
                "re-launch via start_game.cmd / 启动游戏.cmd "
                "(do not start Stage3 EXE manually).",
                flush=True,
            )

    class _Attached:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def poll(self):  # type: ignore[no-untyped-def]
            try:
                return None if psutil.pid_exists(self.pid) else 0
            except Exception:
                return 0

        def terminate(self) -> None:
            try:
                psutil.Process(self.pid).terminate()
            except Exception:
                pass

        def kill(self) -> None:
            try:
                psutil.Process(self.pid).kill()
            except Exception:
                pass

    print(f"[session] client pid={client_pid} (injected)", flush=True)
    return _Attached(client_pid)  # type: ignore[return-value]


def start_client() -> subprocess.Popen:
    global _client_proc, _session_pvf_detection
    _apply_fixed_runtime_features()
    if not CLIENT_EXE.is_file():
        raise FileNotFoundError(f"missing client EXE: {CLIENT_EXE}")

    preprocessed = _preprocess_current_pvf("client-start")
    detected = _detect_current_pvf("client-start")
    _require_preprocessed_pvf(preprocessed, detected)
    _require_remote_pvf_identity(detected)
    if _session_pvf_detection is not None:
        _require_same_pvf(
            _session_pvf_detection, detected, CLIENT_DIR / "Script.pvf"
        )
    else:
        _session_pvf_detection = detected
    injects = _collect_runtime_inject_dlls(detected)
    if injects:
        launcher = _multi_inject_launcher()
        if not launcher.is_file():
            raise FileNotFoundError(
                f"missing multi-DLL inject launcher: {launcher} "
                "(run server_proto/patches/client_injector/build_with_llvm_mingw.ps1)"
            )
        for label, dll_path in injects:
            if not dll_path.is_file():
                raise FileNotFoundError(
                    f"inject enabled but missing DLL ({label}): {dll_path}"
                )
            if label != "cn_fix" and not _is_custom_inject_label(label):
                require_hardened_dll(dll_path, label)

    if injects:
        print(f"[session] inject via {launcher.name} dlls={len(injects)}", flush=True)
        for label, dll_path in injects:
            print(f"[session]   --dll {label}: {dll_path}", flush=True)
        print(
            "[session] client __COMPAT_LAYER=DPIUNAWARE "
            "(Windows display scaling virtualization)",
            flush=True,
        )

        cmd: list[str] = [
            str(launcher),
            "--exe",
            str(CLIENT_EXE),
            "--workdir",
            str(CLIENT_DIR),
        ]
        for _label, dll_path in injects:
            cmd.extend(["--dll", str(dll_path)])
        cmd.extend(["--args", client_launch_argument()])

        client_env = _client_launch_env(detected=detected)
        with _windows_createprocess_dpi_unaware():
            rc = subprocess.call(cmd, cwd=str(ROOT), env=client_env)
        if rc != 0:
            raise RuntimeError(f"inject launcher failed rc={rc}")

        needles = []
        for label, dll_path in injects:
            needles.append(
                dll_path.name.casefold()
                if _is_custom_inject_label(label)
                else label
            )

        attached = _attach_newest_stage3_client(needles)
        if attached is not None:
            _client_proc = attached  # type: ignore[assignment]
            return _client_proc  # type: ignore[return-value]

        print("[session] client launched via inject launcher (pid track best-effort)", flush=True)

        class _UnknownClient:
            pid = 0

            def poll(self):  # type: ignore[no-untyped-def]
                return None

            def terminate(self) -> None:
                return None

            def kill(self) -> None:
                return None

        _client_proc = _UnknownClient()  # type: ignore[assignment]
        return _client_proc  # type: ignore[return-value]

    # No inject DLLs: same console job family; do NOT detach.
    # Env + thread UNAWARE: Stage3 otherwise self-upgrades to PER_MONITOR.
    print(
        "[session] client __COMPAT_LAYER=DPIUNAWARE "
        "(Windows display scaling virtualization)",
        flush=True,
    )
    print("[session] inject disabled (no optional DLLs selected)", flush=True)
    client_env = _client_launch_env(detected=detected)
    with _windows_createprocess_dpi_unaware():
        _client_proc = subprocess.Popen(
            [str(CLIENT_EXE), client_launch_argument()],
            cwd=str(CLIENT_DIR),
            env=client_env,
        )
    print(f"[session] client pid={_client_proc.pid}", flush=True)
    return _client_proc


def _uses_external_launcher_login() -> bool:
    return os.environ.get("USDOF_SESSION_USE_GO_LOGIN") == "1"


def main() -> int:
    _install_console_handlers()
    print("[session] standard server-only session", flush=True)
    print(f"[session] python {describe_runtime()}", flush=True)
    print(f"[session] {describe_standard_local_flow()}", flush=True)
    runtime_bits = 64 if sys.maxsize > 2**32 else 32
    print(
        format_init_log(
            "USDOF Python Runtime",
            "CPython %d.%d.%d x%s" % (*sys.version_info[:3], runtime_bits),
            success=True,
        ),
        flush=True,
    )
    standard = standard_local_flow_env()
    print(
        format_init_log(
            "USDOF Standard Profile",
            "%s / %s"
            % (
                standard["USDOF_SCRIPT_PROFILE"],
                standard["USDOF_CHANNELINFO_PROFILE"],
            ),
            success=True,
        ),
        flush=True,
    )

    _apply_internal_control_env(dict(os.environ))
    control_host = os.environ.get("USDOF_CONTROL_HOST", "127.0.0.1")
    print(
        "[session] control API bind: %s:%s"
        % (control_host, _session_control_port),
        flush=True,
    )

    print("[1/2] start server", flush=True)
    start_server()
    # One shared one-hour deadline covers high-load PVF preload and boot warmup.
    external_login = _uses_external_launcher_login()
    required_ports = [7001, 10052, 10128, 17101, 17200]
    if not external_login:
        required_ports.append(17602)
    required_ports.append(_session_control_port)
    ready = wait_ports(required_ports, server_proc=_server_proc)
    ok7 = bool(ready.get(7001))
    ok5 = bool(ready.get(10052))
    ok_control = bool(ready.get(_session_control_port))
    ok_login = external_login or bool(ready.get(17602))
    ok_extra = all(bool(ready.get(port)) for port in (10128, 17101, 17200))
    print(
        "[session] listen 7001=%s 10052=%s Login/17602=%s "
        "ExtraTCP=%s Control/%s=%s"
        % (ok7, ok5, "external" if external_login else ok_login, ok_extra, _session_control_port, ok_control),
        flush=True,
    )
    if not all(bool(ready.get(port)) for port in required_ports):
        _join_server_output_threads(timeout=2.0)
        print(
            "[session] ERROR: 服务端启动失败；上方已显示 PVF 预热进度和异常详情。",
            file=sys.stderr,
            flush=True,
        )
        if _server_output_log_paths is not None:
            out_log, err_log = _server_output_log_paths
            print(
                f"[session] error logs: out={out_log} err={err_log}",
                file=sys.stderr,
                flush=True,
            )
        cleanup("ports-not-ready")
        return 1

    for line in service_ready_lines(_session_control_port, control_host=control_host):
        if external_login and "USDOF Login HTTP API" in line:
            continue
        print(line, flush=True)

    print("[2/2] READY", flush=True)
    print(" Close this window / Ctrl+C => stop this server", flush=True)
    for line in startup_footer_lines():
        print(line, flush=True)

    # Monitor the owned server even when the current CMD has interactive stdin.
    try:
        while True:
            if _server_proc is not None and _server_proc.poll() is not None:
                print(f"[session] server exited code={_server_proc.returncode}", flush=True)
                cleanup("server-exit")
                return int(_server_proc.returncode or 0)
            time.sleep(0.5)
    except KeyboardInterrupt:
        cleanup("keyboard-interrupt")
        return 0


def main_client_only() -> int:
    """Launch one injected Stage3 client without owning the running server."""
    print("[client-only] Stage3 shared injection path", flush=True)
    print(f"[client-only] python {describe_runtime()}", flush=True)
    prepare_client_cfg()  # no-op: do not touch DFO_UNIV.cfg
    start_client()
    print("[client-only] client launched; existing server left running", flush=True)
    return 0


def cli(argv: list[str] | None = None) -> int:
    _apply_fixed_runtime_features()
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return main()
    if args == ["--client-only"]:
        return main_client_only()
    print("usage: launch_game_session.py [--client-only]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(cli())
