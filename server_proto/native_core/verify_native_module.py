from __future__ import annotations

import pathlib
import random
import struct
import sys


SERVER_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from native_core_loader import native_core_status, require_native_core_module


def main() -> int:
    module = require_native_core_module()
    status = native_core_status("shadow")
    if status["mode"] != "shadow" or not status["loaded"]:
        raise RuntimeError("native core status smoke failed: %r" % status)

    rng = random.Random(0x5553444F46)
    for count in (0, 1, 2, 7, 64, 1024):
        values = tuple(rng.getrandbits(32) for _ in range(count))
        expected = struct.pack("<%dI" % count, *values)
        actual = module.pack_u32le(values)
        if actual != expected:
            raise RuntimeError("native core pack_u32le differential mismatch")
        if module.unpack_u32le(actual) != values:
            raise RuntimeError("native core unpack_u32le differential mismatch")

    parts = (b"USD", bytearray(b"OF"), memoryview(b"-native-core"))
    if module.concat_buffers(parts) != b"USDOF-native-core":
        raise RuntimeError("native core concat_buffers smoke mismatch")
    if module.build_game_header(1, 0x1234, 0x20, 0x78563412, 0x4321, b"abc", 0) != bytes.fromhex(
        "01341220000000123456782143616263"
    ):
        raise RuntimeError("native core game header smoke mismatch")

    print(
        "native core verified: path=%s abi=%s build=%s python=%s machine=0x%04x"
        % (
            status["path"],
            status["abi_version"],
            status["build_version"],
            status["python"],
            status["pe_machine"],
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
