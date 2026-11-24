import contextlib,io,json,os,socket,struct,sys,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace as NS,MethodType
from unittest.mock import Mock,patch
from game import game_server as gs
W=Path(os.environ.get("USDOF_CHARSELECT_TEST_OUTPUT", r"E:\codex\charselect-recovery-20261115"))
MODE="source"

def packet(flag,opcode,body=b''):
    return struct.pack('<BHI I H',flag,opcode,13+len(body),0,0)+body

ROSTER=packet(0,2,b'roles-present')
ADVENTURE=packet(0,0x545,b'adventure-present')

def session(aid=17):
    return NS(account_id=aid,login_session_id='login-'+str(aid),selected_channel_id=10,
              selected_character_id=None,enter_seria_in_progress=False)

class Gate(unittest.TestCase):
    def ready(self):
        s=session();g=gs._ColdCharselectRecovery(s);g.open_sent(10)
        g.observe(1,8,b'\xff\xff\x02');return s,g,[(ROSTER,'roster'),(ADVENTURE,'adventure')]

    def test_missing_piv_releases_existing_packet_objects_once_after_grace(self):
        s,g,p=self.ready();self.assertEqual(g.take_due(s,p,10.999),[])
        result=g.take_due(s,p,11);self.assertIs(result[0][0],ROSTER)
        self.assertIs(result[1][0],ADVENTURE);self.assertEqual(p,[])
        self.assertEqual(g.take_due(s,[(ROSTER,'new')],12),[])

    def test_normal_piv_empty_queue_does_not_recover(self):
        s,g,p=self.ready();p.clear();self.assertEqual(g.take_due(s,p,12),[])
        self.assertFalse(g.released)

    def test_no_request_or_no_successful_open_never_recovers(self):
        for opened,requested in ((False,False),(True,False),(False,True)):
            with self.subTest(opened=opened,requested=requested):
                s=session();g=gs._ColdCharselectRecovery(s)
                if opened:g.open_sent(10)
                if requested:g.observe(1,8,b'\xff\xff\x02')
                self.assertEqual(g.take_due(s,[(ROSTER,'')],100),[])

    def test_native_padded_roster_request_is_accepted(self):
        for size in (3,8,16):
            with self.subTest(size=size):
                s=session();g=gs._ColdCharselectRecovery(s);g.open_sent(10)
                g.observe(1,8,b'\xff\xff\x02'+bytes(size-3))
                self.assertTrue(g.take_due(s,[(ROSTER,'')],11))

    def test_malformed_inspection_wrong_flag_and_wrong_opcode_are_not_authorization(self):
        for flag,opcode,body in ((1,8,b''),(1,8,b'\x01\x00\x03'),(1,8,b'\xff\xff\x02x'),(1,8,b'\xff\xff\x02'+bytes(4)+b'x'),(0,8,b'\xff\xff\x02'),(1,9,b'\xff\xff\x02')):
            with self.subTest(flag=flag,opcode=opcode,body=body):
                s=session();g=gs._ColdCharselectRecovery(s);g.open_sent(10);g.observe(flag,opcode,body)
                self.assertEqual(g.take_due(s,[(ROSTER,'')],11),[])

    def test_other_connection_and_changed_account_login_or_channel_never_receive_old_roster(self):
        s,g,p=self.ready();self.assertEqual(g.take_due(session(),p,12),[])
        for key,value in (('account_id',3),('login_session_id','new-login'),('selected_channel_id',11)):
            with self.subTest(key=key):
                s,g,p=self.ready();setattr(s,key,value)
                self.assertEqual(g.take_due(s,p,12),[]);self.assertEqual(len(p),2)

    def test_selection_or_entry_cancels_recovery(self):
        for key,value in (('selected_character_id',32),('enter_seria_in_progress',True)):
            s,g,p=self.ready();setattr(s,key,value);self.assertEqual(g.take_due(s,p,12),[])

    def test_user_actions_end_startup_recovery(self):
        for opcode in (3,4,5,6,7,0x127,0x1fe,0x57f):
            with self.subTest(opcode=opcode):
                s,g,p=self.ready();g.observe(1,opcode,b'');self.assertEqual(g.take_due(s,p,12),[])

    def test_progress_and_wrong_piv_leave_waiting_until_real_release(self):
        s,g,p=self.ready()
        for opcode in (0x13e,0xab,0x1dd):g.observe(1,opcode,b'\x01\x01\x00')
        self.assertTrue(g.take_due(s,p,11))

    def test_only_roster_first_queue_is_eligible(self):
        s,g,p=self.ready();self.assertEqual(g.take_due(s,[(ADVENTURE,'')],11),[])

    def test_open_timestamp_is_not_postponed_by_repeated_open_notes(self):
        s,g,p=self.ready();g.open_sent(10.9);self.assertTrue(g.take_due(s,p,11))

