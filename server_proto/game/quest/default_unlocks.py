"""PVF-backed default progress grants for local single-player profiles.

The index is built only during PVF boot warmup. Runtime callers consume the
cached current-``Script.pvf`` result and perform no PVF reads.
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from typing import Any, Iterable

from pvf import pvf_runtime


# Stage3 USERINFO1 actor state +0x19C:
#   0x01 auxiliary, 0x02 magic stone, 0x08 aura skin, 0x10 earring.
# The local profile intentionally starts with all four extended equipment
# slots open.  This is a product default, not a replacement for PVF parsing.
EX_EQUIP_ALL_OPEN_STATE = 0x1B

_LOCK = threading.RLock()
_INDEX: "DefaultUnlockIndex | None" = None


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return bool(default)
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "n"}


def _section_text(text: str, tag: str) -> str:
    pattern = re.compile(
        r"(?ims)^\s*\[" + re.escape(tag) + r"\]\s*$"
        r"(.*?)(?=^\s*\[[^\]]+\]|\Z)"
    )
    chunks = []
    for match in pattern.finditer(text or ""):
        chunk = match.group(1).strip()
        if chunk:
            chunks.append(chunk)
    return "\n".join(chunks)


def _section_ints(text: str, tag: str) -> tuple[int, ...]:
    return tuple(int(value) for value in re.findall(r"-?\d+", _section_text(text, tag)))


def _pair_values(values: Iterable[int]) -> tuple[tuple[int, int], ...]:
    data = list(values)
    return tuple(
        (int(data[index]), int(data[index + 1]))
        for index in range(0, len(data) - 1, 2)
        if int(data[index]) > 0
    )


@dataclass(frozen=True)
class HellWorldMap:
    path: str
    dungeon_ids: tuple[int, ...]
    quest_ids: tuple[int, ...]
    freepass_items: tuple[tuple[int, int], ...]
    item_conditions: tuple[int, ...]


@dataclass(frozen=True)
class DefaultUnlockIndex:
    hell_worldmaps: tuple[HellWorldMap, ...]
    hell_quest_ids: tuple[int, ...]
    hell_dungeon_ids: tuple[int, ...]
    # Registered [hell quest] roots plus QST prerequisite descendants that
    # belong to the same Hell achievement chain.  Keeping these separate
    # makes stale WDM IDs visible without writing them into player history.
    hell_chain_quest_ids: tuple[int, ...] = ()
    hell_ep1_quest_ids: tuple[int, ...] = ()
    slot_expansion_quest_ids: tuple[int, ...] = ()
    ignored_hell_quest_ids: tuple[int, ...] = ()

    def default_quest_ids(self, *, include_ep1: bool = True) -> tuple[int, ...]:
        """Return the current-PVF task IDs used by the local default profile."""
        hell = self.hell_chain_quest_ids or self.hell_quest_ids
        if not include_ep1 and self.hell_ep1_quest_ids:
            excluded = set(self.hell_ep1_quest_ids)
            hell = tuple(qid for qid in hell if qid not in excluded)
        return tuple(sorted({*hell, *self.slot_expansion_quest_ids}))


def _parse_hell_worldmap(path: str, text: str) -> HellWorldMap | None:
    hell_flag = _section_ints(text, "hell dungeon")
    if not hell_flag or int(hell_flag[0]) != 1:
        return None
    quest_ids = tuple(sorted({value for value in _section_ints(text, "hell quest") if value > 0}))
    if not quest_ids:
        return None
    dungeon_pairs = _pair_values(_section_ints(text, "dungeon"))
    dungeon_ids = tuple(sorted({dungeon_id for dungeon_id, _gate in dungeon_pairs if dungeon_id > 0}))
    return HellWorldMap(
        path=str(path).replace("\\", "/"),
        dungeon_ids=dungeon_ids,
        quest_ids=quest_ids,
        freepass_items=_pair_values(_section_ints(text, "hell freepass item")),
        item_conditions=tuple(value for value in _section_ints(text, "item condition") if value > 0),
    )


def _current_quest_index() -> Any:
    """Return the fully loaded QuestIndex warmed earlier in the boot order."""
    from game.quest.quest_index import get_quest_index

    index = get_quest_index()
    index.ensure_all_metas_loaded()
    return index


def _derive_quest_unlocks(
    root_ids: Iterable[int],
    quest_index: Any | None,
) -> tuple[
    tuple[int, ...],
    tuple[int, ...],
    tuple[int, ...],
    tuple[int, ...],
]:
    """Derive Hell descendants and slot-expansion tasks from current QST data.

    The WDM only names the first Hell achievement in each branch.  Subsequent
    ``n_quest/Hell/*.qst`` entries declare their relation through
    ``[pre required quest]``; following those edges removes the old fixed
    10103..10110 list while preserving the authored branch shape.
    """
    roots = tuple(sorted({int(qid) for qid in root_ids if int(qid) > 0}))
    if quest_index is None:
        return roots, roots, (), ()
    registered = {
        int(qid)
        for qid in getattr(quest_index, "quest_paths", {})
        if int(qid) > 0
    }
    roots = tuple(sorted(set(roots) & registered))
    metas = tuple(getattr(quest_index, "metas", {}).values())
    chain = set(roots)
    changed = True
    while changed:
        changed = False
        for meta in metas:
            qid = int(getattr(meta, "quest_id", 0) or 0)
            if qid <= 0 or qid in chain:
                continue
            path = str(getattr(meta, "path", "") or "").replace("\\", "/").lower()
            grade = str(getattr(meta, "grade", "") or "").lower()
            if "/hell/" not in ("/" + path) or "achievement" not in grade:
                continue
            groups = getattr(meta, "pre_groups", ()) or ()
            if any(group and all(int(dep) in chain for dep in group) for group in groups):
                chain.add(qid)
                changed = True

    # Preserve the historical optional Ep1 diagnostic switch without naming
    # its IDs: the first level branch is the minimum-level descendant branch.
    descendants = chain - set(roots)
    level_by_id = {
        int(getattr(meta, "quest_id", 0) or 0): int(getattr(meta, "min_level", 0) or 0)
        for meta in metas
        if int(getattr(meta, "quest_id", 0) or 0) in descendants
    }
    ep1_level = min(level_by_id.values()) if level_by_id else None
    ep1 = tuple(sorted(qid for qid, level in level_by_id.items() if level == ep1_level))

    slot_ids: set[int] = set()
    for meta in metas:
        qid = int(getattr(meta, "quest_id", 0) or 0)
        if qid <= 0 or qid not in registered:
            continue
        raw = str(getattr(meta, "raw_text", "") or "")
        match = re.search(
            r"(?ims)^\s*\[reward\s+type\]\s*$\s*`?\s*\[slot\s+expansion\]\s*`?",
            raw,
        )
        if match:
            slot_ids.add(qid)
    # Slot defaults settle the whole dedicated system chain. Marking only
    # the final reward task leaves its first stage active (for example after
    # using a completion ticket) even though the equipment slot is open.
    # Follow only authored system tasks with the same slot attribute. Never
    # walk arbitrary campaign prerequisites of an expansion quest.
    by_id = {int(getattr(meta, "quest_id", 0) or 0): meta for meta in metas}

    def slot_attributes(meta: Any) -> set[str]:
        raw = str(getattr(meta, "raw_text", "") or "")
        return {
            value.strip().lower()
            for value in re.findall(r"`([^`]+)`", _section_text(raw, "attribute"))
            if value.strip()
        }

    attributes: set[str] = set()
    for qid in slot_ids:
        attributes.update(slot_attributes(by_id[qid]))
    pending = list(slot_ids)
    while pending:
        meta = by_id[pending.pop()]
        for group in getattr(meta, "pre_groups", ()) or ():
            for dependency in group:
                qid = int(dependency)
                parent = by_id.get(qid)
                if qid in slot_ids or qid not in registered or parent is None:
                    continue
                if str(getattr(parent, "grade", "") or "").lower() != "[system]":
                    continue
                if not attributes.intersection(slot_attributes(parent)):
                    continue
                slot_ids.add(qid)
                pending.append(qid)
    return roots, tuple(sorted(chain)), ep1, tuple(sorted(slot_ids))


def build_default_unlock_index(*, quest_index: Any | None = None) -> DefaultUnlockIndex:
    """Read current shared PVF worldmaps and derive deep-dungeon prerequisites."""
    maps: list[HellWorldMap] = []
    for path in pvf_runtime.list_paths("worldmap/"):
        normalized = str(path).replace("\\", "/")
        if not normalized.lower().endswith(".wdm"):
            continue
        text = pvf_runtime.read_text(normalized)
        parsed = _parse_hell_worldmap(normalized, text)
        if parsed is not None:
            maps.append(parsed)
    maps.sort(key=lambda entry: entry.path)
    worldmap_quest_ids = tuple(sorted({qid for entry in maps for qid in entry.quest_ids}))
    dungeon_ids = tuple(sorted({did for entry in maps for did in entry.dungeon_ids}))
    if not maps or not worldmap_quest_ids:
        raise RuntimeError("current Script.pvf has no [hell dungeon]/[hell quest] worldmap index")
    if quest_index is None:
        quest_index = _current_quest_index()
    quest_ids, chain_ids, ep1_ids, slot_ids = _derive_quest_unlocks(
        worldmap_quest_ids,
        quest_index,
    )
    if not quest_ids:
        raise RuntimeError("current Script.pvf Hell WDM IDs are not registered in quest.lst")
    return DefaultUnlockIndex(
        hell_worldmaps=tuple(maps),
        hell_quest_ids=quest_ids,
        hell_dungeon_ids=dungeon_ids,
        hell_chain_quest_ids=chain_ids,
        hell_ep1_quest_ids=ep1_ids,
        slot_expansion_quest_ids=slot_ids,
        ignored_hell_quest_ids=tuple(
            sorted(set(worldmap_quest_ids) - set(quest_ids))
        ),
    )


def warmup_default_unlock_index(*, quest_index: Any | None = None) -> dict[str, Any]:
    """Build and retain the current-PVF default progress index before listen."""
    global _INDEX
    with _LOCK:
        _INDEX = build_default_unlock_index(quest_index=quest_index)
        return {
            "worldmap_count": len(_INDEX.hell_worldmaps),
            "hell_quest_count": len(_INDEX.hell_quest_ids),
            "hell_chain_quest_count": len(_INDEX.hell_chain_quest_ids),
            "slot_expansion_quest_count": len(_INDEX.slot_expansion_quest_ids),
            "ignored_hell_quest_count": len(_INDEX.ignored_hell_quest_ids),
            "hell_ep1_quest_ids": list(_INDEX.hell_ep1_quest_ids),
            "slot_expansion_quest_ids": list(_INDEX.slot_expansion_quest_ids),
            "ignored_hell_quest_ids": list(_INDEX.ignored_hell_quest_ids),
            "hell_dungeon_count": len(_INDEX.hell_dungeon_ids),
            "hell_quest_ids": list(_INDEX.hell_quest_ids),
        }


def get_default_unlock_index() -> DefaultUnlockIndex:
    with _LOCK:
        if _INDEX is None:
            raise RuntimeError("default unlock index is not warmed; boot warmup is required")
        return _INDEX


def apply_default_unlocks(repo: Any, character_id: int) -> dict[str, Any]:
    """Idempotently grant enabled PVF-derived defaults to one character."""
    quest_ids: list[int] = []
    hell_enabled = _env_bool("USDOF_GAME_DEFAULT_UNLOCK_HELL", True)
    hell_ep1_enabled = hell_enabled and _env_bool(
        "USDOF_GAME_DEFAULT_UNLOCK_HELL_EP1", True
    )
    if hell_enabled:
        index = get_default_unlock_index()
        hell_quest_ids = index.hell_chain_quest_ids or index.hell_quest_ids
        if not hell_ep1_enabled and index.hell_ep1_quest_ids:
            excluded = set(index.hell_ep1_quest_ids)
            hell_quest_ids = tuple(
                qid for qid in hell_quest_ids if qid not in excluded
            )
        quest_ids.extend(hell_quest_ids)
    ex_equip_state = 0
    if _env_bool("USDOF_GAME_DEFAULT_UNLOCK_EX_EQUIP", True):
        quest_ids.extend(get_default_unlock_index().slot_expansion_quest_ids)
        ex_equip_state = EX_EQUIP_ALL_OPEN_STATE
    ok, result = repo.grant_default_unlock_progress(
        int(character_id),
        quest_ids=quest_ids,
        ex_equip_slot_state=ex_equip_state,
    )
    out = dict(result)
    out.update(
        {
            "ok": bool(ok),
            "character_id": int(character_id),
            "hell_enabled": int(hell_enabled),
            "hell_ep1_enabled": int(hell_ep1_enabled),
            "ex_equip_enabled": int(
                _env_bool("USDOF_GAME_DEFAULT_UNLOCK_EX_EQUIP", True)
            ),
        }
    )
    return out


def backfill_default_unlocks(repo: Any) -> dict[str, int]:
    """Apply defaults to every live character before Channel/Game listeners open."""
    seen = 0
    updated = 0
    for character in repo.list_all_alive():
        seen += 1
        result = apply_default_unlocks(repo, int(character.character_id))
        if not result.get("ok"):
            raise RuntimeError(
                "default unlock backfill failed character_id=%s reason=%s"
                % (character.character_id, result.get("reason"))
            )
        if int(result.get("finished_added") or 0) > 0:
            updated += 1
    return {"characters": seen, "characters_with_new_quests": updated}
