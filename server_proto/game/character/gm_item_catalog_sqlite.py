"""Identity-bound SQLite store for the deferred GM item catalog."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from game.character.gm_item_catalog import (
    CATALOG_CACHE_DOMAIN,
    CATALOG_CACHE_VERSION,
    CATALOG_CATEGORIES,
    CATALOG_PAGE_SIZE_DEFAULT,
    CATALOG_PAGE_SIZE_MAX,
    GmItemRecord,
)


CATALOG_ARTIFACT_NAME = "gm_item_catalog.sqlite3"
CATALOG_SQLITE_MAGIC = "USDOF_GM_ITEM_CATALOG"
CATALOG_SQLITE_SCHEMA_VERSION = 1
CATALOG_BUILDER_VERSION = 1
CATALOG_INSERT_BATCH_SIZE = 1000

_PUBLIC_CATEGORIES = tuple(sorted(CATALOG_CATEGORIES - {"all"}))
_ROW_SELECT = (
    "item_id, name, search_name, search_path, category, rarity, "
    "minimum_level, stack_limit, source_path, sort_name"
)
_REQUIRED_META_KEYS = frozenset(
    {
        "magic",
        "schema_version",
        "domain",
        "semantic_version",
        "builder_version",
        "pvf_identity",
        "pvf_sha256",
        "record_count",
        "category_counts",
        "content_sha256",
        "generated_unix",
    }
)

CatalogWriteProgress = Callable[[int, int], None]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _identity_dict(identity: Any) -> dict[str, Any]:
    if is_dataclass(identity):
        raw = asdict(identity)
    elif isinstance(identity, dict):
        raw = dict(identity)
    else:
        raise ValueError("GM catalog PVF identity must be an object")
    document = {
        "path": str(raw.get("path") or ""),
        "sha256": str(raw.get("sha256") or "").lower(),
        "file_size": int(raw.get("file_size") or 0),
        "file_count": int(raw.get("file_count") or 0),
        "group_count": int(raw.get("group_count") or 0),
        "python": str(raw.get("python") or ""),
    }
    if (
        not document["path"]
        or len(document["sha256"]) != 64
        or min(
            document["file_size"],
            document["file_count"],
            document["group_count"],
        ) <= 0
        or not document["python"]
    ):
        raise ValueError("GM catalog PVF identity is incomplete")
    try:
        int(document["sha256"], 16)
    except ValueError as exc:
        raise ValueError("GM catalog PVF SHA-256 is invalid") from exc
    return document


def catalog_artifact_path(cache: Any) -> Path:
    directory = Path(getattr(cache, "directory", cache)).expanduser().resolve()
    return directory / CATALOG_ARTIFACT_NAME


def catalog_file_sha256(path: Path) -> tuple[str, int]:
    database = Path(path).expanduser().resolve()
    digest = hashlib.sha256()
    with database.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
        closed = os.fstat(handle.fileno())
    final = database.stat()
    opened_stamp = (int(opened.st_size), int(opened.st_mtime_ns))
    if opened_stamp != (int(closed.st_size), int(closed.st_mtime_ns)) or opened_stamp != (
        int(final.st_size),
        int(final.st_mtime_ns),
    ):
        raise RuntimeError("GM item catalog changed while hashing")
    return digest.hexdigest(), int(final.st_size)


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = path.expanduser().resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA case_sensitive_like=ON")
    return connection


def _update_row_digest(digest: Any, row: Iterable[Any]) -> None:
    encoded = _canonical_json(list(row)).encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "little", signed=False))
    digest.update(encoded)


def _database_content_sha256(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for row in connection.execute(
        "SELECT %s FROM items ORDER BY item_id" % _ROW_SELECT
    ):
        _update_row_digest(digest, tuple(row))
    return digest.hexdigest()


def _record_row(record: GmItemRecord) -> tuple[Any, ...]:
    item_id = int(record.item_id)
    name = str(record.name or "")
    category = str(record.category or "").strip().lower()
    rarity = int(record.rarity or 0)
    minimum_level = int(record.minimum_level or 0)
    stack_limit = int(record.stack_limit or 0)
    source_path = str(record.source_path or "").replace("\\", "/")
    if (
        item_id <= 0
        or item_id > 0xFFFFFFFF
        or category not in _PUBLIC_CATEGORIES
        or rarity < 0
        or minimum_level < 0
        or stack_limit < 0
        or not source_path
    ):
        raise ValueError("invalid GM item catalog record")
    return (
        item_id,
        name,
        name.casefold(),
        source_path.casefold(),
        category,
        rarity,
        minimum_level,
        stack_limit,
        source_path,
        name.casefold(),
    )


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA user_version=1;
        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE items (
            item_id INTEGER PRIMARY KEY CHECK(item_id > 0 AND item_id <= 4294967295),
            name TEXT NOT NULL,
            search_name TEXT NOT NULL,
            search_path TEXT NOT NULL,
            category TEXT NOT NULL CHECK(category IN (
                'equipment', 'stackable', 'avatar', 'pet',
                'guild_medal', 'guardian_gem'
            )),
            rarity INTEGER NOT NULL CHECK(rarity >= 0),
            minimum_level INTEGER NOT NULL CHECK(minimum_level >= 0),
            stack_limit INTEGER NOT NULL CHECK(stack_limit >= 0),
            source_path TEXT NOT NULL CHECK(length(source_path) > 0),
            sort_name TEXT NOT NULL
        );
        """
    )