class Sender(unittest.TestCase):
    def game(self,aid=17):return NS(account_id=aid,log=Mock())
    def send(self,g,s,c,mode='delay'):
        return gs.GameServer._send_cold_charselect_recovery(g,c,s,[(ROSTER,'prepared'),(ADVENTURE,'prepared')],connection_id='own-connection',step_mode=mode)

    def test_delayed_followup_is_a_separate_write_without_repository_access(self):
        client=Mock();s=session();g=self.game()
        with patch.object(gs,'send_raid_packet',side_effect=lambda _g,_s,c,p:c.sendall(p)),patch.object(gs.time,'sleep') as sleep:
            remaining,key=self.send(g,s,client)
        self.assertEqual(remaining,[]);self.assertIsNone(key)
        self.assertEqual([c.args[0] for c in client.sendall.call_args_list],[ROSTER,ADVENTURE])
        self.assertGreaterEqual(sleep.call_args.args[0],.05)
        self.assertIsNone(s.selected_character_id)

    def test_explicit_piv_mode_keeps_followups_on_the_existing_queue(self):
        client=Mock()
        with patch.object(gs,'send_raid_packet',side_effect=lambda _g,_s,c,p:c.sendall(p)),patch.object(gs.time,'sleep') as sleep:
            remaining,key=self.send(self.game(),session(),client,'piv')
        self.assertEqual(remaining,[(ADVENTURE,'prepared')]);self.assertEqual(key,(0,2))
        client.sendall.assert_called_once_with(ROSTER);sleep.assert_not_called()

    def test_partial_or_failed_send_is_not_retried(self):
        client=Mock();client.sendall.side_effect=OSError('partially sent')
        with patch.object(gs,'send_raid_packet',side_effect=lambda _g,_s,c,p:c.sendall(p)):
            with self.assertRaises(OSError):self.send(self.game(),session(),client)
        client.sendall.assert_called_once_with(ROSTER)

    def test_wrong_account_or_selected_character_is_rejected_before_any_send(self):
        for aid,cid in ((3,None),(17,32)):
            s=session();s.selected_character_id=cid;client=Mock()
            with self.assertRaises(RuntimeError):self.send(self.game(aid),s,client)
            client.sendall.assert_not_called()

    def test_blocked_socket_does_not_hold_a_global_lock_for_another_connection(self):
        entered=threading.Event();release=threading.Event();done=threading.Event();errors=[]
        def send(g,s,c,p):
            if c=='blocked':entered.set();self.assertTrue(release.wait(3))
        def first():
            try:self.send(self.game(),session(),'blocked','piv')
            except BaseException as e:errors.append(e)
        with patch.object(gs,'send_raid_packet',side_effect=send):
            th=threading.Thread(target=first);th.start();self.assertTrue(entered.wait(2))
            self.send(self.game(3),session(3),'other','piv');done.set();release.set();th.join(3)
        self.assertTrue(done.is_set());self.assertFalse(errors)

