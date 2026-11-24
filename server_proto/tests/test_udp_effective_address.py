import importlib.util
import os
from pathlib import Path
import struct
from types import SimpleNamespace as NS
import unittest
import threading
from game.raid.native_network_protocol import parse_udp_address_report
from game.raid.native_udp import NativeUdpProbeService,native_ipv4_word

class EffectiveAddressTests(unittest.TestCase):
    def setUp(self):
        os.environ['USDOF_NATIVE_UDP_PROBES']='1'
        self.now=1000.
        self.p=NativeUdpProbeService(ports=(12311,12312),clock=lambda:self.now)
        self.s=NS(account_id=3,login_session_id='a'*32)
        self.c=self.p.open_connection(self.s,'203.0.113.9',3)
        self.server=NS(account_id=3,native_udp_probe_service=self.p,_codec_response=lambda *a:a)
        import game.raid.native_udp as udp_module
        root=Path(udp_module.__file__).resolve().parents[2]
        native=root/'opcodes/opcode0002_set_udp_ip_port.cp312-win_amd64.pyd'
        if native.is_file():
            from game.chain_loader import load_chain
            module=load_chain('opcode0002-set_udp_ip_port.py',area='opcodes')
            self.assertEqual(Path(module.__file__).resolve(),native)
        else:
            path=root/'opcodes/opcode0002-set_udp_ip_port.py'
            spec=importlib.util.spec_from_file_location('udp_address_handler_candidate',path)
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.handle=module.handle_udp_address_report
    def frame(self,ip='203.0.113.9',port=40111,kind=4):
        return struct.pack('<BIIHI',kind,native_ipv4_word('192.168.1.2'),native_ipv4_word(ip),port,1472)+b'\0'*4
    def report(self,**kw):
        frame=self.frame(**kw);packets,reason=self.handle(self.server,frame,self.s)
        self.assertEqual(packets,[(1,2,b'\1')]);return self.s._native_udp_latest_address_claim.report
    def recv(self,port=40111,payload=b'\1',listener=12311):
        return self.p.handle_datagram(payload,('203.0.113.9',port),listener,lambda b,e:len(b))
    def ready(self):
        self.assertTrue(self.recv());self.report();match=self.p.current_match(self.s)
        self.assertIsNotNone(match);return match
    def test_failure_does_not_replace_effective_report_or_session_claim(self):
        good=self.ready();self.now+=2
        bad=self.report(ip='192.168.1.2',port=36528,kind=5)
        self.assertIs(self.c.latest_report,bad)
        self.assertIs(self.c.report,good.report)
        self.assertIs(self.s._native_udp_address_claim.report,good.report)
        self.assertEqual(self.p.current_match(self.s).seen_at,1000.)
    def test_failure_report_and_ack_do_not_extend_the_old_lease(self):
        self.ready();self.now+=29;self.report(ip='192.168.1.2',kind=5)
        self.assertIsNotNone(self.p.current_match(self.s));self.now+=1
        self.assertIsNone(self.p.current_match(self.s))
        self.report(ip='192.168.1.2',kind=5)
        self.assertIsNone(self.p.current_match(self.s))
    def test_a_real_heartbeat_keeps_only_the_existing_endpoint_alive(self):
        self.ready();self.now+=20;self.report(ip='192.168.1.2',kind=5)
        self.assertTrue(self.recv(payload=b'\0',listener=12312))
        self.assertEqual(self.p.current_match(self.s).seen_at,self.now)
    def test_expired_failed_refresh_cannot_resurrect_old_ownership_by_claim(self):
        self.ready();self.now+=31;self.report(ip='192.168.1.2',kind=5)
        self.assertFalse(self.recv(payload=b'\0',listener=12312))
        self.assertIsNone(self.p.current_match(self.s))
    def test_a_pending_endpoint_does_not_replace_good_until_observed(self):
        good=self.ready();self.now+=1;candidate=self.report(port=40112)
        self.assertIs(self.p.current_match(self.s).report,good.report)
        self.assertTrue(self.recv(port=40112))
        match=self.p.current_match(self.s)
        self.assertEqual(match.port,40112)
        self.assertIs(match.report,candidate)
        self.assertIs(self.s._native_udp_address_claim.report,candidate)
    def test_report_before_first_probe_is_retained_but_not_published(self):
        report=self.report();self.assertIsNone(self.c.report)
        self.assertIsNone(self.p.current_match(self.s))
        self.assertFalse(self.recv(payload=b'\0',listener=12312))
        self.assertTrue(self.recv());self.assertIs(self.c.report,report)
    def test_first_failure_never_creates_effective_address(self):
        self.recv();self.report(ip='192.168.1.2',kind=5)
        self.assertIsNone(self.c.report);self.assertIsNone(self.p.current_match(self.s))
    def test_kind5_with_a_real_matching_observation_is_not_blanket_rejected(self):
        self.recv();report=self.report(kind=5)
        self.assertIs(self.p.current_match(self.s).report,report)
    def test_success_after_failure_promotes_with_same_claim_identity(self):
        self.ready();self.report(ip='192.168.1.2',kind=5)
        self.now+=1;self.recv();report=self.report()
        self.assertIs(self.p.current_match(self.s).report,report)
        self.assertIs(self.s._native_udp_address_claim.report,report)
    def test_previous_tcp_does_not_share_a_failed_claim_with_reconnect(self):
        self.ready();self.p.close_connection(self.c)
        self.s=NS(account_id=3,login_session_id='a'*32)
        self.c=self.p.open_connection(self.s,'203.0.113.9',3)
        self.report(ip='192.168.1.2',kind=5)
        self.assertIsNone(self.p.current_match(self.s))
    def test_overlap_reconnect_inherits_only_after_its_own_valid_report_and_old_close(self):
        self.ready();old=self.c
        self.s=NS(account_id=3,login_session_id='a'*32)
        self.c=self.p.open_connection(self.s,'203.0.113.9',3)
        report=self.report();self.assertIsNone(self.p.current_match(self.s))
        self.p.close_connection(old)
        self.assertIs(self.p.current_match(self.s).report,report)
    def test_another_account_cannot_inherit_the_old_mapping(self):
        self.ready();self.p.close_connection(self.c)
        self.s=NS(account_id=4,login_session_id='b'*32)
        self.c=self.p.open_connection(self.s,'203.0.113.9',4);self.server.account_id=4
        self.report();self.assertIsNone(self.p.current_match(self.s))
    def test_rejected_owner_and_encoder_failure_leave_both_states_unchanged(self):
        good=self.ready();latest=self.c.latest_report
        self.s.account_id=4
        self.assertEqual(self.handle(self.server,self.frame(),self.s)[0],[])
        self.assertIs(self.c.latest_report,latest)
        self.s.account_id=3
        self.server._codec_response=lambda *a:(_ for _ in ()).throw(ValueError('fixture'))
        self.assertEqual(self.handle(self.server,self.frame(ip='192.168.1.2',kind=5),self.s)[0],[])
        self.assertIs(self.c.report,good.report)
    def test_claim_and_match_snapshot_cannot_mix_concurrent_report_generations(self):
        self.ready();errors=[]
        def update():
            try:
                for _ in range(2000):self.p.record_claim(self.s,parse_udp_address_report(self.frame()))
            except Exception as exc:errors.append(exc)
        worker=threading.Thread(target=update);worker.start()
        for _ in range(2000):
            match,claim=self.p.current_claim_and_match(self.s)
            self.assertIsNotNone(match);self.assertIs(match.report,claim.report)
        worker.join();self.assertFalse(errors)

if __name__=='__main__':unittest.main()
