from __future__ import annotations

import importlib.util
import math
import operator
import os
import pathlib
import struct
import sys
import threading
import time
from collections import deque
from types import ModuleType
from typing import Any, Callable, Deque, Dict, Iterable, Optional, Sequence, Tuple


NATIVE_CORE_ABI_VERSION = 10
NATIVE_CORE_MODULE_FILENAME = "usdof_core.cp312-win_amd64.pyd"
PE_MACHINE_AMD64 = 0x8664
_SERVER_ROOT = pathlib.Path(__file__).resolve().parent
NATIVE_CORE_MODULE_PATH = _SERVER_ROOT / NATIVE_CORE_MODULE_FILENAME
_MODULE_LOCK = threading.RLock()
_RUNTIME_LOCK = threading.RLock()
_MODULE = None  # type: Optional[ModuleType]
_RUNTIME = None  # type: Optional["NativeCoreRuntime"]

_CHARACTER_FLOW_OPERATION_IDS = {
    0x0004: 1,
    0x0005: 2,
    0x0006: 3,
    0x0007: 4,
    0x0008: 5,
    0x0127: 6,
    0x02B5: 7,
    0x02C1: 8,
    0x02A8: 9,
    0x01BA: 10,
    0x0355: 11,
    0x057F: 12,
}


class NativeCoreError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stage: str,
        operation: str = "",
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.stage = str(stage)
        self.operation = str(operation)
        self.context = dict(context or {})
        fields = ["stage=%s" % self.stage]
        if self.operation:
            fields.append("operation=%s" % self.operation)
        fields.extend(
            "%s=%s" % (key, self.context[key]) for key in sorted(self.context)
        )
        super().__init__("native core %s: %s" % (" ".join(fields), message))


class NativeCoreMismatchError(NativeCoreError):
    pass


def _pe_machine(path: pathlib.Path) -> int:
    try:
        with path.open("rb") as stream:
            dos = stream.read(0x40)
            if len(dos) != 0x40 or dos[:2] != b"MZ":
                raise NativeCoreError(
                    "module is not a PE image",
                    stage="pe_gate",
                    context={"path": path},
                )
            pe_offset = struct.unpack_from("<I", dos, 0x3C)[0]
            stream.seek(pe_offset)
            header = stream.read(6)
    except OSError as exc:
        raise NativeCoreError(
            "module cannot be read: %s" % exc,
            stage="pe_gate",
            context={"path": path},
        ) from exc
    if len(header) != 6 or header[:4] != b"PE\0\0":
        raise NativeCoreError(
            "module has an invalid PE header",
            stage="pe_gate",
            context={"path": path},
        )
    return int(struct.unpack_from("<H", header, 4)[0])


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
        raise NativeCoreError(
            "requires CPython 3.12.10 x64; actual=%s" % actual,
            stage="python_gate",
        )


