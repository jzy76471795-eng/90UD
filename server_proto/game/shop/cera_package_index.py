"""PVF-backed definitions for Cera avatar packages opened by C1/0207."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from game.character.item_metadata import ItemMetadata, resolve_item_metadata
from game.shop.cerashop_index import get_cerashop_index
from pvf import pvf_runtime


@dataclass(frozen=True)
class CeraPackageReward:
    item_id: int
    count: int


@dataclass(frozen=True)
class CeraPackageDefinition:
    package_item_id: int
    rewards: tuple[CeraPackageReward, ...]
    source_path: str = ""


@dataclass(frozen=True)
class CeraPackageSkipped:
    """A catalog package omitted because its own PVF definition is malformed."""

    package_item_id: int
    source_path: str
    reason: str


@dataclass(frozen=True)
class CeraPackageIndex:
    packages: dict[int, CeraPackageDefinition]
    skipped_packages: tuple[CeraPackageSkipped, ...] = ()

    def resolve(self, package_item_id: int) -> CeraPackageDefinition | None:
        return self.packages.get(int(package_item_id) & 0xFFFFFFFF)


_LOCK = threading.RLock()
_INDEX: CeraPackageIndex | None = None
_EXTERNAL_DEFINITIONS: dict[int, CeraPackageDefinition] = {}


def _normalized_stackable_type(metadata: Any) -> str:
    return " ".join(str(getattr(metadata, "stackable_type", "") or "").lower().split())


def is_usable_cera_package(metadata: Any) -> bool:
    return _normalized_stackable_type(metadata).startswith("[usable cera package]")


def _package_data_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    in_package_data = False
    section_count = 0

    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        low = line.lower()
        if low == "[package data]":
            if in_package_data:
                raise ValueError("nested [package data] section")
            in_package_data = True
            section_count += 1
            continue
        if low == "[/package data]":
            if not in_package_data:
                raise ValueError("orphan [/package data] section")
            in_package_data = False
            continue
        if in_package_data and line:
            tokens.extend(line.split())

    if in_package_data:
        raise ValueError("unclosed [package data] section")
    if section_count <= 0:
        raise ValueError("missing [package data] section")
    return tokens


def parse_cera_package_text(
    package_item_id: int,
    text: str,
    *,
    source_path: str = "",
) -> CeraPackageDefinition:
    """Parse only ``[package data]``; display-only shop previews are ignored."""
    package_id = int(package_item_id) & 0xFFFFFFFF
    if package_id <= 0:
        raise ValueError("invalid package_item_id=%r" % package_item_id)

    tokens = _package_data_tokens(text)
    if not tokens or len(tokens) % 2 != 0:
        raise ValueError(
            "package data must contain item/count pairs package=%d tokens=%d"
            % (package_id, len(tokens))
        )

    rewards: list[CeraPackageReward] = []
    for offset in range(0, len(tokens), 2):
        try:
            item_id = int(tokens[offset], 0)
            count = int(tokens[offset + 1], 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "invalid package data pair package=%d pair=%d values=%r/%r"
                % (package_id, offset // 2, tokens[offset], tokens[offset + 1])
            ) from exc
        if item_id <= 0 or item_id > 0xFFFFFFFF:
            raise ValueError(
                "invalid reward item package=%d pair=%d item=%d"
                % (package_id, offset // 2, item_id)
            )
        if count <= 0 or count > 0xFFFFFFFF:
            raise ValueError(
                "invalid reward count package=%d pair=%d count=%d"
                % (package_id, offset // 2, count)
            )
        rewards.append(CeraPackageReward(item_id=int(item_id), count=int(count)))

    return CeraPackageDefinition(
        package_item_id=package_id,
        rewards=tuple(rewards),
        source_path=str(source_path or ""),
    )


def _load_index_from_shared_reader() -> CeraPackageIndex:
    catalog = get_cerashop_index()
    package_ids: set[int] = set()
    package_metadata: dict[int, ItemMetadata] = {}

    for product in catalog.products.values():
        item_id = int(product.item_id) & 0xFFFFFFFF
        metadata = resolve_item_metadata(item_id)
        if not is_usable_cera_package(metadata):
            continue
        package_ids.add(item_id)
        package_metadata[item_id] = metadata

    packages: dict[int, CeraPackageDefinition] = {}
    skipped_packages: list[CeraPackageSkipped] = []
    for package_id in sorted(package_ids):
        metadata = package_metadata[package_id]
        source_path = str(getattr(metadata, "source_path", "") or "").strip()
        if not source_path:
            raise RuntimeError("Cera package source path missing item=%d" % package_id)
        text = pvf_runtime.read_text(source_path)
        try:
            definition = parse_cera_package_text(
                package_id,
                text,
                source_path=source_path,
            )
        except ValueError as exc:
            skipped_packages.append(
                CeraPackageSkipped(
                    package_item_id=package_id,
                    source_path=source_path,
                    reason="package_data_invalid:%s" % exc,
                )
            )
            continue

        packages[package_id] = definition
    return CeraPackageIndex(
        packages=packages,
        skipped_packages=tuple(skipped_packages),
    )


def get_cera_package_index() -> CeraPackageIndex:
    global _INDEX
    with _LOCK:
        if _INDEX is None:
            _INDEX = _load_index_from_shared_reader()
        return _INDEX


def _resolve_external_cera_package(
    package_item_id: int,
) -> CeraPackageDefinition | None:
    """Resolve one valid non-shop package from the shared PVF on demand."""
    package_id = int(package_item_id) & 0xFFFFFFFF
    if package_id <= 0:
        return None
    with _LOCK:
        cached = _EXTERNAL_DEFINITIONS.get(package_id)
        if cached is not None:
            return cached

        metadata = resolve_item_metadata(package_id)
        if not is_usable_cera_package(metadata):
            return None
        source_path = str(getattr(metadata, "source_path", "") or "").strip()
        if not source_path:
            return None
        try:
            definition = parse_cera_package_text(
                package_id,
                pvf_runtime.read_text(source_path),
                source_path=source_path,
            )
        except ValueError:
            return None
        _EXTERNAL_DEFINITIONS[package_id] = definition
        return definition


def resolve_cera_package(package_item_id: int) -> CeraPackageDefinition | None:
    package_id = int(package_item_id) & 0xFFFFFFFF
    definition = get_cera_package_index().resolve(package_id)
    if definition is not None:
        return definition
    return _resolve_external_cera_package(package_id)


def clear_cera_package_index_cache() -> None:
    global _INDEX
    with _LOCK:
        _INDEX = None
        _EXTERNAL_DEFINITIONS.clear()


def install_cera_package_index(index: CeraPackageIndex) -> None:
    if not isinstance(index, CeraPackageIndex):
        raise ValueError("invalid Cera package index")
    global _INDEX
    with _LOCK:
        _INDEX = index
        _EXTERNAL_DEFINITIONS.clear()


def warmup_cera_package_index() -> dict[str, Any]:
    """Preload every usable Cera package referenced by the current shop PVF."""
    index = get_cera_package_index()
    reward_ids: set[int] = set()
    reward_units = 0
    avatar_units = 0
    main_units = 0
    job_restricted_rewards = 0
    filtered_reward_count = 0
    filtered_reward_units = 0
    filtered_reward_details: list[str] = []

    for definition in index.packages.values():
        source_meta = resolve_item_metadata(definition.package_item_id)
        if not is_usable_cera_package(source_meta):
            raise RuntimeError(
                "Cera package type mismatch item=%d type=%r"
                % (definition.package_item_id, source_meta.stackable_type)
            )
        for reward in definition.rewards:
            metadata = resolve_item_metadata(reward.item_id)
            if metadata.item_kind not in {"equipment", "stackable"} or not str(
                getattr(metadata, "note", "") or ""
            ).startswith("ok"):
                filtered_reward_count += 1
                filtered_reward_units += int(reward.count)
                filtered_reward_details.append(
                    "package_item_id=%d item_id=%d count=%d kind=%s note=%s"
                    % (
                        definition.package_item_id,
                        reward.item_id,
                        reward.count,
                        metadata.item_kind,
                        metadata.note,
                    )
                )
                continue
            reward_ids.add(int(reward.item_id))
            reward_units += int(reward.count)
            if bool(metadata.is_avatar):
                avatar_units += int(reward.count)
            else:
                main_units += int(reward.count)
            usable = tuple(getattr(metadata, "usable_jobs", ()) or ())
            suitable = tuple(getattr(metadata, "suitable_jobs", ()) or ())
            labels = tuple(str(value).strip().lower() for value in usable + suitable)
            if labels and not all(value in {"all", "[all]"} for value in labels):
                job_restricted_rewards += 1

    return {
        "ok": 1,
        "package_count": len(index.packages),
        "reward_item_count": len(reward_ids),
        "reward_units": reward_units,
        "avatar_units": avatar_units,
        "main_units": main_units,
        "job_restricted_rewards": job_restricted_rewards,
        "filtered_reward_count": filtered_reward_count,
        "filtered_reward_units": filtered_reward_units,
        "filtered_reward_details": tuple(filtered_reward_details),
        "skipped_package_count": len(index.skipped_packages),
        "skipped_package_details": tuple(
            "package_item_id=%d source=%s reason=%s"
            % (skipped.package_item_id, skipped.source_path, skipped.reason)
            for skipped in index.skipped_packages
        ),
    }


__all__ = [
    "CeraPackageDefinition",
    "CeraPackageIndex",
    "CeraPackageReward",
    "CeraPackageSkipped",
    "clear_cera_package_index_cache",
    "get_cera_package_index",
    "is_usable_cera_package",
    "parse_cera_package_text",
    "resolve_cera_package",
    "warmup_cera_package_index",
]
