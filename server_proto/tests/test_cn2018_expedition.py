from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from game.character.character_db import CharacterRepository
from game.character.cn2018_expedition import CN2018Expedition, CN_TIME, ExpeditionRejected, expedition_rotation
from pvf.cn_expedition_index import get_cn_expedition_index


class CN2018ExpeditionTests(unittest.TestCase):
    def setUp(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        self.db = Path(state.name) / "isolated.db"
        self.repo = CharacterRepository(self.db, bootstrap_from_reference=False)
        for account_id in (1, 2):
            self.repo.ensure_account(account_id, f"expedition-qa-{account_id}")
        self._character(101, branch=1)
        self._character(102, branch=1)
        self._character(103, branch=2)
        self._character(104, branch=3, level=89)
        self._character(105, branch=4, awakening=1)
        self._character(201, branch=1, account_id=2)
        self.now = int(datetime(2026, 11, 25, 6, tzinfo=CN_TIME).timestamp())
        self.index = get_cn_expedition_index()
        self.policy = {(1, 6, 1, 1): 3, (1, 12, 1, 1): 5}
        self.service = self._service(self.repo)

    def _character(self, cid, *, branch, awakening=2, level=90, account_id=1, job=0):
        self.repo.create_character(
            account_id=account_id, character_id=cid, name=f"Exp{cid}", job=job,
            grow_type=awakening << 4 | branch, level=level,
        )

    def _service(self, repo):
        return CN2018Expedition(repo, self.index, verified_rewards=self.policy, clock=lambda: self.now)

    def _start(self, request="start-1", cid=101, hours=6, **kwargs):
        arguments = dict(account_id=1, request_id=request, area_id=1, hours=hours, members=(cid,),
                         rotation_key=self.service.status(1)["rotation_key"])
        arguments.update(kwargs)
        return self.service.start(**arguments)

    def test_reads_stock_levels_and_rotates_at_six(self):
        self.assertEqual([area.required_level for area in self.index.areas.values()], [1, 10, 15, 20])
        self.assertEqual(len(self.index.characters), 59)
        self.assertEqual(self.index.collection_characters - self.index.characters.keys(), {(0, 5, 2)})
        key, today = expedition_rotation(self.index, self.now)
        yesterday_key, _ = expedition_rotation(self.index, self.now - 1)
        self.assertEqual(key, yesterday_key + 1)
        self.assertEqual(expedition_rotation(self.index, self.now + 83999), (key, today))
        self.assertNotEqual(today[3][0], today[4][0])
        self.assertEqual(len(set(today[4])), 4)

    def test_collection_filters_and_replaces_representatives_by_branch(self):
        self.assertEqual([item.character_id for item in self.service.collection_candidates(1, 0, 1)], [101, 102])
        self.assertEqual(self.service.collection_candidates(1, 0, 3), ())
        for cid in (104, 105, 201):
            with self.subTest(cid=cid), self.assertRaisesRegex(ExpeditionRejected, "collection_character_ineligible"):
                self.service.set_representative(1, cid)
        for cid in (101, 102, 103):
            self.service.set_representative(1, cid)
        self.assertEqual({item.character_id for item in self.service.collection(1)}, {102, 103})
        self.assertFalse(self.service.remove_representative(2, 102))
        self.assertTrue(self.service.remove_representative(1, 102))
        self.assertEqual([item.character_id for item in self.service.collection(1)], [103])

    def test_rejects_unregistered_locked_invalid_and_unverified(self):
        with self.assertRaisesRegex(ExpeditionRejected, "member_unavailable"):
            self._start()
        self.service.set_representative(1, 101)
        self.service.set_representative(2, 201)
        with self.assertRaisesRegex(ExpeditionRejected, "area_locked"):
            self._start(area_id=4, account_id=2, cid=201)
        with self.assertRaisesRegex(ExpeditionRejected, "party_or_time_invalid"):
            self._start(hours=7)
        with self.assertRaisesRegex(ExpeditionRejected, "party_or_time_invalid"):
            self._start(members=(101, 101))
        with self.assertRaisesRegex(ExpeditionRejected, "rotation_expired"):
            self._start(rotation_key=1)
        with self.assertRaisesRegex(ExpeditionRejected, "reward_unverified"):
            self._start(hours=18)
        self.assertEqual(self.service.status(1)["runs"], ())
        self.assertEqual(self.service.status(1)["glory_balance"], 0)

    def test_collection_only_branch_and_self_awakened_characters(self):
        self._character(106, branch=5)
        self._character(107, branch=1, job=9)
        self._character(108, branch=1, job=10)
        for cid in (106, 107, 108):
            self.service.set_representative(1, cid)
        self.assertEqual({member.character_id for member in self.service.collection(1)}, {106, 107, 108})
        view = {row["character_id"]: row for row in self.service.collection_view(1)}
        self.assertEqual(view[106]["attribute_ids"], [])
        with self.assertRaisesRegex(ExpeditionRejected, "member_unavailable"):
            self._start(cid=106)

    def test_autoparty_retry_cancel_and_account_isolation(self):
        self.service.set_representative(1, 101)
        key, members = self.service.auto_party(1, 1)
        self.assertEqual(members, (101,))
        self.assertEqual([item.character_id for item in self.service.expedition_candidates(1, 1)[2]], [101])
        started = self._start(rotation_key=key)
        self.assertEqual(self._start(rotation_key=key), started)
        self.assertEqual(self.service.auto_party(1, 1)[1], ())
        self.assertEqual(self.service.expedition_candidates(1, 1)[2], ())
        with self.assertRaisesRegex(ExpeditionRejected, "area_busy"):
            self._start(request="different")
        with self.assertRaisesRegex(ExpeditionRejected, "request_conflict"):
            self._start(hours=12)
        with self.assertRaisesRegex(ExpeditionRejected, "run_unknown"):
            self.service.claim(2, started["run_id"])
        returned = self.service.cancel(1, started["run_id"])
        self.assertEqual(returned, self.service.cancel(1, started["run_id"]))
        self.assertEqual(returned["glory_credited"], 0)
        self.assertEqual(self.service.auto_party(1, 1)[1], (101,))
        self.assertEqual(self.service.status(1)["glory_balance"], 0)
        self.assertEqual(self._start(rotation_key=key)["status"], "cancelled")

    def test_persisted_timer_and_concurrent_claim(self):
        self.service.set_representative(1, 101)
        run = self._start(hours=12)
        self.now += 12 * 3600 - 1
        repo = CharacterRepository(self.db, bootstrap_from_reference=False)
        service = self._service(repo)
        self.assertEqual(service.status(1)["runs"][0]["status"], "active")
        with self.assertRaisesRegex(ExpeditionRejected, "not_ready"):
            service.claim(1, run["run_id"])
        self.now += 1
        self.assertEqual(service.status(1)["runs"][0]["status"], "ready")
        with self.assertRaisesRegex(ExpeditionRejected, "not_cancellable"):
            service.cancel(1, run["run_id"])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(pool.map(lambda _: service.claim(1, run["run_id"]), [0, 1]))
        self.assertEqual([result["status"] for result in results], ["claimed", "claimed"])
        self.assertEqual([result["glory_credited"] for result in results], [5, 5])
        self.assertEqual(service.status(1)["glory_balance"], 5)
        self.assertEqual(service.status(1)["runs"], ())
        self.assertEqual(repo.load_adventure_dispatch_state(1)["dispatch_state"], 0)

    def test_two_areas_can_run_but_cannot_share_a_member(self):
        self.service.set_representative(1, 101)
        self.service.set_representative(1, 103)
        for _ in range(30):
            key, team = self.service.auto_party(1, 2)
            if team:
                break
            self.now += 86400
        self.assertTrue(team)
        reserved = team[0]
        other = 101 if reserved == 103 else 103
        self.policy.update({(2, 6, 1, 1): 7, (2, 6, 1, 2): 9})
        self.service = self._service(self.repo)
        first = self._start(cid=reserved, rotation_key=key)
        with self.assertRaisesRegex(ExpeditionRejected, "member_unavailable"):
            self._start(request="area-two-duplicate", cid=reserved, area_id=2, rotation_key=key)
        self.service.cancel(1, first["run_id"])
        self._start(request="area-two", cid=reserved, area_id=2, rotation_key=key)
        self._start(request="area-one", cid=other, rotation_key=key)
        self.assertEqual([run["area_id"] for run in self.service.status(1)["runs"]], [1, 2])

    def test_monthly_glory_cap_and_six_am_rollover(self):
        self.service.set_representative(1, 101)
        self.now = int(datetime(2026, 11, 30, 17, tzinfo=CN_TIME).timestamp())
        run = self._start()
        with self.repo.open() as conn:
            conn.execute(
                "INSERT INTO account_cn2018_glory (account_id,period_yyyymm,balance) VALUES (1,202611,9998)"
            )
        self.now += 6 * 3600
        self.assertEqual(self.service.claim(1, run["run_id"])["glory_credited"], 1)
        self.now = int(datetime(2026, 12, 1, 5, 59, 59, tzinfo=CN_TIME).timestamp())
        self.assertEqual(self.service.status(1)["glory_balance"], 9999)
        self.now += 1
        self.assertEqual(self.service.status(1)["glory_balance"], 0)
        self.assertEqual(self.service.claim(1, run["run_id"])["glory_credited"], 1)
        self.assertEqual(self.service.status(1)["glory_balance"], 0)


if __name__ == "__main__":
    unittest.main()
