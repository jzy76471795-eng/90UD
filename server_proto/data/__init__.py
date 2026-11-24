# -*- coding: utf-8 -*-
"""数据与资源路径调度（DB / PVF / log / back_db）。

对应一级调度入口 ``data``（设计中的 data.py 角色）。
路径真源集中在此；业务通过 ``from data import PVF_PATH, DB_PATH, ...`` 获取。

布局约定（固定）：
  <repo>/
    USDOF/           # 客户端 + 运行时 Script.pvf
    server_proto/    # 本模拟服（与 USDOF 同级）

分享包布局（``server-pack`` 产出）：
  <share>/
    USDOF/
    runtime/server/  # USDOF_SERVER_HOME（等价 server_proto）
  环境变量：
    USDOF_REPO_ROOT    分享包根（含 USDOF）
    USDOF_SERVER_HOME  服务端根（含 data/opcodes/...）
    USDOF_PVF_PATH     可选覆盖 Script.pvf

运行时 Script.pvf 唯一默认源：``../USDOF/Script.pvf``（相对 server_proto）。
``data/pvf/unpack/`` 仅开发期离线对照，运行时禁读。
**禁止**在 server_proto 内再放整包 .pvf 归档。
**禁止**把临时脚本、探针、分析 dump 放进本目录。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Optional


def _path_base() -> Path:
    """相对路径基准：USDOF_PATH_BASE → cwd → 本模块推断。

    分享启动器把路径写成相对分享根的相对路径，并设 cwd=分享根。
    **禁止**在源码里写死盘符绝对路径。
    """
    raw = (os.environ.get("USDOF_PATH_BASE") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = Path.cwd() / p
        try:
            return p.resolve()
        except OSError:
            pass
    try:
        return Path.cwd().resolve()
    except OSError:
        return Path(".").resolve()


def _env_path(name: str) -> Optional[Path]:
    """读取环境变量路径；相对路径相对 ``_path_base()`` 解析。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = _path_base() / p
    try:
        return p.resolve()
    except OSError:
        return p


def _infer_repo_root(server_home: Path) -> Path:
    """从服务端根推断分享包/仓库根。

    分享包：``<root>/runtime/server`` → ``<root>``
    开发树：``<root>/server_proto`` → ``<root>``
    """
    if (
        server_home.name.lower() == "server"
        and server_home.parent.name.lower() == "runtime"
    ):
        return server_home.parent.parent
    return server_home.parent


def _infer_server_home_from_file() -> Path:
    """本文件在 data/__init__.* → parent.parent = server 根。"""
    return Path(__file__).resolve().parent.parent


# server_proto / runtime/server 根
_env_server_home = _env_path("USDOF_SERVER_HOME")
BASE_DIR = _env_server_home if _env_server_home is not None else _infer_server_home_from_file()
# 仓库根：USDOF 与 server_proto 同级（分享包为 USDOF 与 runtime 同级）
_env_repo_root = _env_path("USDOF_REPO_ROOT") or _env_path("USDOF_SHARE_ROOT")
if _env_repo_root is not None:
    REPO_ROOT = _env_repo_root
elif _env_server_home is not None:
    REPO_ROOT = _infer_repo_root(BASE_DIR)
else:
    REPO_ROOT = _infer_repo_root(BASE_DIR)
# 客户端目录（与 server_proto / runtime 同级）
_env_client_dir = _env_path("USDOF_CLIENT_DIR")
CLIENT_DIR = _env_client_dir if _env_client_dir is not None else (REPO_ROOT / "USDOF")
# 本数据目录
DATA_DIR = BASE_DIR / "data"

LOG_DIR = BASE_DIR / "log"
CAPTURE_DIR = LOG_DIR / "captures"
DOC_DIR = BASE_DIR / "docs"
OPCODE_DIR = BASE_DIR / "opcodes"
PATCHES_DIR = BASE_DIR / "patches"

_env_sqlite_db = _env_path("USDOF_SQLITE_DB")
DB_PATH = _env_sqlite_db if _env_sqlite_db is not None else (DATA_DIR / "inventory_usdof_local.db")
BACK_DB_DIR = DATA_DIR / "back_db"

# 开发期对照树（仅 unpack，无整包 .pvf）
PVF_DIR = DATA_DIR / "pvf"
PVF_UNPACK_DIR = PVF_DIR / "unpack"

# 运行时 Script.pvf：默认与客户端共用 USDOF/Script.pvf
DEFAULT_PVF_NAME = "Script.pvf"
DEFAULT_PVF_PATH = CLIENT_DIR / DEFAULT_PVF_NAME
_env_pvf = _env_path("USDOF_PVF_PATH")
PVF_PATH = _env_pvf if _env_pvf is not None else DEFAULT_PVF_PATH

