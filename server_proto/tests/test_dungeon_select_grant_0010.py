#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dungeon SELECT_DUNGEON 0x0010 grant tests (USDOF 副本实现A.md Phase B)."""
from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from game.character.character_db import CharacterRepository
from game.chain_loader import load_chain
from game.game_codec import codec0_seed0_decode, codec1_seed0_decode
from game.quest import quest_index as quest_index_mod
from game.quest.quest_index import (
    EnemyRewardEntry,
    MonsterRewardEntry,
    QuestMeta,
    SpecialPassiveObjectItem,
    get_quest_index,
)
from game.quest.quest_residual import pack_trigger_channels

party_info = load_chain("helper-party_info.py")
build_party_info_type0_solo = party_info.build_party_info_type0_solo
parse_party_info_type0_header = party_info.parse_party_info_type0_header
from game.game_server import GameServer
from opcodes.index import route_dynamic_game_packet

mod = load_chain("flow-dungeon_enter.py")
parse_0010_plain = mod.parse_0010_plain
GameSessionState = load_chain("flow-enter_game.py").GameSessionState
start_room = load_chain("helper-dungeon_start_room.py")
map_mons = load_chain("helper-dungeon_map_monsters.py")
circle_info = load_chain("helper-circle_dungeon_info.py")
move_map = load_chain("opcode002d-move_map.py")


