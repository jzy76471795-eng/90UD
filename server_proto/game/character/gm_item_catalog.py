"""PVF-backed searchable item catalog for the external GM control plane."""
from __future__ import annotations

import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

CATALOG_CACHE_DOMAIN = "gm_item_catalog"
CATALOG_CACHE_VERSION = 7
CATALOG_PAGE_SIZE_DEFAULT = 50
CATALOG_PAGE_SIZE_MAX = 100
CATALOG_CATEGORIES = frozenset(
    {
        "all",
        "equipment",
        "stackable",
        "avatar",
        "pet",
        "guild_medal",
        "guardian_gem",
    }
)

_LST_PAIR_RE = re.compile(r"(?m)^(\d+)\s*(?:\r?\n|\s+)`([^`]+)`")
_LOCK = threading.RLock()
_INDEX: Any = None
_PRELOAD_CACHE_SOURCE: Optional[dict[str, Any]] = None


def _compile_like_pattern(value: str) -> re.Pattern[str]:
    parts = (re.escape(part) for part in value.split("%"))
    return re.compile(r"\A" + ".*".join(parts) + r"\Z", re.DOTALL)


@dataclass(frozen=True)
class GmItemRecord:
    item_id: int
    name: str
    category: str
    rarity: int
    minimum_level: int
    stack_limit: int
    source_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": int(self.item_id),
            "name": self.name,
            "category": self.category,
            "rarity": int(self.rarity),
            "minimum_level": int(self.minimum_level),
            "stack_limit": int(self.stack_limit),
            "source_path": self.source_path,
        }

    def to_cache_row(self) -> list[Any]:
        return [
            int(self.item_id),
            self.name,
            self.category,
            int(self.rarity),
            int(self.minimum_level),
            int(self.stack_limit),
            self.source_path,
        ]

    @classmethod
    def from_cache_row(cls, row: Any) -> "GmItemRecord":
        if not isinstance(row, list) or len(row) != 7:
            raise ValueError("invalid GM item catalog row")
        record = cls(
            item_id=int(row[0]),
            name=str(row[1] or ""),
            category=str(row[2] or ""),
            rarity=max(0, int(row[3] or 0)),
            minimum_level=max(0, int(row[4] or 0)),
            stack_limit=max(0, int(row[5] or 0)),
            source_path=str(row[6] or ""),
        )
        if record.item_id <= 0 or record.category not in CATALOG_CATEGORIES - {"all"}:
            raise ValueError("invalid GM item catalog identity")
        return record


