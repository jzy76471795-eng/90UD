# -*- coding: utf-8 -*-
"""分享包路径解析（server-pack-win10）。

策略：
  1. 启动时只解析一次「分享根」绝对路径（启动入口所在目录）
  2. 布局用相对段拼接（分享根 / runtime/...），禁止在源码写死盘符
  3. 传给注入器 / 子进程时用绝对路径（原生工具对相对路径不稳）

布局（USDOF_Share_win10_gui）::

    <share_root>/
      启动游戏.exe                 ← Win10+ 单文件 GUI 入口
      runtime/
        server/             ← EXE 首次启动后解出的服务端资源 + 补丁 DLL
        temp/               ← 启动器、桥接和客户端工作临时文件
        inventory_usdof_local.db ← 自动生成的角色存档
      DFO.exe               ← 冻结客户端 EXE；与启动器同级
      Script.pvf            ← 客户端固定资源；服务端 PVF 可单独选择任意文件
"""
from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple, Union

PathLike = Union[str, Path]
LogFn = Callable[[str], None]


def _safe_abspath(path: PathLike) -> Path:
    """Normalize Windows paths, including bare and drive-relative roots."""
    if isinstance(path, Path):
        s = str(path)
    else:
        s = str(path or "")
    s = s.strip().strip('"')
    if not s:
        s = os.getcwd()
    # 裸盘符 R: → R:\
    if len(s) == 2 and s[1] == ":":
        s = s + os.sep
    # 盘符相对 R:foo（无斜杠）→ R:\foo
    if len(s) >= 3 and s[1] == ":" and s[2] not in "\\/":
        s = s[:2] + os.sep + s[2:]
    try:
        return Path(os.path.abspath(s))
    except OSError:
        try:
            return Path(os.path.normpath(s))
        except OSError:
            return Path(s)

# 相对段（相对分享根）
REL_RUNTIME = "runtime"
REL_SERVER = "runtime/server"
REL_PYTHON = "runtime/python"
REL_RUNTIME_TEMP = "runtime/temp"
LAUNCHER_CONTENTS_DIR_NAME = "启动游戏-runtime"
GM_CONSOLE_EXE_NAME = "GM控制台.exe"
# 仅兼容旧分享包；新包把 DFO.exe 放在分享根，不再建立 USDOF 子目录。
REL_CLIENT = "USDOF"
SAVE_DB_NAME = "inventory_usdof_local.db"
SETTINGS_NAME = "launcher-settings.json"
EXTERNAL_CONTROL_NAME = "external-control.json"
CONTROL_CONSOLE_SETTINGS_NAME = "control-console.json"
CHARACTER_LOADOUTS_NAME = "character-loadouts.json"
# 单文件启动器启动服务端时使用的内部子进程参数；不对用户界面暴露。
SERVER_CHILD_FLAG = "--usdof-server"
# 同一冻结 EXE 承载 GM PVF worker；该模式不打开 GUI，也不启动 Channel/Game。
GM_PVF_WORKER_FLAG = "--usdof-gm-pvf-worker"
# 同一冻结 EXE 承载离线 GM worker；该模式只读取 PVF 和角色库，不监听端口。
OFFLINE_GM_WORKER_FLAG = "--usdof-offline-gm-worker"

# 解压后写入，用于跳过重复全量拷贝
BUNDLE_STAMP_NAME = ".usdof_bundle_stamp"
# 升级解压时要保留的用户数据（相对 runtime/server）。PVF 索引缓存由服务端
# 启动时写入，必须与日志一样避开 EXE 版本更新的覆盖删除。
_PRESERVE_SERVER_RELS = (
    "data/inventory_usdof_local.db",
    "data/back_db",
    "data/runtime_cache",
    "log",
)

CLIENT_EXE_NAME = "DFO.exe"
CLIENT_EXE_SHA256 = "14b85f7e4325ef2d3c7a420740a88ef16bdd1c8f5bcda97044cc630139a56634"

