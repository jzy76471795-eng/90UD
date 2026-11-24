# -*- coding: utf-8 -*-
"""Neo Premium Plus rental weapon, daily gift, and expiry coverage."""
from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from game.character.character_db import CharacterRepository
from game.character.item_metadata import ItemMetadata, resolve_item_metadata
from game.chain_loader import load_chain
from game.shop.neo_premium_benefits import (
    NEO_PREMIUM_DAILY_GIFT_ITEM_ID,
    NEO_PREMIUM_PLUS_TYPE,
    NeoPremiumRentalIndex,
    NeoPremiumRentalWeapon,
    get_neo_premium_rental_index,
    neo_premium_daily_period,
)
from game.shop.premium_contract_index import ActivePremiumEffects
from opcodes.index import ROUTED_OPCODES


rent_opcode = load_chain("opcode04b5-rent_neo_premium_weapon.py")
gift_opcode = load_chain("opcode04b6-claim_neo_premium_gift.py")


def _weapon_metadata(item_id: int = 101020673) -> ItemMetadata:
    return ItemMetadata(
        item_id=item_id,
        item_kind="equipment",
        equipment_type="[weapon]",
        durability=45,
        minimum_level=85,
        usable_jobs=("swordman",),
        attach_type="trade delete",
    )


def _gift_metadata() -> ItemMetadata:
    return ItemMetadata(
        item_id=NEO_PREMIUM_DAILY_GIFT_ITEM_ID,
        item_kind="stackable",
        stackable_type="[booster] 0",
        stack_limit=1,
        usable_jobs=("all",),
        suitable_jobs=("all",),
        attach_type="account",
    )


def _material_metadata(item_id: int = 600001) -> ItemMetadata:
    return ItemMetadata(
        item_id=item_id,
        item_kind="stackable",
        stackable_type="[material] 0",
        stack_limit=99,
        usable_jobs=("all",),
        suitable_jobs=("all",),
        attach_type="account",
    )


def _avatar_metadata(item_id: int = 600002) -> ItemMetadata:
    return ItemMetadata(
        item_id=item_id,
        item_kind="equipment",
        equipment_type="[hat avatar]",
        is_avatar=True,
        usable_jobs=("all",),
        suitable_jobs=("all",),
        attach_type="account",
    )


class NeoPremiumRepositoryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = CharacterRepository(
            Path(self._tmp.name) / "neo_premium.db",
            bootstrap_from_reference=False,
        )
        self.repo.ensure_account(1)
        self.repo.create_character(
            account_id=1,
            character_id=101,
            name="Eiji",
            job=0,
            grow_type=33,
            level=90,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def activate_plus(self, end_time: int) -> None:
        with self.repo.open() as conn:
            conn.execute(
                "INSERT INTO account_premiums (account_id, premium_type, end_time) "
                "VALUES (1, ?, ?) ON CONFLICT(account_id, premium_type) "
                "DO UPDATE SET end_time = excluded.end_time",
                (NEO_PREMIUM_PLUS_TYPE, int(end_time)),
            )


class NeoPremiumRentalIndexTests(unittest.TestCase):
    def test_live_04b5_body_and_success_layout(self) -> None:
        request = rent_opcode.parse_neo_premium_rental_request(b"\x01\x01\x00\x02")
        self.assertIsNotNone(request)
        self.assertEqual(
            [(row.group_index, row.item_index) for row in request.selections],
            [(0, 2)],
        )
        self.assertIsNone(rent_opcode.parse_neo_premium_rental_request(b"\x01\x01\x00"))
        self.assertEqual(
            rent_opcode.build_neo_premium_rental_success([]), b"\x01\x00"
        )
        self.assertEqual(
            rent_opcode.build_neo_premium_rental_success(
                [{"list_type": 0, "slot_index": 9}]
            ),
            b"\x01\x01\x00\x09\x00",
        )

    def test_group_zero_uses_sorted_subtype_ordinal_and_highest_level(self) -> None:
        rows = [
            NeoPremiumRentalWeapon(101000727, 3, 0, 85, ("swordman",), "equipment/x/weapon/0.equ"),
            NeoPremiumRentalWeapon(101010863, 3, 1, 85, ("swordman",), "equipment/x/weapon/1.equ"),
            NeoPremiumRentalWeapon(101020262, 3, 2, 10, ("swordman",), "equipment/x/weapon/2a.equ"),
            NeoPremiumRentalWeapon(101020673, 3, 2, 85, ("swordman",), "equipment/x/weapon/2b.equ"),
            NeoPremiumRentalWeapon(101030740, 3, 3, 85, ("swordman",), "equipment/x/weapon/3.equ"),
            NeoPremiumRentalWeapon(101040492, 3, 5, 85, ("swordman",), "equipment/x/weapon/5.equ"),
        ]
        index = NeoPremiumRentalIndex(rows)
        selected = index.resolve(
            job=0,
            effective_level=100,
            group_index=0,
            item_index=2,
        )
        self.assertEqual(selected.item_id, 101020673)
        restored = NeoPremiumRentalIndex.from_cache_payload(index.to_cache_payload())
        self.assertEqual(
            restored.resolve(job=0, effective_level=100, group_index=0, item_index=2).item_id,
            101020673,
        )

    def test_daily_period_changes_exactly_at_china_0900_and_routes_are_registered(self) -> None:
        day = 10 * 86400
        before = neo_premium_daily_period(day + 3599)
        after = neo_premium_daily_period(day + 3600)
        self.assertEqual(before, (day - 86400 + 3600, day + 3600))
        self.assertEqual(after, (day + 3600, day + 86400 + 3600))
        self.assertTrue({0x04B5, 0x04B6}.issubset(ROUTED_OPCODES))

    def test_runtime_pvf_maps_eiji_selection_to_current_service_three_club(self) -> None:
        selected = get_neo_premium_rental_index().resolve(
            job=0,
            effective_level=100,
            group_index=0,
            item_index=2,
        )
        self.assertEqual(selected.item_id, 101020673)
        metadata = resolve_item_metadata(selected.item_id)
        self.assertEqual(metadata.minimum_level, 85)
        self.assertEqual(metadata.equipment_type.lower(), "[weapon]")


class NeoPremiumRepositoryTests(NeoPremiumRepositoryFixture):
    def test_rental_requires_plus_and_expires_at_next_daily_reset(self) -> None:
        blocked = self.repo.grant_neo_premium_rental_weapons(
            account_id=1,
            character_id=101,
            premium_type=NEO_PREMIUM_PLUS_TYPE,
            weapons=[_weapon_metadata()],
            rental_item_ids=[101020673],
            now_unix=1000,
        )
        self.assertFalse(blocked[0])
        self.assertIn("inactive", blocked[1])

        self.activate_plus(2000)
        ok, note, detail = self.repo.grant_neo_premium_rental_weapons(
            account_id=1,
            character_id=101,
            premium_type=NEO_PREMIUM_PLUS_TYPE,
            weapons=[_weapon_metadata()],
            rental_item_ids=[101020673],
            now_unix=1000,
        )
        self.assertTrue(ok, note)
        self.assertEqual(detail["premium_end_time"], 2000)
        self.assertEqual(detail["rental_expire_time"], 3600)
        self.assertEqual(detail["withdrawn"], [])
        self.assertEqual(detail["granted"][0]["item_id"], 101020673)
        rows = self.repo.list_inventory_items(101, list_type=0, now_unix=3599)
        rental = next(row for row in rows if row["item_template_id"] == 101020673)
        self.assertEqual(rental["expire_time"], 3600)
        rows = self.repo.list_inventory_items(101, list_type=0, now_unix=3600)
        self.assertFalse(any(row["item_template_id"] == 101020673 for row in rows))

    def test_multi_rental_rolls_back_when_only_one_equipment_slot_is_free(self) -> None:
        self.activate_plus(2000)
        with self.repo.open() as conn:
            for slot in range(9, 64):
                conn.execute(
                    "INSERT INTO character_items "
                    "(owner_scope, owner_id, character_id, list_type, slot_index, "
                    "item_template_id, item_kind, stack_count, instance_value) "
                    "VALUES ('character', 101, 101, 0, ?, ?, 'equipment', 1, 999999998)",
                    (slot, 80000000 + slot),
                )
        granted = self.repo.grant_neo_premium_rental_weapons(
            account_id=1,
            character_id=101,
            premium_type=NEO_PREMIUM_PLUS_TYPE,
            weapons=[_weapon_metadata(101020673), _weapon_metadata(101030740)],
            rental_item_ids=[101020673, 101030740],
            now_unix=1000,
        )
        self.assertFalse(granted[0])
        self.assertIn("container_full", granted[1])
        rows = self.repo.list_inventory_items(101, list_type=0, now_unix=1000)
        self.assertFalse(
            any(row["item_template_id"] in (101020673, 101030740) for row in rows)
        )

    def test_second_rental_withdraws_old_bag_item_and_reuses_its_slot(self) -> None:
        self.activate_plus(3000)
        first = self.repo.grant_neo_premium_rental_weapons(
            account_id=1,
            character_id=101,
            premium_type=NEO_PREMIUM_PLUS_TYPE,
            weapons=[_weapon_metadata(101020673)],
            rental_item_ids=[101020673, 101030740],
            now_unix=1000,
        )
        self.assertTrue(first[0], first[1])
        first_slot = int(first[2]["granted"][0]["slot_index"])

        second = self.repo.grant_neo_premium_rental_weapons(
            account_id=1,
            character_id=101,
            premium_type=NEO_PREMIUM_PLUS_TYPE,
            weapons=[_weapon_metadata(101030740)],
            rental_item_ids=[101020673, 101030740],
            now_unix=1100,
        )
        self.assertTrue(second[0], second[1])
        self.assertEqual(
            [
                (row["list_type"], row["slot_index"], row["item_id"])
                for row in second[2]["withdrawn"]
            ],
            [(0, first_slot, 101020673)],
        )
        self.assertEqual(second[2]["granted"][0]["slot_index"], first_slot)
        rows = self.repo.list_inventory_items(101, list_type=0, now_unix=1100)
        rentals = [
            row
            for row in rows
            if int(row["item_template_id"]) in (101020673, 101030740)
        ]
        self.assertEqual(
            [(row["slot_index"], row["item_template_id"]) for row in rentals],
            [(first_slot, 101030740)],
        )
        self.assertIn('"premium_benefit":"neo_premium_rental"', rentals[0]["extra_json"])

    def test_second_rental_replaces_active_worn_item_in_place(self) -> None:
        self.activate_plus(3000)
        with self.repo.open() as conn:
            conn.execute(
                "INSERT INTO character_equipped_entries "
                "(character_id, slot, item_id, expire_time, raw_entry) "
                "VALUES (101, 12, 101020673, 2500, ?)",
                (b"\x00" * 19,),
            )

        result = self.repo.grant_neo_premium_rental_weapons(
            account_id=1,
            character_id=101,
            premium_type=NEO_PREMIUM_PLUS_TYPE,
            weapons=[_weapon_metadata(101030740)],
            rental_item_ids=[101020673, 101030740],
            now_unix=1100,
        )
        self.assertTrue(result[0], result[1])
        self.assertEqual(result[2]["withdrawn"], [])
        self.assertEqual(
            [
                (row["list_type"], row["slot_index"], row["item_id"])
                for row in result[2]["equipped_withdrawn"]
            ],
            [(3, 12, 101020673)],
        )
        self.assertEqual(
            result[2]["replaced_equipment"],
            [
                {
                    "slot_index": 12,
                    "old_item_id": 101020673,
                    "new_item_id": 101030740,
                }
            ],
        )
        self.assertEqual(result[2]["withdrawn_equipment_slots"], [12])
        self.assertEqual(result[2]["affected_equipment_slots"], [12])
        self.assertEqual(
            [
                (row["slot"], row["item_id"], row["expire_time"])
                for row in self.repo.list_equipped_items(101, now_unix=1100)
            ],
            [(12, 101030740, 3600)],
        )
        rows = self.repo.list_inventory_items(101, list_type=0, now_unix=1100)
        self.assertFalse(
            any(
                int(row["item_template_id"]) in (101020673, 101030740)
                for row in rows
            )
        )

    def test_worn_replacement_succeeds_when_bag_is_full(self) -> None:
        self.activate_plus(3000)
        with self.repo.open() as conn:
            conn.execute(
                "INSERT INTO character_equipped_entries "
                "(character_id, slot, item_id, expire_time, raw_entry) "
                "VALUES (101, 12, 101020673, 2500, ?)",
                (b"\x00" * 19,),
            )
            for slot in range(9, 65):
                conn.execute(
                    "INSERT INTO character_items "
                    "(owner_scope, owner_id, character_id, list_type, slot_index, "
                    "item_template_id, item_kind, stack_count, instance_value) "
                    "VALUES ('character', 101, 101, 0, ?, ?, 'equipment', 1, 999999998)",
                    (slot, 80000000 + slot),
                )

        result = self.repo.grant_neo_premium_rental_weapons(
            account_id=1,
            character_id=101,
            premium_type=NEO_PREMIUM_PLUS_TYPE,
            weapons=[_weapon_metadata(101030740)],
            rental_item_ids=[101020673, 101030740],
            now_unix=1100,
        )
        self.assertTrue(result[0], result[1])
        self.assertEqual(result[2]["withdrawn"], [])
        worn = self.repo.list_equipped_items(101, now_unix=1100)
        self.assertEqual(
            [(row["slot"], row["item_id"], row["expire_time"]) for row in worn],
            [(12, 101030740, 3600)],
        )

    def test_expired_old_rental_location_is_returned_before_regrant(self) -> None:
        self.activate_plus(3000)
        with self.repo.open() as conn:
            conn.execute(
                "INSERT INTO character_items "
                "(owner_scope, owner_id, character_id, list_type, slot_index, "
                "item_template_id, item_kind, stack_count, instance_value, expire_time) "
                "VALUES ('character', 101, 101, 0, 9, 101020673, "
                "'equipment', 1, 999999998, 1050)"
            )

        result = self.repo.grant_neo_premium_rental_weapons(
            account_id=1,
            character_id=101,
            premium_type=NEO_PREMIUM_PLUS_TYPE,
            weapons=[_weapon_metadata(101030740)],
            rental_item_ids=[101020673, 101030740],
            now_unix=1100,
        )
        self.assertTrue(result[0], result[1])
        self.assertEqual(
            [
                (row["list_type"], row["slot_index"], row["item_id"])
                for row in result[2]["withdrawn"]
            ],
            [(0, 9, 101020673)],
        )
        self.assertEqual(result[2]["granted"][0]["slot_index"], 9)

    def test_daily_claim_is_account_once_per_china_0900_period(self) -> None:
        now = 100000
        period, reset = neo_premium_daily_period(now)
        self.activate_plus(reset + 86400)
        first = self.repo.claim_neo_premium_daily_gift(
            account_id=1,
            character_id=101,
            premium_type=NEO_PREMIUM_PLUS_TYPE,
            period_start=period,
            next_reset=reset,
            daily_items=((NEO_PREMIUM_DAILY_GIFT_ITEM_ID, 1),),
            now_unix=now,
        )
        self.assertTrue(first[0], first[1])
        duplicate = self.repo.claim_neo_premium_daily_gift(
            account_id=1,
            character_id=101,
            premium_type=NEO_PREMIUM_PLUS_TYPE,
            period_start=period,
            next_reset=reset,
            daily_items=((NEO_PREMIUM_DAILY_GIFT_ITEM_ID, 1),),
            now_unix=now + 1,
        )
        self.assertFalse(duplicate[0])
        self.assertIn("already_claimed", duplicate[1])
        rows = self.repo.list_inventory_items(101, list_type=0, now_unix=now + 1)
        gifts = [row for row in rows if row["item_template_id"] == NEO_PREMIUM_DAILY_GIFT_ITEM_ID]
        self.assertEqual(len(gifts), 1)
        self.assertEqual(gifts[0]["expire_time"], reset)

        next_period, following_reset = neo_premium_daily_period(reset)
        renewed = self.repo.claim_neo_premium_daily_gift(
            account_id=1,
            character_id=101,
            premium_type=NEO_PREMIUM_PLUS_TYPE,
            period_start=next_period,
            next_reset=following_reset,
            daily_items=((NEO_PREMIUM_DAILY_GIFT_ITEM_ID, 1),),
            now_unix=reset,
        )
        self.assertTrue(renewed[0], renewed[1])
        rows = self.repo.list_inventory_items(101, list_type=0, now_unix=reset)
        gifts = [row for row in rows if row["item_template_id"] == NEO_PREMIUM_DAILY_GIFT_ITEM_ID]
        self.assertEqual(len(gifts), 1)
        self.assertEqual(gifts[0]["expire_time"], following_reset)

    def test_full_consumable_bag_does_not_record_daily_claim(self) -> None:
        now = 100000
        period, reset = neo_premium_daily_period(now)
        self.activate_plus(reset + 86400)
        with self.repo.open() as conn:
            for slot in tuple(range(3, 9)) + tuple(range(65, 121)):
                conn.execute(
                    "INSERT INTO character_items "
                    "(owner_scope, owner_id, character_id, list_type, slot_index, "
                    "item_template_id, item_kind, stack_count, instance_value) "
                    "VALUES ('character', 101, 101, 0, ?, ?, 'stackable', 1, 1)",
                    (slot, 90000000 + slot),
                )
        claimed = self.repo.claim_neo_premium_daily_gift(
            account_id=1,
            character_id=101,
            premium_type=NEO_PREMIUM_PLUS_TYPE,
            period_start=period,
            next_reset=reset,
            daily_items=((NEO_PREMIUM_DAILY_GIFT_ITEM_ID, 1),),
            now_unix=now,
        )
        self.assertFalse(claimed[0])
        self.assertIn("container_full", claimed[1])
        with self.repo.open() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM account_premium_daily_claims"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_multi_container_failure_rolls_back_every_reward(self) -> None:
        now = 100000
        period, reset = neo_premium_daily_period(now)
        self.activate_plus(reset + 86400)
        with self.repo.open() as conn:
            for slot in range(121, 177):
                conn.execute(
                    "INSERT INTO character_items "
                    "(owner_scope, owner_id, character_id, list_type, slot_index, "
                    "item_template_id, item_kind, stack_count, instance_value, "
                    "expire_time) VALUES "
                    "('character', 101, 101, 0, ?, ?, 'stackable', 1, 1, ?)",
                    (slot, 90000000 + slot, reset + 86400),
                )

        def metadata_for(item_id: int) -> ItemMetadata:
            if int(item_id) == 600001:
                return _material_metadata()
            if int(item_id) == 600002:
                return _avatar_metadata()
            return resolve_item_metadata(item_id)

        with patch(
            "game.character.character_db.resolve_item_metadata",
            side_effect=metadata_for,
        ):
            claimed = self.repo.claim_neo_premium_daily_gift(
                account_id=1,
                character_id=101,
                premium_type=NEO_PREMIUM_PLUS_TYPE,
                period_start=period,
                next_reset=reset,
                daily_items=((600002, 1), (600001, 1)),
                now_unix=now,
            )
        self.assertFalse(claimed[0])
        self.assertIn("container_full", claimed[1])
        avatars = self.repo.list_inventory_items(101, list_type=1, now_unix=now)
        self.assertFalse(any(row["item_template_id"] == 600002 for row in avatars))
        with self.repo.open() as conn:
            claim_count = conn.execute(
                "SELECT COUNT(*) FROM account_premium_daily_claims"
            ).fetchone()[0]
        self.assertEqual(claim_count, 0)

    def test_relogin_purges_expired_worn_timed_item(self) -> None:
        with self.repo.open() as conn:
            conn.execute(
                "INSERT INTO character_equipped_entries "
                "(character_id, slot, item_id, expire_time, raw_entry) "
                "VALUES (101, 0, 101020673, 1000, ?)",
                (b"\x00" * 19,),
            )
        record = next(row for row in self.repo.list_by_account(1, now_unix=1000) if row.character_id == 101)
        self.assertEqual(record.equipped_entries, ())


class NeoPremiumHandlerTests(NeoPremiumRepositoryFixture):
    def setUp(self) -> None:
        super().setUp()
        self.server = MagicMock()
        self.server.account_id = 1
        self.server._repo.return_value = self.repo
        self.calls: list[tuple[int, int, bytes]] = []

        def emit(flag: int, opcode: int, body: bytes) -> bytes:
            self.calls.append((int(flag), int(opcode), bytes(body)))
            return struct.pack("<BH", int(flag), int(opcode)) + bytes(body)

        self.server._codec_response.side_effect = emit
        self.session = SimpleNamespace(
            selected_character_id=101,
            character_id=101,
            account_premium_effects=None,
        )

    def test_04b5_grants_eiji_weapon_and_returns_location_rows(self) -> None:
        self.activate_plus(2000)
        selected = NeoPremiumRentalWeapon(
            101020673,
            3,
            2,
            85,
            ("swordman",),
            "equipment/x/weapon/club.equ",
        )
        effects = ActivePremiumEffects(
            active_types=(22, 118),
            valid_until=2000,
            equipment_level_reduction=10,
        )
        with patch.object(rent_opcode.time, "time", return_value=1000), patch.object(
            rent_opcode, "get_active_premium_effects", return_value=effects
        ), patch.object(
            rent_opcode, "resolve_neo_premium_rental_weapon", return_value=selected
        ), patch.object(
            rent_opcode, "resolve_item_metadata", return_value=_weapon_metadata()
        ):
            packets, note = rent_opcode.RentNeoPremiumWeapon04B5Service(
                self.server
            ).handle_rent_neo_premium_weapon_04b5(
                b"\x01\x01\x00\x02",
                session=self.session,
            )
        self.assertEqual(len(packets), 3)
        self.assertIn("effective_level=100", note)
        self.assertEqual([call[:2] for call in self.calls], [(1, 0x04B5), (0, 0x000E), (0, 0x000D)])
        self.assertEqual(self.calls[0][2], b"\x01\x00")
        self.assertEqual(struct.unpack_from("<I", self.calls[1][2], 3 + 43)[0], 3600)

    def test_04b5_second_claim_returns_old_location_then_refreshes_new_item(self) -> None:
        self.activate_plus(2000)
        first = self.repo.grant_neo_premium_rental_weapons(
            account_id=1,
            character_id=101,
            premium_type=NEO_PREMIUM_PLUS_TYPE,
            weapons=[_weapon_metadata(101020673)],
            rental_item_ids=[101020673, 101030740],
            now_unix=900,
        )
        self.assertTrue(first[0], first[1])
        old_slot = int(first[2]["granted"][0]["slot_index"])
        selected = NeoPremiumRentalWeapon(
            101030740,
            3,
            3,
            85,
            ("swordman",),
            "equipment/x/weapon/new.equ",
        )
        effects = ActivePremiumEffects(active_types=(118,), valid_until=2000)
        with patch.object(rent_opcode.time, "time", return_value=1000), patch.object(
            rent_opcode, "get_active_premium_effects", return_value=effects
        ), patch.object(
            rent_opcode, "resolve_neo_premium_rental_weapon", return_value=selected
        ), patch.object(
            rent_opcode, "resolve_item_metadata", return_value=_weapon_metadata(101030740)
        ), patch.object(
            rent_opcode,
            "neo_premium_rental_item_ids",
            return_value=(101020673, 101030740),
        ):
            packets, note = rent_opcode.RentNeoPremiumWeapon04B5Service(
                self.server
            ).handle_rent_neo_premium_weapon_04b5(
                b"\x01\x01\x00\x03", session=self.session
            )
        self.assertEqual(len(packets), 3)
        self.assertIn("withdrawn=[(0, %d)]" % old_slot, note)
        self.assertEqual(
            self.calls[0][2], struct.pack("<BBBH", 1, 1, 0, old_slot)
        )
        rows = self.repo.list_inventory_items(101, list_type=0, now_unix=1000)
        rentals = [
            row
            for row in rows
            if int(row["item_template_id"]) in (101020673, 101030740)
        ]
        self.assertEqual(
            [(row["slot_index"], row["item_template_id"]) for row in rentals],
            [(old_slot, 101030740)],
        )

    def test_04b5_worn_replacement_withdraws_slot_then_refreshes_list3(self) -> None:
        now = 1_800_000_000
        premium_end = 1_900_000_000
        self.activate_plus(premium_end)
        with self.repo.open() as conn:
            conn.execute(
                "INSERT INTO character_equipped_entries "
                "(character_id, slot, item_id, expire_time, raw_entry) "
                "VALUES (101, 12, 101020673, ?, ?)",
                (premium_end, b"\x00" * 19),
            )
        selected = NeoPremiumRentalWeapon(
            101030740,
            3,
            3,
            85,
            ("swordman",),
            "equipment/x/weapon/new.equ",
        )
        effects = ActivePremiumEffects(active_types=(118,), valid_until=premium_end)

        with patch.object(rent_opcode.time, "time", return_value=now), patch.object(
            rent_opcode, "get_active_premium_effects", return_value=effects
        ), patch.object(
            rent_opcode, "resolve_neo_premium_rental_weapon", return_value=selected
        ), patch.object(
            rent_opcode, "resolve_item_metadata", return_value=_weapon_metadata(101030740)
        ), patch.object(
            rent_opcode,
            "neo_premium_rental_item_ids",
            return_value=(101020673, 101030740),
        ):
            packets, note = rent_opcode.RentNeoPremiumWeapon04B5Service(
                self.server
            ).handle_rent_neo_premium_weapon_04b5(
                b"\x01\x01\x00\x03", session=self.session
            )

        self.assertEqual(len(packets), 2)
        self.assertIn("withdrawn=[]", note)
        self.assertIn("equipped=[(3, 12)]", note)
        self.assertIn("replaced=[(12, 101020673, 101030740)]", note)
        self.assertIn("equipment_refresh=S0/000E equip_bar", note)
        self.assertEqual(self.calls[0][2], b"\x01\x01\x03\x0c\x00")
        self.assertEqual(
            [call[:2] for call in self.calls],
            [
                (1, 0x04B5),
                (0, 0x000E),
            ],
        )
        self.assertEqual(self.calls[1][2][:3], b"\x03\x01\x00")
        worn = self.repo.list_equipped_items(101, now_unix=now)
        rental_expire = neo_premium_daily_period(now)[1]
        self.assertEqual(
            [(row["slot"], row["item_id"], row["expire_time"]) for row in worn],
            [(12, 101030740, rental_expire)],
        )

    def test_04b6_success_includes_status_then_refreshes_inventory(self) -> None:
        now = 100000
        _period, reset = neo_premium_daily_period(now)
        self.activate_plus(reset + 86400)
        with patch.object(gift_opcode.time, "time", return_value=now), patch.object(
            gift_opcode,
            "neo_premium_daily_reward_items",
            return_value=((NEO_PREMIUM_DAILY_GIFT_ITEM_ID, 1),),
        ):
            packets, note = gift_opcode.ClaimNeoPremiumGift04B6Service(
                self.server
            ).handle_claim_neo_premium_gift_04b6(b"", session=self.session)
        self.assertEqual(len(packets), 3)
        self.assertIn("NEO_PREMIUM_GIFT OK", note)
        self.assertEqual([call[:2] for call in self.calls], [(1, 0x04B6), (0, 0x000E), (0, 0x000D)])
        self.assertEqual(self.calls[0][2], b"\x01")

    def test_04b6_reads_type118_items_and_refreshes_each_container(self) -> None:
        now = 100000
        _period, reset = neo_premium_daily_period(now)
        self.activate_plus(reset + 86400)
        index = SimpleNamespace(
            resolve_type=lambda premium_type: SimpleNamespace(
                effects=SimpleNamespace(
                    daily_items=((600001, 2), (600002, 1)),
                )
            )
            if int(premium_type) == NEO_PREMIUM_PLUS_TYPE
            else None
        )

        def metadata_for(item_id: int) -> ItemMetadata:
            if int(item_id) == 600001:
                return _material_metadata()
            if int(item_id) == 600002:
                return _avatar_metadata()
            return resolve_item_metadata(item_id)

        with patch.object(gift_opcode.time, "time", return_value=now), patch(
            "game.shop.neo_premium_benefits.get_premium_contract_index",
            return_value=index,
        ), patch(
            "game.character.character_db.resolve_item_metadata",
            side_effect=metadata_for,
        ):
            packets, note = gift_opcode.ClaimNeoPremiumGift04B6Service(
                self.server
            ).handle_claim_neo_premium_gift_04b6(b"", session=self.session)

        self.assertEqual(len(packets), 4)
        self.assertIn("daily_items=[(600001, 2), (600002, 1)]", note)
        self.assertEqual(
            [call[:2] for call in self.calls],
            [(1, 0x04B6), (0, 0x000E), (0, 0x000D), (0, 0x000E)],
        )
        self.assertEqual(self.calls[1][2][0], 0)
        self.assertEqual(self.calls[3][2][0], 1)
        main = self.repo.list_inventory_items(101, list_type=0, now_unix=now)
        materials = [row for row in main if row["item_template_id"] == 600001]
        self.assertEqual(sum(row["stack_count"] for row in materials), 2)
        self.assertTrue(all(row["expire_time"] == reset for row in materials))
        avatars = self.repo.list_inventory_items(101, list_type=1, now_unix=now)
        avatar = next(row for row in avatars if row["item_template_id"] == 600002)
        self.assertEqual(avatar["expire_time"], reset)

    def test_04b6_empty_type118_items_does_not_record_claim(self) -> None:
        now = 100000
        _period, reset = neo_premium_daily_period(now)
        self.activate_plus(reset + 86400)
        with patch.object(gift_opcode.time, "time", return_value=now), patch.object(
            gift_opcode,
            "neo_premium_daily_reward_items",
            return_value=(),
        ):
            packets, note = gift_opcode.ClaimNeoPremiumGift04B6Service(
                self.server
            ).handle_claim_neo_premium_gift_04b6(b"", session=self.session)
        self.assertEqual(packets, [])
        self.assertIn("daily_gift_rewards_empty type=118", note)
        with self.repo.open() as conn:
            claim_count = conn.execute(
                "SELECT COUNT(*) FROM account_premium_daily_claims"
            ).fetchone()[0]
        self.assertEqual(claim_count, 0)


if __name__ == "__main__":
    unittest.main()