RESOURCE_DIR = DATA_DIR / "resources"
# 运行期 PVF 派生索引；不属于角色存档或静态资源，禁止提交和分享打包。
RUNTIME_CACHE_DIR = DATA_DIR / "runtime_cache"
PVF_INDEX_CACHE_DIR = RUNTIME_CACHE_DIR / "pvf_indexes"


def ensure_dirs() -> None:
    """创建运行时必要目录（日志/抓包/文档/备份）。"""
    for path in (
        LOG_DIR,
        CAPTURE_DIR,
        DOC_DIR,
        DATA_DIR,
        BACK_DB_DIR,
        RESOURCE_DIR,
        RUNTIME_CACHE_DIR,
        PVF_INDEX_CACHE_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)


def _pvf_group_residency_mode() -> str:
    """Resolve the normal boot residency strategy.

    ``selective`` is the production default: it keeps the verified PVF source
    and structural indexes in memory, then retains only groups consumed by
    required boot domains.  ``full`` remains available for diagnostics.
    """
    raw = os.environ.get("USDOF_PVF_GROUP_RESIDENCY", "selective").strip().lower()
    if raw in ("", "selective"):
        return "selective"
    if raw == "full":
        return "full"
    raise ValueError("invalid USDOF_PVF_GROUP_RESIDENCY: %s" % raw)


def preload_script_pvf_and_warmup(
    *,
    status_callback: Optional[Callable[[str, dict[str, Any]], None]] = None,
    timing_callback: Optional[Callable[[str, dict[str, Any]], None]] = None,
    warmup_profile: str = "full",
) -> tuple[Any, Any]:
    """Prepare Script.pvf and required indexes before listeners start.

    Default selective mode keeps the source file, name/path metadata, and only
    required-domain groups in memory.  ``warmup_profile=core`` defers the
    GM-only full item catalog; ``full`` waits for that domain's dedicated
    worker process. Group residency remains an independent selective/full
    diagnostic setting.
    """
    from pvf import pvf_runtime
    from pvf.boot_warmup import warmup_all_runtime_indexes

    def emit_timing(event: str, payload: dict[str, Any]) -> None:
        if timing_callback is None:
            return
        try:
            timing_callback(event, payload)
        except Exception:
            # Startup metrics are observational and never affect PVF loading.
            pass

    residency_mode = _pvf_group_residency_mode()
    emit_timing("pvf_read_begin", {"group_residency": residency_mode})
    emit_timing("pvf_identity_begin", {})
    if residency_mode == "full":
        stats = pvf_runtime.preload_script_pvf(str(PVF_PATH))
    else:
        stats = pvf_runtime.prepare_script_pvf_selective_runtime(str(PVF_PATH))
    required_structure = ("memory_backed", "path_index_ready")
    missing = [name for name in required_structure if not getattr(stats, name, False)]
    reader = pvf_runtime.get_shared_reader(str(PVF_PATH))
    if reader is None or not getattr(reader, "name_pools_ready", False):
        missing.append("shared_memory_structure")
    if missing:
        raise RuntimeError("Script.pvf memory preparation incomplete: %s" % ", ".join(missing))
    identity_payload = {
        "path": str(stats.path),
        "source_sha256": str(stats.source_sha256),
        "source_size": int(stats.file_size),
        "file_count": int(stats.file_count),
        "group_count": int(stats.group_count),
        "profile": str(stats.profile_name),
        "directory_cache": str(stats.directory_cache_status),
    }
    emit_timing("pvf_read_end", dict(identity_payload))
    emit_timing("pvf_identity_end", dict(identity_payload))
    directory_event = (
        "pvf_directory_cache_hit"
        if stats.directory_cache_status == "hit"
        else "pvf_directory_cache_miss"
    )
    emit_timing(directory_event, dict(identity_payload))
    warm = warmup_all_runtime_indexes(
        require_preloaded=True,
        status_callback=status_callback,
        timing_callback=timing_callback,
        profile=warmup_profile,
    )
    if not warm.get("ok"):
        raise RuntimeError("PVF domain warmup failed: %s" % (warm.get("error") or "unknown"))
    # drop_index 为 hard domain；再断言一次，避免报告缺域
    drop_dom = (warm.get("domains") or {}).get("drop_index")
    if not drop_dom or not drop_dom.get("ok"):
        raise RuntimeError(
            "drop_index was not warmed at boot (required for monster drops): %s"
            % ((drop_dom or {}).get("error") or "missing domain")
        )
    if residency_mode == "selective":
        stats = pvf_runtime.freeze_script_pvf_selective_runtime(str(PVF_PATH))
    if not pvf_runtime.is_runtime_ready(str(PVF_PATH)):
        raise RuntimeError("Script.pvf runtime was not frozen after domain warmup")
    emit_timing(
        "runtime_frozen",
        {
            "profile": str(stats.profile_name),
            "source_sha256": str(stats.source_sha256),
            "warmup_profile": str(warmup_profile),
            "domain_count": len(warm.get("domains") or {}),
            "resident_group_count": int(
                getattr(stats, "resident_group_count", 0)
            ),
        },
    )
    return stats, warm


