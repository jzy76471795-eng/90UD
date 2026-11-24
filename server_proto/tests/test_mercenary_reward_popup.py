import struct
import unittest

from game.chain_loader import load_opcode


class MercenaryRewardPopupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reward = load_opcode("opcode057d-claim_adventure_dispatch_reward.py")

    def test_consecutive_claims_replace_every_display_row(self):
        slots = [None] * 3
        for granted in (
            [(3330, 151), (10158964, 30)],
            [(3330, 6)],
            [(3330, 7)],
            [(3330, 151), (10158964, 30)],
        ):
            with self.subTest(granted=granted):
                body = self.reward.build_claim_adventure_dispatch_success_057d(101, granted)
                status, character, count = struct.unpack_from("<BIB", body)
                self.assertEqual((status, character, count), (1, 101, 3))
                rows = list(struct.iter_unpack("<iI", body[6:]))
                self.assertEqual(len(rows), count)
                slots[:count] = rows
                self.assertEqual(slots, granted + [(self.reward.EMPTY_REWARD_DISPLAY_ID, 1)] * (3 - len(granted)))

    def test_empty_display_slot_cannot_be_awarded(self):
        for row in ((-1, 0), (0, 0), (3330, 0), (self.reward.EMPTY_REWARD_DISPLAY_ID, 1)):
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.reward.build_claim_adventure_dispatch_success_057d(101, [row])