def _create_query_indexes(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE INDEX idx_items_category_id ON items(category, item_id);
        """
    )


def _remove_sqlite_files(path: Path) -> None:
    for candidate in (
        path,
        Path(str(path) + "-journal"),
        Path(str(path) + "-wal"),
        Path(str(path) + "-shm"),
    ):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def publish_catalog_database(
    path: Path,
    *,
    identity: Any,
    records: Iterable[GmItemRecord],
    expected_record_count: int,
    progress_callback: Optional[CatalogWriteProgress] = None,
) -> dict[str, Any]:
    """Stream records into a temporary database and atomically publish it."""
    destination = Path(path).expanduser().resolve()
    identity_document = _identity_dict(identity)
    total = int(expected_record_count)
    if total <= 0:
        raise ValueError("GM item catalog expected count must be positive")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=destination.name + ".",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    os.close(fd)
    temporary = Path(temporary_name)
    connection: Optional[sqlite3.Connection] = None
    started = time.perf_counter()
    try:
        connection = sqlite3.connect(str(temporary), timeout=30.0)
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA locking_mode=EXCLUSIVE")
        _create_schema(connection)
        counts = {category: 0 for category in _PUBLIC_CATEGORIES}
        inserted = 0
        batch: list[tuple[Any, ...]] = []
        insert_sql = (
            "INSERT INTO items ("
            + _ROW_SELECT
            + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        for record in records:
            row = _record_row(record)
            batch.append(row)
            counts[str(row[4])] += 1
            if len(batch) >= CATALOG_INSERT_BATCH_SIZE:
                connection.executemany(insert_sql, batch)
                inserted += len(batch)
                batch.clear()
                if progress_callback is not None:
                    progress_callback(inserted, total)
        if batch:
            connection.executemany(insert_sql, batch)
            inserted += len(batch)
            batch.clear()
            if progress_callback is not None:
                progress_callback(inserted, total)
        if inserted != total:
            raise ValueError(
                "GM item catalog record count mismatch: expected=%d actual=%d"
                % (total, inserted)
            )
        _create_query_indexes(connection)
        content_sha256 = _database_content_sha256(connection)
        meta = {
            "magic": CATALOG_SQLITE_MAGIC,
            "schema_version": str(CATALOG_SQLITE_SCHEMA_VERSION),
            "domain": CATALOG_CACHE_DOMAIN,
            "semantic_version": str(CATALOG_CACHE_VERSION),
            "builder_version": str(CATALOG_BUILDER_VERSION),
            "pvf_identity": _canonical_json(identity_document),
            "pvf_sha256": identity_document["sha256"],
            "record_count": str(inserted),
            "category_counts": _canonical_json(counts),
            "content_sha256": content_sha256,
            "generated_unix": str(int(time.time())),
        }
        connection.executemany(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            sorted(meta.items()),
        )
        connection.commit()
        check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        if check != "ok":
            raise ValueError("GM item catalog quick_check failed: %s" % check)
        connection.close()
        connection = None
        validated = validate_catalog_database(
            temporary,
            expected_identity=identity_document,
            verify_digest=False,
        )
        os.replace(str(temporary), str(destination))
        validated["cache_file"] = str(destination)
        artifact_sha256, artifact_size = catalog_file_sha256(destination)
        validated["artifact_sha256"] = artifact_sha256
        validated["artifact_size"] = artifact_size
        validated["publish_elapsed_ms"] = int(
            round((time.perf_counter() - started) * 1000.0)
        )
        return validated
    except sqlite3.IntegrityError as exc:
        raise ValueError("GM item catalog contains duplicate or invalid rows") from exc
    finally:
        if connection is not None:
            connection.close()
        _remove_sqlite_files(temporary)


def validate_catalog_database(
    path: Path,
    *,
    expected_identity: Any = None,
    expected_pvf_sha256: str = "",
    verify_digest: bool = True,
) -> dict[str, Any]:
    database = Path(path).expanduser().resolve()
    if not database.is_file():
        raise FileNotFoundError("GM item catalog database is unavailable")
    connection = _readonly_connection(database)
    try:
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != int(
            CATALOG_SQLITE_SCHEMA_VERSION
        ):
            raise ValueError("GM item catalog schema mismatch")
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check != "ok":
            raise ValueError("GM item catalog quick_check failed: %s" % quick_check)
        meta = {
            str(row["key"]): str(row["value"])
            for row in connection.execute("SELECT key, value FROM meta")
        }
        if not _REQUIRED_META_KEYS.issubset(meta):
            raise ValueError("GM item catalog metadata is incomplete")
        if (
            meta["magic"] != CATALOG_SQLITE_MAGIC
            or meta["domain"] != CATALOG_CACHE_DOMAIN
            or int(meta["schema_version"]) != CATALOG_SQLITE_SCHEMA_VERSION
            or int(meta["semantic_version"]) != CATALOG_CACHE_VERSION
            or int(meta["builder_version"]) != CATALOG_BUILDER_VERSION
        ):
            raise ValueError("GM item catalog metadata version mismatch")
        identity = json.loads(meta["pvf_identity"])
        identity_document = _identity_dict(identity)
        if expected_identity is not None and identity_document != _identity_dict(
            expected_identity
        ):
            raise ValueError("GM item catalog full PVF identity mismatch")
        expected_sha = str(expected_pvf_sha256 or "").lower()
        if (
            meta["pvf_sha256"] != identity_document["sha256"]
            or (expected_sha and meta["pvf_sha256"] != expected_sha)
        ):
            raise ValueError("GM item catalog PVF SHA-256 mismatch")
        record_count = int(connection.execute("SELECT COUNT(*) FROM items").fetchone()[0])
        if record_count <= 0 or record_count != int(meta["record_count"]):
            raise ValueError("GM item catalog record count mismatch")
        invalid_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM items WHERE "
                "item_id <= 0 OR item_id > 4294967295 OR rarity < 0 OR "
                "minimum_level < 0 OR stack_limit < 0 OR source_path = ''"
            ).fetchone()[0]
        )
        if invalid_count:
            raise ValueError("GM item catalog contains invalid rows")
        counts = {category: 0 for category in _PUBLIC_CATEGORIES}
        for row in connection.execute(
            "SELECT category, COUNT(*) AS count FROM items GROUP BY category"
        ):
            category = str(row["category"])
            if category not in counts:
                raise ValueError("GM item catalog contains an unknown category")
            counts[category] = int(row["count"])
        if counts != json.loads(meta["category_counts"]):
            raise ValueError("GM item catalog category counts mismatch")
        if verify_digest:
            actual_digest = _database_content_sha256(connection)
            if actual_digest != meta["content_sha256"]:
                raise ValueError("GM item catalog content digest mismatch")
        return {
            "cache_file": str(database),
            "pvf_identity": identity_document,
            "pvf_sha256": identity_document["sha256"],
            "item_count": record_count,
            "category_counts": counts,
            "content_sha256": meta["content_sha256"],
            "generated_unix": int(meta["generated_unix"]),
        }
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError("GM item catalog metadata is invalid") from exc
    finally:
        connection.close()


def _escaped_like_pattern(value: str, *, contains: bool) -> str:
    escaped = value.replace("\\", "\\\\").replace("_", "\\_")
    return "%" + escaped + "%" if contains else escaped


class SqliteGmItemCatalog:
    """Read-only query facade; each request owns a short SQLite connection."""

    def __init__(
        self,
        path: Path,
        *,
        expected_identity: Any = None,
        expected_pvf_sha256: str = "",
        expected_artifact_sha256: str = "",
        expected_artifact_size: int = 0,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        expected_file_sha = str(expected_artifact_sha256 or "").lower()
        if expected_file_sha:
            actual_file_sha, actual_file_size = catalog_file_sha256(self.path)
            if actual_file_sha != expected_file_sha:
                raise ValueError("GM item catalog artifact SHA-256 mismatch")
            if int(expected_artifact_size or 0) > 0 and actual_file_size != int(
                expected_artifact_size
            ):
                raise ValueError("GM item catalog artifact size mismatch")
        self.metadata = validate_catalog_database(
            self.path,
            expected_identity=expected_identity,
            expected_pvf_sha256=expected_pvf_sha256,
            verify_digest=not bool(expected_file_sha),
        )
        self.pvf_sha256 = str(self.metadata["pvf_sha256"])
        self.record_count = int(self.metadata["item_count"])

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
        clauses: list[str] = []
        parameters: list[Any] = []
        if category_key != "all":
            clauses.append("category = ?")
            parameters.append(category_key)
        if needle and all("0" <= char <= "9" for char in needle):
            numeric_id = int(needle)
            if numeric_id > 0x7FFFFFFFFFFFFFFF:
                clauses.append("0")
            else:
                clauses.append("item_id = ?")
                parameters.append(numeric_id)
        elif needle:
            has_wildcard = "%" in needle
            pattern = _escaped_like_pattern(needle, contains=not has_wildcard)
            clauses.append(
                "(CAST(item_id AS TEXT) LIKE ? ESCAPE '\\' OR "
                "search_name LIKE ? ESCAPE '\\' OR "
                "search_path LIKE ? ESCAPE '\\')"
            )
            parameters.extend((pattern, pattern, pattern))
        where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
        connection = _readonly_connection(self.path)
        try:
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM items" + where_sql,
                    parameters,
                ).fetchone()[0]
            )
            page_count = max(1, (total + page_size_value - 1) // page_size_value)
            if page_value > page_count:
                page_value = page_count
            offset = (page_value - 1) * page_size_value
            rows = connection.execute(
                "SELECT item_id, name, category, rarity, minimum_level, "
                "stack_limit, source_path FROM items"
                + where_sql
                + " ORDER BY item_id LIMIT ? OFFSET ?",
                parameters + [page_size_value, offset],
            ).fetchall()
        finally:
            connection.close()
        return {
            "items": [
                {
                    "item_id": int(row["item_id"]),
                    "name": str(row["name"]),
                    "category": str(row["category"]),
                    "rarity": int(row["rarity"]),
                    "minimum_level": int(row["minimum_level"]),
                    "stack_limit": int(row["stack_limit"]),
                    "source_path": str(row["source_path"]),
                }
                for row in rows
            ],
            "page": page_value,
            "page_size": page_size_value,
            "page_count": page_count,
            "total": total,
            "search": search_value,
            "category": category_key,
            "pvf_sha256": self.pvf_sha256,
        }

    def close(self) -> None:
        return None


__all__ = [
    "CATALOG_ARTIFACT_NAME",
    "CATALOG_BUILDER_VERSION",
    "CATALOG_INSERT_BATCH_SIZE",
    "CATALOG_SQLITE_MAGIC",
    "CATALOG_SQLITE_SCHEMA_VERSION",
    "SqliteGmItemCatalog",
    "catalog_artifact_path",
    "catalog_file_sha256",
    "publish_catalog_database",
    "validate_catalog_database",
]
