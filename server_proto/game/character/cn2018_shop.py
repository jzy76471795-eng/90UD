"""CN Adventure Group catalog, wallets and atomic purchases."""

from dataclasses import dataclass
from functools import lru_cache
import os
from types import MappingProxyType
from typing import Mapping

from game.character.adventure_group import load_adventure_group_overview
from game.character.character_db import _AdventureFeatureTransactionReject
from game.character.cn2018_expedition import _server_day
from game.character.item_metadata import resolve_item_metadata
from pvf import pvf_runtime
from pvf.adventure_group_feature_index import (
    _closed_sections, _field_values, _lines, get_adventure_group_feature_index,
)
from pvf.cn_expedition_index import ADVENTURER_2018_PATH


WALLET_TABLES = (
    ("account_adventure_shop_state", "shop_points"),
    ("account_cn2018_glory", "balance"),
    ("account_cn2018_pure", "balance"),
)
REQUEST_MARKER = 0xC0000000
REQUEST_PREFIX_MASK = 0xC0000000
REQUEST_PRODUCT_MASK = 0x1F
SNAPSHOT_START = 0x7FCF0100
SNAPSHOT_VERSION = 1
UNAVAILABLE_PRODUCT = 0x80000000


class ShopRejected(ValueError):
    """The purchase was rejected before any charge or delivery."""


@dataclass(frozen=True)
class Product:
    category: int
    item_id: int
    level: int
    price: int
    limit: int


@dataclass(frozen=True)
class Catalog:
    products: Mapping[int, Product]
    brave_rules: tuple[int, int, int]