LAUNCH_ARG = (
    "9?127.0.0.1?7001?3012345?e3210f9193c9558c3638523332a864a2d93222fc94a85a9d966ab135c0125814"
    "?0?0?0?0?0?2?0?0?0?0?0?0?0?0?0?0"
)

# 进程内缓存：启动时 resolve 一次
_CACHED_SHARE_ROOT: Optional[Path] = None
_RUNTIME_ENSURED = False


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def meipass_dir() -> Optional[Path]:
    """PyInstaller onefile extraction directory (legacy onedir remains readable)."""
    if not is_frozen():
        return None
    raw = getattr(sys, "_MEIPASS", None)
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_dir() else None


def detect_share_root() -> Path:
    """探测分享根（未缓存）。

    优先级：
      1. USDOF_SHARE_ROOT（可相对 cwd）
      2. 冻结 exe（历史 GUI 壳，已归档；仍兼容）
      3. 旧 runtime/launcher_lib 布局 → 分享根
      4. 自本文件向上找 启动游戏.cmd + runtime/（或 USDOF/ + runtime/）
      5. 开发态：server-pack-win10/launcher → 仓库根
    """
    env = (os.environ.get("USDOF_SHARE_ROOT") or "").strip()
    if env:
        p = Path(env).expanduser()
        if not p.is_absolute():
            p = Path(os.getcwd()) / p
        return _safe_abspath(p)
    if is_frozen():
        return _safe_abspath(sys.executable or (sys.argv[0] if sys.argv else os.getcwd())).parent

    # 兼容旧 runtime/launcher_lib 布局。
    try:
        here = _safe_abspath(Path(__file__))
        # .../runtime/launcher_lib/launcher/paths.pyc → 分享根 = parents[3]
        # .../runtime/launcher_lib/paths 结构以目录名为准
        for parent in [here.parent, *here.parents]:
            if parent.name.lower() == "launcher_lib" and parent.parent.name.lower() == "runtime":
                return parent.parent.parent
    except OSError:
        pass

    # 自本文件向上找「分享根」特征目录
    try:
        here = _safe_abspath(Path(__file__))
        for parent in [here.parent, *here.parents]:
            if (parent / CLIENT_EXE_NAME).is_file() and (parent / "runtime").is_dir():
                return parent
            if (parent / "USDOF").is_dir() and (parent / "runtime").is_dir():
                return parent
            # 仅有 runtime + 启动脚本、客户端尚未拷贝。
            has_launcher = any(
                (parent / name).is_file()
                for name in (
                    "启动游戏.cmd",
                    "启动游戏.pyw",
                    "启动游戏.exe",
                    "Start.cmd",
                )
            )
            if has_launcher and (parent / "runtime").is_dir():
                return parent
            # 历史兼容：embed 包 runtime/python + 启动脚本
            if (parent / "runtime" / "python" / "python.exe").is_file() and has_launcher:
                return parent
    except OSError:
        pass

    # 开发态：server-pack-win10/launcher/paths.py → parents[2] = repo root
    here = _safe_abspath(Path(__file__))
    # .../server-pack-win10/launcher/paths.py → parents[2] = repo
    return here.parents[2]


def share_root(*, refresh: bool = False) -> Path:
    """分享包根目录的绝对路径（启动时解析一次并缓存）。"""
    global _CACHED_SHARE_ROOT
    if refresh or _CACHED_SHARE_ROOT is None:
        _CACHED_SHARE_ROOT = detect_share_root()
    return _CACHED_SHARE_ROOT


def abs_str(path: Path) -> str:
    """规范绝对路径字符串（Windows 反斜杠）。"""
    try:
        return str(_safe_abspath(path))
    except OSError:
        return str(path)


def relpath_str(path: Path, base: Path) -> str:
    """path 相对 base 的字符串（仅日志展示用）。"""
    try:
        rel = _safe_abspath(path).relative_to(_safe_abspath(base))
        return str(rel).replace("/", "\\")
    except (ValueError, OSError):
        return abs_str(path)


def join_root(root: Path, *parts: str) -> Path:
    """在分享根下拼接相对段（parts 用 / 或 \\ 均可）。"""
    cur = root
    for part in parts:
        for seg in str(part).replace("\\", "/").split("/"):
            if seg and seg != ".":
                cur = cur / seg
    return cur


