"""Drop-group references must resolve to distinct special-passive instances."""
from __future__ import annotations

import os
import struct
import unittest
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import patch

from game.chain_loader import load_chain
from game.drop import drop_service, drop_generator, room_drop_preload
from game.drop.dnf_lcg import DnfLcg
from game.drop.object_drop_config import ObjectDropConfig
from game.quest import quest_index as qi, quest_index_cache

mm = load_chain("helper-dungeon_map_monsters.py")
payloads = load_chain("helper-dungeon_payloads.py")


def item(group=0, item_id=3341, rate=10000):
    return qi.SpecialPassiveObjectItem(index=group, item_id=item_id, drop_rate=rate)


def actor(index, group=0, **extra):
    return dict(kind="special_passive", packet_index=index,
                item_drop_refs=((group, 1, -1, -1, -1),), **extra)


def session():
    return SimpleNamespace(dungeon_drop_lcg_seed=7, dungeon_drop_scene_slot=5,
                           dungeon_drops={})


class PassiveItemBindingTests(unittest.TestCase):
    def test_parser_preserves_zero_group_and_special_instance_order(self):
        rows = mm.parse_map_special_passive_tokens("""
        [special passive object]
        16145 1 2 0 0
        10673 485 121 0 1 `[item]` 0 1 -1 -1 -1
        10673 683 120 0 1 `[item]` 0 1 -1 -1 -1
        [/special passive object]
        """)
        self.assertEqual([r["packet_index"] for r in rows], [0, 1, 2])
        self.assertEqual([r["item_drop_refs"] for r in rows],
                         [(), ((0, 1, -1, -1, -1),), ((0, 1, -1, -1, -1),)])
        bound = drop_service.bind_passive_object_item_entries([item()], rows)
        self.assertEqual([r["object_index"] for r in bound], [1, 2])
        self.assertEqual([r["drop_group_index"] for r in bound], [0, 0])

    def test_monster_spawn_channel_is_preserved(self):
        rows = mm.parse_map_special_passive_tokens("""
        [special passive object]
        10673 1 2 0 2 `[item]` 0 1 -1 -1 -1 `[monster]` 65021 3 -1 -1 -1
        [/special passive object]
        """)
        self.assertEqual([r["kind"] for r in rows], ["special_passive", "monster"])
        self.assertEqual(rows[1]["code"], 65021)
        self.assertEqual(rows[1]["slot_index"], 1)
        self.assertEqual(rows[0]["item_drop_refs"], ((0, 1, -1, -1, -1),))

    def test_group_is_not_instance_id_or_object_code(self):
        rows = drop_service.bind_passive_object_item_entries(
            [item(0, 3341), item(9, 10099829)],
            [actor(9, 0, code=10673), actor(4, 9, code=10673)],
        )
        self.assertEqual([(r["object_index"], r["item_id"]) for r in rows],
                         [(9, 3341), (4, 10099829)])

    def test_unrelated_ordinary_action_and_unreferenced_objects_get_nothing(self):
        ordinary = dict(kind="passive_object", packet_index=0,
                        item_drop_refs=((0, 1, -1, -1, -1),))
        objects = [ordinary, actor(1, passive_source_kind="action_created"),
                   dict(kind="special_passive", packet_index=2), actor(3, 88)]
        self.assertEqual(drop_service.bind_passive_object_item_entries([item()], objects), [])

    def test_instance_range_never_wraps_and_aliases_do_not_duplicate(self):
        objects = [actor(-1), actor(256), actor(255), actor(255)]
        bound = drop_service.bind_passive_object_item_entries([item()], objects)
        self.assertEqual([r["object_index"] for r in bound], [255])

    def test_repeated_group_rows_and_multiple_references_stay_separate(self):
        entries = [item(0, 3341), item(0, 10099829), item(1, 10158700)]
        obj = actor(7)
        obj["item_drop_refs"] += ((1, 1, -1, -1, -1),)
        rows = drop_service.bind_passive_object_item_entries(entries, [obj, actor(8)])
        self.assertEqual([(r["object_index"], r["item_id"]) for r in rows],
                         [(7, 3341), (7, 10099829), (7, 10158700), (8, 3341), (8, 10099829)])

    def test_each_building_rolls_separately_and_reserves_unique_pickup_keys(self):
        s = session()
        with patch.dict(os.environ, {"USDOF_GAME_DUNGEON_PASSIVE_OBJECT_ITEM": "1"}), \
             patch.object(drop_service, "roll_weighted_occurrences", side_effect=[1, 0]) as roll:
            rows = drop_service.generate_passive_object_item_entries(
                s, [item(rate=1500)], passive_objects=[actor(3), actor(8)],
                allow_item=lambda _: True)
        self.assertEqual(roll.call_count, 2)
        self.assertEqual([c.args[1] for c in roll.call_args_list], [1500, 1500])
        self.assertEqual([r["object_index"] for r in rows], [3])
        self.assertEqual(list(s.dungeon_drops), [6])

    def test_guaranteed_and_residual_weights_keep_authored_rate(self):
        s = session()
        lcg = DnfLcg(7)
        expected = []
        for index in (3, 8):
            expected.extend([index] * (2 + (lcg.next(10000) < 5000)))
        rows = drop_service.generate_passive_object_item_entries(
            s, [item(rate=25000)], passive_objects=[actor(3), actor(8)], allow_item=lambda _: True)
        self.assertEqual([r["object_index"] for r in rows], expected)
        self.assertEqual(s.dungeon_drop_lcg_seed, lcg.seed)
        self.assertEqual(len(s.dungeon_drops), len(rows))
        self.assertEqual(len({r["global_seq"] for r in rows}), len(rows))

    def test_no_reference_cannot_roll_or_advance_session_state(self):
        s = session()
        self.assertEqual(drop_service.generate_passive_object_item_entries(
            s, [item()], allow_item=lambda _: True), [])
        self.assertEqual((s.dungeon_drop_lcg_seed, s.dungeon_drop_scene_slot), (7, 5))
        self.assertEqual(s.dungeon_drops, {})

    def test_blocked_and_unregistered_materials_do_not_advance_random_or_keys(self):
        for registered, blocked in ((False, False), (True, True)):
            with self.subTest(registered=registered, blocked=blocked):
                s = session()
                with patch.object(drop_service, "is_session_item_drop_blocked", return_value=blocked):
                    result = drop_service.generate_passive_object_item_entries(
                        s, [item(rate=1500)], passive_objects=[actor(3)],
                        allow_item=lambda _: registered)
                self.assertEqual(result, [])
                self.assertEqual((s.dungeon_drop_lcg_seed, s.dungeon_drop_scene_slot), (7, 5))

    def test_001d_limit_never_registers_unserialized_extras(self):
        s = session()
        rows = drop_service.generate_passive_object_item_entries(
            s, [item(rate=3000000)], passive_objects=[actor(3)], allow_item=lambda _: True)
        self.assertEqual(len(rows), 255)
        self.assertEqual(len(s.dungeon_drops), 255)

    def test_scoped_tables_inherit_override_and_do_not_flatten(self):
        d = qi.DungeonFileInfo(dungeon_id=1, path="", special_passive_object_items=[item(0, 3341)],
                              mazes=[qi.MazeInfo(index=0),
                                     qi.MazeInfo(index=1, special_passive_object_items=[]),
                                     qi.MazeInfo(index=2, special_passive_object_items=[item(0, 10158700)])])
        self.assertEqual([r.item_id for r in qi.passive_object_items_for_maze(d, 0)], [3341])
        self.assertEqual(qi.passive_object_items_for_maze(d, 1), [])
        self.assertEqual([r.item_id for r in qi.passive_object_items_for_maze(d, 2)], [10158700])
        self.assertEqual(qi.passive_object_items_for_maze(d, 99), [])

    def test_quest_cache_roundtrip_preserves_local_table_types_and_empty_override(self):
        d = qi.DungeonFileInfo(dungeon_id=1, path="", special_passive_object_items=[item()],
                              mazes=[qi.MazeInfo(index=0),
                                     qi.MazeInfo(index=1, special_passive_object_items=[]),
                                     qi.MazeInfo(index=2, special_passive_object_items=[item(2, 10158700)])])
        restored = quest_index_cache._restore_dungeon(asdict(d))
        self.assertEqual(asdict(restored), asdict(d))
        self.assertIsInstance(restored.mazes[2].special_passive_object_items[0], qi.SpecialPassiveObjectItem)
        self.assertGreaterEqual(quest_index_cache.CACHE_SEMANTIC_VERSION, 4)

    def test_real_grandine_two_ores_share_group_but_have_distinct_packet_ids(self):
        d = qi.get_quest_index().get_dungeon(104)
        refs = mm.load_map_passive_objects(42002)
        bound = drop_service.bind_passive_object_item_entries(qi.passive_object_items_for_maze(d, 0), refs)
        self.assertEqual([(r["object_index"], r["drop_group_index"], r["item_id"], r["drop_rate"]) for r in bound],
                         [(0, 0, 3341, 1500), (1, 0, 3341, 1500)])
        s = session()
        with patch.object(drop_service, "roll_weighted_occurrences", return_value=1):
            rows = drop_service.generate_passive_object_item_entries(
                s, qi.passive_object_items_for_maze(d, 0), passive_objects=refs,
                allow_item=lambda _: True)
        wire = payloads.build_start_map_001d_us(monsters=[], map_index=42002, passive_object_drops=rows)
        self.assertEqual(wire[19], 2)
        decoded = [struct.unpack_from("<BIIIHBHHB", wire, 20 + n * 21) for n in range(2)]
        self.assertEqual([(r[0], r[2]) for r in decoded], [(0, 3341), (1, 3341)])
        self.assertEqual({r[1] for r in decoded}, set(s.dungeon_drops))

    def test_real_redwitch_only_active_maze_table_each_crystal_once(self):
        d = qi.get_quest_index().get_dungeon(314)
        self.assertEqual(d.special_passive_object_items, [])
        refs = mm.load_map_passive_objects(44028)
        for maze in (0, 1):
            with self.subTest(maze=maze):
                items = qi.passive_object_items_for_maze(d, maze)
                self.assertEqual([(r.index, r.item_id, r.drop_rate) for r in items], [(0, 10158700, 10000)])
                s = session()
                rows = drop_service.generate_passive_object_item_entries(
                    s, items, passive_objects=refs, allow_item=lambda _: True)
                self.assertEqual([(r["object_index"], r["item_id"]) for r in rows],
                                 [(n, 10158700) for n in range(4)])

    def test_die_fallback_uses_bound_instance_and_default_preload_prevents_duplicates(self):
        config = ObjectDropConfig()
        config.load_from_text("""[drop prob count]
        1
        [drop prob]
        1 200 0 0 0 0 0
        [basis of rarity dicision]
        1000000 1000001 1000002 1000003 1000004 1000005 1000006
        1000000 1000001 1000002 1000003 1000004 1000005 1000006
        1000000 1000001 1000002 1000003 1000004 1000005 1000006
        1000000 1000001 1000002 1000003 1000004 1000005 1000006
        [item drop ref table]
        1 0 0
        """)
        bindings = drop_service.bind_passive_object_item_entries([item()], [actor(7)])
        with patch.object(drop_generator, "get_object_drop_config", return_value=config), \
             patch.object(drop_generator, "get_equip_stackable_pools"), \
             patch.object(drop_generator, "get_gold_table") as gold, \
             patch.dict(os.environ, {"USDOF_GAME_DUNGEON_ITEM_DROP": "1", "USDOF_GAME_DUNGEON_PASSIVE_OBJECT_DROP": "1", "USDOF_GAME_DUNGEON_AREA_MATERIAL_DROP": "0"}):
            gold.return_value.roll_amount.return_value = 1
            for preload, index, entries, expected in (("0", 7, bindings, [3341]), ("0", 0, bindings, []),
                                                       ("0", 0, [item()], []), ("1", 7, bindings, [])):
                with self.subTest(preload=preload, index=index), patch.dict(os.environ, {"USDOF_GAME_DUNGEON_PASSIVE_OBJECT_ITEM": preload}):
                    result, _ = drop_generator.generate_passive_object_drops(
                        object_level=90, difficulty=0, object_code=10673, object_index=index,
                        special_items=entries, lcg=DnfLcg(7), slot_counter=0)
                    self.assertEqual([d.template_id for d in result.drops], expected)

    def test_actor_preload_forwards_resolved_group_for_current_maze(self):
        from game.drop.models import MonsterDropResult
        d = qi.DungeonFileInfo(dungeon_id=104, path="", special_passive_object_items=[item()],
                              mazes=[qi.MazeInfo(index=1, special_passive_object_items=[item(0, 10158700)])])
        s = session()
        s.in_dungeon_run = True
        s.dungeon_id = 104
        s.dungeon_maze_index = 1
        s.dungeon_room_mons = [actor(7, code=10673, type=9)]
        with patch.object(room_drop_preload, "get_quest_index", return_value=SimpleNamespace(get_dungeon=lambda _: d)), \
             patch.object(room_drop_preload, "generate_passive_object_drop_result", return_value=MonsterDropResult()) as generate:
            stats = room_drop_preload.preload_visible_room_drops(SimpleNamespace(), s)
        self.assertEqual(stats.errors, 0)
        self.assertEqual(generate.call_count, 1)
        kwargs = generate.call_args.kwargs
        self.assertEqual(kwargs["object_index"], 7)
        self.assertEqual([(r["object_index"], r["drop_group_index"], r["item_id"]) for r in kwargs["special_items"]],
                         [(7, 0, 10158700)])

    def test_real_move_map_binds_each_ore_and_revisit_does_not_reroll(self):
        import tempfile
        from pathlib import Path
        from game.character.character_db import CharacterRepository
        from game.game_server import GameServer
        from game.game_codec import codec1_seed0_decode
        enter = load_chain("flow-dungeon_enter.py")
        move = load_chain("opcode002d-move_map.py")
        state = load_chain("flow-enter_game.py").GameSessionState
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "move.db"
            repo = CharacterRepository(db, bootstrap_from_reference=False)
            repo.ensure_account(1)
            repo.create_character(account_id=1, character_id=0xE2B3, name="AAAA", job=12, grow_type=0, level=90)
            with patch.dict(os.environ, {"USDOF_GAME_DUNGEON_ENTER_HANDLE": "1", "USDOF_GAME_DUNGEON_ENTER_GRANT": "1",
                                        "USDOF_GAME_DUNGEON_MOVE_MAP_HANDLE": "1", "USDOF_DUNGEON_MONSTERS": "0",
                                        "USDOF_GAME_DUNGEON_PASSIVE_OBJECT_ITEM": "1", "USDOF_GAME_PLAIN_WIRE": "0"}):
                server = GameServer(dynamic_userinfo=True, db_path=str(db))
                s = state()
                s.bind_selected_character(character_id=0xE2B3, slot=0, name="AAAA")
                s.dungeon_party_preloaded = True
                s.in_dungeon_select = True
                packets, note = server._handle_select_dungeon_0010(
                    (104).to_bytes(4, "little") + bytes(5) + b"\xff\xff" + bytes(6), session=s)
                self.assertTrue(s.in_dungeon_run, note)
                origin = (s.dungeon_start_x, s.dungeon_start_y)
                d = qi.get_quest_index().get_dungeon(104)
                maze = d.mazes[s.dungeon_maze_index]
                items = qi.passive_object_items_for_maze(d, s.dungeon_maze_index)
                target = None
                for spec in maze.map_specs:
                    xy = (int(spec["x"]), int(spec["y"]))
                    if xy == origin:
                        continue
                    mids = spec.get("candidates") or [spec.get("map_id")]
                    if mids and mids[0] and drop_service.bind_passive_object_item_entries(items, mm.load_map_passive_objects(mids[0])):
                        target = xy
                        break
                self.assertIsNotNone(target)
                service = move.MoveMap002DService(server)
                with patch.object(drop_service, "generate_passive_object_item_entries", wraps=drop_service.generate_passive_object_item_entries) as generate, \
                     patch.object(drop_service, "roll_weighted_occurrences", return_value=1):
                    first, note = service.handle(bytes(target), session=s)
                    self.assertEqual(len(first), 1, note)
                    self.assertEqual(generate.call_count, 1, note)
                    passed = generate.call_args.kwargs["passive_objects"]
                    expected = drop_service.bind_passive_object_item_entries(items, passed)
                    self.assertGreater(len(expected), 0)
                    body = codec1_seed0_decode(first[0][0x10:])
                    self.assertEqual(body[18], 0)  # no ordinary monster rows in this fixture
                    self.assertEqual(body[19], len(expected))
                    actual = [struct.unpack_from("<BIIIHBHHB", body, 20 + 21 * n) for n in range(body[19])]
                    self.assertEqual([(r[0], r[2]) for r in actual], [(r["object_index"], r["item_id"]) for r in expected])
                    service.handle(bytes(origin), session=s)
                    again, note = service.handle(bytes(target), session=s)
                    self.assertEqual(generate.call_count, 1, note)
                    self.assertIn("room_revisit=1", note)


if __name__ == "__main__":
    unittest.main()
