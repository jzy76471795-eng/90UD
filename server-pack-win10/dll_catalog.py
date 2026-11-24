# -*- coding: utf-8 -*-
"""分享包内置 DLL 清单（与仓库根 ``启动游戏.cmd`` + ``launch_game_session`` 对齐）。

路径一律相对 runtime/server（即 server_home），禁止写死盘符。
列表顺序 = 注入顺序。

对齐规则（2026-08-10）：
  - 内置清单顺序：func_extend -> codec ->
    debug_window（默认关闭） -> desktop_focus_guard -> forbidden_func ->
    weapon_avatar_dungeon -> native_loadout
  - 汉化补丁不再进入分享包内置清单；玩家配置的外置 DLL（包括
    ``usdof_cn_fix.dll``）按保存顺序追加在内置注入项之后
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import List, Set


HARDENING_CONTRACT = (
    b"USDOF_HARDENING_V2:PIN_FAIL_CLOSED:INSTALL_ONCE:DETACH_RESTORE:MODULE_SIBLING_LOG"
)
HARDENING_REQUIRED_STRINGS = (
    HARDENING_CONTRACT,
    b"OK module pinned",
    b"install STOP module is not pinned",
)


def _read_c_string(data: bytes, offset: int) -> str:
    if offset < 0 or offset >= len(data):
        return ""
    end = data.find(b"\0", offset)
    if end < 0:
        return ""
    return data[offset:end].decode("ascii", errors="ignore")


def _pe_imports(data: bytes) -> Set[str]:
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise ValueError("missing DOS signature")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_offset + 24 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise ValueError("missing PE signature")
    machine, section_count, _time, _sym, _sym_count, optional_size, characteristics = (
        struct.unpack_from("<HHIIIHH", data, pe_offset + 4)
    )
    if machine != 0x014C:
        raise ValueError("not PE32 x86")
    if characteristics & 0x2000 == 0:
        raise ValueError("PE is not a DLL")
    optional_offset = pe_offset + 24
    if optional_size < 104 or optional_offset + optional_size > len(data):
        raise ValueError("invalid optional header")
    if struct.unpack_from("<H", data, optional_offset)[0] != 0x010B:
        raise ValueError("not a PE32 optional header")
    import_rva, import_size = struct.unpack_from("<II", data, optional_offset + 104)
    if import_rva == 0 or import_size < 20:
        return set()

    sections = []
    section_offset = optional_offset + optional_size
    for index in range(section_count):
        current = section_offset + index * 40
        if current + 40 > len(data):
            raise ValueError("truncated section table")
        virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from(
            "<IIII", data, current + 8
        )
        sections.append((virtual_address, max(virtual_size, raw_size), raw_offset, raw_size))

    def rva_to_offset(rva: int) -> int:
        for virtual_address, mapped_size, raw_offset, raw_size in sections:
            if virtual_address <= rva < virtual_address + mapped_size:
                delta = rva - virtual_address
                if delta >= raw_size or raw_offset + delta >= len(data):
                    break
                return raw_offset + delta
        raise ValueError("RVA outside file-backed sections")

    imports: Set[str] = set()
    descriptor_offset = rva_to_offset(import_rva)
    descriptor_limit = min(len(data), descriptor_offset + import_size)
    while descriptor_offset + 20 <= descriptor_limit:
        original_first_thunk, _stamp, _chain, name_rva, first_thunk = struct.unpack_from(
            "<IIIII", data, descriptor_offset
        )
        if not any((original_first_thunk, name_rva, first_thunk)):
            break
        module_name = _read_c_string(data, rva_to_offset(name_rva)).casefold()
        thunk_rva = original_first_thunk or first_thunk
        thunk_offset = rva_to_offset(thunk_rva)
        for index in range(65536):
            entry_offset = thunk_offset + index * 4
            if entry_offset + 4 > len(data):
                raise ValueError("truncated import thunk")
            entry = struct.unpack_from("<I", data, entry_offset)[0]
            if entry == 0:
                break
            if entry & 0x80000000:
                continue
            import_name = _read_c_string(data, rva_to_offset(entry) + 2)
            if import_name:
                imports.add("%s!%s" % (module_name, import_name))
        descriptor_offset += 20
    return imports


def hardened_dll_errors(dll_path: Path) -> List[str]:
    try:
        data = Path(dll_path).read_bytes()
    except OSError as exc:
        return ["read failed: %s" % exc]
    errors = [
        "missing marker %s" % marker.decode("ascii", errors="replace")
        for marker in HARDENING_REQUIRED_STRINGS
        if marker not in data
    ]
    try:
        imports = _pe_imports(data)
    except (ValueError, struct.error) as exc:
        errors.append("invalid PE: %s" % exc)
        return errors
    if not any(
        name.endswith("!GetModuleHandleExW") and name.split("!", 1)[0] in {
            "kernel32.dll",
            "kernelbase.dll",
        }
        for name in imports
    ):
        errors.append("missing imported GetModuleHandleExW")
    return errors


def require_hardened_dll(dll_path: Path, label: str) -> None:
    errors = hardened_dll_errors(dll_path)
    if errors:
        raise RuntimeError(
            "内置 DLL 加固校验失败 [%s]: %s; %s"
            % (label, dll_path, "; ".join(errors))
        )


@dataclass(frozen=True)
class DllOption:
    """启动器勾选项。"""

    key: str
    env_name: str
    label: str
    hint: str
    default: bool
    # 相对 server_home 的 DLL 路径（可多候选，按顺序取第一个存在的）
    rel_candidates: tuple


DLL_OPTIONS: List[DllOption] = [
    DllOption(
        key="func_extend",
        env_name="USDOF_FUNC_EXTEND",
        label="客户端功能拓展",
        hint="PVF、原生提示、图片窗与原生视频播放器控制",
        default=True,
        rel_candidates=(
            "patches/usdof_func_extend/build/Release-Win32/usdof_func_extend.dll",
        ),
    ),
    DllOption(
        key="codec_passthrough",
        env_name="USDOF_CODEC_PASSTHROUGH",
        label="Game 全局编解码",
        hint="客户端发包明文；服务端回包保留原生加密（必须）",
        default=True,
        rel_candidates=(
            "patches/usdof_codec_passthrough/build/Release-Win32/usdof_codec_passthrough.dll",
        ),
    ),
    DllOption(
        key="debug_window",
        env_name="USDOF_DEBUG_WINDOW",
        label="实时调试窗口",
        hint="显示服务器数据流、PVF 预热和 NPK 资源初始化",
        default=False,
        rel_candidates=(
            "patches/usdof_debug_window/build/Release-Win32/usdof_debug_window.dll",
        ),
    ),
    DllOption(
        key="desktop_focus_guard",
        env_name="USDOF_DESKTOP_FOCUS_GUARD",
        label="桌面焦点保护",
        hint="只屏蔽已验证的客户端本地桌面命令调用点",
        default=True,
        rel_candidates=(
            "patches/usdof_desktop_focus_guard/build/Release-Win32/usdof_desktop_focus_guard.dll",
        ),
    ),
    DllOption(
        key="forbidden_func",
        env_name="USDOF_FORBIDDEN_FUNC",
        label="邮箱入口屏蔽",
        hint="邮箱在本地入口拦截，避免客户端进入尚未实现的等待状态",
        default=True,
        rel_candidates=(
            "patches/forbidden-func/build/Release-Win32/forbidden-func.dll",
        ),
    ),
    DllOption(
        key="weapon_avatar_dungeon",
        env_name="USDOF_WEAPON_AVATAR_DUNGEON",
        label="副本武器装扮模型修复",
        hint="首次进入副本后提交已穿戴武器装扮的原生渲染对象",
        default=True,
        rel_candidates=(
            "patches/usdof_weapon_avatar_dungeon/build/Release-Win32/"
            "usdof_weapon_avatar_dungeon.dll",
        ),
    ),
    DllOption(
        key="native_loadout",
        env_name="USDOF_NATIVE_LOADOUT",
        label="副本快捷换装原生移动",
        hint="GM 快捷换装在副本内按客户端原生路径逐件提交",
        default=True,
        rel_candidates=(
            "patches/usdof_native_loadout/build/Release-Win32/usdof_native_loadout.dll",
        ),
    ),
]

# 相对 server_home（通用 multi-DLL LoadLibrary 注入器）
INJECT_LAUNCHER_REL = (
    "patches/client_injector/build/Release-Win32/launch_usdof_client.exe"
)
