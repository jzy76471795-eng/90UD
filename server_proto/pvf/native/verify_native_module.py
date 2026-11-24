from __future__ import annotations

import importlib.util
import pathlib
import struct
import sys


EXPECTED_ABI = 1
EXPECTED_NAME = "usdof_pvf_native.cp312-win_amd64.pyd"
SERVER_ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULE_PATH = SERVER_ROOT / EXPECTED_NAME


def _pe_machine(path: pathlib.Path) -> int:
    raw = path.read_bytes()
    if len(raw) < 0x40 or raw[:2] != b"MZ":
        raise RuntimeError("native PVF module is not a PE image")
    pe_offset = struct.unpack_from("<I", raw, 0x3C)[0]
    if pe_offset + 6 > len(raw) or raw[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise RuntimeError("native PVF module has an invalid PE header")
    return struct.unpack_from("<H", raw, pe_offset + 4)[0]


def verify() -> dict[str, object]:
    if sys.implementation.name != "cpython" or sys.version_info[:3] != (3, 12, 10):
        raise RuntimeError(
            "native PVF verification requires CPython 3.12.10; actual=%s"
            % sys.version.split()[0]
        )
    if struct.calcsize("P") != 8:
        raise RuntimeError("native PVF verification requires a 64-bit interpreter")
    if MODULE_PATH.name != EXPECTED_NAME or not MODULE_PATH.is_file():
        raise RuntimeError("missing native PVF module: %s" % MODULE_PATH)
    machine = _pe_machine(MODULE_PATH)
    if machine != 0x8664:
        raise RuntimeError(
            "native PVF module PE machine mismatch: expected=0x8664 actual=0x%04x"
            % machine
        )
    spec = importlib.util.spec_from_file_location("usdof_pvf_native", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("native PVF module import spec is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if pathlib.Path(module.__file__).resolve() != MODULE_PATH:
        raise RuntimeError("native PVF module resolved from an unexpected path")
    if module.abi_version() != EXPECTED_ABI:
        raise RuntimeError("native PVF ABI mismatch")
    plain = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    expected = bytes.fromhex("3f5aebb0a5e50bcb9dbe9c5008ca3f17")
    if module.decrypt_lcg(plain, 0x12345678, 2531011) != expected:
        raise RuntimeError("native PVF LCG smoke vector mismatch")
    utf8_pool = b"\0folder\0entry.stk\0name\0"
    entry = struct.pack("<iiiiii", 16, 2, 3, 4, 5, 1)
    directory = module.build_directory(entry, utf8_pool, b"\0\0")
    if len(directory) != 1 or directory.lookup("FOLDER\\ENTRY.STK") != 0:
        raise RuntimeError("native PVF directory smoke vector mismatch")
    restored = module.load_directory(module.dump_directory(directory))
    if restored.path_at(0) != "folder/entry.stk":
        raise RuntimeError("native PVF directory round-trip mismatch")
    token = struct.pack("<Bi", 3, 36)
    if module.decode_token_text(token, utf8_pool, b"\0\0") != "[name]":
        raise RuntimeError("native PVF token smoke vector mismatch")
    return {
        "module": str(MODULE_PATH),
        "abi": module.abi_version(),
        "machine": "AMD64",
    }


if __name__ == "__main__":
    print(verify())
