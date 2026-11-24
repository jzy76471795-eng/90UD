"""PVF recipe index for Stage3 recipe crafting commands.

The client sends the recipe's stackable item id.  Recipe costs and products
are authored in that item's ``[input item]`` and ``[output item]`` sections.
Equipment design recipes additionally use ``[maintain ability]`` and
``[maintain socket stat]`` to request an in-place template upgrade through
``C1/00EE`` while retaining the source equipment's instance state.
The normal request path resolves that one entry from the shared memory PVF;
the full ``stackable/stackable.lst`` scan remains available for maintenance
validation through ``warmup_compound_item_index()``.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Iterable

from game.character.item_metadata import resolve_item_metadata
from pvf import pvf_runtime


STACKABLE_LIST_PATH = "stackable/stackable.lst"
RECIPE_STACKABLE_TYPE = "[recipe]"

_LST_RE = re.compile(r"(?m)^(\d+)\s*(?:\r?\n|\s+)`([^`]+)`")
_TAG_RE = re.compile(r"^\[\s*(/?)\s*([^\]]+?)\s*\]$", re.IGNORECASE)
_TOKEN_RE = re.compile(r"`([^`]*)`|([^\s]+)")
_LOCK = threading.RLock()
_INDEX: "CompoundItemIndex | None" = None
_LAZY_RESOLVED_IDS: set[int] = set()


def _normalize_label(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _tokens(lines: Iterable[str]) -> list[str]:
    values: list[str] = []
    for raw_line in lines:
        line = str(raw_line or "").strip()
        if not line:
            continue
        for match in _TOKEN_RE.finditer(line):
            values.append(match.group(1) if match.group(1) is not None else match.group(2))
    return values


def _parse_int(value: object, *, context: str) -> int:
    text = str(value or "").strip()
    if text.startswith("`") and text.endswith("`") and len(text) >= 2:
        text = text[1:-1].strip()
    try:
        return int(text, 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid integer %s value=%r" % (context, value)) from exc


def _parse_stackable_paths(text: str) -> tuple[tuple[int, str], ...]:
    entries: list[tuple[int, str]] = []
    seen: set[int] = set()
    for match in _LST_RE.finditer(str(text or "")):
        item_id = int(match.group(1))
        relative = str(match.group(2) or "").replace("\\", "/").strip().lstrip("/")
        if item_id <= 0 or not relative:
            continue
        if item_id in seen:
            raise ValueError("duplicate stackable list item_id=%d" % item_id)
        seen.add(item_id)
        path = relative if relative.lower().startswith("stackable/") else "stackable/" + relative
        entries.append((item_id, path))
    if not entries:
        raise ValueError("stackable.lst has no id/path entries")
    return tuple(entries)


def _section_token_groups(text: str, section_name: str) -> tuple[tuple[str, ...], ...]:
    """Read repeated PVF sections that may or may not have closing tags."""
    target = _normalize_label(section_name)
    active = False
    lines: list[str] = []
    groups: list[tuple[str, ...]] = []

    def finish() -> None:
        nonlocal active, lines
        if active:
            groups.append(tuple(_tokens(lines)))
        active = False
        lines = []

    for raw_line in str(text or "").splitlines():
        line = str(raw_line or "").strip()
        tag = _TAG_RE.fullmatch(line)
        if tag is None:
            if active and line:
                lines.append(line)
            continue

        closing = bool(tag.group(1))
        name = _normalize_label(tag.group(2))
        if active:
            finish()
        if not closing and name == target:
            active = True
    finish()
    return tuple(groups)


def _first_section_token(text: str, section_name: str) -> str:
    groups = _section_token_groups(text, section_name)
    for values in groups:
        if values:
            return _normalize_label(values[0])
    return ""


def _parse_pairs(
    values: Iterable[str],
    *,
    recipe_item_id: int,
    section_name: str,
    allow_gold: bool,
) -> tuple[tuple[int, int], ...]:
    tokens = tuple(values)
    if not tokens or len(tokens) % 2:
        raise ValueError(
            "recipe item=%d invalid [%s] token_count=%d"
            % (recipe_item_id, section_name, len(tokens))
        )
    ordered: list[int] = []
    totals: dict[int, int] = {}
    for offset in range(0, len(tokens), 2):
        item_id = _parse_int(tokens[offset], context="recipe %s item" % section_name)
        count = _parse_int(tokens[offset + 1], context="recipe %s count" % section_name)
        if item_id < 0 or item_id > 0xFFFFFFFF or (item_id == 0 and not allow_gold):
            raise ValueError(
                "recipe item=%d invalid [%s] item_id=%d"
                % (recipe_item_id, section_name, item_id)
            )
        if count <= 0 or count > 0xFFFFFFFF:
            raise ValueError(
                "recipe item=%d invalid [%s] count=%d"
                % (recipe_item_id, section_name, count)
            )
        if item_id not in totals:
            ordered.append(item_id)
            totals[item_id] = 0
        total = totals[item_id] + count
        if total > 0xFFFFFFFF:
            raise ValueError(
                "recipe item=%d [%s] count_overflow item=%d"
                % (recipe_item_id, section_name, item_id)
            )
        totals[item_id] = total
    return tuple((item_id, totals[item_id]) for item_id in ordered)


@dataclass(frozen=True)
class CompoundItemRecipe:
    recipe_item_id: int
    inputs: tuple[tuple[int, int], ...]
    outputs: tuple[tuple[int, int], ...]
    source_path: str = ""
    maintain_ability: str = ""
    maintain_socket_stat: bool = False

    def scale(self, count: int) -> tuple[tuple[tuple[int, int], ...], tuple[tuple[int, int], ...]]:
        multiplier = int(count)
        if multiplier <= 0 or multiplier > 0xFFFF:
            raise ValueError("invalid compound request count=%d" % multiplier)

        def multiply(rows: tuple[tuple[int, int], ...], field: str) -> tuple[tuple[int, int], ...]:
            out: list[tuple[int, int]] = []
            for item_id, unit_count in rows:
                total = int(unit_count) * multiplier
                if total <= 0 or total > 0xFFFFFFFF:
                    raise ValueError(
                        "compound %s overflow recipe=%d item=%d count=%d"
                        % (field, self.recipe_item_id, item_id, total)
                    )
                out.append((int(item_id), total))
            return tuple(out)

        return multiply(self.inputs, "input"), multiply(self.outputs, "output")

    def resolve_preserved_equipment_transform(self) -> tuple[int, int] | None:
        """Return the single source/output equipment pair declared by this recipe."""
        # The normalized shared reader can omit a valueless
        # ``[maintain socket stat]`` marker. ``normal`` plus the exact one-to-one
        # equipment shape remains present in the runtime PVF and is the stable
        # transform discriminator.
        if self.maintain_ability != "normal":
            return None
        if len(self.outputs) != 1 or int(self.outputs[0][1]) != 1:
            return None
        output_item_id = int(self.outputs[0][0])
        output_metadata = resolve_item_metadata(output_item_id)
        if (
            not bool(output_metadata.is_equipment)
            or int(output_metadata.inventory_list_type()) != 0
        ):
            return None

        equipment_inputs: list[tuple[int, int]] = []
        for item_id, count in self.inputs:
            if int(item_id) <= 0:
                continue
            metadata = resolve_item_metadata(int(item_id))
            if bool(metadata.is_equipment):
                equipment_inputs.append((int(item_id), int(count)))
        if len(equipment_inputs) != 1 or int(equipment_inputs[0][1]) != 1:
            return None
        source_item_id = int(equipment_inputs[0][0])
        source_metadata = resolve_item_metadata(source_item_id)
        if int(source_metadata.inventory_list_type()) != 0:
            return None
        return source_item_id, output_item_id


@dataclass(frozen=True)
class CompoundItemSkipped:
    recipe_item_id: int
    source_path: str
    reason: str


@dataclass(frozen=True)
class CompoundItemIndex:
    recipes: dict[int, CompoundItemRecipe]
    skipped: tuple[CompoundItemSkipped, ...] = ()
    scanned_stackable_count: int = 0

    def resolve(self, recipe_item_id: int) -> CompoundItemRecipe | None:
        return self.recipes.get(int(recipe_item_id) & 0xFFFFFFFF)


def parse_compound_recipe_text(
    recipe_item_id: int,
    text: str,
    *,
    source_path: str = "",
) -> CompoundItemRecipe:
    item_id = int(recipe_item_id) & 0xFFFFFFFF
    if item_id <= 0:
        raise ValueError("invalid recipe item_id=%r" % recipe_item_id)
    stackable_type = _first_section_token(text, "stackable type")
    if stackable_type != RECIPE_STACKABLE_TYPE:
        raise ValueError(
            "recipe item=%d stackable_type=%r expected=%s"
            % (item_id, stackable_type, RECIPE_STACKABLE_TYPE)
        )
    input_values = tuple(
        token
        for group in _section_token_groups(text, "input item")
        for token in group
    )
    output_values = tuple(
        token
        for group in _section_token_groups(text, "output item")
        for token in group
    )
    inputs = _parse_pairs(
        input_values,
        recipe_item_id=item_id,
        section_name="input item",
        allow_gold=True,
    )
    outputs = _parse_pairs(
        output_values,
        recipe_item_id=item_id,
        section_name="output item",
        allow_gold=False,
    )
    return CompoundItemRecipe(
        recipe_item_id=item_id,
        inputs=inputs,
        outputs=outputs,
        source_path=str(source_path or ""),
        maintain_ability=_first_section_token(text, "maintain ability"),
        maintain_socket_stat=bool(
            _section_token_groups(text, "maintain socket stat")
        ),
    )


def _validate_referenced_items(recipe: CompoundItemRecipe) -> None:
    for item_id, _count in recipe.inputs + recipe.outputs:
        if item_id == 0:
            continue
        metadata = resolve_item_metadata(item_id)
        if metadata.item_kind not in {"equipment", "stackable"}:
            raise ValueError(
                "recipe item=%d references unresolved item=%d kind=%s"
                % (recipe.recipe_item_id, item_id, metadata.item_kind)
            )


def _load_index_from_shared_reader() -> CompoundItemIndex:
    reader = pvf_runtime.ensure_shared_reader()
    entries = _parse_stackable_paths(reader.read_text(STACKABLE_LIST_PATH))
    recipes: dict[int, CompoundItemRecipe] = {}
    skipped: list[CompoundItemSkipped] = []
    for item_id, source_path in entries:
        text = reader.read_text(source_path)
        if _first_section_token(text, "stackable type") != RECIPE_STACKABLE_TYPE:
            continue
        try:
            recipe = parse_compound_recipe_text(item_id, text, source_path=source_path)
            _validate_referenced_items(recipe)
            recipes[item_id] = recipe
        except ValueError as exc:
            skipped.append(
                CompoundItemSkipped(
                    recipe_item_id=int(item_id),
                    source_path=source_path,
                    reason=str(exc),
                )
            )
    return CompoundItemIndex(
        recipes=recipes,
        skipped=tuple(skipped),
        scanned_stackable_count=len(entries),
    )


def get_compound_item_index() -> CompoundItemIndex:
    global _INDEX
    with _LOCK:
        if _INDEX is None or _INDEX.scanned_stackable_count <= 0:
            _INDEX = _load_index_from_shared_reader()
        return _INDEX


def _load_single_recipe(recipe_item_id: int) -> CompoundItemRecipe | None:
    item_id = int(recipe_item_id) & 0xFFFFFFFF
    if item_id <= 0:
        return None
    metadata = resolve_item_metadata(item_id)
    if not bool(getattr(metadata, "is_stackable", False)):
        return None
    source_path = str(getattr(metadata, "source_path", "") or "")
    if not source_path:
        return None
    text = pvf_runtime.read_text(source_path)
    if _first_section_token(text, "stackable type") != RECIPE_STACKABLE_TYPE:
        return None
    recipe = parse_compound_recipe_text(item_id, text, source_path=source_path)
    _validate_referenced_items(recipe)
    return recipe


def resolve_compound_recipe(recipe_item_id: int) -> CompoundItemRecipe | None:
    """Resolve only the requested recipe unless an explicit full scan exists."""
    global _INDEX
    item_id = int(recipe_item_id) & 0xFFFFFFFF
    with _LOCK:
        existing = _INDEX
        if existing is not None and existing.scanned_stackable_count > 0:
            return existing.resolve(item_id)
        if existing is not None:
            cached = existing.resolve(item_id)
            if cached is not None:
                return cached
        if item_id in _LAZY_RESOLVED_IDS:
            return None
        _LAZY_RESOLVED_IDS.add(item_id)
        try:
            recipe = _load_single_recipe(item_id)
        except (KeyError, ValueError):
            recipe = None
        if recipe is None:
            return None
        recipes = dict(existing.recipes) if existing is not None else {}
        recipes[item_id] = recipe
        _INDEX = CompoundItemIndex(
            recipes=recipes,
            skipped=existing.skipped if existing is not None else (),
            scanned_stackable_count=0,
        )
        return recipe


def install_compound_item_index(index: CompoundItemIndex) -> None:
    if not isinstance(index, CompoundItemIndex):
        raise ValueError("invalid compound item index")
    global _INDEX
    with _LOCK:
        _INDEX = index
        _LAZY_RESOLVED_IDS.clear()


def clear_compound_item_index_cache() -> None:
    global _INDEX
    with _LOCK:
        _INDEX = None
        _LAZY_RESOLVED_IDS.clear()


def warmup_compound_item_index() -> dict[str, int]:
    index = get_compound_item_index()
    output_ids = {
        int(item_id)
        for recipe in index.recipes.values()
        for item_id, _count in recipe.outputs
    }
    input_ids = {
        int(item_id)
        for recipe in index.recipes.values()
        for item_id, _count in recipe.inputs
        if int(item_id) > 0
    }
    preserved_equipment_recipe_count = sum(
        1
        for recipe in index.recipes.values()
        if recipe.resolve_preserved_equipment_transform() is not None
    )
    return {
        "ok": 1,
        "scanned_stackable_count": int(index.scanned_stackable_count),
        "recipe_count": len(index.recipes),
        "input_item_count": len(input_ids),
        "output_item_count": len(output_ids),
        "preserved_equipment_recipe_count": preserved_equipment_recipe_count,
        "skipped_recipe_count": len(index.skipped),
    }


__all__ = [
    "CompoundItemIndex",
    "CompoundItemRecipe",
    "CompoundItemSkipped",
    "RECIPE_STACKABLE_TYPE",
    "STACKABLE_LIST_PATH",
    "clear_compound_item_index_cache",
    "get_compound_item_index",
    "install_compound_item_index",
    "parse_compound_recipe_text",
    "resolve_compound_recipe",
    "warmup_compound_item_index",
]