def runtime_dir(root: Optional[Path] = None) -> Path:
    return join_root(root or share_root(), REL_RUNTIME)


def runtime_temp_dir(root: Optional[Path] = None) -> Path:
    """Return the only writable temporary workspace for a share session."""
    return join_root(root or share_root(), REL_RUNTIME_TEMP)


def runtime_state_dir(root: Optional[Path] = None) -> Path:
    """Return the only persistent writable state directory for the launcher."""
    return runtime_dir(root or share_root())


def inventory_db_path(root: Optional[Path] = None) -> Path:
    """角色库落在 runtime，避免向客户端目录写入运行时文件。"""
    return runtime_state_dir(root) / SAVE_DB_NAME


def launcher_settings_path(root: Optional[Path] = None) -> Path:
    return runtime_state_dir(root) / SETTINGS_NAME


def external_control_path(root: Optional[Path] = None) -> Path:
    return runtime_state_dir(root) / EXTERNAL_CONTROL_NAME


def control_console_settings_path(root: Optional[Path] = None) -> Path:
    return runtime_state_dir(root) / CONTROL_CONSOLE_SETTINGS_NAME


def character_loadouts_path(root: Optional[Path] = None) -> Path:
    return runtime_state_dir(root) / CHARACTER_LOADOUTS_NAME


def _migrate_legacy_root_state(base: Path) -> None:
    """Move writable files from the old flat layout into ``runtime``.

    The repository root contains the developer's inventory master, so it is
    deliberately excluded. Packaged/client roots have no source tree and can
    migrate their previous share-package state in place.
    """
    if (base / "server-pack-win10").is_dir() and (base / "server_proto").is_dir():
        return
    state = runtime_state_dir(base)
    state.mkdir(parents=True, exist_ok=True)
    for name in (
        SAVE_DB_NAME,
        SETTINGS_NAME,
        EXTERNAL_CONTROL_NAME,
        CONTROL_CONSOLE_SETTINGS_NAME,
        CHARACTER_LOADOUTS_NAME,
    ):
        source = base / name
        target = state / name
        if not source.is_file() or target.exists():
            continue
        try:
            os.replace(str(source), str(target))
        except OSError:
            try:
                shutil.copy2(source, target)
                source.unlink()
            except OSError:
                pass


def embedded_runtime_dir() -> Optional[Path]:
    """Return the bundled runtime, preferring onefile extraction over legacy onedir."""
    base = meipass_dir()
    if base is not None:
        cand = base / "runtime"
        if cand.is_dir() and (cand / "server").is_dir():
            return cand
        if (base / "server").is_dir():
            return base
    if is_frozen():
        exe_dir = Path(sys.executable).resolve().parent
        for candidate in (
            exe_dir / LAUNCHER_CONTENTS_DIR_NAME / "runtime",
            exe_dir / LAUNCHER_CONTENTS_DIR_NAME,
        ):
            if candidate.is_dir() and (candidate / "server").is_dir():
                return candidate
    return None


def gm_console_exe(root: Optional[Path] = None) -> Path:
    """Return the independent GM console shipped beside the launcher."""
    return (root or share_root()) / GM_CONSOLE_EXE_NAME


def launcher_contents_dir(root: Optional[Path] = None) -> Path:
    """Return the onedir launcher contents beside ``启动游戏.exe``."""
    return (root or share_root()) / LAUNCHER_CONTENTS_DIR_NAME


