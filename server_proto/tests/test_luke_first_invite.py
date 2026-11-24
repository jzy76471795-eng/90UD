"""Luke C000A mode10 admission plus real connection/UDP/ordinary publication.

The frozen Luke client uses applicant->leader requests and leader approvals;
this tests that flow explicitly, rather than inventing an ordinary mode0 invite.
"""
from contextlib import ExitStack
import struct
import socket
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from dataclasses import replace
from time import monotonic
from test_raid_udp_candidate import luke_setup
import test_raid_ordinary_party as upstream
from test_raid_native_solo_start import decoded
from game.raid.protocol import C_LEAVE_RAID
from game.character.payloads import build_userinfo_subtype0_min

class LukeFirstInviteTests(unittest.TestCase):
    channel=32
    def setUp(self):
        self.f=upstream.OrdinaryPartyTests(methodName='runTest')
        if self.channel==32:
            with patch.object(upstream.owned.NativeJoinTests,'setUp',luke_setup):self.f.setUp()
        else:self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.server,self.service,self.entry=self.f.server,self.f.service,self.f.entry
        self.a,self.b,self.c,self.d=self.f.sessions
        self.probe=self.server.native_udp_probe_service
        self.assertEqual(self.entry.domain.family,'luke' if self.channel==32 else 'anton')
        self.assertTrue(all(s.selected_channel_id==self.channel for s in self.f.sessions))
        for s in (self.b,self.c,self.d):
            with self.server.account_scope(s):
                packets,_=self.service.handle(C_LEAVE_RAID,struct.pack('<H',s.selected_character_id),s)
                self.send(s,packets)
        self.assertEqual([m.character_id for m in self.entry.domain.members],[101])

    def send(self,s,packets):
        self.f.send(s,packets)
        with self.server.account_scope(s):
            for p in packets:self.service.native_join.note_send(s,p)

    def request(self,s):
        with self.server.account_scope(s):
            packets,note=self.service.handle(10,struct.pack('<HBIHI',101,10,0,0,0),s)
            self.send(s,packets)
        return packets,note

    def pump(self):
        for _ in range(3):
            for s in self.f.sessions:
                with self.server.account_scope(s):
                    self.send(s,self.service.native_lobby.pending_packets(s))
                    self.send(s,self.service.native_join.pending_packets(s))
                    self.send(s,self.service.lobby_refresh_packets(s) or [])
                    self.send(s,self.service.native_party.pending_packets(s))

    def accept(self,s):
        with self.server.account_scope(self.a):
            packets,note=self.service.handle(11,struct.pack('<HBI',s.selected_character_id,10,0),self.a)
            self.send(self.a,packets)
        return packets,note

    def fresh(self,*sessions,port_delta=0):
        listener=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);listener.bind(('127.0.0.1',0));listener.settimeout(1)
        self.addCleanup(listener.close);listener_port=listener.getsockname()[1]
        self.probe.ports=frozenset((*self.probe.ports,listener_port))
        for s in sessions:
            connection=self.probe._connections[id(s)]
            previous=connection.report or connection.latest_report
            endpoint=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);endpoint.bind(('127.0.0.1',0));endpoint.settimeout(1);self.addCleanup(endpoint.close)
            endpoint.sendto(b'\1',listener.getsockname());payload,source=listener.recvfrom(128);port=source[1]
            frame=struct.pack('<BIIHI',4,previous.local_ipv4_u32,previous.public_ipv4_u32,port,previous.mtu)+b'\0'*4
            self.assertTrue(self.probe.handle_datagram(payload,source,listener_port,listener.sendto))
            reply,_=endpoint.recvfrom(128);self.assertEqual(reply[:1],b'\2')
            result=self.f.route(s,2,frame);self.send(s,result.response_packets)

    def members(self):return {m.character_id for m in self.entry.domain.members}

    def test_first_request_accept_and_current_ordinary_publication(self):
        self.request(self.b);self.pump();self.accept(self.b);self.pump()
        self.assertEqual(self.members(),{101,102})
        self.assertTrue(self.f.directory.all_ready(self.entry))

    def test_pending_b_request_does_not_block_c_request_or_reverse_accept_order(self):
        self.request(self.b);self.request(self.c);self.pump()
        self.assertEqual(set(self.service.native_join._requests),{102,103})
        self.accept(self.c);self.pump();self.accept(self.b);self.pump()
        self.assertEqual(self.members(),{101,102,103})
        self.assertTrue(self.f.directory.all_ready(self.entry))

    def test_one_pending_request_expiry_does_not_drop_other_actor(self):
        self.request(self.b);self.f.f.clock[0]+=100;self.request(self.c)
        self.f.f.clock[0]+=21;self.pump()
        self.assertNotIn(102,self.service.native_join._requests)
        self.assertIn(103,self.service.native_join._requests)
        self.accept(self.c);self.pump();self.assertEqual(self.members(),{101,103})

    def test_one_hour_online_refresh_then_native_request_accept(self):
        self.f.f.clock[0]+=3600
        for key,value in tuple(self.probe._observations.items()):
            self.probe._observations[key]=replace(value,seen_at=value.seen_at-3600)
        self.fresh(self.a,self.b)
        self.request(self.b);self.pump();self.accept(self.b);self.pump()
        self.assertEqual(self.members(),{101,102});self.assertTrue(self.f.directory.all_ready(self.entry))


    def test_request_before_udp_report_recovers_without_second_request(self):
        old=self.probe._connections[id(self.b)].report
        self.probe._connections[id(self.b)].report=None
        del self.b._native_udp_address_claim
        self.request(self.b);self.pump();self.assertIn(102,self.service.native_join._requests)
        self.fresh(self.a,self.b);self.pump();self.accept(self.b);self.pump()
        self.assertEqual(self.members(),{101,102});self.assertTrue(self.f.directory.all_ready(self.entry))

    def test_new_observed_port_before_accept_is_the_only_published_endpoint(self):
        self.request(self.b);self.pump();self.fresh(self.a,self.b,port_delta=500);self.accept(self.b);self.pump()
        self.assertEqual(self.members(),{101,102})
        party=self.f.directory.current(self.a)
        facts=self.f.directory._facts(self.f.directory._actors(party))
        self.assertEqual(next(p.public_port for p in facts if p.user_id==102),self.probe._connections[id(self.b)].report.public_port)

    def test_selection_epoch_change_invalidates_old_notice_and_new_request_works(self):
        self.request(self.b);self.pump()
        with self.server.account_scope(self.b):
            self.service.native_join.forget_session(self.b)
            self.service.native_lobby.forget_session(self.b)
            self.b._raid_selected_epoch=object()
            body=build_userinfo_subtype0_min(self.server.list_by_account(self.b.account_id)[0])
            packet=self.server._codec_response(0,2,body)
            self.service.native_lobby.prepare_self(self.b,packet,body);self.send(self.b,[packet])
        with self.server.account_scope(self.a),self.assertRaises(Exception):
            self.service.handle(11,struct.pack('<HBI',102,10,0),self.a)
        self.fresh(self.a,self.b);self.request(self.b);self.pump();self.accept(self.b);self.pump()
        self.assertEqual(self.members(),{101,102});self.assertTrue(self.f.directory.all_ready(self.entry))

    def test_multiple_unaccepted_requests_and_duplicate_remain_independent(self):
        self.request(self.b);self.request(self.c);self.request(self.d);self.request(self.b);self.pump()
        self.assertEqual(set(self.service.native_join._requests),{102,103,104})
        for s in (self.d,self.b,self.c):self.accept(s);self.pump()
        self.assertEqual(self.members(),{101,102,103,104});self.assertTrue(self.f.directory.all_ready(self.entry))

    def test_different_character_same_account_rejects_old_notice_and_joins_fresh(self):
        self.request(self.b);self.pump()
        original=self.server.list_by_account
        record=original(2)[0];values=vars(record).copy();values['character_id']=202;values['name_bytes']=b'New202'
        self.server.list_by_account=lambda aid:[NS(**values)] if aid==2 else original(aid)
        with self.server.account_scope(self.b):
            self.service.native_join.forget_session(self.b);self.service.native_lobby.forget_session(self.b)
            self.b.selected_character_id=202;self.b._raid_selected_epoch=object()
            body=build_userinfo_subtype0_min(self.server.list_by_account(2)[0]);packet=self.server._codec_response(0,2,body)
            self.service.native_lobby.prepare_self(self.b,packet,body);self.send(self.b,[packet])
        with self.server.account_scope(self.a),self.assertRaises(Exception):
            self.service.handle(11,struct.pack('<HBI',102,10,0),self.a)
        self.fresh(self.a,self.b);self.request(self.b);self.pump();self.accept(self.b);self.pump()
        self.assertEqual(self.members(),{101,202});self.assertTrue(self.f.directory.all_ready(self.entry))

    def test_approval_before_udp_cannot_publish_peers_until_actual_report(self):
        self.request(self.b);self.pump()
        self.probe._connections[id(self.b)].report=None;del self.b._native_udp_address_claim
        self.accept(self.b);self.pump()
        self.assertFalse(self.f.directory.all_ready(self.entry))
        self.fresh(self.a,self.b);self.pump()
        self.assertEqual(self.members(),{101,102});self.assertTrue(self.f.directory.all_ready(self.entry))


class AntonFirstInviteTests(LukeFirstInviteTests):
    channel=23
