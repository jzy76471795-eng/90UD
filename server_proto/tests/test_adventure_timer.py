import struct,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from game.character import adventure_group_features as feature

NOW=1793754000
DAY=86400

def state(start=NOW,duration=7*DAY,status=1,area=0,points=120):
    return dict(dispatch_state=status,area_index=area,committed_points=points,start_unix=start,finish_unix=start+duration)

class AdventureTimerTests(unittest.TestCase):
    def wire(self,data,now=NOW):
        return struct.unpack('<BBIII',feature.build_mercenary_state_body_0540(data,now_unix=now))

    def test_start_uses_same_unix_base_as_installed_client_clock(self):
        with patch.object(feature,'client_tick_seconds',return_value=123):
            self.assertEqual(self.wire(state()),(1,0,120,NOW,NOW+7*DAY))

    def test_default_path_never_reads_server_uptime(self):
        with patch.object(feature,'time') as clock,patch.object(feature,'client_tick_seconds',side_effect=AssertionError('server uptime is not the client clock')):
            clock.time.return_value=NOW+60
            wire=struct.unpack('<BBIII',feature.build_mercenary_state_body_0540(state()))
            self.assertEqual(wire[3:],(NOW,NOW+7*DAY))
            clock.time.assert_called_once_with()

    def test_reopen_and_restart_continue_from_persisted_finish(self):
        for elapsed in (1,60,DAY,3*DAY,7*DAY-1):
            with self.subTest(elapsed=elapsed),patch.object(feature,'client_tick_seconds',return_value=99):
                wire=self.wire(state(),NOW+elapsed)
                self.assertEqual(wire[4]-(NOW+elapsed),7*DAY-elapsed)
                self.assertEqual((NOW+elapsed)-wire[3],elapsed)
                self.assertEqual(wire[4]-wire[3],7*DAY)

    def test_each_area_preserves_configured_duration(self):
        from pvf.adventure_group_feature_index import get_adventure_group_feature_index
        for area in get_adventure_group_feature_index().dispatch_areas:
            with self.subTest(area=area.area_index):
                wire=self.wire(state(duration=area.duration_seconds,area=area.area_index))
                self.assertEqual(wire[4]-NOW,area.duration_seconds)
                self.assertEqual(wire[0],1)

    def test_completed_and_returned_states_stay_complete_or_empty(self):
        self.assertEqual(self.wire(state(status=2),NOW+8*DAY),(2,0,120,NOW+DAY,NOW+8*DAY))
        self.assertEqual(self.wire(None), (0,0,0,0,0))

    def test_reopening_does_not_change_stored_state(self):
        data=state();saved=dict(data)
        self.wire(data,NOW+DAY)
        self.assertEqual(data,saved)

    def test_explicit_legacy_clock_projection_remains_available(self):
        values=feature.project_dispatch_times_to_client_clock(NOW,NOW+7*DAY,now_unix=NOW+DAY,tick_seconds=500)
        self.assertEqual((500-values[0])&0xffffffff,DAY)
        self.assertEqual((values[1]-500)&0xffffffff,6*DAY)

class AdventureTimerFlowTests(unittest.TestCase):
    def setUp(self):
        from game.character.character_db import CharacterRepository
        from game.chain_loader import load_opcode
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Path(self.tmp.name)/'inventory.db'
        self.repo=CharacterRepository(self.db,bootstrap_from_reference=False)
        self.repo.ensure_account(1)
        self.repo.create_character(account_id=1,character_id=101,name='Timer',job=0,grow_type=1,level=90)
        self.server=NS(account_id=1,_repo=lambda:self.repo,_codec_response=lambda f,o,b:(f,o,b),log=lambda _:None)
        self.session=NS(account_id=1,selected_character_id=101)
        self.start=load_opcode('opcode01bb-start_adventure_dispatch.py')
        self.returned=load_opcode('opcode057c-return_adventure_dispatch.py')
        self.reward=load_opcode('opcode057d-claim_adventure_dispatch_reward.py')
        self.page=load_opcode('opcode0578-contents_play_info.py')

    def start_dispatch(self):
        with patch('time.time',return_value=NOW),patch.object(self.start,'load_adventure_group_overview',return_value=NS(progression=NS(adventure_level=2))),patch.object(self.start,'load_mercenary_character_points',return_value=(NS(level=90,total_points=120),)):
            packets,note=self.start.handle_start_adventure_dispatch_01bb(self.server,b'\0',session=self.session)
        self.assertIn('success',note)
        self.assertEqual(struct.unpack('<BBIII',packets[1][2]),(1,0,120,NOW,NOW+7*DAY))

    def test_start_then_page_reopen_does_not_finish_or_grant_early(self):
        from game.character.character_db import CharacterRepository
        self.start_dispatch()
        self.repo=CharacterRepository(self.db,bootstrap_from_reference=False)
        service=self.page.ContentsPlayInfo0578Service(self.server)
        with patch('time.time',return_value=NOW+DAY),patch.object(service,'build_full_state_refresh_0578',return_value=([],'reopen',{})):
            own_profile = struct.pack('<H', self.session.selected_character_id)
            packets,_=service.handle_contents_play_info_0578(own_profile,session=self.session)
            self.assertEqual(struct.unpack('<BBIII',packets[-1][2]),(1,0,120,NOW,NOW+7*DAY))
            before=self.repo.load_adventure_dispatch_state(1)
            items=self.repo.list_inventory_items(101,list_type=0)
            _,note=self.reward.handle_claim_adventure_dispatch_reward_057d(self.server,struct.pack('<I',101),session=self.session)
            self.assertIn('dispatch_not_complete',note)
            self.assertEqual(self.repo.load_adventure_dispatch_state(1),before)
            self.assertEqual(self.repo.list_inventory_items(101,list_type=0),items)

    def test_early_return_clears_timer_without_rewards(self):
        self.start_dispatch()
        with patch('time.time',return_value=NOW+60):
            items=self.repo.list_inventory_items(101,list_type=0)
            packets,note=self.returned.handle_return_adventure_dispatch_057c(self.server,b'',session=self.session)
            self.assertIn('dispatch_returned_early',note)
            self.assertEqual(struct.unpack('<BBIII',packets[1][2]),(0,0,0,0,0))
            self.assertEqual(self.repo.list_inventory_items(101,list_type=0),items)

    def test_expired_dispatch_claims_correct_rewards_once(self):
        self.start_dispatch()
        with patch('time.time',return_value=NOW+7*DAY+1):
            packets,note=self.reward.handle_claim_adventure_dispatch_reward_057d(self.server,struct.pack('<I',101),session=self.session)
            self.assertIn('success',note)
            self.assertEqual(packets[0][2],self.reward.build_claim_adventure_dispatch_success_057d(101,((3330,55),(3339,109))))
            self.assertEqual(struct.unpack('<BBIII',packets[1][2]),(0,0,0,0,0))
            items=self.repo.list_inventory_items(101,list_type=0)
            _,note=self.reward.handle_claim_adventure_dispatch_reward_057d(self.server,struct.pack('<I',101),session=self.session)
            self.assertIn('dispatch_not_complete',note)
            self.assertEqual(self.repo.list_inventory_items(101,list_type=0),items)