class DungeonSelectGrant0010Tests(unittest.TestCase):
    def test_0010_registers_passive_rows_after_reset_and_actor_preload(self) -> None:
        """DGN rows must survive the new-run drop-state reset and reserve fresh keys."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "inventory.db"
            repo = CharacterRepository(db_path, bootstrap_from_reference=False)
            repo.ensure_account(1)
            repo.create_character(
                account_id=1,
                character_id=0xE2B3,
                name="AAAA",
                job=12,
                grow_type=0,
                level=1,
            )
            real_index = get_quest_index()
            real_dungeon = real_index.get_dungeon(144)
            self.assertIsNotNone(real_dungeon)
            dgn_with_passive_item = replace(
                real_dungeon,
                special_passive_object_items=[
                    SpecialPassiveObjectItem(index=0, item_id=3341, drop_rate=10000)
                ],
                mazes=[replace(m, special_passive_object_items=None) for m in real_dungeon.mazes],
            )

            class _QuestIndexProxy:
                def get_dungeon(self, dungeon_id):
                    if int(dungeon_id) == 144:
                        return dgn_with_passive_item
                    return real_index.get_dungeon(dungeon_id)

                def __getattr__(self, name):
                    return getattr(real_index, name)

            events = []
            from game.drop import drop_service

            original_generate = drop_service.generate_passive_object_item_entries

            def preload(server_arg, session_arg, *, source):
                self.assertEqual(source, "dungeon_enter_001d")
                self.assertTrue(session_arg.in_dungeon_run)
                self.assertEqual(session_arg.dungeon_drops, {})
                session_arg.dungeon_drop_scene_slot = 5
                events.append("preload")
                return "drop_preload source=dungeon_enter_001d generated=0"

            def generate(session_arg, items, **kwargs):
                self.assertEqual(events, ["preload"])
                self.assertEqual(session_arg.dungeon_drop_scene_slot, 5)
                events.append("passive")
                return original_generate(session_arg, items, **kwargs)

            with patch.dict(
                os.environ,
                {
                    "USDOF_GAME_DUNGEON_ENTER_HANDLE": "1",
                    "USDOF_GAME_DUNGEON_ENTER_GRANT": "1",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_0009": "0",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_0117": "0",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_019F": "0",
                    "USDOF_GAME_DUNGEON_PASSIVE_OBJECT_ITEM": "1",
                    "USDOF_DUNGEON_MONSTERS": "0",
                    "USDOF_GAME_PLAIN_WIRE": "0",
                },
                clear=False,
            ), patch.object(
                quest_index_mod,
                "get_quest_index",
                return_value=_QuestIndexProxy(),
            ), patch.object(
                mod,
                "load_map_passive_objects",
                return_value=[
                    {
                        "kind": "special_passive",
                        "item_drop_refs": ((0, 1, -1, -1, -1),),
                        "code": 48447,
                        "packet_index": 0,
                    }
                ],
            ), patch.object(
                mod,
                "preload_room_drops_before_visibility",
                side_effect=preload,
            ), patch.object(
                drop_service,
                "generate_passive_object_item_entries",
                side_effect=generate,
            ):
                server = GameServer(dynamic_userinfo=True, db_path=str(db_path))
                session = GameSessionState()
                session.dungeon_party_preloaded = True
                session.bind_selected_character(
                    character_id=0xE2B3,
                    slot=0,
                    name="AAAA",
                )
                session.dungeon_party_preloaded = True
                session.in_dungeon_select = True
                packets, note = server._handle_select_dungeon_0010(
                    bytes.fromhex(
                        "90 00 00 00 00 00 00 00 00 ff ff 00 00 00 00 00 00"
                    ),
                    session=session,
                )

        self.assertEqual(events, ["preload", "passive"])
        self.assertEqual(session.dungeon_drops[6].template_id, 3341)
        self.assertIn("passive_item map=", note)
        self.assertIn("rows=1", note)
        self.assertIn("/extra=1/", note)
        start_map = next(
            packet for packet in packets if int.from_bytes(packet[1:3], "little") == 0x001D
        )
        body = codec1_seed0_decode(start_map[0x10:])
        self.assertEqual(body[18], 0)
        self.assertEqual(body[19], 1)
        self.assertEqual(int.from_bytes(body[21:25], "little"), 6)

    def test_0010_preloads_before_building_start_map(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "inventory.db"
            repo = CharacterRepository(db_path, bootstrap_from_reference=False)
            repo.ensure_account(1)
            repo.create_character(
                account_id=1,
                character_id=0xE2B3,
                name="AAAA",
                job=12,
                grow_type=0,
                level=1,
            )
            with patch.dict(
                os.environ,
                {
                    "USDOF_GAME_DUNGEON_ENTER_HANDLE": "1",
                    "USDOF_GAME_DUNGEON_ENTER_GRANT": "1",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_0009": "0",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_0117": "0",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_019F": "0",
                    "USDOF_DUNGEON_MONSTERS": "0",
                    "USDOF_GAME_PLAIN_WIRE": "0",
                },
                clear=False,
            ):
                server = GameServer(dynamic_userinfo=True, db_path=str(db_path))
                session = GameSessionState()
                session.dungeon_party_preloaded = True
                session.bind_selected_character(
                    character_id=0xE2B3,
                    slot=0,
                    name="AAAA",
                )
                session.dungeon_party_preloaded = True
                session.in_dungeon_select = True
                events = []
                original_build = mod.build_start_map_001d_us

                def preload(server_arg, session_arg, *, source):
                    self.assertIs(server_arg, server)
                    self.assertIs(session_arg, session)
                    self.assertEqual(source, "dungeon_enter_001d")
                    self.assertTrue(session.in_dungeon_run)
                    self.assertGreater(session.dungeon_drop_lcg_seed, 0)
                    events.append("preload")
                    return "drop_preload source=dungeon_enter_001d generated=0"

                def build(*args, **kwargs):
                    events.append("build")
                    return original_build(*args, **kwargs)

                with patch.object(
                    mod,
                    "preload_room_drops_before_visibility",
                    side_effect=preload,
                ), patch.object(
                    mod,
                    "build_start_map_001d_us",
                    side_effect=build,
                ):
                    packets, note = server._handle_select_dungeon_0010(
                        bytes.fromhex(
                            "90 00 00 00 00 00 00 00 00 ff ff 00 00 00 00 00 00"
                        ),
                        session=session,
                    )

        self.assertGreaterEqual(len(packets), 2, note)
        self.assertEqual(events, ["preload", "build"])
        self.assertIn("drop_preload source=dungeon_enter_001d generated=0", note)

    def test_parse_live_luteon_17b(self) -> None:
        plain = bytes.fromhex("90 00 00 00 00 00 00 00 00 FF FF 00 00 00 00 00 00")
        parsed = parse_0010_plain(plain)
        self.assertEqual(parsed["dungeon_id"], 0x90)
        self.assertEqual(parsed["difficulty"], 0)
        self.assertIn("17B", parsed["layout"])

    def test_parse_special_dungeon_2009_aes_padded_32b(self) -> None:
        """银色村庄特殊本「遗忘之森」live 0010：codec2 对齐到 32B 尾零。

        历史 bug：reserved3+17B 抢分 → dungeon_id=256 → map_index=0 闪退。
        正确：leading 17B → 2009 difficulty=1。
        """
        # live plain after codec2 decode (prefix 17B + zero pad)
        plain = bytes.fromhex(
            "d9 07 00 00 01 00 00 00 00 ff ff 00 00 00 00 00 00"
            " 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
        )
        parsed = parse_0010_plain(plain)
        self.assertEqual(parsed["dungeon_id"], 2009)
        self.assertEqual(parsed["difficulty"], 1)
        self.assertIn("17B", parsed["layout"])
        self.assertNotIn("reserved3", parsed["layout"])

    def test_parse_special_dungeon_2009_exact_17b(self) -> None:
        plain = bytes.fromhex("d9 07 00 00 01 00 00 00 00 ff ff 00 00 00 00 00 00 00")
        parsed = parse_0010_plain(plain)
        self.assertEqual(parsed["dungeon_id"], 2009)
        self.assertEqual(parsed["difficulty"], 1)

    def test_resolve_special_sealforest_2009(self) -> None:
        info = start_room.resolve_dungeon_start_room(2009, difficulty=1)
        self.assertEqual(info["dungeon_id"], 2009)
        self.assertEqual(info["start_x"], 0)
        self.assertEqual(info["start_y"], 0)
        self.assertEqual(info["boss_x"], 6)
        self.assertEqual(info["boss_y"], 0)
        self.assertEqual(info["map_index"], 17096)
        self.assertNotEqual(info["reason"], "no_maze")
        self.assertIn("SealForest", str(info.get("path") or ""))

    def test_resolve_pungjin_training_start_room_from_map_lst(self) -> None:
        info = start_room.resolve_dungeon_start_room(5000, difficulty=0)
        self.assertEqual(info["start_x"], 0)
        self.assertEqual(info["start_y"], 0)
        self.assertEqual(info["boss_x"], 0)
        self.assertEqual(info["boss_y"], 0)
        self.assertEqual(info["map_index"], 36250)
        self.assertEqual(info["map_source"], "pungjin_training_map_lst")
        self.assertEqual(
            map_mons.resolve_map_path(info["map_index"]).lower(),
            "map/poongjintrainingroom/11100(0,0)start.map",
        )

    def test_resolve_prophetic_dream_native_start_and_first_move(self) -> None:
        """PVF-native omitted starts keep their maze-specific MAP resource."""
        report = map_mons.warmup_map_index()
        self.assertGreaterEqual(report["map_native_start_area_count"], 1)

        start = start_room.resolve_dungeon_start_room(
            1004,
            difficulty=0,
            active_quest_ids=[2069],
        )
        self.assertEqual(start["maze_index"], 1)
        self.assertEqual(start["reason"], "active_connection")
        self.assertEqual((start["start_x"], start["start_y"]), (7, 0))
        self.assertEqual((start["boss_x"], start["boss_y"]), (0, 1))
        self.assertEqual(start["map_index"], 13426)
        self.assertEqual(start["map_source"], "map_native_start_area")
        self.assertEqual(
            map_mons.resolve_map_path(start["map_index"]).lower(),
            "map/groznyoutside/quest_grozny01.map",
        )

        next_room = start_room.resolve_dungeon_room(
            1004,
            6,
            0,
            difficulty=0,
            maze_index=1,
        )
        self.assertEqual(next_room["map_index"], 13420)
        returned_start = start_room.resolve_dungeon_room(
            1004,
            7,
            0,
            difficulty=0,
            maze_index=1,
        )
        self.assertEqual(returned_start["map_index"], 13426)

        # Quest 12687 targets the SeaChase special dungeon. Its logical (0,0)
        # start is absent from the DGN map-specification table; several
        # same-directory maps expose a start area, but only 17073 is the
        # unclaimed resource adjacent to the maze's explicit 17074 entry.
        sea_chase = start_room.resolve_dungeon_start_room(
            2006,
            difficulty=0,
            active_quest_ids=[12687],
        )
        self.assertEqual(sea_chase["maze_index"], 0)
        self.assertEqual((sea_chase["start_x"], sea_chase["start_y"]), (0, 0))
        self.assertEqual(sea_chase["map_index"], 17073)
        self.assertEqual(sea_chase["map_source"], "map_native_start_area")
        self.assertEqual(
            map_mons.resolve_map_path(sea_chase["map_index"]).lower(),
            "map/seachase/17073(0,0)start.map",
        )

    def test_seed_formula(self) -> None:
        seed = start_room.compute_dungeon_seed(0x90, 0xE2B3)
        expected = (0x12345678 ^ (0x90 << 8) ^ 0xE2B3) & ~0x40000
        self.assertEqual(seed, expected & 0xFFFFFFFF)

    def test_resolve_abnova_144_start_room(self) -> None:
        info = start_room.resolve_dungeon_start_room(144, difficulty=0)
        self.assertEqual(info["dungeon_id"], 144)
        self.assertEqual(info["start_x"], 0)
        self.assertEqual(info["start_y"], 3)
        self.assertEqual(info["boss_x"], 1)
        self.assertEqual(info["boss_y"], 0)
        self.assertEqual(info["map_index"], 8153)

    def test_resolve_black_snow_mountain_selected_boss_map_and_quest_target(self) -> None:
        """The selected free-maze endpoint must use its paired boss MAP."""
        room = start_room.resolve_dungeon_room(
            170,
            6,
            0,
            difficulty=2,
            maze_index=0,
            active_quest_ids=[10001],
        )
        self.assertTrue(room["is_boss_cell"])
        self.assertEqual(room["map_index"], 14347)
        self.assertEqual(
            map_mons.resolve_map_path(room["map_index"]).lower(),
            "map/170_nightmare_ice/14347_(6,0)basic_boss1.map",
        )

        actors = map_mons.load_map_monsters(
            room["map_index"],
            active_quest_ids=[10001],
            dungeon_id=170,
        )
        sasquatch = [
            actor
            for actor in actors
            if actor.get("kind") == "monster" and int(actor.get("code") or 0) == 69550
        ]
        self.assertEqual(len(sasquatch), 1)
        self.assertEqual(int(sasquatch[0].get("type") or 0), 3)
        self.assertFalse(any(actor.get("kind") == "ai_character" for actor in actors))

        drops, note = get_quest_index().resolve_quest_kill_drops(
            [10001],
            source_code=69550,
            enemy_type=1,
            dungeon_id=170,
            difficulty=2,
        )
        self.assertEqual(drops, [(10089787, 1, 100, 1)], note)

        # The other PVF boss candidates are ordinary rooms in this run because
        # S0/001C selected the first declared boss coordinate, (6, 0).
        for cell_x, cell_y, map_index in ((5, 1, 14353), (6, 2, 14359)):
            alternate = start_room.resolve_dungeon_room(
                170,
                cell_x,
                cell_y,
                difficulty=2,
                maze_index=0,
            )
            self.assertFalse(alternate["is_boss_cell"])
            self.assertEqual(alternate["map_index"], map_index)


    def test_timebreak_13509_selects_boss_variant_for_unfinished_targets(self) -> None:
        trigger = pack_trigger_channels(1, 1, 1)
        plan = start_room.build_dungeon_boss_map_variant_plan(
            2007,
            difficulty=0,
            maze_index=0,
            active_quest_pairs=[(13509, trigger)],
        )
        self.assertEqual(plan, {("boss", 9, 0): 17148})

        room = start_room.resolve_dungeon_room(
            2007,
            9,
            0,
            difficulty=0,
            maze_index=0,
            boss_map_variants=plan,
        )
        self.assertTrue(room["is_boss_cell"])
        self.assertEqual(room["map_index"], 17148)
        self.assertTrue(
            {69267, 69268, 69269}.issubset(
                map_mons.collect_map_actor_codes(room["map_index"])
            )
        )

    def test_ancient_times_heart_1919_selects_seeking_material_boss_variant(self) -> None:
        """An unfinished seeking material source must bind its matching BOSS MAP."""
        with patch.object(
            start_room.random,
            "choice",
            side_effect=lambda pool: pool[-1],
        ) as choice:
            plan = start_room.build_dungeon_boss_map_variant_plan(
                154,
                difficulty=0,
                maze_index=0,
                active_quest_pairs=[(1919, 1)],
            )

        self.assertEqual(plan, {("boss", 4, 1): 8212})
        choice.assert_not_called()
        room = start_room.resolve_dungeon_room(
            154,
            4,
            1,
            difficulty=0,
            maze_index=0,
            boss_map_variants=plan,
        )
        self.assertEqual(room["map_index"], 8212)
        self.assertIn(64054, map_mons.collect_map_actor_codes(room["map_index"]))

        with patch.object(
            start_room.random,
            "choice",
            side_effect=lambda pool: pool[-1],
        ) as choice:
            completed = start_room.build_dungeon_boss_map_variant_plan(
                154,
                difficulty=0,
                maze_index=0,
                active_quest_pairs=[(1919, 0)],
            )

        self.assertEqual(completed, {("boss", 4, 1): 8213})
        choice.assert_called_once_with((8211, 8212, 8213))

    def test_boss_variant_follows_active_task_acceptance_order(self) -> None:
        """The first accepted task wins before later task target coverage."""
        early_1919 = start_room.build_dungeon_boss_map_variant_plan(
            154,
            difficulty=0,
            maze_index=0,
            active_quest_pairs=[(1919, 1), (1923, 1)],
        )
        early_1923 = start_room.build_dungeon_boss_map_variant_plan(
            154,
            difficulty=0,
            maze_index=0,
            active_quest_pairs=[(1923, 1), (1919, 1)],
        )

        self.assertEqual(early_1919, {("boss", 4, 1): 8212})
        self.assertEqual(early_1923, {("boss", 4, 1): 8211})

    def test_boss_variant_skips_earlier_task_without_current_map_strategy(self) -> None:
        plan = start_room.build_dungeon_boss_map_variant_plan(
            154,
            difficulty=0,
            maze_index=0,
            active_quest_pairs=[
                (13509, pack_trigger_channels(1, 1, 1)),
                (1919, 1),
            ],
        )

        self.assertEqual(plan, {("boss", 4, 1): 8212})

    def test_boss_variant_uses_the_first_matching_task_without_later_tiebreaker(self) -> None:
        actor_codes = {
            9101: {70001},
            9102: {70001, 70002},
        }
        loader_result = SimpleNamespace(
            collect_map_actor_codes=lambda map_id: actor_codes[int(map_id)]
        )
        with patch("game.chain_loader.load_chain", return_value=loader_result):
            self.assertEqual(
                start_room._choose_boss_map_variant(
                    (9101, 9102),
                    quest_targets=(),
                    quest_target_groups=((70001,), (70002,)),
                ),
                9101,
            )
            self.assertEqual(
                start_room._choose_boss_map_variant(
                    (9101, 9102),
                    quest_targets=(),
                    quest_target_groups=((79999,), (70002,)),
                ),
                9102,
            )
            with patch.object(start_room.random, "choice", return_value=9102) as choice:
                self.assertEqual(
                    start_room._choose_boss_map_variant(
                        (9101, 9102),
                        quest_targets=(),
                        quest_target_groups=((79999,), (79998,)),
                    ),
                    9102,
                )
                choice.assert_called_once_with((9101, 9102))

    def test_dungeon_entry_reads_acceptance_order_not_task_slot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "inventory.db"
            repo = CharacterRepository(db_path, bootstrap_from_reference=False)
            repo.ensure_account(1)
            repo.create_character(
                account_id=1,
                character_id=0xE2B3,
                name="AAAA",
                job=12,
                grow_type=0,
                level=90,
            )
            with repo.open() as conn:
                # Slot 3 was accepted first; task slot order must not make 1923 win.
                conn.execute(
                    "INSERT INTO character_active_quests "
                    "(character_id, slot, quest_id, trigger_value, accepted_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (0xE2B3, 3, 1919, 1, 10),
                )
                conn.execute(
                    "INSERT INTO character_active_quests "
                    "(character_id, slot, quest_id, trigger_value, accepted_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (0xE2B3, 0, 1923, 1, 20),
                )
            with patch.dict(
                os.environ,
                {
                    "USDOF_GAME_DUNGEON_ENTER_HANDLE": "1",
                    "USDOF_GAME_DUNGEON_ENTER_GRANT": "1",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_0009": "0",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_0117": "0",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_019F": "0",
                    # Keep maze 0 so this test isolates DB order -> BOSS plan.
                    "USDOF_GAME_DUNGEON_QUEST_MAZE": "0",
                    "USDOF_GAME_DUNGEON_001C_MAZE_INDEX": "1",
                    "USDOF_GAME_DUNGEON_ENTER_001D_CELL": "start",
                    "USDOF_DUNGEON_MONSTERS": "0",
                    "USDOF_GAME_PLAIN_WIRE": "0",
                },
                clear=False,
            ):
                server = GameServer(dynamic_userinfo=True, db_path=str(db_path))
                session = GameSessionState()
                session.dungeon_party_preloaded = True
                session.bind_selected_character(
                    character_id=0xE2B3,
                    slot=0,
                    name="AAAA",
                )
                session.dungeon_party_preloaded = True
                session.in_dungeon_select = True
                packets, note = server._handle_select_dungeon_0010(
                    (154).to_bytes(4, "little") + bytes(5) + b"\xFF\xFF" + bytes(6),
                    session=session,
                )

        self.assertGreaterEqual(len(packets), 2, note)
        self.assertEqual(session.active_quest_pairs, [(1919, 1), (1923, 1)])
        self.assertEqual(
            session.dungeon_boss_map_variants,
            {("boss", 4, 1): 8212},
        )

    def test_unfinished_quest_actor_targets_cover_both_pvf_reward_tables(self) -> None:
        """BOSS matching must not depend on a task's concrete type."""
        meta = QuestMeta(
            quest_id=9919,
            path="test/reward_target.qst",
            quest_type="[clear map]",
            monster_rewards=[
                MonsterRewardEntry(
                    monster_code=71001,
                    dungeon_id=154,
                    difficulty=-1,
                    item_id=10100001,
                ),
                MonsterRewardEntry(
                    monster_code=71001,
                    dungeon_id=154,
                    difficulty=0,
                    item_id=10100002,
                ),
                MonsterRewardEntry(
                    monster_code=71003,
                    dungeon_id=155,
                    difficulty=-1,
                    item_id=10100003,
                ),
            ],
            enemy_rewards=[
                EnemyRewardEntry(
                    enemy_code=71002,
                    enemy_type=2,
                    dungeon_id=154,
                    difficulty=0,
                    item_id=10100004,
                ),
                EnemyRewardEntry(
                    enemy_code=71004,
                    enemy_type=3,
                    dungeon_id=154,
                    difficulty=1,
                    item_id=10100005,
                ),
            ],
        )
        hunt_meta = QuestMeta(
            quest_id=9920,
            path="test/hunt_target.qst",
            quest_type="[hunt monster]",
            int_data="154 0 71005 1",
            monster_rewards=[
                MonsterRewardEntry(
                    monster_code=71005,
                    dungeon_id=154,
                    difficulty=0,
                    item_id=10100006,
                )
            ],
            enemy_rewards=[
                EnemyRewardEntry(
                    enemy_code=71005,
                    enemy_type=1,
                    dungeon_id=154,
                    difficulty=0,
                    item_id=10100007,
                )
            ],
        )
        metas = {9919: meta, 9920: hunt_meta}

        class _QuestIndex:
            def get_meta(self, quest_id):
                return metas.get(int(quest_id))

        with patch.object(start_room, "get_quest_index", return_value=_QuestIndex()):
            self.assertEqual(
                start_room._unfinished_quest_actor_targets(
                    154,
                    0,
                    [(9919, 1)],
                ),
                (71001, 71002),
            )
            self.assertEqual(
                start_room._unfinished_quest_actor_targets(
                    154,
                    0,
                    [(9919, 0)],
                ),
                (),
            )
            self.assertEqual(
                start_room._unfinished_quest_actor_targets(
                    154,
                    0,
                    [(9920, pack_trigger_channels(1))],
                ),
                (71005,),
            )

    def test_timebreak_boss_variant_fallback_keeps_pvf_duplicate_weights(self) -> None:
        seen_pools = []

        def choose_last(pool):
            seen_pools.append(tuple(pool))
            return pool[-1]

        with patch.object(start_room.random, "choice", side_effect=choose_last):
            plan = start_room.build_dungeon_boss_map_variant_plan(
                2007,
                difficulty=0,
                maze_index=0,
                active_quest_pairs=(),
            )

        expected_pool = (17145, 17148, 17145, 17148, 17139, 17144, 17145, 17148)
        self.assertEqual(seen_pools, [expected_pool])
        self.assertEqual(plan, {("boss", 9, 0): 17148})

    def test_timebreak_boss_variant_plan_survives_move_map_revisit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "inventory.db"
            repo = CharacterRepository(db_path, bootstrap_from_reference=False)
            repo.ensure_account(1)
            repo.create_character(
                account_id=1,
                character_id=0xE2B3,
                name="AAAA",
                job=12,
                grow_type=0,
                level=90,
            )
            repo.accept_quest(
                0xE2B3,
                13509,
                trigger_value=pack_trigger_channels(1, 1, 1),
            )
            with patch.dict(
                os.environ,
                {
                    "USDOF_GAME_DUNGEON_ENTER_HANDLE": "1",
                    "USDOF_GAME_DUNGEON_ENTER_GRANT": "1",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_0009": "0",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_0117": "0",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_019F": "0",
                    "USDOF_GAME_DUNGEON_QUEST_MAZE": "1",
                    "USDOF_GAME_DUNGEON_001C_MAZE_INDEX": "1",
                    "USDOF_GAME_DUNGEON_ENTER_001D_CELL": "start",
                    "USDOF_GAME_DUNGEON_MOVE_MAP_HANDLE": "1",
                    "USDOF_DUNGEON_MONSTERS": "1",
                    "USDOF_GAME_PLAIN_WIRE": "0",
                },
                clear=False,
            ):
                server = GameServer(dynamic_userinfo=True, db_path=str(db_path))
                session = GameSessionState()
                session.dungeon_party_preloaded = True
                session.bind_selected_character(
                    character_id=0xE2B3,
                    slot=0,
                    name="AAAA",
                )
                session.dungeon_party_preloaded = True
                session.in_dungeon_select = True
                packets, note = server._handle_select_dungeon_0010(
                    (2007).to_bytes(4, "little") + bytes(5) + b"\xFF\xFF" + bytes(6),
                    session=session,
                )
                self.assertGreaterEqual(len(packets), 2, note)
                self.assertEqual(
                    session.dungeon_boss_map_variants,
                    {("boss", 9, 0): 17148},
                )

                service = move_map.MoveMap002DService(server)
                first_packets, first_note = service.handle(bytes((9, 0)), session=session)
                self.assertEqual(len(first_packets), 1, first_note)
                self.assertEqual(session.dungeon_map_index, 17148)
                self.assertTrue(
                    {69267, 69268, 69269}.issubset(
                        {
                            int(row.get("code") or 0)
                            for row in list(session.dungeon_room_mons or [])
                        }
                    ),
                    first_note,
                )

                session.active_quest_pairs = [(13509, 0)]
                back_packets, back_note = service.handle(bytes((0, 0)), session=session)
                self.assertEqual(len(back_packets), 1, back_note)
                revisit_packets, revisit_note = service.handle(
                    bytes((9, 0)),
                    session=session,
                )
                self.assertEqual(len(revisit_packets), 1, revisit_note)
                self.assertEqual(session.dungeon_map_index, 17148)
                self.assertIn("room_revisit=1", revisit_note)


    def test_resolve_abnova_144_quest_1776_start_room(self) -> None:
        info = start_room.resolve_dungeon_start_room(
            144,
            difficulty=0,
            active_quest_ids=[1776],
            finished_quest_ids=[101, 1016],
        )
        self.assertEqual(info["maze_index"], 1)
        self.assertEqual(info["reason"], "active_connection")
        self.assertEqual(info["start_x"], 0)
        self.assertEqual(info["start_y"], 0)
        self.assertEqual(info["boss_x"], 5)
        self.assertEqual(info["boss_y"], 0)
        self.assertEqual(info["map_index"], 55612)
        self.assertEqual(info["quest_connection"], [0, 1776, -1])

    def test_resolve_noire_ferra_1925_native_vaughn_start_room(self) -> None:
        report = map_mons.warmup_map_index()
        self.assertGreaterEqual(report["quest_companion_start_count"], 1)

        info = start_room.resolve_dungeon_start_room(
            239,
            difficulty=0,
            active_quest_ids=[1925],
            finished_quest_ids=[1924],
        )
        self.assertEqual(info["maze_index"], 0)
        self.assertEqual((info["start_x"], info["start_y"]), (0, 1))
        self.assertEqual(info["map_index"], 32620)
        self.assertEqual(info["map_source"], "quest_companion_native_map")
        self.assertEqual(
            map_mons.resolve_map_path(32620).lower(),
            "map/noirpera/32620.map",
        )

        actors = map_mons.load_map_monsters(
            32620,
            active_quest_ids=[1925],
            dungeon_id=239,
        )
        vaughn = [
            row
            for row in actors
            if row.get("kind") == "ai_character" and int(row.get("code") or 0) == 1524
        ]
        self.assertEqual(len(vaughn), 1)
        self.assertEqual(vaughn[0].get("faction"), "character")

    def test_handle_0010_grant_uses_active_quest_maze(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "inventory.db"
            repo = CharacterRepository(db_path, bootstrap_from_reference=False)
            repo.ensure_account(1)
            repo.create_character(
                account_id=1,
                character_id=0xE2B3,
                name="AAAA",
                job=12,
                grow_type=0,
                level=1,
            )
            repo.accept_quest(0xE2B3, 1776, trigger_value=1)
            repo.mark_quest_finished(0xE2B3, 101)
            repo.mark_quest_finished(0xE2B3, 1016)
            with patch.dict(
                os.environ,
                {
                    "USDOF_GAME_DUNGEON_ENTER_HANDLE": "1",
                    "USDOF_GAME_DUNGEON_ENTER_GRANT": "1",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_0009": "0",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_0117": "0",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_019F": "0",
                    # Daily default is quest-connection maze ON (90qiyuan parity).
                    # Explicit env keeps the test independent of shell overrides.
                    "USDOF_GAME_DUNGEON_QUEST_MAZE": "1",
                    "USDOF_GAME_DUNGEON_001C_MAZE_INDEX": "1",
                    "USDOF_GAME_DUNGEON_ENTER_001D_CELL": "start",
                    "USDOF_DUNGEON_MONSTERS": "1",
                    "USDOF_GAME_PLAIN_WIRE": "0",
                },
                clear=False,
            ):
                server = GameServer(dynamic_userinfo=True, db_path=str(db_path))
                session = GameSessionState()
                session.dungeon_party_preloaded = True
                session.bind_selected_character(character_id=0xE2B3, slot=0, name="AAAA")
                session.dungeon_party_preloaded = True
                session.in_dungeon_select = True
                plain = bytes.fromhex("90 00 00 00 00 00 00 00 00 FF FF 00 00 00 00 00 00")
                packets, note = server._handle_select_dungeon_0010(plain, session=session)
                self.assertGreaterEqual(len(packets), 2, note)
                self.assertIn("maze=1", note)
                self.assertIn("map_index=55612", note)
                self.assertIn("active=1776", note)
                self.assertIn("cell=start(0,0)", note)
                self.assertIn("quest_maze=1", note)
                self.assertIn("quest_connected=1", note)
                self.assertIn("mon=", note)
                self.assertEqual(getattr(session, "dungeon_maze_index", None), 1)
                self.assertEqual(getattr(session, "dungeon_map_index", None), 55612)
                self.assertEqual(getattr(session, "dungeon_cell_x", None), 0)
                self.assertEqual(getattr(session, "dungeon_cell_y", None), 0)
                self.assertEqual(getattr(session, "dungeon_boss_x", None), 5)
                self.assertEqual(getattr(session, "dungeon_boss_y", None), 0)
                self.assertTrue(getattr(session, "quest_connected", False))
                # PARTY_INFO uid and START_MAP seq share the Actor table.
                self.assertEqual(getattr(session, "dungeon_room_first_seq", None), 0xE2B3)
                actor_keys = [int(mon.get("seq") or 0) for mon in session.dungeon_room_mons]
                self.assertNotIn(0xE2B3, actor_keys)
                self.assertTrue(all(key > 0xE2B3 for key in actor_keys))
                recovery = repo.get_dungeon_login_recovery(0xE2B3)
                self.assertIsNotNone(recovery)
                self.assertEqual(int(recovery["town_id"]), 1)
                self.assertIn("dungeon_login_recovery=saved", note)

                by_op = {int.from_bytes(p[1:3], "little"): p for p in packets}
                body_c = codec0_seed0_decode(by_op[0x001C][0x10:])
                self.assertEqual(int.from_bytes(body_c[0:4], "little"), 0x90)
                # 90qiyuan/Stage3: u16 EntryOption + u8 MazeIndex（禁止把 maze 写成 u16）
                self.assertEqual(int.from_bytes(body_c[5:7], "little"), 0)  # entry_option
                self.assertEqual(body_c[7], 1)  # maze_index
                self.assertEqual(body_c[8], 5)  # boss_x
                self.assertEqual(body_c[9], 0)  # boss_y
                body_d = by_op[0x001D][0x10:]
                decoded_d = codec1_seed0_decode(body_d)
                self.assertEqual(decoded_d[0], 0)
                self.assertEqual(decoded_d[1], 0)
                self.assertEqual(int.from_bytes(decoded_d[14:18], "little"), 55612)

    def test_handle_0010_grant_empty_map(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "inventory.db"
            repo = CharacterRepository(db_path, bootstrap_from_reference=False)
            repo.ensure_account(1)
            repo.create_character(
                account_id=1,
                character_id=0xE2B3,
                name="AAAA",
                job=12,
                grow_type=0,
                level=1,
            )
            with patch.dict(
                os.environ,
                {
                    "USDOF_GAME_DUNGEON_ENTER_HANDLE": "1",
                    "USDOF_GAME_DUNGEON_ENTER_GRANT": "1",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_0009": "0",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_0117": "0",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_019F": "0",
                    # 无 active 剧情任务时仍应落到自由迷宫（maze0 / 8153）
                    "USDOF_GAME_DUNGEON_QUEST_MAZE": "1",
                    "USDOF_GAME_DUNGEON_001C_MAZE_INDEX": "1",
                    "USDOF_GAME_DUNGEON_ENTER_001D_CELL": "start",
                    "USDOF_DUNGEON_MONSTERS": "1",
                    "USDOF_GAME_PLAIN_WIRE": "0",
                },
                clear=False,
            ):
                server = GameServer(dynamic_userinfo=True, db_path=str(db_path))
                session = GameSessionState()
                session.dungeon_party_preloaded = True
                session.bind_selected_character(character_id=0xE2B3, slot=0, name="AAAA")
                session.dungeon_party_preloaded = True
                session.in_dungeon_select = True
                plain = bytes.fromhex("90 00 00 00 00 00 00 00 00 FF FF 00 00 00 00 00 00")
                packets, note = server._handle_select_dungeon_0010(plain, session=session)
                self.assertGreaterEqual(len(packets), 2, note)
                opcodes = [int.from_bytes(p[1:3], "little") for p in packets]
                # Friend / Stage3 daily baseline: DUNGEON_INFO + START_MAP only.
                self.assertEqual(opcodes[:2], [0x001C, 0x001D], note)
                self.assertNotIn(0x0009, opcodes)
                self.assertNotIn(0x0117, opcodes)
                self.assertNotIn(0x019F, opcodes)
                self.assertTrue(getattr(session, "in_dungeon_run", False), note)
                self.assertFalse(getattr(session, "in_dungeon_select", True), note)
                self.assertEqual(getattr(session, "selected_dungeon_id", None), 0x90)
                self.assertEqual(getattr(session, "dungeon_map_index", None), 8153)
                self.assertTrue(getattr(session, "dungeon_recommended", False))
                self.assertIn("recommended=1 char_lv=1 range=1..5", note)
                self.assertIn("reason=no_connection", note)
                self.assertIn("quest_connected=0", note)
                self.assertFalse(getattr(session, "quest_connected", True))

                by_op = {int.from_bytes(p[1:3], "little"): p for p in packets}
                # 0x001C % 14 == 0 -> codec0 XTEA-ECB seed0
                body_c = codec0_seed0_decode(by_op[0x001C][0x10:])
                self.assertEqual(int.from_bytes(body_c[0:4], "little"), 0x90)
                body_d = by_op[0x001D][0x10:]
                decoded_d = codec1_seed0_decode(body_d)
                self.assertEqual(decoded_d[0], 0)  # cell_x
                self.assertEqual(decoded_d[1], 3)  # cell_y start for 144
                self.assertEqual(int.from_bytes(decoded_d[14:18], "little"), 8153)
                # Solo partyMemberIndex at end of START_MAP must be 0 (not 0xFF).
                self.assertEqual(decoded_d[-1], 0, decoded_d.hex(" "))

    def test_handle_0010_freezes_non_recommended_level_from_pvf(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "inventory.db"
            repo = CharacterRepository(db_path, bootstrap_from_reference=False)
            repo.ensure_account(1)
            repo.create_character(
                account_id=1,
                character_id=0xE2B3,
                name="AAAA",
                job=12,
                grow_type=0,
                level=18,
            )
            with patch.dict(
                os.environ,
                {
                    "USDOF_GAME_DUNGEON_ENTER_HANDLE": "1",
                    "USDOF_GAME_DUNGEON_ENTER_GRANT": "1",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_0009": "0",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_0117": "0",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_019F": "0",
                    "USDOF_DUNGEON_MONSTERS": "0",
                    "USDOF_GAME_PLAIN_WIRE": "0",
                },
                clear=False,
            ):
                server = GameServer(dynamic_userinfo=True, db_path=str(db_path))
                session = GameSessionState()
                session.dungeon_party_preloaded = True
                session.bind_selected_character(
                    character_id=0xE2B3,
                    slot=0,
                    name="AAAA",
                )
                session.dungeon_party_preloaded = True
                session.in_dungeon_select = True
                plain = (
                    (149).to_bytes(4, "little")
                    + bytes(5)
                    + b"\xFF\xFF"
                    + bytes(6)
                )
                packets, note = server._handle_select_dungeon_0010(
                    plain,
                    session=session,
                )

                self.assertGreaterEqual(len(packets), 2, note)
                self.assertFalse(getattr(session, "dungeon_recommended", True))
                self.assertIn("recommended=0 char_lv=18 range=15..17", note)

    def test_handle_0010_pungjin_training_enters_without_select_ui(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "inventory.db"
            repo = CharacterRepository(db_path, bootstrap_from_reference=False)
            repo.ensure_account(1)
            repo.create_character(
                account_id=1,
                character_id=500001,
                name="AAAA",
                job=12,
                grow_type=0,
                level=1,
            )
            with patch.dict(
                os.environ,
                {
                    "USDOF_GAME_DUNGEON_ENTER_HANDLE": "1",
                    "USDOF_GAME_DUNGEON_ENTER_GRANT": "1",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_0009": "0",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_0117": "0",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_019F": "0",
                    "USDOF_DUNGEON_MONSTERS": "0",
                    "USDOF_GAME_PLAIN_WIRE": "0",
                },
                clear=False,
            ):
                server = GameServer(dynamic_userinfo=True, db_path=str(db_path))
                session = GameSessionState()
                session.dungeon_party_preloaded = True
                session.bind_selected_character(character_id=500001, slot=0, name="AAAA")
                session.dungeon_party_preloaded = True
                # Stage3 live C1/0010 from Pungjin's Enter Training button.
                plain = bytes.fromhex(
                    "88 13 00 00 00 00 00 00 01 FF FF 00 00 00 00 00 00"
                )
                packets, note = server._handle_select_dungeon_0010(plain, session=session)

                opcodes = [int.from_bytes(packet[1:3], "little") for packet in packets]
                self.assertEqual(opcodes, [0x001C, 0x001D], note)
                self.assertNotIn(0x0017, opcodes)
                self.assertNotIn(0x001B, opcodes)
                self.assertTrue(getattr(session, "in_dungeon_run", False), note)
                self.assertEqual(getattr(session, "dungeon_map_index", None), 36250)
                self.assertIn("map_index=36250", note)
                self.assertIn("map_source=pungjin_training_map_lst", note)

                by_op = {int.from_bytes(packet[1:3], "little"): packet for packet in packets}
                body_c = codec0_seed0_decode(by_op[0x001C][0x10:])
                self.assertEqual(int.from_bytes(body_c[0:4], "little"), 5000)
                body_d = codec1_seed0_decode(by_op[0x001D][0x10:])
                self.assertEqual(body_d[0:2], b"\x00\x00")
                self.assertEqual(int.from_bytes(body_d[14:18], "little"), 36250)

    def test_parse_0308_live_red_guide(self) -> None:
        """红色引导 0x0308 live plain: dungeon=149 circle=14887。"""
        parse_0308_plain = mod.parse_0308_plain
        plain = bytes.fromhex("95 00 00 00 27 3a 00 00")
        parsed = parse_0308_plain(plain)
        self.assertEqual(parsed["dungeon_id"], 149)
        self.assertEqual(parsed["circle_id"], 14887)
        self.assertEqual(parsed["field1"], 14887)
        self.assertIn("2xu32", parsed["layout"])

    def test_circle_dungeon_info_maps_pair_to_protocol_difficulty(self) -> None:
        parsed = circle_info.parse_circle_dungeon_info(
            """
[circle Dungeon List Info]
[Worldmap Index]
22
[difficulty level]
1
[Dungeon]
160
14983
160
14984
[/Dungeon]
[/circle Dungeon List Info]
[circle Dungeon List Info]
[Worldmap Index]
17
[difficulty level]
4
[Dungeon]
70
15106
[/Dungeon]
[/circle Dungeon List Info]
"""
        )
        self.assertEqual(parsed.group_count, 2)
        self.assertEqual(parsed.entry_count, 3)
        self.assertEqual(parsed.difficulty_for(160, 14983), 1)
        self.assertEqual(parsed.difficulty_for(70, 15106), 4)
        self.assertIsNone(parsed.difficulty_for(160, 15106))

    def test_circle_dungeon_info_rejects_duplicate_pair(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicated"):
            circle_info.parse_circle_dungeon_info(
                """
[circle Dungeon List Info]
[difficulty level]
0
[Dungeon]
149 14887
[/Dungeon]
[/circle Dungeon List Info]
[circle Dungeon List Info]
[difficulty level]
1
[Dungeon]
149 14887
[/Dungeon]
[/circle Dungeon List Info]
"""
            )

    def test_handle_0308_red_guide_reuses_0010_grant(self) -> None:
        """无主线时红色按钮只发 0x0308，应走 001C+001D grant。"""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "inventory.db"
            repo = CharacterRepository(db_path, bootstrap_from_reference=False)
            repo.ensure_account(1)
            repo.create_character(
                account_id=1,
                character_id=0xE2B3,
                name="AAAA",
                job=12,
                grow_type=0,
                level=89,
            )
            with patch.dict(
                os.environ,
                {
                    "USDOF_GAME_DUNGEON_ENTER_HANDLE": "1",
                    "USDOF_GAME_DUNGEON_ENTER_GRANT": "1",
                    "USDOF_GAME_DUNGEON_GUIDE_0308": "1",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_0009": "0",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_0117": "0",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_019F": "0",
                    "USDOF_GAME_DUNGEON_QUEST_MAZE": "1",
                    "USDOF_GAME_DUNGEON_001C_MAZE_INDEX": "1",
                    "USDOF_GAME_DUNGEON_ENTER_001D_CELL": "start",
                    "USDOF_DUNGEON_MONSTERS": "0",
                    "USDOF_GAME_PLAIN_WIRE": "0",
                },
                clear=False,
            ), patch.object(
                mod,
                "resolve_circle_dungeon_difficulty",
                return_value=1,
            ) as resolve_difficulty:
                server = GameServer(dynamic_userinfo=True, db_path=str(db_path))
                session = GameSessionState()
                session.dungeon_party_preloaded = True
                session.bind_selected_character(character_id=0xE2B3, slot=0, name="AAAA")
                session.dungeon_party_preloaded = True
                session.in_dungeon_select = True
                # live red random-entry body (2026-08-06 08:26:51)
                plain = bytes.fromhex("a0 00 00 00 87 3a 00 00")
                result = route_dynamic_game_packet(
                    server,
                    flag=1,
                    opcode=0x0308,
                    plain=plain,
                    session=session,
                    create_character_userinfo_refresh_pending=False,
                    charselect_0004_sent=False,
                )
                self.assertTrue(result.handled, result.note)
                self.assertGreaterEqual(len(result.response_packets), 2, result.note)
                self.assertIn("C1/0308 GUIDE_ENTER_DUNGEON", result.note)
                self.assertIn("dungeon_id=160", result.note)
                self.assertIn("circle_id=14983", result.note)
                self.assertIn(
                    "guide_difficulty=1 source=pvf_circledungeoninfo",
                    result.note,
                )
                self.assertIn("reuse_0010_grant", result.note)
                self.assertIn("enter_run", result.note)
                resolve_difficulty.assert_called_once_with(160, 14983)
                self.assertEqual(getattr(session, "selected_dungeon_id", None), 160)
                self.assertEqual(getattr(session, "selected_difficulty", None), 1)
                self.assertEqual(getattr(session, "cur_dungeon_difficulty", None), 1)
                self.assertTrue(getattr(session, "in_dungeon_run", False), result.note)
                opcodes = [
                    int.from_bytes(p[1:3], "little") for p in result.response_packets
                ]
                self.assertEqual(opcodes[:2], [0x001C, 0x001D], result.note)
                by_opcode = {
                    int.from_bytes(packet[1:3], "little"): packet
                    for packet in result.response_packets
                }
                dungeon_info = codec0_seed0_decode(by_opcode[0x001C][0x10:])
                self.assertEqual(dungeon_info[4], 1)

    def test_handle_0010_reject_dungeon_zero(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "inventory.db"
            repo = CharacterRepository(db_path, bootstrap_from_reference=False)
            repo.ensure_account(1)
            repo.create_character(
                account_id=1,
                character_id=910003,
                name="AAAA",
                job=12,
                grow_type=0,
                level=1,
            )
            with patch.dict(
                os.environ,
                {
                    "USDOF_GAME_DUNGEON_ENTER_HANDLE": "1",
                    "USDOF_GAME_DUNGEON_ENTER_GRANT": "1",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_0009": "0",
                },
                clear=False,
            ):
                server = GameServer(dynamic_userinfo=True, db_path=str(db_path))
                session = GameSessionState()
                session.dungeon_party_preloaded = True
                session.bind_selected_character(character_id=910003, slot=0, name="AAAA")
                session.dungeon_party_preloaded = True
                packets, note = server._handle_select_dungeon_0010(
                    bytes.fromhex("00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"),
                    session=session,
                )
                self.assertEqual(packets, [])
                self.assertIn("reject dungeon_id=0", note)

    def test_router_dispatches_0010_grant(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "inventory.db"
            repo = CharacterRepository(db_path, bootstrap_from_reference=False)
            repo.ensure_account(1)
            repo.create_character(
                account_id=1,
                character_id=0xE2B3,
                name="AAAA",
                job=12,
                grow_type=0,
                level=1,
            )
            with patch.dict(
                os.environ,
                {
                    "USDOF_GAME_DUNGEON_ENTER_HANDLE": "1",
                    "USDOF_GAME_DUNGEON_ENTER_GRANT": "1",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_0009": "0",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_0117": "0",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_019F": "0",
                    "USDOF_GAME_PLAIN_WIRE": "0",
                },
                clear=False,
            ):
                server = GameServer(dynamic_userinfo=True, db_path=str(db_path))
                session = GameSessionState()
                session.dungeon_party_preloaded = True
                session.bind_selected_character(character_id=0xE2B3, slot=0, name="AAAA")
                session.dungeon_party_preloaded = True
                route = route_dynamic_game_packet(
                    server,
                    flag=1,
                    opcode=0x0010,
                    plain=bytes.fromhex("90 00 00 00 00 00 00 00 00 FF FF 00 00 00 00 00 00"),
                    session=session,
                    create_character_userinfo_refresh_pending=False,
                    charselect_0004_sent=True,
                )
                self.assertTrue(route.handled)
                opcodes = [int.from_bytes(p[1:3], "little") for p in route.response_packets]
                self.assertEqual(opcodes, [0x001C, 0x001D], route.note)



    def test_codec2_decode_live_0010(self) -> None:
        from game.game_codec import decode_payload_skeleton

        # Live wire from game_server_20260717_065925.log around 07:00:12.
        raw = bytes.fromhex(
            "01 10 00 2d 00 00 00 64 a8 bd dc 44 00 59 ca 53 "
            "72 d0 4a 67 57 f9 26 c9 f8 33 6b 02 fa c8 2e b9 5c "
            "c0 03 2a 0f 20 71 c5 68 57 92 5b 70"
        )
        codec_body = raw[0x0D:]
        dec = decode_payload_skeleton(0x0010, codec_body)
        self.assertTrue(dec.ok, dec.note)
        self.assertIn("idx2", dec.note)
        self.assertEqual(int.from_bytes(dec.plain[:4], "little"), 0x90)
        self.assertEqual(dec.plain[4], 0)



    def test_party_info_type0_solo_body(self) -> None:
        body = build_party_info_type0_solo(0xE2B3, dungeon_id=144, difficulty=0)
        parsed = parse_party_info_type0_header(body)
        self.assertEqual(parsed["block_count"], 1)
        self.assertEqual(parsed["type"], 0)
        self.assertEqual(parsed["member_count"], 1)
        self.assertEqual(parsed["members"][0]["slot"], 0)
        self.assertEqual(parsed["members"][0]["uid"], 0xE2B3)
        self.assertEqual(parsed["dungeon_id"], 144)
        self.assertEqual(parsed["difficulty"], 0)
        self.assertEqual(parsed["has_extra"], 0)
        self.assertEqual(parsed["parsed_len"], parsed["body_len"])

    def test_handle_0010_grant_sends_0009_before_001c(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "inventory.db"
            repo = CharacterRepository(db_path, bootstrap_from_reference=False)
            repo.ensure_account(1)
            repo.create_character(
                account_id=1,
                character_id=0xE2B3,
                name="AAAA",
                job=12,
                grow_type=0,
                level=1,
            )
            with patch.dict(
                os.environ,
                {
                    "USDOF_GAME_DUNGEON_ENTER_HANDLE": "1",
                    "USDOF_GAME_DUNGEON_ENTER_GRANT": "1",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_0009": "1",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_0117": "0",
                    "USDOF_GAME_DUNGEON_ENTER_SEND_019F": "0",
                    "USDOF_GAME_PLAIN_WIRE": "0",
                },
                clear=False,
            ):
                server = GameServer(dynamic_userinfo=True, db_path=str(db_path))
                session = GameSessionState()
                session.dungeon_party_preloaded = True
                session.bind_selected_character(character_id=0xE2B3, slot=0, name="AAAA")
                session.dungeon_party_preloaded = True
                session.in_dungeon_select = True
                plain = bytes.fromhex("90 00 00 00 00 00 00 00 00 FF FF 00 00 00 00 00 00")
                packets, note = server._handle_select_dungeon_0010(plain, session=session)
                opcodes = [int.from_bytes(p[1:3], "little") for p in packets]
                self.assertEqual(opcodes[:3], [0x0009, 0x001C, 0x001D], note)
                self.assertIn("0009/", note)
                by_op = {int.from_bytes(p[1:3], "little"): p for p in packets}
                body9 = by_op[0x0009][0x10:]
                try:
                    parsed = parse_party_info_type0_header(body9)
                except Exception:
                    from game.game_codec import decode_payload_skeleton
                    dec = decode_payload_skeleton(0x0009, body9)
                    self.assertTrue(dec.ok, dec.note)
                    parsed = parse_party_info_type0_header(dec.plain)
                self.assertEqual(parsed["members"][0]["uid"], 0xE2B3)
                self.assertEqual(parsed["members"][0]["slot"], 0)

if __name__ == "__main__":
    unittest.main()


