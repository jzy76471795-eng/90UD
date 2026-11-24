# -*- coding: utf-8 -*-
"""加载 hyphen 文件名模块（opcode / game 域 / channel flows）。

查找规则：
- area=\"opcodes\"：仅 ``server_proto/opcodes/``
- area=\"game\"：依次 ``opcodes/``、``game/quest|character|dungeon/``
- area=\"channel\"：``channel/flows/``

注：目录名用 ``opcodes``（复数），避免遮蔽 Python 标准库 ``opcode``。

文件名全局唯一；跨目录禁止同名。

分享包加载顺序：
1. 加密内存库（``usdof_protect_runtime``，若已 install）
2. 磁盘 ``name.py``
3. 同级 ``name.pyc``
4. ``__pycache__/name.cpython-*.pyc``
"""
from __future__ import annotations

import importlib.util
import importlib.machinery
import os
import sys
from pathlib import Path
from typing import Any, Optional


def _server_root() -> Path:
    env = (os.environ.get("USDOF_SERVER_HOME") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


_ROOT = _server_root()
_OPCODE = _ROOT / "opcodes"
_GAME_DOMAIN_ROOTS = [
    _ROOT / "game" / "quest",
    _ROOT / "game" / "character",
    _ROOT / "game" / "dungeon",
    _ROOT / "game" / "drop",
    _ROOT / "game" / "titlebook",
    _ROOT / "game" / "shop",
]
_CHANNEL = _ROOT / "channel" / "flows"
_CACHE: dict[str, Any] = {}


def _search_roots(area: str) -> list[Path]:
    if area in ("opcodes", "opcode"):
        return [_OPCODE]
    if area == "channel":
        return [_CHANNEL]
    if area == "game":
        return [_OPCODE, *_GAME_DOMAIN_ROOTS]
    raise ValueError("unknown load_chain area: %r" % (area,))


def _rel_search_roots(area: str) -> list[str]:
    """相对 server 根的 posix 路径（加密库用）。"""
    if area in ("opcodes", "opcode"):
        return ["opcodes"]
    if area == "channel":
        return ["channel/flows"]
    if area == "game":
        return [
            "opcodes",
            "game/quest",
            "game/character",
            "game/dungeon",
            "game/drop",
            "game/titlebook",
            "game/shop",
        ]
    raise ValueError("unknown load_chain area: %r" % (area,))


def _resolve_module_file(base: Path, module_filename: str) -> Optional[Path]:
    """解析 hyphen 模块路径：.py → 同级 .pyc → __pycache__。"""
    stem = Path(module_filename).stem
    for suffix in importlib.machinery.EXTENSION_SUFFIXES:
        native = base / (stem.replace("-", "_") + suffix)
        if native.is_file():
            return native
    direct = base / module_filename
    if direct.is_file():
        return direct
    stem = Path(module_filename).stem
    sibling_pyc = base / (stem + ".pyc")
    if sibling_pyc.is_file():
        return sibling_pyc
    cache_dir = base / "__pycache__"
    if cache_dir.is_dir():
        matches = sorted(cache_dir.glob("%s.cpython-*.pyc" % stem), reverse=True)
        if matches:
            return matches[0]
    return None


def _try_protected_load(module_filename: str, area: str, mod_name: str):
    """加密分享包：从内存 blob 加载 hyphen 模块。"""
    try:
        import usdof_protect_runtime as protect  # type: ignore
    except Exception:
        return None
    if not getattr(protect, "_STORE", None):
        return None
    roots = _rel_search_roots(area)
    stem = Path(module_filename).stem
    for root in roots:
        for rel in (
            "%s/%s.pyc" % (root, stem),
            "%s/%s" % (root, module_filename),
        ):
            nrel = rel.replace("\\", "/").lstrip("/").lower()
            if nrel.endswith(".py"):
                nrel = nrel[:-3] + ".pyc"
            if not nrel.endswith(".pyc"):
                continue
            if protect.has_pyc(nrel):
                return protect.exec_pyc_module(mod_name, nrel, is_package=False)
    return None


def load_chain(module_filename: str, *, area: str = "game"):
    key = "%s:%s" % (area, module_filename)
    if key in _CACHE:
        return _CACHE[key]

    mod_name = "usdof.%s.chains.%s" % (area, Path(module_filename).stem.replace("-", "_"))
    if mod_name in sys.modules:
        module = sys.modules[mod_name]
        _CACHE[key] = module
        return module

    # 1) 加密内存库（分享包默认）
    protected = _try_protected_load(module_filename, area, mod_name)
    if protected is not None:
        _CACHE[key] = protected
        return protected

    # 2) 磁盘 .py / .pyc（开发态 / 旧式分享包）
    path: Optional[Path] = None
    for base in _search_roots(area):
        found = _resolve_module_file(base, module_filename)
        if found is not None:
            path = found
            break
    if path is None:
        roots = ", ".join(str(p) for p in _search_roots(area))
        raise FileNotFoundError("%s (searched: %s)" % (module_filename, roots))

    if path.suffix == ".pyd":
        # The same opcode may be requested through game and opcodes aliases.
        canonical_area = "channel" if area == "channel" else "game"
        canonical = "usdof.%s.chains.%s" % (canonical_area, Path(module_filename).stem.replace("-", "_"))
        if canonical in sys.modules:
            module = sys.modules[canonical]
            sys.modules[mod_name] = module
            _CACHE[key] = module
            return module
    else:
        canonical = mod_name
    spec = importlib.util.spec_from_file_location(canonical, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load %s" % path)
    module = importlib.util.module_from_spec(spec)
    # dataclasses require the module to exist in sys.modules during class body exec
    sys.modules[canonical] = module
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(canonical, None)
        sys.modules.pop(mod_name, None)
        raise
    _CACHE[key] = module
    return module


def load_quest_chain(module_filename: str):
    return load_chain(module_filename, area="game")


def load_opcode(module_filename: str):
    """仅从顶级 opcodes/ 加载协议处理模块。"""
    return load_chain(module_filename, area="opcodes")