@lru_cache(maxsize=4)
def parse_catalog(text: str) -> Catalog:
    (shops,) = _closed_sections(_lines(text), "adventurer shop list")
    products = {}
    counts = []
    brave = ()
    for index, shop in enumerate(_closed_sections(shops, "shop")):
        if _field_values(shop, "index") != [str(index)]:
            raise ValueError("CN shop categories are out of order")
        if index == 0:
            brave = tuple(map(int, _field_values(shop, "max shop point") + _field_values(shop, "exp to get shop point")))
        (rows,) = _closed_sections(shop, "adventurer shop purchase info")
        counts.append(len(rows) // 5)
        for item, level, price, limit, rule in zip(*(map(int, rows[offset::5]) for offset in range(5)), strict=True):
            if item in products or min(item, level, price, limit) <= 0 or rule != 3:
                raise ValueError("invalid CN monthly shop product")
            products[item] = Product(index, item, level, price, limit)
    if counts != [11, 7, 1] or len(brave) != 3 or min(brave) <= 0:
        raise ValueError("invalid CN Adventure Group shop configuration")
    return Catalog(MappingProxyType(products), brave)


def open_shop(repo):
    return CN2018Shop(repo, parse_catalog(pvf_runtime.read_text(ADVENTURER_2018_PATH)))


def enabled():
    return os.environ.get("USDOF_CN2018_SHOP") == "1"


def is_purchase_request(word: int) -> bool:
    return (word & REQUEST_PREFIX_MASK) == REQUEST_MARKER


def product_for_request(catalog: Catalog, word: int) -> int:
    product_index = word & REQUEST_PRODUCT_MASK
    if product_index >= len(catalog.products):
        raise ShopRejected("invalid_product_or_quantity")
    return tuple(catalog.products)[product_index]


def snapshot_rows(repo, account: int, now: int, receipt: tuple[int, int] = (0, 0)):
    from game.character import cn2018_capsule

    shop = open_shop(repo)
    balances, remaining, available = shop.state(account, now)
    request_id, result = receipt
    fields = (SNAPSHOT_VERSION, balances[1], balances[2], request_id, result)
    return (*((SNAPSHOT_START + i, value) for i, value in enumerate(fields + cn2018_capsule.view(repo, account, now))),
            *((SNAPSHOT_START + 16 + i, count if item in available else count | UNAVAILABLE_PRODUCT)
              for i, (item, count) in enumerate(remaining)))


class CN2018Shop:
    def __init__(self, repo, catalog: Catalog):
        self.repo = repo
        self.catalog = catalog

    def _settle(self, account: int, period: int):
        stage3 = get_adventure_group_feature_index()
        rules = (stage3.maximum_shop_points, stage3.shop_exp_per_award, stage3.shop_points_per_award)
        if rules != self.catalog.brave_rules:
            raise ShopRejected("brave_wallet_policy_mismatch")
        overview = load_adventure_group_overview(self.repo, account)
        self.repo.load_adventure_shop_state(
            account, total_character_exp=overview.progression.total_character_exp,
            maximum_points=rules[0], exp_per_award=rules[1], points_per_award=rules[2], period_yyyymm=period,
        )
        return overview.progression.adventure_level

    @staticmethod
    def _period(now: int):
        date = _server_day(now)
        return date.year * 100 + date.month

    @staticmethod
    def _balance(conn, account: int, category: int, period: int):
        table, column = WALLET_TABLES[category]
        if category == 1:
            row = conn.execute(
                "SELECT balance FROM account_cn2018_glory WHERE account_id=? AND period_yyyymm=?", (account, period),
            ).fetchone()
        else:
            row = conn.execute(f"SELECT {column} FROM {table} WHERE account_id=?", (account,)).fetchone()
        return int(row[0]) if row else 0

    def state(self, account: int, now: int):
        period = self._period(now)
        with self.repo.open() as conn:
            balances = tuple(self._balance(conn, account, category, period) for category in range(3))
            purchases = dict(conn.execute(
                "SELECT item_id,quantity FROM account_cn2018_shop_purchases WHERE account_id=? AND period_yyyymm=?",
                (account, period),
            ).fetchall())
        remaining = tuple((item, max(0, product.limit - purchases.get(item, 0)))
                          for item, product in self.catalog.products.items())
        available = frozenset(item for item, _ in remaining if self._metadata(item) is not None)
        return balances, remaining, available

    @staticmethod
    def _metadata(item: int):
        metadata = resolve_item_metadata(item)
        return metadata if metadata.item_kind == "stackable" and metadata.source_path else None

    def buy(self, account: int, character: int, item: int, quantity: int, request_id: int, now: int):
        product = self.catalog.products.get(item)
        if product is None or not 1 <= quantity <= product.limit or not 0 < request_id <= 0xFFFFFFFF:
            raise ShopRejected("invalid_product_or_quantity")
        if self.repo.get_account_id_for_character(character) != account:
            raise ShopRejected("character_account_mismatch")
        metadata = self._metadata(item)
        if metadata is None:
            raise ShopRejected("product_item_unavailable")
        period = self._period(now)
        level = self._settle(account, period)
        with self.repo.inventory_transaction_scope() as conn:
            receipt = conn.execute(
                "SELECT character_id,item_id,quantity FROM account_cn2018_shop_receipts WHERE account_id=? AND request_id=?",
                (account, request_id),
            ).fetchone()
            if receipt is not None:
                if tuple(receipt) != (character, item, quantity):
                    raise ShopRejected("request_id_conflict")
                return False
            owner = conn.execute(
                "SELECT 1 FROM characters WHERE character_id=? AND account_id=? AND delete_flag=0", (character, account),
            ).fetchone()
            if owner is None or level < product.level:
                raise ShopRejected("character_unavailable" if owner is None else "adventure_level_low")
            row = conn.execute(
                "SELECT quantity FROM account_cn2018_shop_purchases WHERE account_id=? AND item_id=? AND period_yyyymm=?",
                (account, item, period),
            ).fetchone()
            purchased = int(row[0]) if row else 0
            if purchased + quantity > product.limit:
                raise ShopRejected("purchase_limit_reached")
            cost = product.price * quantity
            balance = self._balance(conn, account, product.category, period)
            if balance < cost:
                raise ShopRejected("crystals_insufficient")
            try:
                self.repo._grant_adventure_feature_item_tx(
                    conn, character_id=character, item_id=item, count=quantity, metadata=metadata, now_unix=now,
                )
            except _AdventureFeatureTransactionReject as exc:
                raise ShopRejected(exc.reason) from None
            table, column = WALLET_TABLES[product.category]
            conn.execute(f"UPDATE {table} SET {column}=? WHERE account_id=?", (balance - cost, account))
            conn.execute(
                "INSERT INTO account_cn2018_shop_purchases(account_id,item_id,period_yyyymm,quantity) VALUES (?,?,?,?) "
                "ON CONFLICT(account_id,item_id,period_yyyymm) DO UPDATE SET quantity=excluded.quantity",
                (account, item, period, purchased + quantity),
            )
            conn.execute(
                "INSERT INTO account_cn2018_shop_receipts(account_id,request_id,character_id,item_id,quantity) VALUES (?,?,?,?,?)",
                (account, request_id, character, item, quantity),
            )
        return True
