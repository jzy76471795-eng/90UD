"""Dynamic Cera-shop catalog backed by the process-shared Script.pvf reader."""
from __future__ import annotations

import logging
import struct
import threading
from dataclasses import dataclass, replace
from typing import Any, Iterable, Sequence

from game.character.item_metadata import resolve_item_expire_time
from pvf import pvf_runtime


CERASHOP_PATH_CANDIDATES: tuple[str, ...] = (
    "etc/(r)cerashop.etc",
    "etc/cerashop.etc",
)
MILEAGE_SHOP_PATH = "etc/mileageshop.etc"
MILEAGE_RARE_VOUCHER = (900001101, 2026112102, 2681594)
_MILEAGE_DELIVERY_PATH = "etc/mileage_delivery.etc"

# Current Stage3 catalog grammars. Other *shop*.etc files use different
# currencies/layouts and deliberately do not enter this catalog.
_ROW14_SECTIONS: frozenset[str] = frozenset(
    {
        "avatar",
        "avatar dye",
        "creature",
        "dont trade avatar",
        "item",
        "item etc",
        "item mod or ext",
        "item period or contract",
        "item second",
    }
)
_ROW13_SECTIONS: frozenset[str] = frozenset({"package", "monthly"})
_MILEAGE_ROW_WIDTHS = {
    "item": 14,
    "item second": 14,
    "avatar": 13,
    "item event": 14,
}

# Column 2 is section-specific. It is the granted stack count for ordinary
# item/creature rows, but a classification/value field for avatar and the two
# 13-token container sections. Those products always grant one item instance.
_SINGLE_INSTANCE_SECTIONS: frozenset[str] = frozenset(
    {"avatar", "dont trade avatar", "package", "monthly"}
)

@dataclass(frozen=True)
class CerashopProduct:
    commodity_id: int
    item_id: int
    quantity: int
    cera_price: int
    section: str
    raw_quantity: int
    source_path: str = ""
    row_index: int = 0
    gold_price: int = 0
    mileage_price: int = 0
    display_item_id: int = 0
    avatar_material_id: int = 0
    avatar_material_count: int = 0


@dataclass(frozen=True)
class CerashopSkippedDuplicate:
    """A later catalog row ignored by the first-occurrence commodity policy."""

    commodity_id: int
    kept: CerashopProduct
    skipped: CerashopProduct


@dataclass(frozen=True)
class CerashopIndex:
    products: dict[int, CerashopProduct]
    source_path: str
    skipped_duplicates: tuple[CerashopSkippedDuplicate, ...] = ()

    def resolve(self, commodity_id: int) -> CerashopProduct | None:
        return self.products.get(int(commodity_id) & 0xFFFFFFFF)

    @property
    def item_count(self) -> int:
        return len({int(product.item_id) for product in self.products.values()})


_LOCK = threading.RLock()
_INDEX: CerashopIndex | None = None
_MILEAGE_INDEX: dict[int, CerashopProduct] | None = None
_MILEAGE_DISPLAY_ITEMS: dict[int, int] = {}


