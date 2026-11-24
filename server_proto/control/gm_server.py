"""GM-capable HTTP control service.

The game process owns this service on its authenticated control port.  The
standalone GM process exposes the player-facing listener and forwards requests
here, so the external listener has an independent lifecycle while commands and
the PVF catalog keep their existing Game owner.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from config import LOG_DIR
from control.control_server import ControlServer


GM_API_HOST = "0.0.0.0"
GM_API_PORT = 17601
GM_ENDPOINT_PATH = LOG_DIR / "gm_endpoint.json"


class GmApiServer(ControlServer):
    """Plain HTTP v1 GM API with caller-provided Bearer authentication."""

    def __init__(
        self,
        game_server: Any,
        *,
        host: str = GM_API_HOST,
        port: int = GM_API_PORT,
        token: str | None = None,
        endpoint_host: str | None = None,
        endpoint_path: str | Path | None = None,
        startup_metric: Optional[Callable[..., Any]] = None,
        audit_prefix: str = "GM",
        service_name: str = "usdof-gm",
        include_token_in_endpoint: bool = False,
    ) -> None:
        super().__init__(
            game_server,
            host=host,
            port=port,
            token=token,
            endpoint_host=endpoint_host,
            endpoint_path=endpoint_path or GM_ENDPOINT_PATH,
            audit_prefix=audit_prefix,
            service_name=service_name,
            include_token_in_endpoint=include_token_in_endpoint,
        )
        self._gm_pvf_resources = getattr(game_server, "gm_pvf_resources", None)
        self._gm_preload_lock = threading.Lock()
        self._gm_preload_thread: threading.Thread | None = None
        self._gm_worker_lock = threading.Lock()
        self._gm_worker_process: Any = None
        self._gm_worker_cancel = threading.Event()
        self._startup_metric = startup_metric

    def _record_startup_metric(self, event: str, **fields: Any) -> None:
        callback = self._startup_metric
        if callback is None:
            return
        try:
            callback(event, **fields)
        except Exception:
            # Diagnostics must not change cache construction or API readiness.
            pass

    def _start_gm_cache_preload(self) -> None:
        resources = self._gm_pvf_resources
        if resources is None:
            return
        public_status = getattr(resources, "public_status", None)
        if callable(public_status):
            try:
                if str(public_status().get("state") or "").lower() in {
                    "ready",
                    "failed",
                }:
                    return
            except Exception:
                pass
        with self._gm_preload_lock:
            thread = self._gm_preload_thread
            if thread is not None and thread.is_alive():
                return
            thread = threading.Thread(
                target=self._preload_gm_cache,
                name="usdof-gm-pvf-cache",
                daemon=True,
            )
            self._gm_preload_thread = thread
            thread.start()

    def _preload_gm_cache(self) -> None:
        resources = self._gm_pvf_resources
        if resources is None:
            return
        try:
            status = dict(
                resources.preload_from_server_cache(
                    cache_builder=self._prepare_gm_catalog_cache,
                )
            )
            self._log(
                "[GM] PVF cache ready records=%s pvf_sha256=%s"
                % (
                    status.get("record_count", 0),
                    str(status.get("pvf_sha256") or "")[:12],
                )
            )
        except Exception as exc:
            self._log(
                "[GM] PVF cache preload failed: %s: %s"
                % (type(exc).__name__, exc)
            )

    def _set_gm_worker_process(self, process: Any) -> None:
        with self._gm_worker_lock:
            self._gm_worker_process = process

    def _prepare_gm_catalog_cache(self, report_progress) -> dict[str, Any]:
        """Build or validate the GM artifact in a dedicated child process."""
        from control.gm_catalog_worker import run_gm_catalog_worker
        from pvf.index_cache import create_cache_context

        cache = create_cache_context(clear=False)
        started = time.perf_counter()
        self._record_startup_metric("gm_catalog_worker_begin")
        try:
            result = run_gm_catalog_worker(
                cache,
                progress_callback=report_progress,
                log_callback=lambda line: self._log("[GM worker] %s" % line),
                cancel_event=self._gm_worker_cancel,
                process_callback=self._set_gm_worker_process,
            )
        except Exception as exc:
            self._record_startup_metric(
                "gm_catalog_worker_end",
                ok=False,
                worker_duration_ms=round(
                    (time.perf_counter() - started) * 1000.0,
                    3,
                ),
                error="%s: %s" % (type(exc).__name__, exc),
            )
            raise
        self._record_startup_metric(
            "gm_catalog_worker_end",
            ok=True,
            worker_duration_ms=round(
                (time.perf_counter() - started) * 1000.0,
                3,
            ),
            worker_reported_ms=int(result.get("elapsed_ms") or 0),
            cache=str(result.get("cache") or ""),
            item_count=int(result.get("item_count") or 0),
            artifact_size=int(result.get("artifact_size") or 0),
        )
        return dict(result)

    def _gm_cache_health(self) -> dict[str, Any]:
        resources = self._gm_pvf_resources
        if resources is None:
            return {
                "state": "unavailable",
                "progress": 0.0,
                "detail": "GM cache owner is unavailable",
                "error": "",
                "record_count": 0,
                "memory_catalog": 0,
            }
        public_status = getattr(resources, "public_status", None)
        if callable(public_status):
            return dict(public_status())
        status = getattr(resources, "status", None)
        if callable(status):
            value = dict(status())
            value.pop("cache_file", None)
            return value
        return {
            "state": "unavailable",
            "progress": 0.0,
            "detail": "GM cache status is unavailable",
            "error": "",
            "record_count": 0,
            "memory_catalog": 0,
        }

    def _handle_health(self) -> tuple[int, dict[str, Any]]:
        # The launcher itself only probes the TCP port.  The first health call
        # comes from an opened GM console and is the explicit lazy-load trigger.
        self._start_gm_cache_preload()
        status, payload = super()._handle_health()
        payload["gm_pvf_cache"] = self._gm_cache_health()
        return status, payload

    def stop(self) -> None:
        self._gm_worker_cancel.set()
        with self._gm_worker_lock:
            process = self._gm_worker_process
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
            except Exception:
                pass
        super().stop()
        with self._gm_preload_lock:
            thread = self._gm_preload_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        resources = self._gm_pvf_resources
        close = getattr(resources, "close", None)
        if callable(close):
            close()

    def _endpoint_payload(self) -> dict[str, Any]:
        payload = super()._endpoint_payload()
        if self.include_token_in_endpoint:
            return payload
        return {
            "api_version": payload["api_version"],
            "host": payload["host"],
            "port": payload["port"],
            "instance_id": payload["instance_id"],
        }

__all__ = ["GM_API_HOST", "GM_API_PORT", "GM_ENDPOINT_PATH", "GmApiServer"]
