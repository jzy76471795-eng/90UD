"""PVF-driven normalization for deterministic Cera-booster shop wrappers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from game.character.character_db import InventoryGrantSpec
from game.character.item_metadata import ItemMetadata, resolve_item_metadata
from game.shop.booster_item_index import (
    normalize_booster_stackable_type,
    resolve_booster_item,
)


CERA_BOOSTER_TYPE_PREFIX = "[cera booster]"
_MAX_U32 = 0xFFFFFFFF


@dataclass(frozen=True)
class CeraBoosterWrapperResolution:
    """One deterministic PVF ``[cera booster]`` wrapper transition."""

    source_item_id: int
    source_metadata: ItemMetadata
    reward_item_id: int
    reward_count: int
    reward_metadata: ItemMetadata


def is_cera_booster_metadata(metadata: object) -> bool:
    """Return whether metadata denotes the non-client-usable Cera wrapper."""
    stackable_type = normalize_booster_stackable_type(
        str(getattr(metadata, "stackable_type", "") or "")
    )
    return bool(getattr(metadata, "is_stackable", False)) and stackable_type.startswith(
        CERA_BOOSTER_TYPE_PREFIX
    )


def resolve_deterministic_cera_booster_wrapper(
    source_item_id: int,
    *,
    source_metadata: ItemMetadata | None = None,
) -> CeraBoosterWrapperResolution | None:
    """Resolve one current-PVF Cera wrapper without rolling or delivering it.

    ``[cera booster]`` is a shop transport wrapper.  It must have exactly one
    positive, deterministic child before the server may replace the wrapper
    at checkout or during legacy-inventory migration.
    """
    source_id = int(source_item_id) & _MAX_U32
    if source_id <= 0:
        raise ValueError("invalid_cera_booster_source=%r" % source_item_id)
    metadata = (
        source_metadata
        if source_metadata is not None
        else resolve_item_metadata(source_id)
    )
    metadata_id = int(getattr(metadata, "item_id", source_id) or source_id) & _MAX_U32
    if metadata_id != source_id:
        raise ValueError(
            "cera_booster_source_metadata_mismatch source=%d metadata=%d"
            % (source_id, metadata_id)
        )
    if not is_cera_booster_metadata(metadata):
        return None

    definition = resolve_booster_item(source_id)
    if definition is None:
        raise ValueError("cera_booster_definition_missing source=%d" % source_id)
    if len(definition.groups) != 1:
        raise ValueError(
            "cera_booster_group_count source=%d groups=%d"
            % (source_id, len(definition.groups))
        )
    group = definition.groups[0]
    if int(group.draw_count) != 1 or len(group.candidates) != 1:
        raise ValueError(
            "cera_booster_not_deterministic source=%d draw_count=%d candidates=%d"
            % (source_id, int(group.draw_count), len(group.candidates))
        )
    candidate = group.candidates[0]
    reward_id = int(candidate.item_id) & _MAX_U32
    reward_count = int(candidate.count)
    if reward_id <= 0 or reward_id == source_id:
        raise ValueError(
            "cera_booster_reward_id_invalid source=%d reward=%d"
            % (source_id, reward_id)
        )
    if int(candidate.weight) <= 0 or reward_count <= 0 or reward_count > _MAX_U32:
        raise ValueError(
            "cera_booster_reward_invalid source=%d reward=%d weight=%d count=%d"
            % (source_id, reward_id, int(candidate.weight), reward_count)
        )
    reward_metadata = resolve_item_metadata(reward_id)
    if reward_metadata.item_kind not in {"equipment", "stackable"}:
        raise ValueError(
            "cera_booster_reward_metadata_unavailable source=%d reward=%d kind=%s"
            % (source_id, reward_id, reward_metadata.item_kind)
        )
    return CeraBoosterWrapperResolution(
        source_item_id=source_id,
        source_metadata=metadata,
        reward_item_id=reward_id,
        reward_count=reward_count,
        reward_metadata=reward_metadata,
    )


def build_cera_booster_wrapper_grant(
    resolution: CeraBoosterWrapperResolution,
    *,
    copies: int = 1,
    expire_time: int = 0,
) -> InventoryGrantSpec:
    """Build the atomically delivered inner-item grant for wrapper copies."""
    multiplier = int(copies)
    if multiplier <= 0 or multiplier > _MAX_U32:
        raise ValueError("invalid_cera_booster_copy_count=%d" % multiplier)
    count = int(resolution.reward_count) * multiplier
    if count <= 0 or count > _MAX_U32:
        raise ValueError(
            "cera_booster_reward_count_overflow source=%d count=%d"
            % (int(resolution.source_item_id), count)
        )
    expiry = int(expire_time)
    if expiry < 0 or expiry > _MAX_U32:
        raise ValueError("invalid_cera_booster_expire_time=%d" % expiry)
    return InventoryGrantSpec(
        item_id=int(resolution.reward_item_id),
        count=count,
        metadata=resolution.reward_metadata,
        expire_time=expiry,
    )


def migrate_cera_booster_wrappers_for_character(
    repo: Any,
    *,
    account_id: int,
    character_id: int,
    now_unix: int | None = None,
) -> dict[str, Any]:
    """Replace legacy one-count Cera wrappers before the START inventory view.

    The repository's existing package transaction keeps source consumption,
    target insertion, capacity checks, and all changed-slot accounting atomic.
    Invalid or non-deterministic wrappers remain untouched and are reported in
    the result instead of blocking character entry.
    """
    account = int(account_id)
    character = int(character_id)
    now = None if now_unix is None else max(0, int(now_unix))
    result: dict[str, Any] = {
        "migrated_rows": 0,
        "migrated_items": 0,
        "changed_slots": {},
        "migrated": [],
        "skipped": [],
    }
    if account <= 0 or character <= 0:
        result["error"] = "invalid_owner"
        return result

    try:
        rows = repo.list_inventory_items(
            character,
            list_type=0,
            now_unix=now,
        )
    except Exception as exc:
        result["error"] = "inventory_read_error=%s:%s" % (
            type(exc).__name__,
            exc,
        )
        return result

    changed_by_list: dict[int, set[int]] = {}
    for row in rows:
        source_id = int(row.get("item_template_id", 0) or 0) & _MAX_U32
        source_slot = int(row.get("slot_index", -1) or -1)
        if source_id <= 0 or source_slot < 0 or source_slot > 0xFFFF:
            continue
        try:
            resolution = resolve_deterministic_cera_booster_wrapper(source_id)
        except Exception as exc:
            result["skipped"].append(
                "source=%d slot=%d resolve=%s:%s"
                % (source_id, source_slot, type(exc).__name__, exc)
            )
            continue
        if resolution is None:
            continue

        source_count = max(0, int(row.get("stack_count", 0) or 0))
        if source_count != 1:
            result["skipped"].append(
                "source=%d slot=%d unsupported_stack_count=%d"
                % (source_id, source_slot, source_count)
            )
            continue
        try:
            grant = build_cera_booster_wrapper_grant(
                resolution,
                expire_time=max(0, int(row.get("expire_time", 0) or 0)),
            )
            ok, note, detail = repo.open_cera_package(
                account_id=account,
                character_id=character,
                source_slot=source_slot,
                expected_package_item_id=source_id,
                grants=(grant,),
                source_metadata=resolution.source_metadata,
                allowed_source_types=(CERA_BOOSTER_TYPE_PREFIX,),
                enforce_reward_job_restrictions=True,
                now_unix=now,
            )
        except Exception as exc:
            result["skipped"].append(
                "source=%d slot=%d transaction=%s:%s"
                % (source_id, source_slot, type(exc).__name__, exc)
            )
            continue
        if not ok:
            result["skipped"].append(
                "source=%d slot=%d transaction=%s" % (source_id, source_slot, note)
            )
            continue

        result["migrated_rows"] += 1
        result["migrated_items"] += source_count
        result["migrated"].append(
            {
                "source_item_id": source_id,
                "source_slot": source_slot,
                "reward_item_id": int(resolution.reward_item_id),
                "reward_count": int(grant.count),
            }
        )
        for list_type, slots in dict(detail.get("changed_slots", {}) or {}).items():
            bucket = changed_by_list.setdefault(int(list_type), set())
            bucket.update(int(slot) for slot in slots)

    result["changed_slots"] = {
        list_type: sorted(slots)
        for list_type, slots in sorted(changed_by_list.items())
        if slots
    }
    return result


__all__ = [
    "CERA_BOOSTER_TYPE_PREFIX",
    "CeraBoosterWrapperResolution",
    "build_cera_booster_wrapper_grant",
    "is_cera_booster_metadata",
    "migrate_cera_booster_wrappers_for_character",
    "resolve_deterministic_cera_booster_wrapper",
]
