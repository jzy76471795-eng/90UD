import sys
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from game.shop.neo_premium_benefits import NeoPremiumRentalIndex, NeoPremiumRentalWeapon, get_neo_premium_rental_index
from game.character import item_metadata

ROOT = Path(__file__).resolve().parents[1]
CATALOG = get_neo_premium_rental_index().to_cache_payload()
MENU = item_metadata._get_reader().read_text('event/rentalservice.evt')


class RentalSelectionRegressionTests(unittest.TestCase):
    def setUp(self):
        self.index = NeoPremiumRentalIndex.from_cache_payload(CATALOG)

    def test_greatsword_selection_does_not_grant_katana(self):
        for job in (0, 9, 11):
            with self.subTest(job=job):
                row = self.index.resolve(job=job, effective_level=90, group_index=0, item_index=1)
                self.assertEqual(row.item_id, 101030740)
                self.assertIn('/hsword/', row.source_path)

    def test_katana_selection_does_not_grant_greatsword(self):
        for job in (0, 9, 11):
            with self.subTest(job=job):
                row = self.index.resolve(job=job, effective_level=90, group_index=0, item_index=3)
                self.assertEqual(row.item_id, 101010863)
                self.assertIn('/katana/', row.source_path)

    def test_all_sword_menu_entries_match_pvf_order_at_every_level_section(self):
        by_id = {w.item_id:w for w in self.index.weapons}
        # Section 0 is displayed from level 1, but its weapon templates require
        # level 10. Exercise the existing equipment level gate separately.
        sections = (10,20,30,40,50,60,70,80,85)
        groups = re.findall(r'\[group\]\s*`\[([^]]+)\]`\s*(\d+)\s*\[package selection\](.*?)\[/package selection\]',MENU,re.S)
        checks = 0
        for job_name, section, content in groups:
            if job_name not in ('swordman', 'demonic swordman', 'at swordman'):
                continue
            job = {'swordman':0,'demonic swordman':9,'at swordman':11}[job_name]
            ids = [int(v) for v in re.findall(r'\d+', content)][::4]
            level = sections[int(section)]
            for item_index, item_id in enumerate(ids):
                with self.subTest(job=job,level=level,item_index=item_index):
                    row = self.index.resolve(job=job,effective_level=level,group_index=0,item_index=item_index)
                    self.assertEqual(row.subtype, by_id[item_id].subtype)
                    self.assertLessEqual(row.minimum_level,level)
                    checks += 1
        self.assertEqual(checks,129)

    def test_unaffected_jobs_keep_existing_selection(self):
        from game.character.item_metadata import CHARACTER_JOB_LABELS
        for job, label in enumerate(CHARACTER_JOB_LABELS):
            if job in (0,9,11):continue
            rows = [w for w in self.index.weapons if label in w.usable_jobs]
            for i, subtype in enumerate(sorted({w.subtype for w in rows})):
                with self.subTest(job=job,index=i):
                    selected = self.index.resolve(job=job,effective_level=100,group_index=0,item_index=i)
                    expected = max((w for w in rows if w.subtype==subtype and w.minimum_level<=100),key=lambda w:(w.minimum_level,w.item_id))
                    self.assertEqual(selected,expected)

    def test_missing_sword_type_does_not_shift_menu_positions(self):
        rows = [w for w in self.index.weapons if w.subtype != 3]
        index = NeoPremiumRentalIndex(rows)
        with self.assertRaises(LookupError):
            index.resolve(job=0,effective_level=90,group_index=0,item_index=1)
        self.assertEqual(index.resolve(job=0,effective_level=90,group_index=0,item_index=3).item_id,101010863)

    def test_level_and_group_bounds_remain_enforced(self):
        for job in (0,9,11):
            for index in (-1,5,255):
                with self.subTest(job=job,index=index),self.assertRaises(LookupError):
                    self.index.resolve(job=job,effective_level=90,group_index=0,item_index=index)
            with self.assertRaises(LookupError):
                self.index.resolve(job=job,effective_level=90,group_index=1,item_index=1)
            with self.assertRaises(LookupError):
                self.index.resolve(job=job,effective_level=0,group_index=0,item_index=1)

    def test_cache_round_trip_preserves_selection(self):
        restored = NeoPremiumRentalIndex.from_cache_payload(self.index.to_cache_payload())
        for job in (0,9,11):
            for i in range(5):
                self.assertEqual(restored.resolve(job=job,effective_level=90,group_index=0,item_index=i),self.index.resolve(job=job,effective_level=90,group_index=0,item_index=i))


from types import SimpleNamespace
from unittest.mock import patch
from test_neo_premium_benefits import NeoPremiumRepositoryFixture, rent_opcode
from game.shop.premium_contract_index import ActivePremiumEffects


class RentalGrantRegressionTests(NeoPremiumRepositoryFixture):
    def grant_sequence(self, selections):
        self.activate_plus(5000)
        server = SimpleNamespace(account_id=1, _repo=lambda:self.repo,
            _codec_response=lambda flag,opcode,body:(flag,opcode,body))
        session = SimpleNamespace(selected_character_id=101,account_id=1,account_premium_effects=None)
        effects = ActivePremiumEffects(active_types=(118,), valid_until=5000)
        slot = None
        with patch.object(rent_opcode.time,'time',return_value=1000),patch.object(rent_opcode,'get_active_premium_effects',return_value=effects):
            for choice, expected_id in selections:
                packets,note=rent_opcode.RentNeoPremiumWeapon04B5Service(server).handle_rent_neo_premium_weapon_04b5(bytes((1,1,0,choice)),session=session)
                self.assertIn('NEO_PREMIUM_RENT OK',note)
                self.assertEqual(packets[0][:2],(1,0x04B5))
                rows=self.repo.list_inventory_items(101,list_type=0,now_unix=1000)
                rental=[r for r in rows if int(r['item_template_id']) in (101030740,101010863)]
                self.assertEqual([int(r['item_template_id']) for r in rental],[expected_id])
                self.assertEqual(int(rental[0]['expire_time']),3600)
                if slot is not None:self.assertEqual(int(rental[0]['slot_index']),slot)
                slot=int(rental[0]['slot_index'])

    def test_real_request_grants_greatsword_then_exchanges_for_katana(self):
        self.grant_sequence(((1,101030740),(3,101010863)))

    def test_real_request_grants_katana_then_exchanges_for_greatsword(self):
        self.grant_sequence(((3,101010863),(1,101030740)))