def _smoke_test(module: ModuleType) -> None:
    actual_abi = int(module.abi_version())
    if actual_abi != NATIVE_CORE_ABI_VERSION:
        raise NativeCoreError(
            "ABI mismatch",
            stage="abi_gate",
            context={"expected": NATIVE_CORE_ABI_VERSION, "actual": actual_abi},
        )
    build = dict(module.build_info())
    if int(build.get("abi_version", -1)) != NATIVE_CORE_ABI_VERSION:
        raise NativeCoreError(
            "build metadata ABI mismatch",
            stage="smoke",
            context={"build": build},
        )
    values = (0, 1, 0x78563412, 0xFFFFFFFF)
    packed = bytes.fromhex("000000000100000012345678ffffffff")
    if module.pack_u32le(values) != packed:
        raise NativeCoreError("pack_u32le vector mismatch", stage="smoke")
    userinfo1_row = (
        7,
        0x11223344,
        0x55667788,
        0x99,
        0xAABB,
        0xCCDDEEFF,
        0x01020304,
        5,
        6,
        0x7788,
        0,
        0,
        b"",
    )
    userinfo1_expected = (
        b"\x01"
        + struct.pack(
            "<BIIBHIIBBH",
            7,
            0x11223344,
            0x55667788,
            0x99,
            0xAABB,
            0xCCDDEEFF,
            0x01020304,
            5,
            6,
            0x7788,
        )
        + (b"\x00" * 29)
    )
    if module.pack_userinfo1_rows((userinfo1_row,)) != userinfo1_expected:
        raise NativeCoreError("pack_userinfo1_rows vector mismatch", stage="smoke")
    if module.character_flow_dispatch(0x0004, b"\x03\x00") != (1, 1):
        raise NativeCoreError("character_flow_dispatch handled mismatch", stage="smoke")
    if module.character_flow_dispatch(0x0127, bytes(8)) != (6, 0):
        raise NativeCoreError("character_flow_dispatch vector mismatch", stage="smoke")
    if module.character_flow_dispatch(0xFFFF, b"") != (0, 0):
        raise NativeCoreError("character_flow_dispatch unknown mismatch", stage="smoke")
    if module.plan_select_character(b"\x03\x00", (0, 3, 7)) != (3, 1, 1, 1):
        raise NativeCoreError("plan_select_character exact-slot mismatch", stage="smoke")
    if module.plan_select_character(b"", (4, 9)) != (0, 0, 0, 2):
        raise NativeCoreError("plan_select_character index fallback mismatch", stage="smoke")
    if module.unpack_u32le(packed) != values:
        raise NativeCoreError("unpack_u32le vector mismatch", stage="smoke")
    if module.concat_buffers((b"USD", bytearray(b"OF"), memoryview(b"-core"))) != b"USDOF-core":
        raise NativeCoreError("concat_buffers vector mismatch", stage="smoke")
    if module.build_game_header(1, 0x1234, 0x20, 0x78563412, 0x4321, b"abc", 0) != bytes.fromhex(
        "01341220000000123456782143616263"
    ):
        raise NativeCoreError("build_game_header vector mismatch", stage="smoke")
    if module.parse_game_header(bytes.fromhex("01341220000000123456782143616263")) != (
        1,
        0x1234,
        0x20,
        0x78563412,
        0x4321,
        16,
    ):
        raise NativeCoreError("parse_game_header vector mismatch", stage="smoke")
    aes_key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    aes_plain = bytes.fromhex("00112233445566778899aabbccddeeff")
    aes_cipher = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
    if module.aes128_ecb_encrypt(aes_plain, aes_key) != aes_cipher:
        raise NativeCoreError("aes128_ecb_encrypt vector mismatch", stage="smoke")
    if module.aes128_ecb_decrypt(aes_cipher, aes_key) != aes_plain:
        raise NativeCoreError("aes128_ecb_decrypt vector mismatch", stage="smoke")
    codec2_key = bytes.fromhex(
        "ac d4 ff c4 18 e4 14 f9 59 ba 09 fe e8 7c b6 55 "
        "07 f3 1a 13 49 f4 fe f2 54 1f d5 a1 e6 cd e7 5e "
        "16 43 ae c7 dd 2c 80 ed d4 46 6f ab 3f 5b 6e 36 "
        "4b 73 b5 f4 94 48 66 92 ea fa 70 51"
    )
    codec2_state = bytes.fromhex(
        "ac d4 ff c4 18 e4 14 f9 59 ba 09 fe e8 7c b6 55 "
        "07 f3 1a 13 49 f4 fe f2 54 1f d5 a1 e6 cd e7 5e "
        "6a 44 39 91 b1 c2 4b 86 2e cb 37 87 58 06 c3 af "
        "62 86 0d 94 1a 9e 91 b4 ad ff d3 14 b1 96 7e 42 "
        "56 17 d3 1b 79 2d 00 1d 09 49 9a 20"
    )
    if module.codec2_build_state(codec2_key) != codec2_state:
        raise NativeCoreError("codec2_build_state vector mismatch", stage="smoke")
    codec2_plain = bytes.fromhex("00 00 00 00 11 22 33 44 55 66 77 88 99 aa bb cc")
    codec2_cipher = bytes.fromhex("30 69 f8 6e 76 92 46 b7 ae 8b 15 c5 35 1c 14 1b")
    if module.codec2_encode(codec2_state, codec2_plain) != codec2_cipher:
        raise NativeCoreError("codec2_encode vector mismatch", stage="smoke")
    if module.codec2_decode(codec2_state, codec2_cipher) != codec2_plain:
        raise NativeCoreError("codec2_decode vector mismatch", stage="smoke")
    codec13_key = bytes.fromhex(
        "00 11 22 33 44 55 66 77 88 99 aa bb cc dd ee ff "
        "10 32 54 76 98 ba dc fe 12 34 56 78 9a bc de f0 "
        "0f ed cb a9 87 65 43 21"
    )
    codec13_state = module.codec13_build_state(codec13_key)
    codec13_plain = bytes.fromhex(
        "00 00 00 00 11 22 33 44 55 66 77 88 99 aa bb cc"
    )
    codec13_cipher = module.codec13_encode(codec13_state, codec13_plain)
    if module.codec13_decode(codec13_state, codec13_cipher) != codec13_plain:
        raise NativeCoreError("codec13 encode/decode vector mismatch", stage="smoke")