def _read_stamp(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _runtime_looks_ready(dest: Path) -> bool:
    """目标 runtime 是否具备启动最小文件。

    单文件启动器的 Python 与 GUI 均在 EXE 内；落盘 runtime 只需服务端资源。
    """
    if not dest.is_dir():
        return False
    server = dest / "server"
    if not server.is_dir():
        return False
    entry_ok = any(
        (server / name).is_file()
        for name in (
            "server.blob",
            "usdof_server_main.pyc",
            "usdof_server_main.py",
            "start.pyc",
            "start.py",
        )
    )
    if not entry_ok:
        return False
    return True


def _is_mutable_server_file(rel: Path) -> bool:
    """Match the writable/external boundary used by the protected runtime."""
    parts = tuple(part.casefold() for part in rel.parts)
    if parts[:1] == ("patches",):
        return True
    if rel.suffix.casefold() in {".log", ".pvf"}:
        return True
    if any(part in {"log", "docs", "back_db", "runtime_cache"} for part in parts):
        return True
    return (
        len(parts) >= 2
        and parts[0] == "data"
        and parts[1].startswith("inventory_usdof_local")
    )


def _protected_server_inventory(server: Path) -> Dict[str, Tuple[int, bytes]]:
    """Return size/hash rows for files covered by the runtime HMAC."""
    inventory: Dict[str, Tuple[int, bytes]] = {}
    if not server.is_dir():
        return inventory
    for path in sorted(server.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(server)
        if _is_mutable_server_file(rel):
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        inventory[rel.as_posix().casefold()] = (path.stat().st_size, digest.digest())
    return inventory


def _protected_runtime_matches(embedded: Path, installed: Path) -> bool:
    return _protected_server_inventory(
        embedded / "server"
    ) == _protected_server_inventory(installed / "server")


def _validate_frozen_runtime_bundle(embedded: Optional[Path]) -> None:
    """Reject a partially copied EXE/onedir pair before Python imports the UI."""
    if not is_frozen():
        return
    if embedded is None:
        raise RuntimeError(
            "启动器内嵌运行组件缺失：请重新解压完整的启动游戏.exe"
        )
    server = embedded / "server"
    if not (server / "server.blob").is_file():
        raise RuntimeError(
            "启动器内嵌运行组件不完整：缺少 server.blob，请重新解压完整发布包"
        )
    contents = embedded.parent
    if not any(
        path.is_file() and path.suffix.casefold() == ".pyd"
        and path.name.casefold().startswith("usdof_native_ui")
        for path in contents.iterdir()
    ):
        raise RuntimeError(
            "启动器内嵌运行组件不完整：缺少 usdof_native_ui，不能初始化启动窗口"
        )
    try:
        import win10_protect_config as config  # type: ignore
        from usdof_protect_runtime import runtime_manifest

        secret = bytes(config.payload_secret())
        expected = bytes(getattr(config, "RUNTIME_MANIFEST", b""))
        actual = runtime_manifest(server, secret, include_patch_files=False)
    except Exception as exc:
        raise RuntimeError(
            "启动器内嵌运行组件版本不成对：请重新解压完整的启动游戏.exe"
        ) from exc
    if len(expected) != len(actual) or not hmac.compare_digest(expected, actual):
        raise RuntimeError(
            "启动器内嵌运行组件版本不成对：请重新解压完整的启动游戏.exe"
        )


def _backup_user_data(server_dst: Path, backup_root: Path) -> None:
    """升级覆盖前暂存角色库、日志与 PVF 派生索引缓存。"""
    if not server_dst.is_dir():
        return
    for rel in _PRESERVE_SERVER_RELS:
        src = server_dst.joinpath(*rel.split("/"))
        if not src.exists():
            continue
        dst = backup_root.joinpath(*rel.split("/"))
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        elif src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)


def _restore_user_data(server_dst: Path, backup_root: Path) -> None:
    if not backup_root.is_dir():
        return
    for rel in _PRESERVE_SERVER_RELS:
        src = backup_root.joinpath(*rel.split("/"))
        if not src.exists():
            continue
        dst = server_dst.joinpath(*rel.split("/"))
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        elif src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)


def _remove_runtime_path(path: Path) -> None:
    """Remove an obsolete static path, retrying transient scanner locks."""
    for attempt in range(5):
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
            return
        except OSError:
            if attempt == 4:
                raise
            time.sleep(0.15 * (attempt + 1))


