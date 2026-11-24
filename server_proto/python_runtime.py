# -*- coding: utf-8 -*-
"""USDOF 服务端 Python 运行时（硬锁 3.12.10，与分享包一致）。

硬规则
------
- **开服 / 一键会话 / 控制脚本** 必须使用 **CPython 3.12.10**（x64）。
- 与 ``server-pack-win10/build_share_package.py`` 编译进分享包的 magic 版本一致。
- 只维护 Windows 10 / Windows 11 x64，开发机与分享包使用同一微版本。

查找顺序（第一个版本校验通过的 ``python.exe``）
------------------------------------------------
1. 环境变量 ``USDOF_PYTHON_EXE``（显式指定）
2. ``<repo>/server-pack-win10/cache/python-3.12.10-full/python.exe``
3. ``<repo>/runtime/python/python.exe``（兼容仓库内独立 runtime，仍须版本匹配）
4. 常见本机安装路径（``Python312`` / ``Python3.12.10``）
5. ``py -3.12-64`` 启动器解析出的解释器
6. 当前 ``sys.executable``（仅当已是 3.12.10）

入口用法
--------
- 进程入口（``start.py`` / ``launch_game_session.py``）调用
  ``ensure_server_python(reexec=True)``：版本不对则自动切到 3.12.10 重入。
- 子进程开服用 ``server_python_executable()``，不要裸用 PATH 上的 ``python``。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

# 与唯一 Win10/Win11 分享包的构建解释器对齐。
REQUIRED_PY_VERSION: Tuple[int, int, int] = (3, 12, 10)
REQUIRED_PY_VERSION_TEXT = "%d.%d.%d" % REQUIRED_PY_VERSION

# server_proto/python_runtime.py -> parents[0]=server_proto, parents[1]=repo
SERVER_DIR = Path(__file__).resolve().parent
REPO_ROOT = SERVER_DIR.parent

_ENV_PYTHON = "USDOF_PYTHON_EXE"
_ENV_SKIP_REEXEC = "USDOF_PYTHON_NO_REEXEC"
_ENV_ALLOW_OTHER = "USDOF_ALLOW_NON_31210"  # 仅紧急 A/B；默认禁止
_STDIO_HARDENED = False


def ensure_server_dir_on_sys_path() -> None:
    """保证 ``server_proto`` 在 sys.path 最前。

    官方 embed + ``python38._pth`` 默认隔离路径，不会自动把脚本目录加入
    ``sys.path``，从仓库根执行 ``python server_proto/start.py`` 会
    ``No module named 'channel'``。开发开服与 embed 均需本函数。
    """
    s = str(SERVER_DIR)
    if s not in sys.path:
        sys.path.insert(0, s)


def harden_stdio() -> None:
    """Keep console logging alive when a Windows code page cannot encode text.

    The GUI child uses UTF-8 log files, while repository command entries may
    still inherit a locale-specific console. Only the error policy is relaxed;
    the caller's selected encoding remains unchanged.
    """
    global _STDIO_HARDENED
    if _STDIO_HARDENED:
        return
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            if hasattr(stream, "reconfigure"):
                # 3.7+：只改 errors，不改 encoding
                stream.reconfigure(errors="replace")
                continue
        except Exception:
            pass
        try:
            import io

            buf = getattr(stream, "buffer", None)
            if buf is None:
                continue
            enc = getattr(stream, "encoding", None) or "gbk"
            wrapped = io.TextIOWrapper(
                buf,
                encoding=enc,
                errors="replace",
                line_buffering=bool(getattr(stream, "line_buffering", True)),
                write_through=bool(getattr(stream, "write_through", False)),
            )
            setattr(sys, name, wrapped)
        except Exception:
            pass
    _STDIO_HARDENED = True


def safe_print(*args: Any, **kwargs: Any) -> None:
    """Print with replacement fallback instead of terminating the service."""
    harden_stdio()
    try:
        print(*args, **kwargs)
        return
    except UnicodeEncodeError:
        pass
    except Exception:
        # 流已关闭等：再试一次降级
        pass
    enc = getattr(sys.stdout, "encoding", None) or "gbk"
    parts = []
    for a in args:
        text = str(a)
        try:
            text.encode(enc)
        except UnicodeEncodeError:
            text = text.encode(enc, errors="replace").decode(enc, errors="replace")
        parts.append(text)
    sep = kwargs.get("sep", " ")
    end = kwargs.get("end", "\n")
    flush = bool(kwargs.get("flush", False))
    line = sep.join(parts) + end
    try:
        stream = kwargs.get("file") or sys.stdout
        stream.write(line)
        if flush:
            stream.flush()
    except Exception:
        try:
            sys.__stdout__.write(line.encode(enc, errors="replace").decode(enc, errors="replace"))
            if flush:
                sys.__stdout__.flush()
        except Exception:
            pass


# Import-time setup keeps entry points consistent before channel/game imports.
ensure_server_dir_on_sys_path()
harden_stdio()


def required_version_tuple() -> Tuple[int, int, int]:
    return REQUIRED_PY_VERSION


def is_required_version(info: Optional[Sequence[int]] = None) -> bool:
    if info is None:
        info = sys.version_info
    return (int(info[0]), int(info[1]), int(info[2])) == REQUIRED_PY_VERSION


def _version_of_exe(exe: Path) -> Optional[Tuple[int, int, int]]:
    """查询解释器三元组；失败返回 None。"""
    try:
        out = subprocess.check_output(
            [
                str(exe),
                "-c",
                "import sys; print('%d.%d.%d' % sys.version_info[:3])",
            ],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=15,
        ).strip()
        parts = out.split(".")
        if len(parts) != 3:
            return None
        return int(parts[0]), int(parts[1]), int(parts[2])
    except Exception:
        return None


def _candidate_paths() -> List[Path]:
    paths: List[Path] = []
    seen = set()

    def add(p: Optional[Path]) -> None:
        if p is None:
            return
        try:
            rp = p.expanduser().resolve()
        except Exception:
            rp = Path(p)
        key = str(rp).lower()
        if key in seen:
            return
        seen.add(key)
        paths.append(rp)

    env = os.environ.get(_ENV_PYTHON, "").strip().strip('"')
    if env:
        add(Path(env))

    # 唯一 Win10/Win11 构建缓存使用完整安装布局。
    add(
        REPO_ROOT
        / "server-pack-win10"
        / "cache"
        / "python-3.12.10-full"
        / "python.exe"
    )
    add(REPO_ROOT / "runtime" / "python" / "python.exe")

    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        add(Path(local) / "Programs" / "Python" / "Python312" / "python.exe")
        add(Path(local) / "Programs" / "Python" / "Python31210" / "python.exe")

    for base in (
        Path(r"C:\Python312"),
        Path(r"C:\Python3.12.10"),
        Path(r"D:\Python312"),
        Path(r"D:\Python3.12.10"),
        Path(r"D:\Software\Python312"),
        Path(r"D:\Software\Python3.12.10"),
    ):
        add(base / "python.exe")

    # Python launcher -> 3.12 x64.
    try:
        out = subprocess.check_output(
            ["py", "-3.12-64", "-c", "import sys; print(sys.executable)"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
        ).strip()
        if out:
            add(Path(out))
    except Exception:
        pass

    try:
        add(Path(sys.executable))
    except Exception:
        pass

    return paths


def find_server_python(*, require_exists: bool = True) -> Optional[Path]:
    """返回第一个确认为 3.12.10 的 python.exe；找不到则 None。"""
    # Win10 onefile share runs the server inside the frozen launcher. Although
    # sys.executable names the launcher EXE, it is not a Python command-line
    # interpreter and must never receive a ``-c`` version probe.
    if getattr(sys, "frozen", False) and is_required_version():
        return Path(sys.executable)
    for cand in _candidate_paths():
        if not cand.is_file():
            continue
        ver = _version_of_exe(cand)
        if ver == REQUIRED_PY_VERSION:
            return cand
    if require_exists:
        return None
    return None


def server_python_executable() -> Path:
    """供子进程 / 控制脚本使用的 3.12.10 路径；找不到则抛错。"""
    found = find_server_python(require_exists=True)
    if found is None:
        raise RuntimeError(_missing_message())
    return found


def _missing_message() -> str:
    runtime = REPO_ROOT / "runtime" / "python" / "python.exe"
    build = (
        REPO_ROOT
        / "server-pack-win10"
        / "cache"
        / "python-3.12.10-full"
        / "python.exe"
    )
    return (
        "USDOF 服务端硬锁 CPython %s（与 Win10/Win11 分享包一致）。\n"
        "未找到可用的 %s 解释器。\n"
        "请任选其一：\n"
        "  1) 使用仓库内 embed：%s\n"
        "  2) 使用构建缓存：%s\n"
        "  3) 安装官方 Python %s x64，或设置环境变量 %s=完整\\python.exe\n"
        "当前进程: %s (%s)\n"
        % (
            REQUIRED_PY_VERSION_TEXT,
            REQUIRED_PY_VERSION_TEXT,
            runtime,
            build,
            REQUIRED_PY_VERSION_TEXT,
            _ENV_PYTHON,
            sys.executable,
            "%d.%d.%d" % sys.version_info[:3],
        )
    )


def _same_exe(a: Path, b: Path) -> bool:
    try:
        return a.resolve().samefile(b.resolve())
    except Exception:
        return str(a).lower() == str(b).lower()


def ensure_server_python(*, reexec: bool = True, script_argv: Optional[Sequence[str]] = None) -> Path:
    """确保当前进程是 3.12.10；否则定位仓库内解释器并 re-exec。

    Parameters
    ----------
    reexec:
        True 时若版本不对则重入 3.12.10（保留 argv）。
        False 时版本不对直接 ``SystemExit``。
    script_argv:
        传给新解释器的 argv（含脚本路径）；默认 ``sys.argv``。
    """
    harden_stdio()

    if is_required_version():
        # 已是 3.12.10：仅首次打印，避免 main/__main__ 双次刷屏
        exe = Path(sys.executable)
        flag = "USDOF_PYTHON_VERSION_LOGGED"
        if os.environ.get(flag) != "1":
            # 路径用 str；safe_print 保证 GBK 不可编码字符不会炸进程
            safe_print(
                "[python] %s ok (%s)" % (REQUIRED_PY_VERSION_TEXT, exe),
                flush=True,
            )
            os.environ[flag] = "1"
        return exe

    if os.environ.get(_ENV_ALLOW_OTHER, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        # 用 ASCII 连字符，避免 em-dash 在极端代码页下出问题
        safe_print(
            "[python] WARNING: %s 已放行非 %s 解释器 %s (%s) - 仅紧急 A/B，禁止日常/分享"
            % (
                _ENV_ALLOW_OTHER,
                REQUIRED_PY_VERSION_TEXT,
                sys.executable,
                "%d.%d.%d" % sys.version_info[:3],
            ),
            flush=True,
        )
        return Path(sys.executable)

    target = find_server_python(require_exists=True)
    if target is None:
        raise SystemExit(_missing_message())

    cur = Path(sys.executable)
    if is_required_version() and _same_exe(cur, target):
        return cur

    if not reexec or os.environ.get(_ENV_SKIP_REEXEC, "").strip() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise SystemExit(
            "当前解释器不是 %s：%s (%s)\n请改用：%s\n%s"
            % (
                REQUIRED_PY_VERSION_TEXT,
                cur,
                "%d.%d.%d" % sys.version_info[:3],
                target,
                _missing_message(),
            )
        )

    argv = list(script_argv if script_argv is not None else sys.argv)
    if not argv:
        argv = ["-c", "pass"]
    # Windows 上 os.execv 对带空格路径/控制台会话不可靠 -> 同步子进程后退出。
    # 子进程内跳过二次 re-exec 查找（已是正确解释器）。
    new_argv = [str(target)] + list(argv)
    safe_print(
        "[python] re-exec %s -> %s\n         was %s (%s)"
        % (
            REQUIRED_PY_VERSION_TEXT,
            target,
            cur,
            "%d.%d.%d" % sys.version_info[:3],
        ),
        flush=True,
    )
    env = os.environ.copy()
    env[_ENV_PYTHON] = str(target)
    # embed ._pth 常忽略外部 site；PYTHONPATH + 子进程内 ensure_server_dir 双保险
    prev_pp = env.get("PYTHONPATH", "")
    server_s = str(SERVER_DIR)
    if prev_pp:
        if server_s not in prev_pp.split(os.pathsep):
            env["PYTHONPATH"] = server_s + os.pathsep + prev_pp
    else:
        env["PYTHONPATH"] = server_s
    # 子进程已是 3.12.10；若仍不对则由 ensure 硬失败。
    env.pop(_ENV_SKIP_REEXEC, None)
    try:
        # cwd 保持调用方；import 依赖 PYTHONPATH / ensure_server_dir_on_sys_path
        rc = subprocess.call(new_argv, env=env)
    except OSError as exc:
        raise SystemExit(
            "无法启动 %s：%s\n%s" % (target, exc, _missing_message())
        ) from exc
    raise SystemExit(rc)


def describe_runtime() -> str:
    """一行摘要，供日志 / README。"""
    found = find_server_python(require_exists=False)
    cur = "%d.%d.%d" % sys.version_info[:3]
    if found is not None:
        return "required=%s current=%s preferred=%s" % (
            REQUIRED_PY_VERSION_TEXT,
            cur,
            found,
        )
    return "required=%s current=%s preferred=<missing>" % (
        REQUIRED_PY_VERSION_TEXT,
        cur,
    )


__all__ = [
    "REQUIRED_PY_VERSION",
    "REQUIRED_PY_VERSION_TEXT",
    "REPO_ROOT",
    "SERVER_DIR",
    "describe_runtime",
    "ensure_server_python",
    "find_server_python",
    "harden_stdio",
    "is_required_version",
    "required_version_tuple",
    "safe_print",
    "server_python_executable",
]