class WireLoop(unittest.TestCase):
    def run_flow(self,actions,*,wait=1.45):
        server,client=socket.socketpair();client.settimeout(.1)
        sent=[];logs=[];errors=[];received=bytearray();s=gs.GameSessionState(account_id=17,account_m_id='986521736',login_session_id='test-login')
        repo=NS(game_connection_scope=contextlib.nullcontext)
        metrics=NS(connection=lambda _:contextlib.nullcontext(),begin_request=lambda _:None,finish_request=lambda:None)
        g=NS(account_id=17,account_m_id='986521736',launcher_account_id=3012345,port=10062,
             log=logs.append,_resolve_channel_id_for_port=lambda _:10,
             register_control_game_client=lambda *a,**k:NS(),force_exp_notify=NS(register_client=lambda *a:None),
             _repo=lambda:repo,_cleanup_death_tower_items_on_game_close=lambda *a,**k:contextlib.nullcontext(),
             hotpath_metrics=metrics,initial_probes=[],dynamic_userinfo=True,diagnostic_trace=False,protocol_traffic_log=False,
             disjoint_item_001a=NS(pending_batch_deadline=lambda _:None,has_pending_batch=lambda _:False),
             _drain_control_commands=lambda _:None,_drain_tournament_reward_manual_refresh=lambda _:None,_drain_altar_due_packets=lambda _:None,
             _log_game_sequence=lambda **k:None,_log_plain_wire_packet=lambda *a:None,
             _channel_leave_reentry_lock=threading.RLock(),_channel_leave_reentry_armed=False,_channel_leave_reentry_armed_at=0,
             _build_reentry_open_before_userinfo_queue=lambda **k:([(packet(1,1),'login'),(packet(1,4),'open'),(ROSTER,'roster'),(ADVENTURE,'adventure')],True,True,False,True),
             piv_response_probes=[],ack_response_probes=[],pop_delayed_response_packets=lambda:[],
             _codec_response=lambda f,o,b:packet(f,o,b))
        if hasattr(gs.GameServer,'_send_cold_charselect_recovery'):
            g._send_cold_charselect_recovery=MethodType(gs.GameServer._send_cold_charselect_recovery,g)
        def send(_g,_s,c,p):sent.append((time.monotonic(),p));c.sendall(p)
        def route(_g,**kw):
            return NS(handled=kw['opcode']==8,response_packets=[ROSTER,ADVENTURE] if kw['opcode']==8 else [],create_character_userinfo_refresh_pending=False,note='fixture request')
        def handle(opcode,body,**kw):return NS(response_packets=[packet(0,1)] if opcode==0 else [],note='fixture decision')
        stack=contextlib.ExitStack()
        stack.enter_context(patch.object(gs,'native_game_connection',lambda *a,**k:contextlib.nullcontext()))
        stack.enter_context(patch.object(gs,'native_udp_connection',lambda *a,**k:contextlib.nullcontext()))
        stack.enter_context(patch.object(gs,'pump_local_raid',lambda *a:None))
        stack.enter_context(patch.object(gs,'send_raid_packet',send))
        stack.enter_context(patch.object(gs,'capture_login_send',lambda *a:None))
        stack.enter_context(patch.object(gs,'confirm_login_send',lambda *a:False))
        stack.enter_context(patch.object(gs,'route_dynamic_game_packet',route))
        stack.enter_context(patch.object(gs,'handle_decoded_packet',handle))
        stack.enter_context(patch.object(gs.raid_lobby,'note_send',lambda *a:None))
        stack.enter_context(patch.dict(os.environ,USDOF_GAME_REENTRY_USERINFO_REQUIRE_ARM='0',USDOF_GAME_REENTRY_OPEN_BEFORE_USERINFO='1',USDOF_GAME_COLD_USERINFO_GATE_ON_0004_PIV='1',USDOF_GAME_COLD_SUPPRESS_DUP_0008_AFTER_USERINFO='1',USDOF_GAME_CLIENT_TIMEOUT='0',USDOF_GAME_EXIT_CLOSE_DELAY_MS='0',USDOF_GAME_FOLLOWUP_QUEUE_STEP_MODE='delay'))
        for name in ('game.native_chat','game.town_social','game.normal_party','game.character.daily_item_expiry','game.quest.daily_rollover'):
            import importlib
            m=importlib.import_module(name);stack.enter_context(patch.object(m,'pump',lambda *a:None))
        def run():
            try:gs.GameServer._handle_client_connection(g,server,('127.0.0.1',41000),10062,identity=NS(),session_state=s,prefix='test-owned-connection')
            except BaseException as ex:errors.append(ex)
        try:
            th=threading.Thread(target=run);th.start();start=time.monotonic()
            client.sendall(packet(1,0))
            for delay,body in actions:
                while time.monotonic()-start<delay:time.sleep(.005)
                client.sendall(body)
            while time.monotonic()-start<wait and not errors:
                try:received.extend(client.recv(65536))
                except socket.timeout:pass
            client.close();th.join(3)
            if errors:raise errors[0]
            self.assertFalse(th.is_alive())
            frames=[];offset=0
            while offset+13<=len(received):
                flag,opcode,total,_,_=struct.unpack_from('<BHI I H',received,offset)
                self.assertGreaterEqual(total,13);self.assertLessEqual(offset+total,len(received))
                frames.append((flag,opcode));offset+=total
            self.assertEqual(offset,len(received));self.assertEqual(frames[:3],[(0,1),(1,1),(1,4)])
            (W/f'reports/wire-{MODE}-{self._testMethodName}.json').write_text(json.dumps({'frames':frames,'logs':logs,'module':gs.__file__,'mocked_components':'repository routing, non-character pumps, prepared fixture packet bodies; real connection receive/poll/send loop and socket framing'},ensure_ascii=False,indent=2),'utf-8')
            return sent,logs
        finally:
            client.close();server.close();stack.close()

    def test_missing_open_piv_with_repeated_requests_recovers_once_during_receive_poll(self):
        sent,logs=self.run_flow([(.04,packet(1,8,b'\xff\xff\x02')),(.08,packet(1,8,b'\xff\xff\x02'))])
        values=[p for _,p in sent]
        self.assertEqual(values.count(ROSTER),1);self.assertEqual(values.count(ADVENTURE),1)
        self.assertTrue(any('recovered requested roster' in x for x in logs))

    def test_real_piv_before_timeout_keeps_original_path(self):
        sent,logs=self.run_flow([(.03,packet(1,8,b'\xff\xff\x02')),(.07,packet(1,0x13e,b'\x01\x04\x00\xaa\xbb\xcc\xdd')),(.12,packet(1,0x13e,b'\x00\x02\x00\xaa\xbb\xcc\xdd'))],wait=.4)
        self.assertEqual([p for _,p in sent].count(ROSTER),1)
        self.assertFalse(any('recovered requested roster' in x for x in logs))

    def test_late_real_piv_after_recovery_does_not_duplicate_roster(self):
        sent,logs=self.run_flow([(.03,packet(1,8,b'\xff\xff\x02')),(1.25,packet(1,0x13e,b'\x01\x04\x00\xaa\xbb\xcc\xdd'))])
        self.assertEqual([p for _,p in sent].count(ROSTER),1)
        self.assertEqual([p for _,p in sent].count(ADVENTURE),1)

    def test_exit_before_timeout_does_not_receive_recovery(self):
        sent,logs=self.run_flow([(.03,packet(1,8,b'\xff\xff\x02')),(.1,packet(1,3))],wait=.4)
        self.assertNotIn(ROSTER,[p for _,p in sent])

if __name__=='__main__':unittest.main()