def _copy_runtime_tree(src: Path, dst: Path, log: Optional[LogFn] = None) -> None:
    """全量复制 runtime；覆盖前会先删目标（调用方负责备份用户数据）。"""
    if dst.exists():
        # 只替换内嵌静态主体；顶层玩家存档与设置保持原位。
        for name in ("python", "server", "README_RUNTIME.txt", BUNDLE_STAMP_NAME):
            p = dst / name
            _remove_runtime_path(p)
    dst.mkdir(parents=True, exist_ok=True)
    # 复制 python
    src_py = src / "python"
    if src_py.is_dir():
        if log:
            log("  解压嵌入 Python …")
        shutil.copytree(src_py, dst / "python", dirs_exist_ok=True)
    # 复制 server
    src_sv = src / "server"
    if src_sv.is_dir():
        if log:
            log("  解压服务端 runtime …")
        shutil.copytree(src_sv, dst / "server", dirs_exist_ok=True)
    # 其它顶层文件
    for p in src.iterdir():
        if p.name in {"python", "server"}:
            continue
        target = dst / p.name
        if p.is_file():
            shutil.copy2(p, target)
        elif p.is_dir() and not target.exists():
            shutil.copytree(p, target)


def ensure_runtime_extracted(
    root: Optional[Path] = None,
    *,
    log: Optional[LogFn] = None,
    force: bool = False,
) -> Path:
    """确保分享根下 runtime/ 可用。

    - 开发态 / 外置 runtime 已就绪：直接返回
    - 冻结 exe：从 MEIPASS 解压到 share_root/runtime；stamp 相同则跳过
    - 升级解压时保留 server/data 角色库、PVF 派生索引缓存与 log
    """
    global _RUNTIME_ENSURED
    base = root or share_root()
    dest = runtime_dir(base)
    _log = log or (lambda _m: None)
    runtime_temp_dir(base).mkdir(parents=True, exist_ok=True)
    _migrate_legacy_root_state(base)

    # 开发态：仓库根已有 runtime 或 server_proto，无需内嵌解压
    emb = embedded_runtime_dir()
    _validate_frozen_runtime_bundle(emb)
    if emb is None:
        if not _runtime_looks_ready(dest):
            # 开发态允许 server_proto 回退，不在这里报错
            pass
        _RUNTIME_ENSURED = True
        return dest

    stamp_src = emb / BUNDLE_STAMP_NAME
    want_stamp = _read_stamp(stamp_src) if stamp_src.is_file() else ""
    have_stamp = _read_stamp(dest / BUNDLE_STAMP_NAME) if (dest / BUNDLE_STAMP_NAME).is_file() else ""

    stamp_matches = (
        not force
        and _runtime_looks_ready(dest)
        and want_stamp
        and have_stamp == want_stamp
    )
    if stamp_matches:
        try:
            content_matches = _protected_runtime_matches(emb, dest)
        except OSError as exc:
            content_matches = False
            _log("runtime 静态资源读取异常，将自动修复：%s" % exc)
        if content_matches:
            _log("runtime 已就绪（版本戳与静态资源匹配，跳过解压）")
            _RUNTIME_ENSURED = True
            return dest
        _log("runtime 版本戳匹配但静态资源不完整，正在自动修复")

    if _runtime_looks_ready(dest) and not force and not want_stamp and have_stamp == "":
        # 无戳但已完整：旧布局，不强制覆盖
        _log("runtime 已存在（无版本戳），跳过解压")
        _RUNTIME_ENSURED = True
        return dest

    _log("从启动器内嵌包解压 runtime → %s" % abs_str(dest))
    t0 = time.time()
    backup = runtime_temp_dir(base) / ".usdof_user_data_bak"
    completed = False
    try:
        if backup.exists():
            shutil.rmtree(backup)
        backup.mkdir(parents=True, exist_ok=True)
        _backup_user_data(dest / "server", backup)
        _copy_runtime_tree(emb, dest, log=_log)
        _restore_user_data(dest / "server", backup)
        if not _protected_runtime_matches(emb, dest):
            raise RuntimeError("runtime 静态资源复制后校验不一致")
        # 写入 stamp
        if want_stamp:
            (dest / BUNDLE_STAMP_NAME).write_text(want_stamp + "\n", encoding="utf-8")
        elif stamp_src.is_file():
            shutil.copy2(stamp_src, dest / BUNDLE_STAMP_NAME)
        else:
            # 构建应写入 stamp；缺省用时间戳
            (dest / BUNDLE_STAMP_NAME).write_text(
                "unspecified-%d\n" % int(time.time()), encoding="utf-8"
            )
        # 这些路径都是玩家运行时可写副产物，不能被受保护静态资源校验覆盖。
        (dest / "server" / "log").mkdir(parents=True, exist_ok=True)
        (dest / "server" / "data" / "back_db").mkdir(parents=True, exist_ok=True)
        (dest / "server" / "data" / "runtime_cache" / "pvf_indexes").mkdir(
            parents=True, exist_ok=True
        )
        completed = True
    except Exception:
        # 如果替换中断，先把已备份的用户数据还原到部分 runtime。
        # 还原本身失败时保留 backup，便于下次启动或手工取回。
        try:
            _restore_user_data(dest / "server", backup)
        except OSError as restore_exc:
            raise RuntimeError(
                "runtime 更新中断，用户数据备份已保留在 %s：%s"
                % (abs_str(backup), restore_exc)
            ) from restore_exc
        raise
    finally:
        if completed and backup.exists():
            shutil.rmtree(backup)

    if not _runtime_looks_ready(dest):
        raise RuntimeError(
            "内嵌 runtime 解压后仍不完整：%s（请重新打包 启动游戏.exe）" % abs_str(dest)
        )
    _log("runtime 解压完成（+%.1fs）" % (time.time() - t0))
    _RUNTIME_ENSURED = True
    return dest


