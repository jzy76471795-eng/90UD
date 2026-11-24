from __future__ import annotations

import math
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator


HOTPATH_OPCODES = frozenset((0x002B, 0x002C, 0x0027, 0x002D))


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(0, int(math.ceil((percentile / 100.0) * len(ordered))) - 1)
    return float(ordered[min(rank, len(ordered) - 1)])


class RequestTiming:
    def __init__(self, connection: "ConnectionHotpathMetrics", opcode: int) -> None:
        self.connection = connection
        self.opcode = int(opcode) & 0xFFFF
        self.started_ns = time.perf_counter_ns()
        self.decode_started_ns = self.started_ns
        self.decode_finished_ns = self.started_ns
        self.route_started_ns = self.started_ns
        self.route_finished_ns = self.started_ns
        self.first_send_started_ns = 0
        self.first_send_finished_ns = 0
        self.finished_ns = 0
        self.stages_ns: dict[str, int] = {}
        self._finished = False

    def mark_decode_start(self) -> None:
        self.decode_started_ns = time.perf_counter_ns()

    def mark_decode_end(self) -> None:
        self.decode_finished_ns = time.perf_counter_ns()

    def mark_route_start(self) -> None:
        self.route_started_ns = time.perf_counter_ns()

    def mark_route_end(self) -> None:
        self.route_finished_ns = time.perf_counter_ns()

    def mark_first_send_start(self) -> None:
        if self.first_send_started_ns <= 0:
            self.first_send_started_ns = time.perf_counter_ns()

    def mark_first_send_end(self) -> None:
        if self.first_send_started_ns > 0 and self.first_send_finished_ns <= 0:
            self.first_send_finished_ns = time.perf_counter_ns()

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        started_ns = time.perf_counter_ns()
        try:
            yield
        finally:
            elapsed_ns = max(0, time.perf_counter_ns() - started_ns)
            self.stages_ns[name] = self.stages_ns.get(name, 0) + elapsed_ns

    def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        self.finished_ns = time.perf_counter_ns()
        self.connection.finish_request(self)

    def sample_ms(self) -> dict[str, float]:
        finished_ns = self.finished_ns or time.perf_counter_ns()
        first_send_start = self.first_send_started_ns or finished_ns
        first_send_end = self.first_send_finished_ns or first_send_start
        route_end = self.route_finished_ns or self.route_started_ns
        sample = {
            "decode": max(0, self.decode_finished_ns - self.decode_started_ns) / 1_000_000.0,
            "route": max(0, self.route_finished_ns - self.route_started_ns) / 1_000_000.0,
            "before_first_send": max(0, first_send_start - route_end) / 1_000_000.0,
            "first_send": max(0, first_send_end - first_send_start) / 1_000_000.0,
            "first_response": max(0, first_send_end - self.started_ns) / 1_000_000.0,
            "total": max(0, finished_ns - self.started_ns) / 1_000_000.0,
        }
        for name, elapsed_ns in self.stages_ns.items():
            sample[name] = max(0, elapsed_ns) / 1_000_000.0
        return sample


class ConnectionHotpathMetrics:
    def __init__(
        self,
        recorder: "HotpathMetricsRecorder",
        connection_id: str,
    ) -> None:
        self.recorder = recorder
        self.connection_id = str(connection_id)
        self.samples: dict[int, list[dict[str, float]]] = {}
        self.closed = False

    def __enter__(self) -> "ConnectionHotpathMetrics":
        self.recorder._local.connection = self
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def begin_request(self, opcode: int) -> RequestTiming | None:
        if not self.recorder.enabled or int(opcode) not in HOTPATH_OPCODES:
            self.recorder._local.request = None
            return None
        request = RequestTiming(self, int(opcode))
        self.recorder._local.request = request
        return request

    def finish_request(self, request: RequestTiming) -> None:
        if getattr(self.recorder._local, "request", None) is request:
            self.recorder._local.request = None
        sample = request.sample_ms()
        bucket = self.samples.setdefault(request.opcode, [])
        bucket.append(sample)
        if sample.get("total", 0.0) > self.recorder.slow_ms:
            self.recorder.log(self._format_slow(request.opcode, sample))
        if len(bucket) >= self.recorder.summary_every:
            self._flush_opcode(request.opcode, reason="count")

    def _format_slow(self, opcode: int, sample: dict[str, float]) -> str:
        phases = " ".join(
            "%s=%.2fms" % (name, sample[name])
            for name in sorted(sample)
        )
        return (
            "[Game/Hotpath] slow connection=%s opcode=0x%04X %s"
            % (self.connection_id, opcode, phases)
        )

    def _flush_opcode(self, opcode: int, *, reason: str) -> None:
        bucket = self.samples.get(opcode) or []
        if not bucket:
            return
        field_names = sorted({name for sample in bucket for name in sample})
        fields: list[str] = []
        for name in field_names:
            values = [sample.get(name, 0.0) for sample in bucket]
            fields.append(
                "%s[p50=%.2f p95=%.2f max=%.2f]"
                % (
                    name,
                    _percentile(values, 50.0),
                    _percentile(values, 95.0),
                    max(values),
                )
            )
        self.recorder.log(
            "[Game/Hotpath] summary connection=%s opcode=0x%04X n=%d reason=%s %s"
            % (self.connection_id, opcode, len(bucket), reason, " ".join(fields))
        )
        self.samples[opcode] = []

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        current = getattr(self.recorder._local, "request", None)
        if current is not None and current.connection is self:
            current.finish()
        for opcode in sorted(self.samples):
            self._flush_opcode(opcode, reason="connection_close")
        if getattr(self.recorder._local, "connection", None) is self:
            self.recorder._local.connection = None


class HotpathMetricsRecorder:
    def __init__(self, log: Callable[[str], None]) -> None:
        self.log = log
        self.enabled = _env_bool("USDOF_GAME_HOTPATH_METRICS", True)
        self.summary_every = 100
        self.slow_ms = 50.0
        self._local = threading.local()

    def connection(self, connection_id: str) -> ConnectionHotpathMetrics:
        return ConnectionHotpathMetrics(self, connection_id)

    def begin_request(self, opcode: int) -> RequestTiming | None:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            return None
        return connection.begin_request(opcode)

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        request = getattr(self._local, "request", None)
        if request is None:
            yield
            return
        with request.stage(name):
            yield

    def finish_request(self) -> None:
        request = getattr(self._local, "request", None)
        if request is not None:
            request.finish()