def require_native_core_module() -> ModuleType:
    global _MODULE
    with _MODULE_LOCK:
        if _MODULE is not None:
            return _MODULE
        _interpreter_gate()
        path = NATIVE_CORE_MODULE_PATH.resolve()
        if path.name != NATIVE_CORE_MODULE_FILENAME or not path.is_file():
            raise NativeCoreError(
                "module is missing",
                stage="path_gate",
                context={"expected": path},
            )
        machine = _pe_machine(path)
        if machine != PE_MACHINE_AMD64:
            raise NativeCoreError(
                "PE machine mismatch",
                stage="pe_gate",
                context={
                    "expected": "0x8664",
                    "actual": "0x%04x" % machine,
                    "path": path,
                },
            )
        spec = importlib.util.spec_from_file_location("usdof_core", path)
        if spec is None or spec.loader is None:
            raise NativeCoreError(
                "import spec is unavailable",
                stage="import",
                context={"path": path},
            )
        try:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:
            raise NativeCoreError(
                "import failed: %s: %s" % (type(exc).__name__, exc),
                stage="import",
                context={"path": path},
            ) from exc
        actual_path = pathlib.Path(str(module.__file__)).resolve()
        if actual_path != path:
            raise NativeCoreError(
                "module path mismatch",
                stage="path_gate",
                context={"expected": path, "actual": actual_path},
            )
        _smoke_test(module)
        _MODULE = module
        return module


def normalize_native_core_mode(value: Optional[str] = None) -> str:
    raw = os.environ.get("USDOF_NATIVE_CORE", "shadow") if value is None else value
    mode = str(raw).strip().lower()
    aliases = {
        "0": "0",
        "off": "0",
        "python": "0",
        "shadow": "shadow",
        "compare": "shadow",
        "1": "1",
        "on": "1",
        "native": "1",
    }
    if mode not in aliases:
        raise NativeCoreError(
            "invalid USDOF_NATIVE_CORE mode",
            stage="mode",
            context={"value": raw},
        )
    return aliases[mode]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


