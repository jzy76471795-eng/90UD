"""Real loopback TCP commands and UDP probes; native-machine-generated C0002.
Controlled login/profile producers; this does not drive a DFO GUI or NAT WAN.
"""
import socket,struct
from pathlib import Path
import sys
from test_social import Fixture
from game.game_server import GameServer
from game.raid.native_udp import NativeUdpProbeService
from game.chain_loader import load_chain
from test_raid_native_solo_start import decoded
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from native_writer_check import run as native_report

class WireSocket:
    def __init__(self,server,peer):self.server,self.peer,self.sent=server,peer,[]
    def getpeername(self):return self.server.getpeername()
    def fileno(self):return self.server.fileno()
    def sendall(self,p):
        self.server.sendall(p);left=len(p);received=bytearray()
        while left:
            b=self.peer.recv(left);assert b;left-=len(b);received.extend(b)
        assert bytes(received)==p;self.sent.append(p)

class OrdinaryWireTests(Fixture):
    def setUp(self):
        super().setUp();self.party.clock=lambda:self.now;self.server._codec_response=GameServer._codec_response
        self.transports={};self.endpoints={};directory=self.server.native_game_connections
        for i,s in enumerate(self.ss,1):
            directory.close_connection(directory._connections[id(s)])
            listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1)
            peer=socket.create_connection(listener.getsockname());server,_=listener.accept();listener.close()
            peer.settimeout(2);server.settimeout(2);self.addCleanup(peer.close);self.addCleanup(server.close)
            client=WireSocket(server,peer);self.transports[i]=client
            self.server.account_id=i;c=directory.open_connection(s,client,identity=None)
            body=directory.prepare_login_body(s,b'\1'+bytes(151),1);p=b'L'+bytes([i]);directory.track_login_packet(s,p,body,1)
            self.assertTrue(directory.confirm_send(directory.capture_send(s,client,p)))
            if hasattr(s,'_native_udp_address_claim'):del s._native_udp_address_claim
            self.udp_endpoint(i)
        self.listener=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);self.listener.bind(('127.0.0.1',0));self.listener.settimeout(2);self.addCleanup(self.listener.close)
        self.probe=NativeUdpProbeService(ports=(self.listener.getsockname()[1],),clock=lambda:self.now)
        self.server.native_udp_probe_service=self.probe
        for i,s in enumerate(self.ss,1):self.probe.open_connection(s,'127.0.0.1',i)
        for _ in range(3):
            for i in range(1,7):self.pump(i)

    def udp_endpoint(self,i):
        p=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);p.bind(('127.0.0.1',0));p.settimeout(2)
        self.addCleanup(p.close);self.endpoints[i]=p;return p

    def issue(self,i,opcode,body):
        client=self.transports[i];packet=b'\1'+struct.pack('<HI',opcode,13+len(body))+bytes(6)+body
        client.peer.sendall(packet);buf=bytearray()
        while len(buf)<len(packet):buf.extend(client.server.recv(len(packet)-len(buf)))
        self.assertEqual(bytes(buf),packet);self.server.account_id=i
        if opcode==2:packets,note=load_chain('opcode0002-set_udp_ip_port.py',area='opcodes').handle_udp_address_report(self.server,bytes(buf[13:]),self.ss[i-1])
        else:packets,note=self.party.handle(opcode,bytes(buf[13:]),self.ss[i-1])
        for p in packets:client.sendall(p)
        return packets,note

    def fresh(self,*ids):
        for i in ids:
            endpoint=self.endpoints[i];endpoint.sendto(b'\1',self.listener.getsockname())
            payload,source=self.listener.recvfrom(128)
            self.assertTrue(self.probe.handle_datagram(payload,source,self.listener.getsockname()[1],self.listener.sendto))
            reply,_=endpoint.recvfrom(128);self.assertEqual(reply[:1],b'\2')
            native=native_report(0,endpoint=(b'\x7f\0\0\1',source[1]))
            self.assertFalse(any('guard' in e for e in native['events']))
            body=bytes.fromhex(native['frames'][0]['body']);self.assertEqual(len(body),31)
            packets,_=self.issue(i,2,body);self.assertEqual(len(packets),1)

    def invite_wire(self,target):return self.issue(1,10,struct.pack('<HBIHI',target,0,0,0,0))
    def accept_wire(self,who,leader=1):return self.issue(who,11,struct.pack('<HBI',leader,0,0))

    def test_cold_first_native_report_invite_accept_over_tcp(self):
        self.fresh(1,2);self.invite_wire(2);self.pump(2);self.accept_wire(2)
        self.assertEqual(len(self.party.current(self.ss[0]).members),2)

    def test_invite_before_udp_then_another_target_and_reverse_acceptance(self):
        self.invite_wire(2);self.invite_wire(3);self.assertEqual(len(self.party.udp.pending),2)
        self.fresh(1,3);self.now+=.3;self.pump(1);self.pump(3);self.accept_wire(3)
        self.fresh(2);self.now+=.3;self.pump(1);self.pump(2);self.accept_wire(2)
        self.assertEqual({s[4] for s in self.party.current(self.ss[0]).members.values()},{1,2,3})

    def test_changed_role_after_old_pending_uses_new_actor(self):
        self.invite_wire(2);s=self.ss[0];self.party.forget(s);s.selected_character_id=101;s.chat_selection_generation+=1
        self.records[1].append(type(self.records[1][0])(character_id=101,account_id=1,name_bytes=b'New1'))
        for _ in range(3):
            for i in (1,2):self.pump(i)
        self.fresh(1,2);self.invite_wire(2);self.pump(2);self.accept_wire(2,leader=101)
        self.assertEqual({s[4] for s in self.party.current(self.ss[0]).members.values()},{101,2})

    def test_port_changes_before_accept_publish_current_actual_port(self):
        self.fresh(1,2);self.invite_wire(2);self.pump(2);new=self.udp_endpoint(2)
        self.fresh(2);self.accept_wire(2);self.pump(1)
        from game.raid.native_network_protocol import parse_peer_addresses
        peers=[decoded(p) for p in self.transports[1].sent]
        peers=parse_peer_addresses([p[2] for p in peers if p[:2]==(0,11)][-1])
        self.assertEqual(next(p.public_port for p in peers if p.user_id==2),new.getsockname()[1])

    def test_long_idle_expired_mapping_keeps_invite_until_real_udp_returns(self):
        self.fresh(1,2);self.now+=3600;self.invite_wire(2)
        self.assertTrue(self.party.udp.pending);self.assertFalse(self.party._invites)
        self.fresh(1,2);self.now+=.3;self.pump(1);self.pump(2);self.accept_wire(2)
        self.assertEqual(len(self.party.current(self.ss[0]).members),2)