def prepare_script_pvf_for_lazy_start() -> Any:
    """Prepare the verified shared reader without blocking listeners on warmup."""
    from pvf import pvf_runtime

    stats = pvf_runtime.prepare_script_pvf_selective_runtime(str(PVF_PATH))
    required = ("memory_backed", "path_index_ready")
    missing = [name for name in required if not getattr(stats, name, False)]
    reader = pvf_runtime.get_shared_reader(str(PVF_PATH))
    if reader is None or not getattr(reader, "name_pools_ready", False):
        missing.append("shared_memory_structure")
    if missing:
        raise RuntimeError("Script.pvf lazy preparation incomplete: %s" % ", ".join(missing))
    return stats


def finish_lazy_script_pvf_warmup(
    *,
    status_callback: Optional[Callable[[str, dict[str, Any]], None]] = None,
    timing_callback: Optional[Callable[[str, dict[str, Any]], None]] = None,
    warmup_profile: str = "core",
) -> tuple[Any, Any]:
    """Use the prepared reader, parallelize group materialization, then warm indexes."""
    from pvf import pvf_runtime
    from pvf.boot_warmup import warmup_all_runtime_indexes

    reader = pvf_runtime.get_shared_reader(str(PVF_PATH))
    if reader is None or not pvf_runtime.is_memory_prepared(str(PVF_PATH)):
        raise RuntimeError("lazy PVF memory reader is unavailable")
    workers = int(os.environ.get("USDOF_PVF_LOAD_WORKERS", "0") or 0)
    reader.preload_all_groups_parallel(workers if workers > 0 else None)
    warm = warmup_all_runtime_indexes(
        require_preloaded=True,
        status_callback=status_callback,
        timing_callback=timing_callback,
        profile=warmup_profile,
    )
    if not warm.get("ok"):
        raise RuntimeError("PVF domain warmup failed: %s" % (warm.get("error") or "unknown"))
    drop_dom = (warm.get("domains") or {}).get("drop_index")
    if not drop_dom or not drop_dom.get("ok"):
        raise RuntimeError(
            "drop_index was not warmed during lazy startup: %s"
            % ((drop_dom or {}).get("error") or "missing domain")
        )
    stats = pvf_runtime.freeze_script_pvf_selective_runtime(str(PVF_PATH))
    if stats is None or not pvf_runtime.is_runtime_ready(str(PVF_PATH)):
        raise RuntimeError("lazy PVF runtime was not frozen")
    return stats, warm


def open_script_pvf_for_quick_start() -> Any:
    """Verify and share Script.pvf without full preload or domain warmup.

    Used only by the Win10 launcher quick-start toggle for repeated PVF edits.
    Runtime reads still go through ``pvf_runtime``'s one shared reader.
    """
    from pvf import pvf_runtime

    stats = pvf_runtime.open_script_pvf_on_demand(str(PVF_PATH))
    reader = pvf_runtime.get_shared_reader(str(PVF_PATH))
    if reader is None:
        raise RuntimeError("quick-start Script.pvf reader unavailable: %s" % PVF_PATH)
    return stats


def format_pvf_preload_line(stats: Any) -> str:
    return (
        "[PVF] prepared Script.pvf "
        f"profile={stats.profile_name} guarded={int(stats.guarded)} "
        f"bridge_required={int(stats.bridge_required)} sha256={stats.source_sha256} "
        f"file_mb={stats.file_size / (1024 * 1024):.1f} "
        f"files={stats.file_count} groups={stats.group_count} "
        f"group_residency={getattr(stats, 'group_residency', 'full')} "
        f"memory_backed={int(stats.memory_backed)} "
        f"groups_preloaded={int(stats.groups_preloaded)} "
        f"path_index_ready={int(stats.path_index_ready)} "
        f"directory_cache={getattr(stats, 'directory_cache_status', 'uninitialized')} "
        f"runtime_frozen={int(stats.runtime_frozen)} "
        f"resident_groups={int(getattr(stats, 'resident_group_count', 0))}/{stats.group_count} "
        f"resident_decompressed_mb="
        f"{getattr(stats, 'resident_decompressed_bytes', stats.decompressed_bytes) / (1024 * 1024):.1f} "
        f"runtime_group_loads={int(getattr(stats, 'runtime_group_load_count', 0))} "
        f"elapsed_ms={stats.elapsed_ms}"
    )


def format_pvf_quick_start_line(stats: Any) -> str:
    return (
        "[PVF] quick-start opened Script.pvf "
        f"profile={stats.profile_name} guarded={int(stats.guarded)} "
        f"bridge_required={int(stats.bridge_required)} sha256={stats.source_sha256} "
        f"file_mb={stats.file_size / (1024 * 1024):.1f} "
        f"files={stats.file_count} groups={stats.group_count} "
        "mode=on_demand group_cache=16 "
        f"elapsed_ms={stats.elapsed_ms}"
    )
