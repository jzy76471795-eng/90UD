"""Crash-safe persistent cache for verified PVF runtime indexes.

The cache is strictly a derived artifact.  It is keyed by the complete in-memory
Script.pvf SHA-256 and is never used to replace the mandatory PVF preload.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional


CACHE_MAGIC = "USDOF_PVF_INDEX_CACHE"
CACHE_FORMAT_VERSION = 4
_LOCK = threading.RLock()


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or not str(value).strip():
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "off", "n"}


@dataclass(frozen=True)
class PvfIdentity:
    path: str
    sha256: str
    file_size: int
    file_count: int
    group_count: int
    python: str

    @property
    def sha_prefix(self) -> str:
        return self.sha256[:12]


class PvfIndexCache:
    """One boot's PVF-index cache context.

    ``read`` only accepts a fully verified artifact.  Any failed decode removes
    the affected file and yields ``None`` so callers take their existing build
    path without weakening startup correctness.
    """

    def __init__(
        self,
        identity: PvfIdentity,
        *,
        root: Optional[Path] = None,
        enabled: Optional[bool] = None,
        clear: Optional[bool] = None,
    ) -> None:
        from data import PVF_INDEX_CACHE_DIR

        self.identity = identity
        self.enabled = _env_bool("USDOF_PVF_INDEX_CACHE", True) if enabled is None else bool(enabled)
        self.clear_requested = _env_bool("USDOF_PVF_INDEX_CACHE_CLEAR", False) if clear is None else bool(clear)
        configured = os.environ.get("USDOF_PVF_INDEX_CACHE_DIR", "").strip()
        self.root = Path(configured).expanduser() if configured else (root or PVF_INDEX_CACHE_DIR)
        self.root = self.root.resolve()
        path_token = hashlib.sha256(identity.path.encode("utf-8")).hexdigest()[:24]
        self.path_root = self.root / path_token
        self.directory = self.path_root / identity.sha256
        self.cleanup_count = 0
        self.last_status: dict[str, str] = {}
        if self.enabled:
            self._prepare()

    def _prepare(self) -> None:
        with _LOCK:
            self.path_root.mkdir(parents=True, exist_ok=True)
            for child in list(self.path_root.iterdir()):
                if child.name == self.identity.sha256:
                    continue
                self._remove(child)
                self.cleanup_count += 1
            self.directory.mkdir(parents=True, exist_ok=True)
            for child in list(self.directory.iterdir()):
                if child.name.endswith(".tmp"):
                    self._remove(child)
                    self.cleanup_count += 1
            if self.clear_requested:
                for child in list(self.directory.iterdir()):
                    self._remove(child)
                    self.cleanup_count += 1

    @staticmethod
    def _remove(path: Path) -> None:
        try:
            if path.is_dir():
                shutil.rmtree(str(path))
            else:
                path.unlink()
        except FileNotFoundError:
            pass

    def artifact_path(self, domain: str) -> Path:
        if not domain or "/" in domain or "\\" in domain:
            raise ValueError("invalid PVF cache domain: %r" % domain)
        return self.directory / (domain + ".json.gz")

    def read(self, domain: str, semantic_version: int) -> Optional[dict[str, Any]]:
        if not self.enabled or self.clear_requested:
            self.last_status[domain] = "disabled" if not self.enabled else "rebuild"
            return None
        path = self.artifact_path(domain)
        if not path.is_file():
            self.last_status[domain] = "miss"
            return None
        try:
            with gzip.open(str(path), "rb") as handle:
                document = json.loads(handle.read().decode("utf-8"))
            manifest = document["manifest"]
            payload = document["payload"]
            if manifest.get("magic") != CACHE_MAGIC:
                raise ValueError("magic")
            if manifest.get("format_version") != CACHE_FORMAT_VERSION:
                raise ValueError("format_version")
            if manifest.get("domain") != domain or int(manifest.get("semantic_version")) != int(semantic_version):
                raise ValueError("domain/version")
            if manifest.get("pvf_identity") != asdict(self.identity):
                raise ValueError("PVF identity")
            raw_payload = self._canonical_payload(payload)
            if manifest.get("payload_sha256") != hashlib.sha256(raw_payload).hexdigest():
                raise ValueError("payload sha256")
            if int(manifest.get("record_count", -1)) < 0:
                raise ValueError("record count")
            if not isinstance(payload, dict):
                raise ValueError("payload type")
        except Exception:
            self._remove(path)
            self.last_status[domain] = "rebuild"
            return None
        self.last_status[domain] = "hit"
        return payload

    def write(self, domain: str, semantic_version: int, payload: dict[str, Any], *, record_count: int) -> None:
        if not self.enabled:
            self.last_status[domain] = "disabled"
            return
        raw_payload = self._canonical_payload(payload)
        document = {
            "manifest": {
                "magic": CACHE_MAGIC,
                "format_version": CACHE_FORMAT_VERSION,
                "domain": domain,
                "semantic_version": int(semantic_version),
                "pvf_identity": asdict(self.identity),
                "payload_sha256": hashlib.sha256(raw_payload).hexdigest(),
                "record_count": int(record_count),
                "generated_unix": int(time.time()),
            },
            "payload": payload,
        }
        encoded = json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        compressed = gzip.compress(encoded, compresslevel=6)
        path = self.artifact_path(domain)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(compressed)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, str(path))
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
        previous = self.last_status.get(domain)
        self.last_status[domain] = "rebuild" if (self.clear_requested or previous == "rebuild") else "miss"

    def discard(self, domain: str) -> None:
        if self.enabled:
            self._remove(self.artifact_path(domain))
        self.last_status[domain] = "rebuild"

    @staticmethod
    def _canonical_payload(payload: Any) -> bytes:
        return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def create_cache_context(*, clear: Optional[bool] = None) -> PvfIndexCache:
    """Build identity from the prepared memory reader without reopening PVF.

    ``clear=False`` is used by a post-start GM worker coordinator.  The normal
    boot path keeps the environment-selected clear behavior, while a lazy
    worker must not erase the domains that the game process has just prepared.
    """
    from config import PVF_PATH
    from pvf import pvf_runtime

    stats = pvf_runtime.last_preload_stats()
    if stats is None or not stats.memory_backed or not pvf_runtime.is_memory_prepared(PVF_PATH):
        raise RuntimeError("PVF index cache requires prepared memory PVF structure")
    identity = PvfIdentity(
        path=str(Path(stats.path).resolve()),
        sha256=pvf_runtime.source_sha256(PVF_PATH),
        file_size=int(stats.file_size),
        file_count=int(stats.file_count),
        group_count=int(stats.group_count),
        python="%d.%d.%d" % sys.version_info[:3],
    )
    return PvfIndexCache(identity, clear=clear)


def _metadata_to_payload(metadata: Any) -> dict[str, Any]:
    return {
        "item_id": int(metadata.item_id), "item_kind": metadata.item_kind,
        "stackable_type": metadata.stackable_type, "stack_limit": int(metadata.stack_limit),
        "equipment_type": metadata.equipment_type, "is_avatar": bool(metadata.is_avatar),
        "durability": int(metadata.durability), "minimum_level": int(metadata.minimum_level),
        "usable_jobs": list(metadata.usable_jobs), "suitable_jobs": list(metadata.suitable_jobs),
        "usable_period_days": int(metadata.usable_period_days),
        "expiration_timestamp": int(metadata.expiration_timestamp), "attach_type": metadata.attach_type,
        "name": metadata.name, "source_path": metadata.source_path, "note": metadata.note,
        "buy_price": int(metadata.buy_price), "sell_value": int(metadata.sell_value),
        "need_material_id": int(metadata.need_material_id),
        "need_material_count": int(metadata.need_material_count), "rarity": int(metadata.rarity),
        "set_item_ids": list(metadata.set_item_ids),
        "impossible_contents": list(metadata.impossible_contents),
        "item_categories": list(metadata.item_categories),
        "grade": int(metadata.grade),
        "avatar_option_change_scope": list(metadata.avatar_option_change_scope),
        "avatar_option_change_grade": int(metadata.avatar_option_change_grade),
        "avatar_option_change_mode": int(metadata.avatar_option_change_mode),
        "avatar_emblem_target_types": list(metadata.avatar_emblem_target_types),
        "avatar_socket_types": list(metadata.avatar_socket_types),
        "avatar_socket_source": metadata.avatar_socket_source,
        "custom_ability_type": metadata.custom_ability_type,
        "custom_ability_count": int(metadata.custom_ability_count),
        "custom_ability_part_set_index": int(metadata.custom_ability_part_set_index),
        "equipment_sub_type": int(metadata.equipment_sub_type),
        "output_index": int(getattr(metadata, "output_index", 0) or 0),
        "action_type": str(getattr(metadata, "action_type", "") or ""),
        "action_values": list(getattr(metadata, "action_values", ()) or ()),
    }


def build_shop_bundle_payload() -> dict[str, Any]:
    """Serialize the already warmed shop object graph and referenced metadata."""
    from game.character.item_metadata import snapshot_resolved_item_metadata
    from game.shop.booster_item_index import get_booster_item_index
    from game.shop.cera_package_index import get_cera_package_index
    from game.shop.cerashop_index import get_cerashop_index

    shop = get_cerashop_index()
    packages = get_cera_package_index()
    boosters = get_booster_item_index()
    return {
        "products": [
            [p.commodity_id, p.item_id, p.quantity, p.cera_price, p.section, p.raw_quantity, p.source_path, p.row_index]
            for _, p in sorted(shop.products.items())
        ],
        "skipped_duplicates": [
            [
                duplicate.skipped.commodity_id,
                duplicate.skipped.item_id,
                duplicate.skipped.quantity,
                duplicate.skipped.cera_price,
                duplicate.skipped.section,
                duplicate.skipped.raw_quantity,
                duplicate.skipped.source_path,
                duplicate.skipped.row_index,
            ]
            for duplicate in shop.skipped_duplicates
        ],
        "shop_source_path": shop.source_path,
        "packages": [
            [package_id, definition.source_path, [[reward.item_id, reward.count] for reward in definition.rewards]]
            for package_id, definition in sorted(packages.packages.items())
        ],
        "skipped_packages": [
            [skipped.package_item_id, skipped.source_path, skipped.reason]
            for skipped in packages.skipped_packages
        ],
        "boosters": [
            [
                item_id,
                definition.stackable_type,
                definition.source_path,
                [
                    [
                        group.draw_count,
                        [
                            [
                                candidate.item_id,
                                candidate.weight,
                                candidate.count,
                                candidate.option_value,
                            ]
                            for candidate in group.candidates
                        ],
                    ]
                    for group in definition.groups
                ],
                [
                    [
                        candidate.child_name,
                        candidate.group_index,
                        candidate.candidate_index,
                        candidate.item_id,
                        candidate.weight,
                        candidate.count,
                    ]
                    for candidate in definition.skipped_zero_count_candidates
                ],
            ]
            for item_id, definition in sorted(boosters.definitions.items())
        ],
        "selection_boosters": [
            [item_id, definition.stackable_type, list(definition.category_dimensions), definition.selection_count,
             definition.source_path,
             [[category.category_a, category.category_b,
               [[reward.item_id, reward.count, reward.child_name, int(reward.wire_avatar), reward.avatar_option_mode, reward.avatar_default_option] for reward in category.rewards]]
              for _, category in sorted(definition.categories.items())]]
            for item_id, definition in sorted(boosters.selection_definitions.items())
        ],
        "metadata": [_metadata_to_payload(value) for _, value in sorted(snapshot_resolved_item_metadata().items())],
    }


def install_shop_bundle_payload(payload: dict[str, Any]) -> None:
    """Restore a shop bundle and run the normal warmup validators afterwards."""
    from game.character.item_metadata import ItemMetadata, install_preset_item_metadata
    from game.shop.booster_item_index import (
        BoosterItemDefinition, BoosterItemIndex, BoosterRewardCandidate,
        BoosterRewardGroup, BoosterSkippedCandidate,
        install_booster_item_index,
    )
    from game.shop.booster_selection_index import (
        BoosterSelectionCategory, BoosterSelectionDefinition, BoosterSelectionReward,
    )
    from game.shop.cera_package_index import (
        CeraPackageDefinition, CeraPackageIndex, CeraPackageReward, CeraPackageSkipped,
        install_cera_package_index,
        is_usable_cera_package,
    )
    from game.shop.cerashop_index import (
        CerashopIndex,
        CerashopProduct,
        CerashopSkippedDuplicate,
        install_cerashop_index,
    )
    from game.shop.booster_item_index import is_direct_booster_metadata
    from game.shop.booster_selection_index import is_selection_booster_metadata

    required = (
        "products",
        "skipped_duplicates",
        "packages",
        "skipped_packages",
        "boosters",
        "selection_boosters",
        "metadata",
    )
    if not isinstance(payload, dict) or any(not isinstance(payload.get(key), list) for key in required):
        raise ValueError("shop bundle shape")
    metadata: dict[int, ItemMetadata] = {}
    for row in payload["metadata"]:
        if not isinstance(row, dict):
            raise ValueError("metadata row")
        values = dict(row)
        for key in (
            "usable_jobs",
            "suitable_jobs",
            "impossible_contents",
            "item_categories",
            "set_item_ids",
            "avatar_option_change_scope",
            "avatar_emblem_target_types",
            "avatar_socket_types",
            "action_values",
        ):
            if not isinstance(values.get(key, []), list):
                raise ValueError("metadata tuple field")
            if key in {
                "avatar_socket_types",
                "avatar_emblem_target_types",
                "set_item_ids",
                "action_values",
            }:
                values[key] = tuple(int(value) for value in values[key])
            else:
                values[key] = tuple(str(value) for value in values[key])
        item = ItemMetadata(**values)
        if item.item_id <= 0 or item.item_id in metadata:
            raise ValueError("metadata identity")
        metadata[item.item_id] = item
    products: dict[int, CerashopProduct] = {}
    for row in payload["products"]:
        if not isinstance(row, list) or len(row) != 8:
            raise ValueError("shop product row")
        product = CerashopProduct(int(row[0]), int(row[1]), int(row[2]), int(row[3]), str(row[4]), int(row[5]), str(row[6]), int(row[7]))
        if product.commodity_id <= 0 or product.item_id not in metadata or product.commodity_id in products:
            raise ValueError("shop product reference")
        products[product.commodity_id] = product
    skipped_duplicates = []
    for row in payload["skipped_duplicates"]:
        if not isinstance(row, list) or len(row) != 8:
            raise ValueError("shop skipped duplicate row")
        skipped = CerashopProduct(
            int(row[0]),
            int(row[1]),
            int(row[2]),
            int(row[3]),
            str(row[4]),
            int(row[5]),
            str(row[6]),
            int(row[7]),
        )
        kept = products.get(skipped.commodity_id)
        if skipped.commodity_id <= 0 or skipped.item_id <= 0 or kept is None:
            raise ValueError("shop skipped duplicate reference")
        skipped_duplicates.append(
            CerashopSkippedDuplicate(skipped.commodity_id, kept, skipped)
        )
    packages: dict[int, CeraPackageDefinition] = {}
    for row in payload["packages"]:
        if not isinstance(row, list) or len(row) != 3:
            raise ValueError("package row")
        package_id = int(row[0])
        rewards = tuple(CeraPackageReward(int(value[0]), int(value[1])) for value in row[2])
        if (
            package_id not in metadata
            or not is_usable_cera_package(metadata[package_id])
            or not rewards
            or any(reward.item_id not in metadata or reward.count <= 0 for reward in rewards)
        ):
            raise ValueError("package reference")
        packages[package_id] = CeraPackageDefinition(package_id, rewards, str(row[1]))
    skipped_packages = []
    for row in payload["skipped_packages"]:
        if not isinstance(row, list) or len(row) != 3:
            raise ValueError("skipped package row")
        package_id = int(row[0])
        if package_id <= 0:
            raise ValueError("skipped package reference")
        skipped_packages.append(
            CeraPackageSkipped(package_id, str(row[1]), str(row[2]))
        )
    definitions: dict[int, BoosterItemDefinition] = {}
    for row in payload["boosters"]:
        if not isinstance(row, list) or len(row) != 5:
            raise ValueError("booster row")
        item_id = int(row[0])
        groups = tuple(
            BoosterRewardGroup(
                int(group[0]),
                tuple(
                    BoosterRewardCandidate(
                        int(value[0]), int(value[1]), int(value[2]), int(value[3])
                    )
                    for value in group[1]
                ),
            )
            for group in row[3]
        )
        skipped_zero_count_candidates = tuple(
            BoosterSkippedCandidate(
                item_id,
                str(row[2]),
                str(value[0]),
                int(value[1]),
                int(value[2]),
                int(value[3]),
                int(value[4]),
                int(value[5]),
            )
            for value in row[4]
        )
        if (
            item_id not in metadata
            or not is_direct_booster_metadata(metadata[item_id])
            or any(candidate.item_id not in metadata for group in groups for candidate in group.candidates)
        ):
            raise ValueError("booster reference")
        definitions[item_id] = BoosterItemDefinition(
            item_id,
            str(row[1]),
            groups,
            str(row[2]),
            skipped_zero_count_candidates,
        )
    selections: dict[int, BoosterSelectionDefinition] = {}
    for row in payload["selection_boosters"]:
        if not isinstance(row, list) or len(row) != 6:
            raise ValueError("selection booster row")
        item_id = int(row[0])
        categories = {}
        for category in row[5]:
            rewards = tuple(BoosterSelectionReward(int(value[0]), int(value[1]), str(value[2]), bool(value[3]), int(value[4]), int(value[5])) for value in category[2])
            key = (int(category[0]), int(category[1]))
            categories[key] = BoosterSelectionCategory(key[0], key[1], rewards)
        if (
            item_id not in metadata
            or not is_selection_booster_metadata(metadata[item_id])
            or not categories
            or any(reward.item_id not in metadata for category in categories.values() for reward in category.rewards)
        ):
            raise ValueError("selection booster reference")
        selections[item_id] = BoosterSelectionDefinition(item_id, str(row[1]), tuple(int(value) for value in row[2]), int(row[3]), categories, str(row[4]))
    if not products:
        raise ValueError("empty shop products")
    install_preset_item_metadata(metadata)
    install_cerashop_index(
        CerashopIndex(
            products,
            str(payload.get("shop_source_path") or ""),
            tuple(skipped_duplicates),
        )
    )
    install_cera_package_index(CeraPackageIndex(packages, tuple(skipped_packages)))
    install_booster_item_index(BoosterItemIndex(definitions, selections))


def shop_bundle_record_count(payload: dict[str, Any]) -> int:
    return sum(len(payload.get(key) or ()) for key in ("products", "packages", "boosters", "selection_boosters", "metadata"))


__all__ = [
    "PvfIdentity", "PvfIndexCache", "build_shop_bundle_payload", "create_cache_context",
    "install_shop_bundle_payload", "shop_bundle_record_count",
]
