from __future__ import annotations

import importlib.util
import pathlib
import struct
import sys
import threading
from types import ModuleType
from typing import Optional


NATIVE_ABI_VERSION = 1
NATIVE_MODULE_FILENAME = "usdof_pvf_native.cp312-win_amd64.pyd"
PE_MACHINE_AMD64 = 0x8664
_SERVER_ROOT = pathlib.Path(__file__).resolve().parents[1]
NATIVE_MODULE_PATH = _SERVER_ROOT / NATIVE_MODULE_FILENAME
_LOCK = threading.RLock()
_MODULE: Optional[ModuleType] = None


class NativePvfModuleError(RuntimeError):
    pass


def _pe_machine(path: pathlib.Path) -> int:
    try:
        with path.open("rb") as stream:
            dos = stream.read(0x40)
            if len(dos) != 0x40 or dos[:2] != b"MZ":
                raise NativePvfModuleError("native PVF module is not a PE image")
            pe_offset = struct.unpack_from("<I", dos, 0x3C)[0]
            stream.seek(pe_offset)
            header = stream.read(6)
    except OSError as exc:
        raise NativePvfModuleError(
            "native PVF module cannot be read: %s: %s" % (path, exc)
        ) from exc
    if len(header) != 6 or header[:4] != b"PE\0\0":
        raise NativePvfModuleError("native PVF module has an invalid PE header")
    return struct.unpack_from("<H", header, 4)[0]


def _interpreter_gate() -> None:
    actual = "%s %s %s-bit" % (
        sys.implementation.name,
        sys.version.split()[0],
        struct.calcsize("P") * 8,
    )
    if (
        sys.implementation.name != "cpython"
        or sys.version_info[:3] != (3, 12, 10)
        or struct.calcsize("P") != 8
    ):
        raise NativePvfModuleError(
            "native PVF module requires CPython 3.12.10 x64; actual=%s" % actual
        )


def _smoke_test(module: ModuleType) -> None:
    if int(module.abi_version()) != NATIVE_ABI_VERSION:
        raise NativePvfModuleError(
            "native PVF ABI mismatch: expected=%d actual=%r path=%s"
            % (NATIVE_ABI_VERSION, module.abi_version(), NATIVE_MODULE_PATH)
        )
    plain = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    expected = bytes.fromhex("3f5aebb0a5e50bcb9dbe9c5008ca3f17")
    if module.decrypt_lcg(plain, 0x12345678, 2531011) != expected:
        raise NativePvfModuleError("native PVF LCG smoke vector mismatch")
    utf8_pool = b"\0folder\0entry.stk\0name\0"
    directory = module.build_directory(
        struct.pack("<iiiiii", 16, 2, 3, 4, 5, 1),
        utf8_pool,
        b"\0\0",
    )
    if len(directory) != 1 or directory.lookup("FOLDER\\ENTRY.STK") != 0:
        raise NativePvfModuleError("native PVF directory smoke vector mismatch")
    restored = module.load_directory(module.dump_directory(directory))
    if restored.path_at(0) != "folder/entry.stk":
        raise NativePvfModuleError("native PVF directory cache smoke mismatch")
    if module.decode_token_text(
        struct.pack("<Bi", 3, 36), utf8_pool, b"\0\0"
    ) != "[name]":
        raise NativePvfModuleError("native PVF token smoke vector mismatch")


def require_native_module() -> ModuleType:
    global _MODULE
    with _LOCK:
        if _MODULE is not None:
            return _MODULE
        _interpreter_gate()
        path = NATIVE_MODULE_PATH.resolve()
        if path.name != NATIVE_MODULE_FILENAME or not path.is_file():
            raise NativePvfModuleError(
                "native PVF module is missing: expected=%s" % path
            )
        machine = _pe_machine(path)
        if machine != PE_MACHINE_AMD64:
            raise NativePvfModuleError(
                "native PVF PE machine mismatch: expected=0x8664 actual=0x%04x path=%s"
                % (machine, path)
            )
        spec = importlib.util.spec_from_file_location("usdof_pvf_native", path)
        if spec is None or spec.loader is None:
            raise NativePvfModuleError(
                "native PVF import spec is unavailable: %s" % path
            )
        try:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:
            raise NativePvfModuleError(
                "native PVF module import failed: %s: %s" % (path, exc)
            ) from exc
        actual_path = pathlib.Path(str(module.__file__)).resolve()
        if actual_path != path:
            raise NativePvfModuleError(
                "native PVF module path mismatch: expected=%s actual=%s"
                % (path, actual_path)
            )
        _smoke_test(module)
        _MODULE = module
        return module


def native_module_status() -> dict[str, object]:
    module = require_native_module()
    return {
        "path": str(pathlib.Path(str(module.__file__)).resolve()),
        "abi_version": int(module.abi_version()),
        "pe_machine": PE_MACHINE_AMD64,
        "python": sys.version.split()[0],
    }


__all__ = [
    "NATIVE_ABI_VERSION",
    "NATIVE_MODULE_FILENAME",
    "NATIVE_MODULE_PATH",
    "NativePvfModuleError",
    "native_module_status",
    "require_native_module",
]