def _strip_tick(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("`") and text.endswith("`") and len(text) >= 2:
        return text[1:-1].strip()
    return text


def _parse_int(value: Any, *, field: str, section: str, row_index: int) -> int:
    text = _strip_tick(value)
    try:
        return int(text, 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "cerashop invalid %s section=%s row=%d value=%r"
            % (field, section, row_index, value)
        ) from exc


def _iter_supported_sections(
    tokens: Sequence[str],
    supported: frozenset[str] = _ROW14_SECTIONS | _ROW13_SECTIONS,
) -> Iterable[tuple[str, list[str]]]:
    current_name = ""
    current_values: list[str] = []

    for raw in tokens:
        token = str(raw).strip()
        low = token.lower()
        if low.startswith("[/") and low.endswith("]"):
            closing = low[2:-1].strip()
            if current_name and closing == current_name:
                yield current_name, current_values
                current_name = ""
                current_values = []
            continue
        if low.startswith("[") and low.endswith("]"):
            opening = low[1:-1].strip()
            if opening in supported:
                if current_name:
                    raise ValueError(
                        "cerashop nested supported section %s inside %s"
                        % (opening, current_name)
                    )
                current_name = opening
                current_values = []
            continue
        if current_name:
            current_values.append(str(raw))

    if current_name:
        raise ValueError("cerashop unclosed section=%s" % current_name)


def parse_cerashop_tokens(
    tokens: Sequence[str],
    *,
    source_path: str = "",
) -> CerashopIndex:
    """Parse all known catalog sections into a commodity-keyed index.

    Resolution never uses the row position. Reordering, adding, or removing
    rows changes only the resulting mapping for their commodity IDs.
    """
    products: dict[int, CerashopProduct] = {}
    skipped_duplicates: list[CerashopSkippedDuplicate] = []
    section_seen = 0

    for section, values in _iter_supported_sections(tokens):
        section_seen += 1
        avatar_compact = section == "avatar" and len(values) >= 13 and str(values[12]).strip().startswith(chr(96))
        if avatar_compact:
            # The native tradable avatar grammar omits the bound variant's partner SKU column.
            width = 13
            cera_offset = 5
        elif section in _ROW14_SECTIONS:
            width = 14
            cera_offset = 5
        else:
            width = 13
            cera_offset = 4
        if len(values) % width != 0:
            raise ValueError(
                "cerashop section=%s tokens=%d row_width=%d"
                % (section, len(values), width)
            )
        for offset in range(0, len(values), width):
            row_index = offset // width
            row = values[offset : offset + width]
            commodity_id = _parse_int(
                row[0], field="commodity_id", section=section, row_index=row_index
            )
            item_id = _parse_int(
                row[1], field="item_id", section=section, row_index=row_index
            )
            raw_quantity = _parse_int(
                row[2], field="quantity", section=section, row_index=row_index
            )
            cera_price = _parse_int(
                row[cera_offset],
                field="cera_price",
                section=section,
                row_index=row_index,
            )
            if commodity_id <= 0 or commodity_id > 0xFFFFFFFF:
                raise ValueError(
                    "cerashop invalid commodity_id section=%s row=%d value=%d"
                    % (section, row_index, commodity_id)
                )
            if item_id <= 0 or item_id > 0xFFFFFFFF:
                raise ValueError(
                    "cerashop invalid item_id section=%s row=%d commodity=%d value=%d"
                    % (section, row_index, commodity_id, item_id)
                )
            if cera_price < 0 or cera_price > 0x7FFFFFFF:
                raise ValueError(
                    "cerashop invalid cera_price section=%s row=%d commodity=%d value=%d"
                    % (section, row_index, commodity_id, cera_price)
                )
            if section in _SINGLE_INSTANCE_SECTIONS:
                quantity = 1
            else:
                if raw_quantity <= 0 or raw_quantity > 0xFFFFFFFF:
                    raise ValueError(
                        "cerashop invalid grant quantity section=%s row=%d "
                        "commodity=%d value=%d"
                        % (section, row_index, commodity_id, raw_quantity)
                    )
                quantity = raw_quantity
            gold_price = _parse_int(row[3], field="gold_price", section=section, row_index=row_index) if width == 14 or avatar_compact else 0
            if gold_price < 0 or gold_price > 0x7FFFFFFF:
                raise ValueError("cerashop invalid gold_price=%d" % gold_price)
            product = CerashopProduct(
                commodity_id=commodity_id,
                item_id=item_id,
                quantity=quantity,
                cera_price=cera_price,
                section=section,
                raw_quantity=raw_quantity,
                source_path=source_path,
                row_index=row_index,
                gold_price=gold_price,
                avatar_material_id=int(row[6]) if section in ("avatar", "dont trade avatar") else 0,
                avatar_material_count=int(row[7]) if section in ("avatar", "dont trade avatar") else 0,
            )
            previous = products.get(commodity_id)
            if previous is not None:
                # The frozen PVF may list one commodity ID in multiple package
                # rows. C1/0040 identifies the row only by commodity ID, so the
                # established local policy is deterministic: retain its first
                # definition and make every later definition visible at boot.
                skipped_duplicates.append(
                    CerashopSkippedDuplicate(
                        commodity_id=commodity_id,
                        kept=previous,
                        skipped=product,
                    )
                )
                continue
            products[commodity_id] = product

    if section_seen <= 0 or not products:
        raise ValueError("cerashop contains no supported product rows")
    return CerashopIndex(
        products=products,
        source_path=source_path,
        skipped_duplicates=tuple(skipped_duplicates),
    )


def parse_cerashop_text(text: str, *, source_path: str = "") -> CerashopIndex:
    return parse_cerashop_tokens(str(text or "").splitlines(), source_path=source_path)


def _decode_reader_tokens(reader: Any, path: str) -> list[str]:
    """Decode typed PVF tokens without losing embedded newlines in string tokens."""
    try:
        entry = reader.get_entry(path)
        raw = reader.read_raw(path)
    except (AttributeError, NotImplementedError):
        return str(reader.read_text(path) or "").splitlines()

    if int(getattr(entry, "data_type", 0) or 0) != 1 or len(raw) % 5 != 0:
        return str(reader.read_text(path) or "").splitlines()

    out: list[str] = []
    for offset in range(0, len(raw), 5):
        token_type = raw[offset]
        value = struct.unpack_from("<i", raw, offset + 1)[0]
        if token_type == 0:
            out.append(str(value))
        elif token_type == 2:
            out.append(repr(struct.unpack("<f", struct.pack("<i", value))[0]))
        elif token_type == 3:
            name = str(reader.resolve_name(value))
            out.append(name if name.startswith("[") and name.endswith("]") else "[%s]" % name)
        elif token_type in (5, 7):
            out.append(str(reader.resolve_name(value)))
        elif token_type == 6:
            out.append("`%s`" % reader.resolve_name(value))
        else:
            out.append("<token:%d:0x%08x>" % (token_type, value & 0xFFFFFFFF))
    return out


def _load_index_from_shared_reader() -> CerashopIndex:
    reader = pvf_runtime.ensure_shared_reader()
    errors: list[str] = []
    for path in CERASHOP_PATH_CANDIDATES:
        try:
            tokens = _decode_reader_tokens(reader, path)
            return parse_cerashop_tokens(tokens, source_path=path)
        except (KeyError, FileNotFoundError) as exc:
            errors.append("%s:%s" % (path, exc))
    raise FileNotFoundError("Cera-shop PVF file missing: %s" % "; ".join(errors))


def get_cerashop_index() -> CerashopIndex:
    global _INDEX
    with _LOCK:
        if _INDEX is None:
            _INDEX = _load_index_from_shared_reader()
        return _INDEX


def resolve_cerashop_product(commodity_id: int) -> CerashopProduct | None:
    return get_cerashop_index().resolve(int(commodity_id))


def parse_mileage_shop_tokens(tokens: Sequence[str]) -> dict[int, CerashopProduct]:
    """Read the four Stage3 mileage sections into the separate points catalog."""
    products: dict[int, CerashopProduct] = {}
    supported = (_ROW14_SECTIONS | _ROW13_SECTIONS) | frozenset(_MILEAGE_ROW_WIDTHS)
    for section, values in _iter_supported_sections(tokens, supported):
        if not values:
            continue
        if section not in _MILEAGE_ROW_WIDTHS:
            raise ValueError(f"unsupported mileage shop section {section}")
        width = _MILEAGE_ROW_WIDTHS[section]
        if len(values) % width:
            raise ValueError(f"invalid mileage shop {section} row width")
        for index in range(0, len(values), width):
            row = values[index : index + width]
            commodity_id, item_id, raw_quantity, gold, points = (int(value) for value in row[:5])
            if (
                not 0 < commodity_id <= 0xFFFFFFFF
                or not 0 < item_id <= 0xFFFFFFFF
                or not 0 < raw_quantity <= 0xFFFFFFFF
                or gold
                or not 0 < points <= 0x7FFFFFFF
                or commodity_id in products
            ):
                raise ValueError(f"invalid mileage shop {section} product {commodity_id}")
            products[commodity_id] = CerashopProduct(
                commodity_id=commodity_id,
                item_id=item_id,
                quantity=1 if section in _SINGLE_INSTANCE_SECTIONS else raw_quantity,
                cera_price=0,
                section=section,
                raw_quantity=raw_quantity,
                source_path=MILEAGE_SHOP_PATH,
                row_index=index // width,
                mileage_price=points,
            )
    return products


def load_mileage_shop_index(reader: Any) -> dict[int, CerashopProduct]:
    products = parse_mileage_shop_tokens(_decode_reader_tokens(reader, MILEAGE_SHOP_PATH))
    try:
        tokens = _decode_reader_tokens(reader, _MILEAGE_DELIVERY_PATH)
    except (KeyError, FileNotFoundError):
        return products

    # The native shop groups some catalog IDs by profession. Display aliases
    # retain equivalent equipment definitions; checkout grants original IDs.
    # Each display ID may have its own path to isolate the native path cache.
    aliases = {p.item_id: p for p in products.values() if p.section == "avatar"}
    voucher_commodity, voucher_display, voucher_item = MILEAGE_RARE_VOUCHER
    voucher = products.get(voucher_commodity)
    if voucher and voucher.section == "item" and voucher.item_id == voucher_display:
        aliases[voucher_display] = voucher
    if len(tokens) % 2:
        raise ValueError("invalid mileage shop delivery row width")
    seen: set[int] = set()
    for index in range(0, len(tokens), 2):
        display_id, item_id = (int(value) for value in tokens[index:index + 2])
        if (
            display_id not in aliases or display_id in seen
            or not 0 < item_id <= 0x7FFFFFFF or item_id in aliases
            or (display_id == voucher_display and item_id != voucher_item)
        ):
            raise ValueError(f"invalid mileage delivery alias {display_id} -> {item_id}")
        seen.add(display_id)
        product = aliases[display_id]
        products[product.commodity_id] = replace(product, item_id=item_id, display_item_id=display_id)
    return products


def get_mileage_shop_index() -> dict[int, CerashopProduct]:
    global _MILEAGE_INDEX, _MILEAGE_DISPLAY_ITEMS
    with _LOCK:
        if _MILEAGE_INDEX is None:
            try:
                reader = pvf_runtime.ensure_shared_reader()
                _MILEAGE_INDEX = load_mileage_shop_index(reader)
                _MILEAGE_DISPLAY_ITEMS = {
                    product.display_item_id: product.item_id
                    for product in _MILEAGE_INDEX.values() if product.display_item_id
                }
            except (KeyError, FileNotFoundError):
                _MILEAGE_INDEX = {}
            except ValueError as exc:
                logging.getLogger(__name__).warning("Could not load %s: %s", MILEAGE_SHOP_PATH, exc)
                _MILEAGE_INDEX = {}
        return _MILEAGE_INDEX


def resolve_mileage_inventory_item(item_id: int) -> int:
    """Map a known shop display alias back to its persisted item ID."""
    with _LOCK:
        get_mileage_shop_index()
        return _MILEAGE_DISPLAY_ITEMS.get(item_id, item_id)


def resolve_account_purchase_limit(product: CerashopProduct) -> int:
    """Return the active checkout limit; package limits are paused."""
    _ = product
    return 0


def clear_cerashop_index_cache() -> None:
    global _INDEX, _BONUS_ITEMS_CACHE, _MILEAGE_INDEX, _MILEAGE_DISPLAY_ITEMS
    with _LOCK:
        _INDEX = None
        _BONUS_ITEMS_CACHE = None
        _MILEAGE_INDEX = None
        _MILEAGE_DISPLAY_ITEMS = {}


def install_cerashop_index(index: CerashopIndex) -> None:
    if not isinstance(index, CerashopIndex) or not index.products:
        raise ValueError("cannot install empty Cera-shop index")
    global _INDEX
    with _LOCK:
        _INDEX = index


def resolve_purchase_expire_time(metadata: Any, *, now_unix: int | None = None) -> int:
    """Materialize an instance lifetime from the purchased item's own script."""
    return resolve_item_expire_time(metadata, now_unix=now_unix)


def warmup_cerashop_index() -> dict[str, int | str]:
    """Build the catalog and cache every referenced item metadata definition."""
    index = get_cerashop_index()
    from game.character.item_metadata import resolve_item_metadata

    bonus_index = get_cerashop_bonus_index()
    unknown = set(bonus_index) - set(index.products)
    if unknown:
        raise ValueError("bonus references missing commodities: %s" % sorted(unknown))
    bonus_ids = {gift.item_id for gifts in bonus_index.values() for gift in gifts}
    for item_id in bonus_ids:
        metadata = resolve_item_metadata(item_id)
        if metadata.item_kind != "stackable" or int(metadata.inventory_list_type()) != 0:
            raise ValueError("unsupported cerashop bonus item=%d" % item_id)
    item_ids = sorted({int(product.item_id) for product in index.products.values()} | bonus_ids)
    avatar_count = 0
    job_restricted_count = 0
    unresolved: list[int] = []
    for item_id in item_ids:
        metadata = resolve_item_metadata(item_id)
        if metadata.item_kind not in {"equipment", "stackable"} or not str(
            getattr(metadata, "note", "") or ""
        ).startswith("ok"):
            unresolved.append(item_id)
            continue
        if bool(getattr(metadata, "is_avatar", False)):
            avatar_count += 1
        usable = tuple(getattr(metadata, "usable_jobs", ()) or ())
        suitable = tuple(getattr(metadata, "suitable_jobs", ()) or ())
        labels = tuple(str(label).strip().lower() for label in usable + suitable)
        if labels and not all(label in {"all", "[all]"} for label in labels):
            job_restricted_count += 1
    if unresolved:
        sample = ",".join(str(item_id) for item_id in unresolved[:16])
        raise RuntimeError(
            "cerashop item metadata unresolved count=%d sample=%s"
            % (len(unresolved), sample)
        )
    return {
        "ok": 1,
        "source_path": index.source_path,
        "commodity_count": len(index.products),
        "bonus_commodity_count": len(bonus_index),
        "bonus_item_count": len(bonus_ids),
        "item_count": len(item_ids),
        "avatar_count": avatar_count,
        "job_restricted_count": job_restricted_count,
        "skipped_duplicate_count": len(index.skipped_duplicates),
        "skipped_duplicate_details": tuple(
            "commodity_id=%d keep=%s[%d] item_id=%d cera_price=%d "
            "skip=%s[%d] item_id=%d cera_price=%d "
            "reason=duplicate_commodity_id_keep_first"
            % (
                duplicate.commodity_id,
                duplicate.kept.section,
                duplicate.kept.row_index,
                duplicate.kept.item_id,
                duplicate.kept.cera_price,
                duplicate.skipped.section,
                duplicate.skipped.row_index,
                duplicate.skipped.item_id,
                duplicate.skipped.cera_price,
            )
            for duplicate in index.skipped_duplicates
        ),
    }


__all__ = [
    "CERASHOP_PATH_CANDIDATES",
    "CerashopIndex",
    "CerashopProduct",
    "CerashopSkippedDuplicate",
    "clear_cerashop_index_cache",
    "get_cerashop_index",
    "parse_cerashop_text",
    "parse_cerashop_tokens",
    "resolve_account_purchase_limit",
    "resolve_cerashop_product",
    "resolve_purchase_expire_time",
    "warmup_cerashop_index",
]


@dataclass(frozen=True)
class CerashopBonusItem:
    item_id: int
    count: int


CERASHOP_BONUS_PATH = "etc/chn_cerashop_multibonus.etc"
_BONUS_ITEMS_CACHE: dict[int, tuple[CerashopBonusItem, ...]] | None = None


def parse_cerashop_bonus_tokens(tokens: Sequence[str]) -> dict[int, tuple[CerashopBonusItem, ...]]:
    opening = "[one plus one bonus item info ipg]"
    closing = "[/one plus one bonus item info ipg]"
    if opening not in tokens:
        return {}
    start = tokens.index(opening) + 1
    end = tokens.index(closing, start)
    result: dict[int, tuple[CerashopBonusItem, ...]] = {}
    index = start
    while index < end:
        if tokens[index] != "[bonus item]":
            raise ValueError("invalid cerashop bonus opening at %d" % index)
        finish = tokens.index("[/bonus item]", index + 1)
        if finish >= end:
            raise ValueError("cerashop bonus child exceeds its parent")
        values = [int(str(v).strip("`"), 0) for v in tokens[index + 1:finish]]
        if len(values) < 3 or len(values) % 2 != 1:
            raise ValueError("cerashop bonus must be commodity + item/count pairs")
        if any(n <= 0 or n > 0xFFFFFFFF for n in values):
            raise ValueError("invalid cerashop bonus id/count")
        commodity = values[0]
        if commodity in result:
            raise ValueError("duplicate cerashop bonus commodity=%d" % commodity)
        gifts = tuple(CerashopBonusItem(values[i], values[i + 1]) for i in range(1, len(values), 2))
        if len({gift.item_id for gift in gifts}) != len(gifts):
            raise ValueError("duplicate cerashop bonus item commodity=%d" % commodity)
        result[commodity] = gifts
        index = finish + 1
    return result


def get_cerashop_bonus_index() -> dict[int, tuple[CerashopBonusItem, ...]]:
    global _BONUS_ITEMS_CACHE
    with _LOCK:
        if _BONUS_ITEMS_CACHE is None:
            reader = pvf_runtime.ensure_shared_reader()
            try:
                tokens = _decode_reader_tokens(reader, CERASHOP_BONUS_PATH)
            except (KeyError, FileNotFoundError):
                _BONUS_ITEMS_CACHE = {}
            else:
                _BONUS_ITEMS_CACHE = parse_cerashop_bonus_tokens(tokens)
        return _BONUS_ITEMS_CACHE


def resolve_cerashop_bonus_items(commodity_id: int) -> tuple[CerashopBonusItem, ...]:
    return get_cerashop_bonus_index().get(int(commodity_id), ())


__all__ += ["CerashopBonusItem", "get_cerashop_bonus_index", "parse_cerashop_bonus_tokens", "resolve_cerashop_bonus_items"]
