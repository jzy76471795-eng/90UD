"""Ordinary first/changed-selection/concurrent/delayed UDP invitation lifecycle."""
import struct
from test_udp_waits import UdpWaitTests

class FirstInviteTests(UdpWaitTests):
    def setUp(self):
        super().setUp()
        for _ in range(3):
            for i in range(1,7):self.pump(i)

    def pending_targets(self):
        return {int.from_bytes(p.body[:2],'little') for p in self.party.udp.pending.values()}

    def test_first_invite_and_first_accept_create_exact_two_members(self):
        self.invite();packets,_=self.accept()
        self.assertEqual(packets,[(1,11,b'\1\1\0\0')])
        self.assertEqual(len(self.party.current(self.ss[0]).members),2)

    def test_unanswered_b_does_not_block_c_and_both_can_accept(self):
        self.invite(1,2);self.invite(1,3)
        self.assertEqual(len(self.party._invites),2)
        self.accept(1,3);self.accept(1,2)
        party=self.party.current(self.ss[0])
        self.assertEqual({s[4] for s in party.members.values()},{1,2,3})

    def test_two_udp_waits_from_same_sender_are_retained_independently(self):
        self.expire();self.request(1,2);self.request(1,3)
        self.assertEqual(self.pending_targets(),{2,3})
        self.fresh(1,3);self.now+=.3;self.pump(1);self.pump(3)
        self.assertEqual(self.pending_targets(),{2})
        self.accept(1,3)
        self.fresh(2);self.now+=.3;self.pump(1);self.pump(2)
        self.assertFalse(self.party.udp.pending)
        self.accept(1,2)
        self.assertEqual({s[4] for s in self.party.current(self.ss[0]).members.values()},{1,2,3})

    def test_second_ready_target_does_not_cancel_first_missing_target(self):
        del self.ss[1]._native_udp_address_claim
        self.request(1,2);self.invite(1,3);self.accept(1,3)
        self.assertEqual(self.pending_targets(),{2})
        self.fresh(2);self.now+=.3;self.pump(1);self.pump(2);self.accept(1,2)
        self.assertEqual(len(self.party.current(self.ss[0]).members),3)

    def test_late_report_after_invite_resumes_without_second_click(self):
        del self.ss[1]._native_udp_address_claim
        self.request(1,2);self.now+=2;self.pump(1)
        self.assertFalse(self.party._invites)
        self.fresh(2);self.now+=.3;self.pump(1);self.pump(2);self.accept()
        self.assertEqual(len(self.party.current(self.ss[0]).members),2)

    def test_a_failed_target_timeout_does_not_cancel_another_invitation(self):
        del self.ss[1]._native_udp_address_claim
        self.request(1,2);self.invite(1,3)
        self.now+=19;self.pump(1);self.pump(3)
        self.assertFalse(self.party.udp.pending)
        self.assertEqual(len(self.party._invites),1)
        self.accept(1,3)
        self.assertEqual(len(self.party.current(self.ss[0]).members),2)

    def test_duplicate_target_never_extends_or_multiplies_wait(self):
        self.expire();self.request(1,2);first=next(iter(self.party.udp.pending.values()));deadline=first.expires
        self.now+=1;self.request(1,3);self.request(1,2)
        self.assertEqual(len(self.party.udp.pending),2)
        self.assertIn(first,self.party.udp.pending.values())
        self.assertEqual(first.expires,deadline)

    def test_wait_for_d_during_existing_party_acceptance_of_c_is_not_lost(self):
        self.invite();self.accept()
        del self.ss[3]._native_udp_address_claim
        self.request(1,4);self.invite(1,3);self.accept(1,3)
        self.assertEqual(self.pending_targets(),{4})
        self.fresh(4);self.now+=.3;self.pump(1);self.pump(4);self.accept(1,4)
        self.assertEqual(len(self.party.current(self.ss[0]).members),4)

    def test_late_acceptance_cannot_overfill_four_player_party(self):
        for i in (2,3,4,5):self.invite(1,i)
        for i in (2,3,4):self.accept(1,i)
        with self.assertRaises(Exception):self.accept(1,5)
        self.assertEqual(len(self.party.current(self.ss[0]).members),4)
        self.assertIsNone(self.party.current(self.ss[4]))

    def test_selection_change_clears_all_old_invites_and_new_actor_can_invite(self):
        self.invite(1,2);self.invite(1,3)
        s=self.ss[0];self.party.forget(s)
        s.selected_character_id=101;s.chat_selection_generation+=1
        self.records[1].append(type(self.records[1][0])(character_id=101,account_id=1,name_bytes=b'New1'))
        for _ in range(3):
            for i in (1,2,3):self.pump(i)
        self.assertFalse(self.party._invites)
        self.invite(1,2)
        self.call(2,11,struct.pack('<HBI',101,0,0))
        self.assertEqual({x[4] for x in self.party.current(s).members.values()},{101,2})

    def test_same_numeric_uid_new_selection_does_not_resume_old_wait(self):
        self.expire();self.request(1,2);self.request(1,3)
        self.ss[0].chat_selection_generation+=1
        self.fresh(1,2,3);self.now+=.3;self.pump(1)
        self.assertFalse(self.party.udp.pending);self.assertFalse(self.party._invites)
        for _ in range(3):
            for i in (1,2):self.pump(i)
        self.invite();self.accept()
        self.assertEqual(len(self.party.current(self.ss[0]).members),2)

    def test_one_hour_online_with_real_refreshes_then_first_invite(self):
        for _ in range(180):self.now+=20;self.fresh(1,2)
        self.invite();self.accept()
        self.assertEqual(len(self.party.current(self.ss[0]).members),2)

    def test_refresh_lost_then_accept_wait_recovers(self):
        self.now+=3600;self.fresh(1,2);self.invite()
        self.now+=29;del self.ss[1]._native_udp_address_claim
        self.assertEqual(self.accept()[0],[])
        self.fresh(1,2);self.now+=.3;self.pump(2)
        self.assertEqual(len(self.party.current(self.ss[0]).members),2)

    def test_two_refresh_cycles_lost_then_next_success_resumes_within_wait(self):
        # No datagram or claim arrives during two consecutive healthy cycles.
        self.now+=20
        self.now+=11
        self.request(1,2)
        started=self.now
        self.assertEqual(self.pending_targets(),{2})
        self.now+=10
        self.fresh(1,2);self.now+=.3;self.pump(1);self.pump(2)
        self.assertLess(self.now-started,18)
        self.assertFalse(self.party.udp.pending)
        self.accept()
        self.assertEqual(len(self.party.current(self.ss[0]).members),2)

    def test_forged_report_alone_keeps_all_waits_uncommitted(self):
        self.expire();self.request(1,2);self.request(1,3)
        self.fresh(1,2,3,datagram=False);self.now+=.3;self.pump(1)
        self.assertEqual(self.pending_targets(),{2,3});self.assertFalse(self.party._invites)

    def test_declining_one_deferred_accept_does_not_remove_other_target(self):
        self.invite(1,2);self.invite(1,3)
        del self.ss[1]._native_udp_address_claim;self.accept(1,2)
        self.call(2,11,struct.pack('<HBI',1,0,1))
        self.accept(1,3)
        self.assertEqual({x[4] for x in self.party.current(self.ss[0]).members.values()},{1,3})
