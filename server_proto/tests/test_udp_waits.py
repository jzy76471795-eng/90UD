import struct
from types import SimpleNamespace as NS

from test_social import Fixture
from game.normal_party import route
from game.raid.native_network_protocol import parse_udp_address_report
from game.raid.native_udp import native_ipv4_word


class UdpWaitTests(Fixture):
    def setUp(self):
        super().setUp()
        self.party.clock = lambda: self.now
        self.probe = self.server.native_udp_probe_service
        self.ready()

    def request(self, owner=1, target=2, mode=0):
        return self.call(owner, 10, struct.pack('<HBIHI', target, mode, 123, 0, 0))

    def fresh(self, *ids, report=True, datagram=True):
        for i in ids:
            session = self.ss[i-1]
            if datagram:
                self.assertTrue(self.probe.handle_datagram(b'\1', ('127.0.0.1', 30000+i), 12311, lambda b,e:len(b)))
            if report:
                value = parse_udp_address_report(struct.pack('<BIIHII', 4,
                    native_ipv4_word('192.168.1.2'), native_ipv4_word('127.0.0.1'), 30000+i, 1280, 0))
                self.assertTrue(self.probe.record_claim(session, value))
                session._native_udp_address_claim = NS(account_id=i, report=value)

    def expire(self):
        self.now += 31

    def test_ready_invite_remains_immediate(self):
        packets, _ = self.request()
        self.assertEqual(packets, [(1,10,b'\1')])
        self.assertFalse(self.party.udp.pending)

    def test_expired_addresses_wait_then_deliver_once(self):
        self.expire()
        self.assertEqual(self.request()[0], [])
        self.assertEqual(len(self.party.udp.pending), 1)
        self.assertFalse(self.party._invites)
        self.fresh(1,2)
        self.now += .3
        self.pump(1);self.pump(2);self.pump(1);self.pump(2)
        self.assertFalse(self.party.udp.pending)
        self.assertEqual(sum(p[:2]==(1,10) for p in self.pump(1)), 1)
        self.assertEqual(sum(p[:2]==(0,7) for p in self.pump(2)), 1)

    def test_report_without_real_udp_does_not_resume(self):
        self.expire();self.request();self.fresh(1,2,datagram=False)
        self.now += .3;self.pump(1);self.pump(2)
        self.assertEqual(len(self.party.udp.pending), 1)
        self.assertFalse(self.party._invites)

    def test_missing_report_is_not_invented_from_same_public_ip(self):
        del self.ss[1]._native_udp_address_claim
        self.request();self.now += .3;self.pump(1)
        self.assertEqual(len(self.party.udp.pending), 1)
        self.fresh(2);self.now += .3;self.pump(1);self.pump(2)
        self.assertEqual(sum(p[:2]==(0,7) for p in self.pump(2)), 1)

    def test_duplicate_click_keeps_original_deadline(self):
        self.expire();self.request()
        first = next(iter(self.party.udp.pending.values()))
        self.now += 4
        self.request();self.request()
        self.assertIs(next(iter(self.party.udp.pending.values())), first)
        self.assertEqual(first.expires, self.now + 14)

    def test_timeout_sends_one_native_error_and_can_retry(self):
        self.expire();self.request();self.now += 19
        self.pump(1);self.pump(1)
        self.assertFalse(self.party.udp.pending)
        self.assertEqual(sum(p==(1,10,b'\0\x13\0') for p in self.pump(1)), 1)
        self.fresh(1,2)
        self.assertEqual(self.request()[0], [(1,10,b'\1')])

    def test_accept_wait_keeps_membership_unchanged_until_both_ready(self):
        self.invite();del self.ss[1]._native_udp_address_claim
        self.assertEqual(self.accept()[0], [])
        self.assertIsNone(self.party.current(self.ss[0]))
        self.assertIsNone(self.party.current(self.ss[1]))
        self.fresh(1,2);self.now += .3
        self.pump(2);self.pump(1)
        self.assertIs(self.party.current(self.ss[0]), self.party.current(self.ss[1]))
        self.assertEqual(len(self.party.current(self.ss[0]).members), 2)
        self.assertEqual(sum(p==(1,11,b'\1\1\0\0') for p in self.pump(2)), 1)

    def test_accept_timeout_releases_sender_and_recipient(self):
        self.invite();self.expire()
        # Invitations themselves last30s. Refresh expires while an invitation
        # is pending by dropping one live report instead of aging the invite.
        self.now -= 31
        del self.ss[1]._native_udp_address_claim
        self.accept();self.now += 19
        self.pump(2);self.pump(1)
        self.assertFalse(self.party.udp.pending)
        self.assertFalse(self.party._invites)
        self.assertIn((1,11,b'\0\x13\0'), self.pump(2))
        self.assertTrue(any(p[:2]==(0,8) for p in self.pump(1)))

    def test_changed_selection_cancels_wait(self):
        self.expire();self.request();self.ss[1].chat_selection_generation += 1
        self.pump(1)
        self.assertFalse(self.party.udp.pending)
        self.assertFalse(self.party._invites)

    def test_peer_disconnect_cancels_wait(self):
        self.expire();self.request()
        directory = self.server.native_game_connections
        directory.close_connection(directory._connections[id(self.ss[1])])
        self.pump(1)
        self.assertFalse(self.party.udp.pending)
        self.assertFalse(self.party._invites)

    def test_leave_cancels_wait_without_creating_party(self):
        self.expire();self.request();self.call(1,13,b'')
        self.fresh(1,2);self.now += .3;self.pump(1);self.pump(2)
        self.assertFalse(self.party._invites)
        self.assertFalse(self.party.udp.pending)

    def test_unrelated_ready_invite_is_not_blocked(self):
        del self.ss[1]._native_udp_address_claim
        self.request();self.pump(3);self.pump(4);self.pump(3);self.pump(4)
        self.assertEqual(self.request(3,4)[0], [(1,10,b'\1')])
        self.assertEqual(len(self.party.udp.pending), 1)

    def test_same_recipient_is_reserved_during_refresh(self):
        del self.ss[1]._native_udp_address_claim
        self.request();self.pump(3);self.pump(2);self.pump(3)
        with self.assertRaises(Exception):
            self.request(3,2)
        self.assertEqual(len(self.party.udp.pending), 1)

    def test_peer_moving_area_prevents_delayed_delivery(self):
        self.expire();self.request();self.ss[1].cur_area_id = 2
        self.fresh(1,2);self.now += .3;self.pump(1)
        self.assertFalse(self.party.udp.pending)
        self.assertFalse(self.party._invites)

    def test_existing_party_change_cancels_pending_invite(self):
        self.invite();self.accept();self.pump(3);self.pump(1);self.pump(3)
        del self.ss[2]._native_udp_address_claim
        self.request(1,3)
        self.call(2,13,b'')
        self.fresh(1,3);self.now += .3;self.pump(1)
        self.assertFalse(self.party.udp.pending)
        self.assertFalse(self.party._invites)

    def test_explicit_decline_cancels_deferred_accept(self):
        self.invite();del self.ss[1]._native_udp_address_claim
        self.accept()
        result = self.call(2,11,struct.pack('<HBI',1,0,1))
        self.assertIn('declined', result[1])
        self.assertFalse(self.party.udp.pending)
        self.assertFalse(self.party._invites)

    def test_packet_send_happens_outside_party_lock(self):
        self.expire();self.request();self.fresh(1,2);self.now += .3
        socket = self.server.native_game_connections._connections[id(self.ss[0])].client
        original = socket.sendall
        def checked(packet):
            self.assertFalse(self.party._lock._is_owned())
            original(packet)
        socket.sendall = checked
        self.pump(1)

    def test_mode12_timeout_preserves_native_mode_byte(self):
        self.expire();self.request(mode=12);self.now += 19;self.pump(1)
        self.assertIn((1,10,b'\0\x13\x0c'), self.pump(1))

    def test_missing_peer_does_not_rebuild_ready_members_profiles(self):
        del self.ss[1]._native_udp_address_claim
        original = self.social.build_body
        self.social.build_body = lambda selection: self.fail('profile build during UDP wait')
        self.request();self.now += .3;self.pump(1)
        self.assertEqual(len(self.party.udp.pending), 1)
        self.social.build_body = original