class GmItemCatalog:
    def __init__(self, records: Iterable[GmItemRecord], *, pvf_sha256: str = "") -> None:
        unique: dict[int, GmItemRecord] = {}
        for record in records:
            if int(record.item_id) > 0:
                unique[int(record.item_id)] = record
        self.records = tuple(unique[item_id] for item_id in sorted(unique))
        self.pvf_sha256 = str(pvf_sha256 or "")
        self._records_by_id = {record.item_id: record for record in self.records}
        self._search_fields = tuple(
            (str(record.item_id), record.name, record.source_path)
            for record in self.records
        )
        self._search_fields = tuple(
            tuple(value.casefold() for value in fields)
            for fields in self._search_fields
        )

    def query(
        self,
        *,
        search: str = "",
        category: str = "all",
        page: int = 1,
        page_size: int = CATALOG_PAGE_SIZE_DEFAULT,
    ) -> dict[str, Any]:
        category_key = str(category or "all").strip().lower()
        if category_key not in CATALOG_CATEGORIES:
            raise ValueError(
                "category must be all, equipment, stackable, avatar, pet, "
                "guild_medal, or guardian_gem"
            )
        page_value = int(page)
        page_size_value = int(page_size)
        if page_value < 1:
            raise ValueError("page must be at least 1")
        if not 1 <= page_size_value <= CATALOG_PAGE_SIZE_MAX:
            raise ValueError("page_size must be between 1 and %d" % CATALOG_PAGE_SIZE_MAX)
        search_value = str(search or "").strip()
        needle = search_value.casefold()
        numeric_id = (
            int(search_value)
            if re.fullmatch(r"[0-9]+", search_value) is not None
            else None
        )
        like_pattern = _compile_like_pattern(needle) if "%" in needle else None
        matched: list[GmItemRecord] = []
        if numeric_id is not None:
            record = self._records_by_id.get(numeric_id)
            if record is not None and (
                category_key == "all" or record.category == category_key
            ):
                matched.append(record)
        else:
            for record, search_fields in zip(self.records, self._search_fields):
                if category_key != "all" and record.category != category_key:
                    continue
                if like_pattern is not None:
                    if not any(
                        like_pattern.fullmatch(value) for value in search_fields
                    ):
                        continue
                elif needle and not any(needle in value for value in search_fields):
                    continue
                matched.append(record)
        total = len(matched)
        page_count = max(1, (total + page_size_value - 1) // page_size_value)
        if page_value > page_count:
            page_value = page_count
        start = (page_value - 1) * page_size_value
        return {
            "items": [record.to_dict() for record in matched[start : start + page_size_value]],
            "page": page_value,
            "page_size": page_size_value,
            "page_count": page_count,
            "total": total,
            "search": search_value,
            "category": category_key,
            "pvf_sha256": self.pvf_sha256,
        }

    def to_cache_payload(self) -> dict[str, Any]:
        return {
            "pvf_sha256": self.pvf_sha256,
            "rows": [record.to_cache_row() for record in self.records],
        }

    @classmethod
    def from_cache_payload(cls, payload: Any, *, pvf_sha256: str) -> "GmItemCatalog":
        if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
            raise ValueError("invalid GM item catalog payload")
        cached_sha = str(payload.get("pvf_sha256") or "")
        if cached_sha and cached_sha != str(pvf_sha256 or ""):
            raise ValueError("GM item catalog PVF identity mismatch")
        return cls(
            (GmItemRecord.from_cache_row(row) for row in payload["rows"]),
            pvf_sha256=pvf_sha256,
        )


def _parse_lst(text: str, prefix: str) -> dict[int, str]:
    out: dict[int, str] = {}
    normalized_prefix = str(prefix).strip("/")
    for raw_id, raw_path in _LST_PAIR_RE.findall(str(text or "")):
        item_id = int(raw_id)
        path = raw_path.replace("\\", "/").strip().lstrip("/")
        if not path.casefold().startswith(normalized_prefix.casefold() + "/"):
            path = normalized_prefix + "/" + path
        if item_id > 0:
            out[item_id] = path
    return out


def _category_for_metadata(metadata: Any) -> str:
    if bool(getattr(metadata, "is_guild_medal", False)):
        return "guild_medal"
    if bool(getattr(metadata, "is_guardian_gem", False)):
        return "guardian_gem"
    if bool(getattr(metadata, "is_pet_inventory_item", False)):
        return "pet"
    if bool(getattr(metadata, "is_avatar", False)):
        return "avatar"
    if bool(getattr(metadata, "is_equipment", False)):
        return "equipment"
    return "stackable"


CatalogProgressCallback = Callable[[int, int], None]


def install_item_catalog(catalog: Any) -> None:
    global _INDEX
    with _LOCK:
        _INDEX = catalog


def get_item_catalog() -> Any:
    """Return the server-owned catalog for boot-time internal maintenance only."""
    with _LOCK:
        if _INDEX is None:
            from pvf.index_cache import create_cache_context

            warmup_item_catalog_index(create_cache_context())
        if _INDEX is None:
            raise RuntimeError("GM item catalog did not initialize")
        return _INDEX


def _publish_preload_cache_source(cache: Any, pvf_sha256: str) -> None:
    global _PRELOAD_CACHE_SOURCE
    source: Optional[dict[str, Any]] = None
    if cache is not None and bool(getattr(cache, "enabled", False)):
        from game.character.gm_item_catalog_sqlite import catalog_artifact_path

        path = catalog_artifact_path(cache)
        if path.is_file():
            source = {
                "cache_file": str(path.resolve()),
                "pvf_sha256": str(pvf_sha256),
                "pvf_identity": asdict(cache.identity),
            }
    with _LOCK:
        _PRELOAD_CACHE_SOURCE = source


def load_item_catalog_from_preload_cache(
    cache_file: str | Path | None = None,
    *,
    expected_pvf_sha256: str = "",
    expected_pvf_identity: Optional[dict[str, Any]] = None,
    expected_artifact_sha256: str = "",
    expected_artifact_size: int = 0,
) -> tuple[Any, dict[str, Any]]:
    """Open the identity-bound GM SQLite artifact without reading PVF data."""
    if cache_file is None:
        with _LOCK:
            source = dict(_PRELOAD_CACHE_SOURCE or {})
        path = Path(str(source.get("cache_file") or ""))
    else:
        path = Path(cache_file).expanduser()
        source = {
            "cache_file": str(path.resolve()),
            "pvf_sha256": str(expected_pvf_sha256 or ""),
        }
        if expected_pvf_identity is not None:
            source["pvf_identity"] = dict(expected_pvf_identity)
        if expected_artifact_sha256:
            source["artifact_sha256"] = str(expected_artifact_sha256)
        if int(expected_artifact_size or 0) > 0:
            source["artifact_size"] = int(expected_artifact_size)
    expected_identity = source.get("pvf_identity")
    identity_sha256 = (
        str(expected_identity.get("sha256") or "")
        if isinstance(expected_identity, dict)
        else ""
    )
    pvf_sha256 = str(
        source.get("pvf_sha256")
        or expected_pvf_sha256
        or identity_sha256
        or ""
    )
    if not path.is_file():
        raise RuntimeError("server GM preload cache file is unavailable")
    from game.character.gm_item_catalog_sqlite import SqliteGmItemCatalog

    catalog = SqliteGmItemCatalog(
        path,
        expected_identity=expected_identity,
        expected_pvf_sha256=pvf_sha256,
        expected_artifact_sha256=str(source.get("artifact_sha256") or ""),
        expected_artifact_size=int(source.get("artifact_size") or 0),
    )
    source["cache_file"] = str(path.resolve())
    source["pvf_sha256"] = catalog.pvf_sha256
    source["pvf_identity"] = dict(catalog.metadata["pvf_identity"])
    return catalog, source


def warmup_item_catalog_index(
    cache: Any = None,
    *,
    install_runtime: bool = True,
    progress_callback: Optional[CatalogProgressCallback] = None,
) -> dict[str, Any]:
    if cache is None:
        from pvf.index_cache import create_cache_context

        cache = create_cache_context(clear=False)
    from control.gm_catalog_worker import run_gm_catalog_worker

    def report_worker_progress(progress: float, _detail: str) -> None:
        if progress_callback is not None:
            progress_callback(int(round(float(progress) * 10000.0)), 10000)

    worker_result = run_gm_catalog_worker(
        cache,
        progress_callback=report_worker_progress,
    )
    pvf_identity = worker_result.get("pvf_identity")
    catalog, source = load_item_catalog_from_preload_cache(
        worker_result.get("cache_file"),
        expected_pvf_sha256=str(worker_result.get("pvf_sha256") or ""),
        expected_pvf_identity=(
            dict(pvf_identity) if isinstance(pvf_identity, dict) else None
        ),
        expected_artifact_sha256=str(worker_result.get("artifact_sha256") or ""),
        expected_artifact_size=int(worker_result.get("artifact_size") or 0),
    )
    cache_status = str(worker_result.get("cache") or "miss")
    if progress_callback is not None:
        progress_callback(catalog.record_count, catalog.record_count)
    if install_runtime:
        install_item_catalog(catalog)
    _publish_preload_cache_source(cache, str(source.get("pvf_sha256") or ""))
    counts = dict(catalog.metadata["category_counts"])
    return {
        "item_count": catalog.record_count,
        "equipment_count": counts["equipment"],
        "stackable_count": counts["stackable"],
        "avatar_count": counts["avatar"],
        "pet_count": counts["pet"],
        "guild_medal_count": counts["guild_medal"],
        "guardian_gem_count": counts["guardian_gem"],
        "cache": cache_status,
        "runtime_catalog": int(bool(install_runtime)),
        "pvf_sha256": str(source.get("pvf_sha256") or "")[:12],
    }


__all__ = [
    "CATALOG_CATEGORIES",
    "CATALOG_PAGE_SIZE_DEFAULT",
    "CATALOG_PAGE_SIZE_MAX",
    "GmItemCatalog",
    "GmItemRecord",
    "get_item_catalog",
    "install_item_catalog",
    "load_item_catalog_from_preload_cache",
    "warmup_item_catalog_index",
]