def _percentile_ns(values: Sequence[int], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(int(value) for value in values)
    rank = max(0, int(math.ceil(percentile / 100.0 * len(ordered))) - 1)
    return float(ordered[min(rank, len(ordered) - 1)]) / 1_000_000.0


class _OperationMetrics:
    def __init__(self, sample_limit: int) -> None:
        self.calls = 0
        self.input_bytes = 0
        self.output_bytes = 0
        self.mismatches = 0
        self.errors = 0
        self.fallbacks = 0
        self.python_wall_ns = deque(maxlen=sample_limit)  # type: Deque[int]
        self.python_cpu_ns = deque(maxlen=sample_limit)  # type: Deque[int]
        self.native_wall_ns = deque(maxlen=sample_limit)  # type: Deque[int]
        self.native_cpu_ns = deque(maxlen=sample_limit)  # type: Deque[int]

    @staticmethod
    def _timing(values: Sequence[int]) -> Dict[str, float]:
        if not values:
            return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
        return {
            "p50": round(_percentile_ns(values, 50.0), 6),
            "p95": round(_percentile_ns(values, 95.0), 6),
            "p99": round(_percentile_ns(values, 99.0), 6),
            "max": round(float(max(values)) / 1_000_000.0, 6),
        }

    def snapshot(self) -> Dict[str, Any]:
        return {
            "calls": self.calls,
            "input_bytes": self.input_bytes,
            "output_bytes": self.output_bytes,
            "mismatches": self.mismatches,
            "errors": self.errors,
            "fallbacks": self.fallbacks,
            "python_wall_ms": self._timing(self.python_wall_ns),
            "python_cpu_ms": self._timing(self.python_cpu_ns),
            "native_wall_ms": self._timing(self.native_wall_ns),
            "native_cpu_ms": self._timing(self.native_cpu_ns),
        }


class NativeCoreRuntime:
    def __init__(
        self,
        mode: Optional[str] = None,
        *,
        module: Optional[ModuleType] = None,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.mode = normalize_native_core_mode(mode)
        self.module = module
        if self.mode != "0" and self.module is None:
            self.module = require_native_core_module()
        self.fallback_enabled = _env_bool("USDOF_NATIVE_CORE_FALLBACK", True)
        self.summary_every = max(
            1,
            int(os.environ.get("USDOF_NATIVE_CORE_SUMMARY_EVERY", "100") or 100),
        )
        self.sample_limit = max(
            16,
            int(os.environ.get("USDOF_NATIVE_CORE_SAMPLE_LIMIT", "2048") or 2048),
        )
        self.logger = logger
        self._lock = threading.RLock()
        self._metrics = {}  # type: Dict[str, _OperationMetrics]

    def set_logger(self, logger: Optional[Callable[[str], None]]) -> None:
        self.logger = logger

    def _operation_metrics(self, operation: str) -> _OperationMetrics:
        with self._lock:
            metric = self._metrics.get(operation)
            if metric is None:
                metric = _OperationMetrics(self.sample_limit)
                self._metrics[operation] = metric
            return metric

    @staticmethod
    def _measure(call: Callable[[], Any]) -> Tuple[Any, int, int]:
        wall_started = time.perf_counter_ns()
        cpu_started = time.process_time_ns()
        try:
            result = call()
        finally:
            wall_elapsed = max(0, time.perf_counter_ns() - wall_started)
            cpu_elapsed = max(0, time.process_time_ns() - cpu_started)
        return result, wall_elapsed, cpu_elapsed

    @staticmethod
    def _output_size(result: Any) -> int:
        try:
            return int(memoryview(result).nbytes)
        except TypeError:
            try:
                return len(result)
            except (TypeError, ValueError):
                return 0

    def _log(self, message: str) -> None:
        logger = self.logger
        if logger is not None:
            logger("[NativeCore] %s" % message)

    def _maybe_log_summary(self, operation: str, metric: _OperationMetrics) -> None:
        if metric.calls % self.summary_every != 0:
            return
        snapshot = metric.snapshot()
        self._log(
            "summary mode=%s operation=%s calls=%d input=%d output=%d "
            "mismatch=%d error=%d fallback=%d python_p95=%.6fms native_p95=%.6fms"
            % (
                self.mode,
                operation,
                snapshot["calls"],
                snapshot["input_bytes"],
                snapshot["output_bytes"],
                snapshot["mismatches"],
                snapshot["errors"],
                snapshot["fallbacks"],
                snapshot["python_wall_ms"]["p95"],
                snapshot["native_wall_ms"]["p95"],
            )
        )

    def execute(
        self,
        operation: str,
        python_call: Callable[[], Any],
        native_call: Callable[[], Any],
        *,
        input_bytes: int = 0,
        output_bytes: Optional[int] = None,
    ) -> Any:
        operation = str(operation)
        metric = self._operation_metrics(operation)
        with self._lock:
            metric.calls += 1
            metric.input_bytes += max(0, int(input_bytes))

        if self.mode == "0":
            result, wall_ns, cpu_ns = self._measure(python_call)
            with self._lock:
                metric.python_wall_ns.append(wall_ns)
                metric.python_cpu_ns.append(cpu_ns)
                metric.output_bytes += (
                    self._output_size(result)
                    if output_bytes is None
                    else max(0, int(output_bytes))
                )
                self._maybe_log_summary(operation, metric)
            return result

        if self.mode == "shadow":
            python_result, python_wall, python_cpu = self._measure(python_call)
            try:
                native_result, native_wall, native_cpu = self._measure(native_call)
            except Exception as exc:
                with self._lock:
                    metric.python_wall_ns.append(python_wall)
                    metric.python_cpu_ns.append(python_cpu)
                    metric.errors += 1
                raise NativeCoreError(
                    "%s: %s" % (type(exc).__name__, exc),
                    stage="shadow_native",
                    operation=operation,
                    context={"input_bytes": input_bytes},
                ) from exc
            with self._lock:
                metric.python_wall_ns.append(python_wall)
                metric.python_cpu_ns.append(python_cpu)
                metric.native_wall_ns.append(native_wall)
                metric.native_cpu_ns.append(native_cpu)
                metric.output_bytes += (
                    self._output_size(python_result)
                    if output_bytes is None
                    else max(0, int(output_bytes))
                )
            if native_result != python_result:
                with self._lock:
                    metric.mismatches += 1
                self._log(
                    "mismatch mode=shadow operation=%s input=%d python_output=%d native_output=%d"
                    % (
                        operation,
                        input_bytes,
                        self._output_size(python_result),
                        self._output_size(native_result),
                    )
                )
                raise NativeCoreMismatchError(
                    "Python/native result mismatch",
                    stage="shadow_compare",
                    operation=operation,
                    context={
                        "input_bytes": input_bytes,
                        "python_output_bytes": self._output_size(python_result),
                        "native_output_bytes": self._output_size(native_result),
                    },
                )
            with self._lock:
                self._maybe_log_summary(operation, metric)
            return python_result

        try:
            result, native_wall, native_cpu = self._measure(native_call)
        except Exception as exc:
            with self._lock:
                metric.errors += 1
            if not self.fallback_enabled:
                raise NativeCoreError(
                    "%s: %s" % (type(exc).__name__, exc),
                    stage="native_execute",
                    operation=operation,
                    context={"input_bytes": input_bytes},
                ) from exc
            fallback, python_wall, python_cpu = self._measure(python_call)
            with self._lock:
                metric.fallbacks += 1
                metric.python_wall_ns.append(python_wall)
                metric.python_cpu_ns.append(python_cpu)
                metric.output_bytes += (
                    self._output_size(fallback)
                    if output_bytes is None
                    else max(0, int(output_bytes))
                )
                self._maybe_log_summary(operation, metric)
            self._log(
                "fallback mode=1 operation=%s input=%d error=%s: %s"
                % (operation, input_bytes, type(exc).__name__, exc)
            )
            return fallback
        with self._lock:
            metric.native_wall_ns.append(native_wall)
            metric.native_cpu_ns.append(native_cpu)
            metric.output_bytes += (
                self._output_size(result)
                if output_bytes is None
                else max(0, int(output_bytes))
            )
            self._maybe_log_summary(operation, metric)
        return result

    def status(self) -> Dict[str, Any]:
        payload = {
            "mode": self.mode,
            "loaded": self.module is not None,
            "fallback": self.fallback_enabled,
            "module_filename": NATIVE_CORE_MODULE_FILENAME,
        }
        if self.module is None:
            return payload
        build = dict(self.module.build_info())
        payload.update(
            {
                "path": str(pathlib.Path(str(self.module.__file__)).resolve()),
                "abi_version": int(self.module.abi_version()),
                "build_version": str(build.get("build_version", "")),
                "compiler": str(build.get("compiler", "")),
                "pointer_bits": int(build.get("pointer_bits", 0)),
                "pe_machine": PE_MACHINE_AMD64,
                "python": sys.version.split()[0],
            }
        )
        return payload

    def metrics_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            operations = {
                operation: metric.snapshot()
                for operation, metric in sorted(self._metrics.items())
            }
        return {"mode": self.mode, "operations": operations}


def get_native_core_runtime(mode: Optional[str] = None) -> NativeCoreRuntime:
    global _RUNTIME
    requested = normalize_native_core_mode(mode)
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            _RUNTIME = NativeCoreRuntime(requested)
        elif _RUNTIME.mode != requested:
            raise NativeCoreError(
                "runtime mode is already fixed for this process",
                stage="mode",
                context={"active": _RUNTIME.mode, "requested": requested},
            )
        return _RUNTIME


def configure_native_core_logger(logger: Optional[Callable[[str], None]]) -> None:
    get_native_core_runtime().set_logger(logger)


def native_core_status(mode: Optional[str] = None) -> Dict[str, Any]:
    return get_native_core_runtime(mode).status()


def native_core_metrics_snapshot() -> Dict[str, Any]:
    return get_native_core_runtime().metrics_snapshot()


def concat_buffers(parts: Iterable[Any]) -> bytes:
    values = tuple(parts)
    input_bytes = 0
    for value in values:
        try:
            input_bytes += memoryview(value).nbytes
        except TypeError as exc:
            raise TypeError("concat_buffers parts must support the buffer protocol") from exc
    runtime = get_native_core_runtime()
    return runtime.execute(
        "concat_buffers",
        lambda: b"".join(values),
        lambda: runtime.module.concat_buffers(values),
        input_bytes=input_bytes,
        output_bytes=input_bytes,
    )


def pack_u32le(values: Iterable[int]) -> bytes:
    normalized = []
    for value in values:
        integer = operator.index(value)
        if integer < 0 or integer > 0xFFFFFFFF:
            raise OverflowError("pack_u32le value is outside uint32")
        normalized.append(integer)
    packed_values = tuple(normalized)
    runtime = get_native_core_runtime()
    return runtime.execute(
        "pack_u32le",
        lambda: struct.pack("<%dI" % len(packed_values), *packed_values),
        lambda: runtime.module.pack_u32le(packed_values),
        input_bytes=len(packed_values) * 4,
        output_bytes=len(packed_values) * 4,
    )


def character_flow_dispatch(opcode: int, body: Any) -> Tuple[int, int]:
    opcode_value = operator.index(opcode)
    if opcode_value < 0 or opcode_value > 0xFFFF:
        raise OverflowError("character-flow opcode is outside uint16")
    try:
        body_view = memoryview(body)
    except TypeError as exc:
        raise TypeError("character-flow body must support the buffer protocol") from exc
    if not body_view.contiguous:
        raise BufferError("character-flow body must be contiguous")
    body_bytes = bytes(body_view)
    python_result = (
        _CHARACTER_FLOW_OPERATION_IDS.get(opcode_value, 0),
        1 if opcode_value == 0x0004 else 0,
    )
    runtime = get_native_core_runtime()
    result = runtime.execute(
        "character_flow_dispatch",
        lambda: python_result,
        lambda: runtime.module.character_flow_dispatch(opcode_value, body_bytes),
        input_bytes=2 + len(body_bytes),
        output_bytes=8,
    )
    if not isinstance(result, tuple) or len(result) != 2:
        raise NativeCoreMismatchError(
            "character-flow result must contain operation_id and handled",
            stage="result_shape",
            operation="character_flow_dispatch",
        )
    operation_id = operator.index(result[0])
    handled = operator.index(result[1])
    if operation_id != python_result[0] or handled not in (0, 1):
        raise NativeCoreMismatchError(
            "character-flow result is outside the stable routing contract",
            stage="result_value",
            operation="character_flow_dispatch",
            context={
                "opcode": "0x%04x" % opcode_value,
                "expected_operation_id": python_result[0],
                "actual_operation_id": operation_id,
                "handled": handled,
            },
        )
    return operation_id, handled


def plan_select_character(body: Any, slot_indexes: Iterable[int]) -> Tuple[int, int, int, int]:
    try:
        body_view = memoryview(body)
    except TypeError as exc:
        raise TypeError("SELECT_CHARACTER body must support the buffer protocol") from exc
    if not body_view.contiguous:
        raise BufferError("SELECT_CHARACTER body must be contiguous")
    body_bytes = bytes(body_view)

    normalized_slots = []
    for value in slot_indexes:
        slot = operator.index(value)
        if slot < -(1 << 63) or slot > (1 << 63) - 1:
            raise OverflowError("SELECT_CHARACTER slot index is outside int64")
        normalized_slots.append(slot)
    slots = tuple(normalized_slots)

    requested_slot = int.from_bytes(body_bytes[:2], "little") if len(body_bytes) >= 2 else 0
    parse_source = 1 if len(body_bytes) >= 2 else 0
    selected_index = -1
    selection_kind = 0
    for index, slot in enumerate(slots):
        if slot == requested_slot:
            selected_index = index
            selection_kind = 1
            break
    if selected_index < 0 and 0 <= requested_slot < len(slots):
        selected_index = requested_slot
        selection_kind = 2
    if selected_index < 0 and slots:
        selected_index = 0
        selection_kind = 3
    python_result = (
        requested_slot,
        parse_source,
        selected_index,
        selection_kind,
    )

    runtime = get_native_core_runtime()
    result = runtime.execute(
        "select_character_plan",
        lambda: python_result,
        lambda: runtime.module.plan_select_character(body_bytes, slots),
        input_bytes=len(body_bytes) + len(slots) * 8,
        output_bytes=16,
    )
    if not isinstance(result, tuple) or len(result) != 4:
        raise NativeCoreMismatchError(
            "SELECT_CHARACTER plan must contain four integers",
            stage="result_shape",
            operation="select_character_plan",
        )
    normalized_result = tuple(operator.index(value) for value in result)
    if normalized_result != python_result:
        raise NativeCoreMismatchError(
            "SELECT_CHARACTER plan differs from frozen Python rules",
            stage="result_value",
            operation="select_character_plan",
            context={"expected": python_result, "actual": normalized_result},
        )
    return normalized_result


def _pack_userinfo1_rows_python(rows: Tuple[Tuple[Any, ...], ...]) -> bytes:
    if len(rows) > 0xFF:
        raise ValueError("USERINFO1 row count must fit uint8")
    out = bytearray((len(rows) & 0xFF,))
    tail = b"\x00" * 29
    for row in rows:
        if len(row) != 13:
            raise ValueError("USERINFO1 row must contain exactly 13 fields")
        (
            slot,
            item_id,
            instance_value,
            attr,
            durability,
            clear_avatar,
            enchant,
            enchant_upgrade,
            amplify_type,
            amplify_value,
            creature_expire_time,
            kind,
            avatar_extra,
        ) = row
        kind_value = operator.index(kind)
        if kind_value not in (0, 1, 2):
            raise ValueError("USERINFO1 row kind must be 0, 1 or 2")
        out += struct.pack(
            "<BIIBHIIBBH",
            operator.index(slot),
            operator.index(item_id),
            operator.index(instance_value),
            operator.index(attr),
            operator.index(durability),
            operator.index(clear_avatar),
            operator.index(enchant),
            operator.index(enchant_upgrade),
            operator.index(amplify_type),
            operator.index(amplify_value),
        )
        if kind_value == 1:
            out += bytes(avatar_extra)
        elif kind_value == 2:
            out += struct.pack("<I", operator.index(creature_expire_time))
        out += tail
    return bytes(out)


def pack_userinfo1_rows(rows: Iterable[Sequence[Any]]) -> bytes:
    normalized_rows = tuple(tuple(row) for row in rows)
    input_bytes = 1
    for row in normalized_rows:
        if len(row) != 13:
            input_bytes += 53
            continue
        kind = operator.index(row[11])
        input_bytes += 57 if kind == 2 else 53
        if kind == 1:
            try:
                input_bytes += memoryview(row[12]).nbytes
            except TypeError:
                pass
    runtime = get_native_core_runtime()
    return runtime.execute(
        "userinfo1_equipment_rows",
        lambda: _pack_userinfo1_rows_python(normalized_rows),
        lambda: runtime.module.pack_userinfo1_rows(normalized_rows),
        input_bytes=input_bytes,
    )


def unpack_u32le(data: Any) -> Tuple[int, ...]:
    view = memoryview(data)
    if not view.c_contiguous:
        raise BufferError("unpack_u32le input must be contiguous")
    if view.nbytes % 4:
        raise ValueError("unpack_u32le input length must be divisible by 4")
    runtime = get_native_core_runtime()
    return runtime.execute(
        "unpack_u32le",
        lambda: struct.unpack("<%dI" % (view.nbytes // 4), view),
        lambda: runtime.module.unpack_u32le(view),
        input_bytes=view.nbytes,
        output_bytes=view.nbytes,
    )


def build_game_header(
    opcode: int,
    payload_len: int,
    *,
    compact: bool = False,
    flag_or_magic: int = 1,
    checksum_or_tag: int = 0,
    sub_or_seq: int = 0,
    reserved: bytes = b"\x00\x00\x00",
) -> bytes:
    opcode_value = operator.index(opcode) & 0xFFFF
    payload_value = operator.index(payload_len)
    if payload_value < 0:
        raise ValueError("payload_len cannot be negative")
    flag_value = operator.index(flag_or_magic) & 0xFF
    checksum_value = operator.index(checksum_or_tag) & 0xFFFFFFFF
    sub_value = operator.index(sub_or_seq) & 0xFFFF
    reserved_bytes = bytes(reserved)
    if not compact and len(reserved_bytes) != 3:
        raise ValueError("reserved must be exactly 3 bytes")
    header_size = 13 if compact else 16
    total = (header_size + payload_value) & 0xFFFFFFFF

    def python_header() -> bytes:
        if compact:
            return (
                bytes([flag_value])
                + struct.pack("<H", opcode_value)
                + struct.pack("<I", total)
                + struct.pack("<I", checksum_value)
                + struct.pack("<H", sub_value)
            )
        return (
            bytes([flag_value])
            + struct.pack("<H", opcode_value)
            + struct.pack("<I", total)
            + struct.pack("<I", checksum_value)
            + struct.pack("<H", sub_value)
            + reserved_bytes
        )

    runtime = get_native_core_runtime()
    return runtime.execute(
        "game_header_compact" if compact else "game_header",
        python_header,
        lambda: runtime.module.build_game_header(
            flag_value,
            opcode_value,
            total,
            checksum_value,
            sub_value,
            reserved_bytes,
            bool(compact),
        ),
        input_bytes=header_size,
        output_bytes=header_size,
    )


def parse_game_header(data: Any) -> Tuple[int, int, int, int, int, int]:
    """Parse the fixed Game header while preserving Python validation semantics.

    The final tuple is ``flag, opcode, total_length, checksum, sub_or_seq,
    header_view_size``.  Packet size limits remain in ``game.game_wire`` so
    the native kernel only owns fixed-width byte extraction.
    """
    view = memoryview(data)
    if not view.c_contiguous:
        raise BufferError("parse_game_header input must be contiguous")
    if view.nbytes < 13:
        raise ValueError("parse_game_header input must contain at least 13 bytes")

    def python_parse() -> Tuple[int, int, int, int, int, int]:
        total = struct.unpack_from("<I", view, 3)[0]
        return (
            int(view[0]),
            struct.unpack_from("<H", view, 1)[0],
            total,
            struct.unpack_from("<I", view, 7)[0],
            struct.unpack_from("<H", view, 0x0B)[0],
            16 if total >= 16 and view.nbytes >= 16 else 13,
        )

    runtime = get_native_core_runtime()
    return runtime.execute(
        "game_header_parse",
        python_parse,
        lambda: runtime.module.parse_game_header(view),
        input_bytes=min(view.nbytes, 16),
        output_bytes=24,
    )


def _reset_native_core_for_tests() -> None:
    global _MODULE, _RUNTIME
    with _RUNTIME_LOCK, _MODULE_LOCK:
        _RUNTIME = None
        _MODULE = None


__all__ = [
    "NATIVE_CORE_ABI_VERSION",
    "NATIVE_CORE_MODULE_FILENAME",
    "NATIVE_CORE_MODULE_PATH",
    "NativeCoreError",
    "NativeCoreMismatchError",
    "NativeCoreRuntime",
    "character_flow_dispatch",
    "concat_buffers",
    "build_game_header",
    "configure_native_core_logger",
    "get_native_core_runtime",
    "native_core_metrics_snapshot",
    "native_core_status",
    "normalize_native_core_mode",
    "parse_game_header",
    "plan_select_character",
    "pack_u32le",
    "pack_userinfo1_rows",
    "require_native_core_module",
    "unpack_u32le",
]
