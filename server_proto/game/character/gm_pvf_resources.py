"""GM-only read-only view over a validated server PVF SQLite artifact.

The external GM API may ask a caller-owned builder to create the cache after
the player runtime is already listening.  This module keeps only a query facade
for that artifact.  It never opens a PVF reader or accesses ``pvf_runtime``.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Optional


GmCacheProgress = Callable[[float, str], None]
GmCacheBuilder = Callable[[GmCacheProgress], Any]


class GmPvfResourcesError(RuntimeError):
    """Base error for the GM-only cached resource view."""


class GmPvfCacheNotReady(GmPvfResourcesError):
    """Raised while the GM memory catalog has not finished loading."""


class GmPvfCacheFailed(GmPvfResourcesError):
    """Raised when the prewarmed cache file cannot be restored."""


class GmPvfResources:
    """Own one validated, read-only GM catalog query facade."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._catalog: Any = None
        self._state = "pending"
        self._progress = 0.0
        self._detail = "Waiting for server PVF warmup"
        self._error = ""
        self._cache_file = ""
        self._pvf_sha256 = ""
        self._record_count = 0
        self._generation = 0
        self._preparing = False

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state,
                "progress": round(max(0.0, min(1.0, self._progress)), 4),
                "detail": self._detail,
                "error": self._error,
                "cache_file": self._cache_file,
                "pvf_sha256": self._pvf_sha256,
                "record_count": int(self._record_count),
                "memory_catalog": 0,
            }

    def public_status(self) -> dict[str, Any]:
        """Return the GM API status without exposing the local cache path."""
        current = self.status()
        return {
            "state": current["state"],
            "progress": current["progress"],
            "detail": current["detail"],
            "error": current["error"],
            "pvf_sha256": current["pvf_sha256"],
            "record_count": current["record_count"],
            "memory_catalog": current["memory_catalog"],
        }

    def _set_status_if_current(
        self,
        generation: int,
        state: str,
        progress: float,
        detail: str,
        *,
        error: str = "",
    ) -> bool:
        with self._lock:
            if generation != self._generation:
                return False
            self._state = str(state)
            self._progress = max(0.0, min(1.0, float(progress)))
            self._detail = str(detail)
            self._error = str(error)
            return True

    def preload_from_server_cache(
        self,
        *,
        cache_builder: Optional[GmCacheBuilder] = None,
    ) -> dict[str, Any]:
        """Prepare the GM artifact if needed, then restore its memory copy.

        ``cache_builder`` is owned by the GM API layer and starts a dedicated
        reader process from the server's verified identity. This resource
        object only receives progress and validates the published SQLite file.
        """
        with self._lock:
            if self._catalog is not None and self._state == "ready":
                return self.status()
            if self._preparing:
                return self.status()
            if self._state == "failed":
                raise GmPvfCacheFailed(self._error or "GM PVF cache failed")
            self._generation += 1
            generation = self._generation
            self._preparing = True
        cache_source: Any = None
        try:
            if cache_builder is not None:
                if not self._set_status_if_current(
                    generation,
                    "preparing",
                    0.03,
                    "Preparing GM item catalog cache",
                ):
                    return self.status()

                def report_build_progress(progress: float, detail: str) -> None:
                    normalized = max(0.0, min(1.0, float(progress)))
                    self._set_status_if_current(
                        generation,
                        "preparing",
                        0.03 + normalized * 0.77,
                        str(detail),
                    )

                cache_source = cache_builder(report_build_progress)
            if not self._set_status_if_current(
                generation,
                "preparing",
                0.82,
                "Locating server GM cache",
            ):
                return self.status()
            from game.character.gm_item_catalog import (
                load_item_catalog_from_preload_cache,
            )

            if not self._set_status_if_current(
                generation,
                "preparing",
                0.90,
                "Opening GM item catalog database",
            ):
                return self.status()
            source_file = None
            source_sha256 = ""
            source_identity = None
            source_artifact_sha256 = ""
            source_artifact_size = 0
            if isinstance(cache_source, dict):
                source_file = cache_source.get("cache_file")
                source_sha256 = str(cache_source.get("pvf_sha256") or "")
                source_artifact_sha256 = str(
                    cache_source.get("artifact_sha256") or ""
                )
                source_artifact_size = int(cache_source.get("artifact_size") or 0)
                raw_identity = cache_source.get("pvf_identity")
                if isinstance(raw_identity, dict):
                    source_identity = dict(raw_identity)
            catalog, source = load_item_catalog_from_preload_cache(
                source_file,
                expected_pvf_sha256=source_sha256,
                expected_pvf_identity=source_identity,
                expected_artifact_sha256=source_artifact_sha256,
                expected_artifact_size=source_artifact_size,
            )
        except Exception as exc:
            error = "%s: %s" % (type(exc).__name__, exc)
            with self._lock:
                active = generation == self._generation
                self._preparing = False
                if active:
                    self._state = "failed"
                    self._progress = 1.0
                    self._detail = "GM cache initialization failed"
                    self._error = error
            raise GmPvfCacheFailed(error) from exc
        with self._lock:
            if generation != self._generation:
                self._preparing = False
                return self.status()
            self._catalog = catalog
            self._cache_file = str(source.get("cache_file") or "")
            self._pvf_sha256 = str(source.get("pvf_sha256") or "")
            self._record_count = int(getattr(catalog, "record_count", 0) or 0)
            self._preparing = False
            self._state = "ready"
            self._progress = 1.0
            self._detail = "GM item catalog ready"
            self._error = ""
            return self.status()

    def get_item_catalog(self):
        with self._lock:
            catalog = self._catalog
            state = self._state
            detail = self._detail
            error = self._error
        if catalog is not None and state == "ready":
            return catalog
        if state == "failed":
            raise GmPvfCacheFailed(error or "GM PVF cache failed")
        raise GmPvfCacheNotReady(detail or "GM PVF cache is preparing")

    def close(self) -> None:
        with self._lock:
            self._generation += 1
            catalog = self._catalog
            self._catalog = None
            self._state = "pending"
            self._progress = 0.0
            self._detail = "Waiting for server PVF warmup"
            self._error = ""
            self._cache_file = ""
            self._pvf_sha256 = ""
            self._record_count = 0
            self._preparing = False
        close = getattr(catalog, "close", None)
        if callable(close):
            close()


__all__ = [
    "GmPvfCacheFailed",
    "GmPvfCacheNotReady",
    "GmPvfResources",
    "GmPvfResourcesError",
]