def server_home(root: Optional[Path] = None) -> Path:
    """分享包：runtime/server；开发态回退仓库 server_proto。"""
    base = root or share_root()
    packed = join_root(base, REL_SERVER)
    if packed.is_dir() and (
        (packed / "server.blob").is_file()
        or (packed / "start.pyc").is_file()
        or (packed / "start.py").is_file()
        or (packed / "usdof_server_main.pyc").is_file()
        or (packed / "usdof_server_main.py").is_file()
    ):
        return packed
    # 冻结态若尚未解压，仍返回预期路径（调用方应先 ensure_runtime_extracted）
    if is_frozen():
        return packed
    dev = base / "server_proto"
    if dev.is_dir() and ((dev / "start.py").is_file() or (dev / "start.pyc").is_file()):
        return dev
    return packed


def python_home(root: Optional[Path] = None) -> Path:
    return join_root(root or share_root(), REL_PYTHON)


def has_embedded_python(root: Optional[Path] = None) -> bool:
    """分享包是否自带 runtime/python（host-python 包为 False）。"""
    home = python_home(root)
    return (home / "python.exe").is_file() or (home / "pythonw.exe").is_file()


def _win_python_creationflags() -> int:
    """Run probes detached from the GUI while retaining process control."""
    if os.name != "nt":
        return 0
    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    return 0x00000008 | 0x00000200


def _looks_like_python312(exe: Path) -> bool:
    """粗检是否为 3.12.x；完整微版本由上层门禁校验。"""
    try:
        import subprocess

        out = subprocess.check_output(
            [str(exe), "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            stdin=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            creationflags=_win_python_creationflags(),
        )
        return out.decode("ascii", errors="ignore").strip() == "3.12"
    except Exception:
        return False


def resolve_host_python() -> Optional[Path]:
    """从环境变量 / PATH 解析本机 Python（不硬编码盘符路径）。

    优先级：
      1. USDOF_PYTHON（可选覆盖，可为 python / py / 绝对路径）
      2. 当前解释器 sys.executable（已是 3.x 时）
      3. PATH：python.exe → python3.exe → py.exe -3.12 → py.exe -3
    """
    import shutil
    import subprocess

    def _abs_if_file(p: Path) -> Optional[Path]:
        try:
            if p.is_file():
                return _safe_abspath(p)
        except OSError:
            pass
        return None

    env = (os.environ.get("USDOF_PYTHON") or "").strip().strip('"')
    if env:
        # 允许写 "py -3.12" 整串，或单个可执行名/路径。
        parts = env.split()
        if len(parts) == 1:
            raw = Path(parts[0])
            if raw.is_file():
                return _safe_abspath(raw)
            found = shutil.which(parts[0])
            if found:
                return _safe_abspath(found)
        else:
            # 用 ``-c`` 解析 launcher 背后的真实解释器路径。
            try:
                out = subprocess.check_output(
                    parts + ["-c", "import sys; print(sys.executable)"],
                    stdin=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=8,
                    creationflags=_win_python_creationflags(),
                )
                pe = out.decode("utf-8", errors="ignore").strip().strip('"')
                if pe:
                    hit = _abs_if_file(Path(pe))
                    if hit is not None:
                        return hit
            except Exception:
                pass

    # 当前进程已是可用解释器（CMD 用 PATH 上的 python 起 run_console 时走这里）
    try:
        cur = Path(sys.executable or "")
        if cur.is_file() and sys.version_info[:2] == (3, 12):
            return _safe_abspath(cur)
        if cur.is_file() and sys.version_info[0] == 3:
            # 其它 3.x 仍返回，由上层给出精确版本错误。
            return _safe_abspath(cur)
    except Exception:
        pass

    # PATH 搜索：先找 3.12，再保留任意 Python 供上层报告版本。
    path_candidates = []
    for name in ("python.exe", "python3.exe", "python", "python3"):
        found = shutil.which(name)
        if not found:
            continue
        p = _safe_abspath(found)
        if not p.is_file():
            continue
        if _looks_like_python312(p):
            return p
        path_candidates.append(p)

    # Windows py launcher（仍不硬编码安装目录）
    for args in (
        ["py", "-3.12", "-c", "import sys; print(sys.executable)"],
        ["py", "-3", "-c", "import sys; print(sys.executable)"],
    ):
        try:
            out = subprocess.check_output(
                args,
                stdin=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
                creationflags=_win_python_creationflags(),
            )
            pe = out.decode("utf-8", errors="ignore").strip().strip('"')
            hit = _abs_if_file(Path(pe)) if pe else None
            if hit is not None:
                return hit
        except Exception:
            continue

    if path_candidates:
        return path_candidates[0]
    return None


def python_exe(root: Optional[Path] = None) -> Path:
    """解析用于起服务端的 Python。

    - 有 runtime/python/python.exe → 使用仓库内 runtime
    - 否则 → PATH / USDOF_PYTHON / 当前解释器
    """
    home = python_home(root)
    for name in ("python.exe", "pythonw.exe"):
        p = home / name
        if p.is_file():
            return p
    host = resolve_host_python()
    if host is not None:
        return host
    # 最后回退当前解释器（开发态）
    return Path(sys.executable or "python")


def client_dir(root: Optional[Path] = None) -> Path:
    configured = (os.environ.get("USDOF_CLIENT_DIR") or "").strip().strip('"')
    if configured:
        return _safe_abspath(configured)
    base = root or share_root()
    # 新包直接解压到完整客户端根时，启动器和 DFO.exe 同级；旧包仍可用。
    if (base / CLIENT_EXE_NAME).is_file():
        return base
    legacy = join_root(base, REL_CLIENT)
    if (legacy / CLIENT_EXE_NAME).is_file():
        return legacy
    return base


def client_exe(root: Optional[Path] = None) -> Path:
    return client_dir(root) / CLIENT_EXE_NAME


def client_pvf_path(root: Optional[Path] = None) -> Path:
    return client_dir(root) / "Script.pvf"


def server_pvf_path(root: Optional[Path] = None) -> Path:
    configured = (os.environ.get("USDOF_SERVER_PVF_PATH") or "").strip().strip('"')
    if configured:
        return _safe_abspath(configured)
    return client_pvf_path(root)


def pvf_path(root: Optional[Path] = None) -> Path:
    """Compatibility alias for the server-side runtime PVF."""
    return server_pvf_path(root)
