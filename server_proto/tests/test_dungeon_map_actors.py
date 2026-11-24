#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""地图 actor 加载：monster / ai，任务明确要求时才加载 passive object。"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from game.chain_loader import load_chain
from game.quest.quest_residual import apply_kill_to_active_quests, match_hunt_kill_channel
from game.quest.quest_index import QuestMeta

mm = load_chain("helper-dungeon_map_monsters.py")
payloads = load_chain("helper-dungeon_payloads.py")


SAMPLE_MAP = """
[monster]
65021 1 0 994 230 0 1 1 `[fixed]` `[normal]`
68004 1 0 798 214 0 1 1 `[fixed]` `[normal]`
[/monster]
[ai character]
6510 1149 250 0 `[monster]` `[boss]` 0 0
[/ai character]
[special passive object]
49014 860 176 0 1 `[item]` 1 3 -1 -1 -1
[/special passive object]
[passive object]
18553 268 274 0
18553 157 221 0
18568 587 317 0
[/passive object]
[NPC]
1000 `[left]` 0 0 0
[/NPC]
"""

# 同房敌对 APC + 队友 APC（通用敌友分流样例）
MIXED_AI_MAP = """
[monster]
65021 1 0 100 200 0 1 1 `[fixed]` `[normal]`
[/monster]
[ai character]
6510 1149 250 0 `[monster]` `[boss]` 0 0
201 300 200 0 `[character]` `[normal]` 0 0
11404 500 200 0 `[neutral]` `[normal]` 0 0
[/ai character]
"""

# [monster team] 0 = 剧情友方怪（不挡门）
ALLY_TEAM_MAP = """
[monster]
100 1 0 10 20 0 1 1 `[fixed]` `[normal]`
200 1 0 30 40 0 1 1 `[fixed]` `[normal]`
300 1 0 50 60 0 1 1 `[fixed]` `[normal]`
[/monster]
[monster team]
100
0
100
"""


class ParseMapActorsTests(unittest.TestCase):
    def test_parse_monster_rows(self) -> None:
        rows = mm.parse_map_monster_tokens(SAMPLE_MAP)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["code"], 65021)
        self.assertTrue(rows[0]["is_blocking"])
        self.assertEqual(rows[0]["quest_enemy_type"], 1)

    def test_standalone_rank_marker_applies_to_next_actor(self) -> None:
        raw = """
[monster]
10001 1 0 100 200 0 1 1 `[fixed]` `[normal]`
`[boss]`
10002 1 0 300 200 0 1 1 `[fixed]` `[normal]`
`[champion]`
10003 1 0 500 200 0 1 1 `[fixed]` `[normal]`
[/monster]
"""
        rows = mm.parse_map_monster_tokens(raw)
        self.assertEqual([row["code"] for row in rows], [10001, 10002, 10003])
        self.assertEqual([row["type"] for row in rows], [0, 3, 1])

    def test_parse_ai_character_boss(self) -> None:
        rows = mm.parse_map_ai_character_tokens(SAMPLE_MAP)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["code"], 6510)
        self.assertEqual(row["type"], 8)  # boss APC
        self.assertEqual(row["faction"], "monster")
        self.assertEqual(row["role"], "enemy")
        self.assertTrue(row["is_hostile"])
        self.assertFalse(row["is_ally"])
        self.assertEqual(row["quest_enemy_type"], 2)
        # 敌对 BOSS APC 当怪物：挡门，未击杀不算清房/通关
        self.assertTrue(row["is_blocking"])
        self.assertTrue(row["grants_exp"])
        self.assertTrue(row["killable"])

    def test_parse_ai_character_ally_vs_enemy(self) -> None:
        rows = mm.parse_map_ai_character_tokens(MIXED_AI_MAP)
        by_code = {r["code"]: r for r in rows}
        enemy = by_code[6510]
        ally = by_code[201]
        neutral = by_code[11404]
        # 敌人
        self.assertEqual(enemy["faction"], "monster")
        self.assertEqual(enemy["role"], "enemy")
        self.assertTrue(enemy["is_hostile"])
        self.assertFalse(enemy["is_ally"])
        self.assertTrue(enemy["killable"])
        self.assertTrue(enemy["grants_exp"])
        self.assertTrue(enemy["is_blocking"])
        self.assertEqual(enemy["quest_enemy_type"], 2)
        # 队友
        self.assertEqual(ally["faction"], "character")
        self.assertEqual(ally["role"], "ally")
        self.assertFalse(ally["is_hostile"])
        self.assertTrue(ally["is_ally"])
        self.assertFalse(ally["killable"])
        self.assertFalse(ally["grants_exp"])
        self.assertFalse(ally["is_blocking"])
        self.assertEqual(ally["quest_enemy_type"], 0)
        # 中立
        self.assertEqual(neutral["faction"], "neutral")
        self.assertEqual(neutral["role"], "neutral")
        self.assertFalse(neutral["is_hostile"])
        self.assertFalse(neutral["is_ally"])
        self.assertFalse(neutral["killable"])
        self.assertFalse(neutral["is_blocking"])
        self.assertEqual(neutral["quest_enemy_type"], 0)

    def test_monster_team_zero_is_ally_non_blocking(self) -> None:
        rows = mm.parse_map_monster_tokens(ALLY_TEAM_MAP)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["team"], 100)
        self.assertTrue(rows[0]["is_hostile"])
        self.assertTrue(rows[0]["is_blocking"])
        # team=0 剧情友方
        self.assertEqual(rows[1]["team"], 0)
        self.assertTrue(rows[1]["is_ally"])
        self.assertFalse(rows[1]["is_hostile"])
        self.assertFalse(rows[1]["is_blocking"])
        self.assertFalse(rows[1]["grants_exp"])
        self.assertEqual(rows[1]["quest_enemy_type"], 0)
        self.assertEqual(rows[2]["team"], 100)
        self.assertTrue(rows[2]["is_blocking"])

    def test_actor_combat_helpers(self) -> None:
        enemy = {"kind": "ai_character", "faction": "monster", "role": "enemy", "is_hostile": True, "is_ally": False, "killable": True}
        ally = {"kind": "ai_character", "faction": "character", "role": "ally", "is_hostile": False, "is_ally": True, "killable": False}
        self.assertTrue(mm.actor_is_hostile(enemy))
        self.assertTrue(mm.actor_counts_for_combat(enemy))
        self.assertTrue(mm.actor_is_ally(ally))
        self.assertFalse(mm.actor_is_hostile(ally))
        self.assertFalse(mm.actor_counts_for_combat(ally))

    def test_parse_special_passive_type9_skips_item_spawn(self) -> None:
        rows = mm.parse_map_special_passive_tokens(SAMPLE_MAP)
        # object 49014 type9 only; [item] spawn 不进 mon 表
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["code"], 49014)
        self.assertEqual(rows[0]["type"], 9)
        self.assertEqual(rows[0]["quest_enemy_type"], 3)
        self.assertFalse(rows[0]["is_blocking"])

    def test_parse_passive_object_ant_eggs(self) -> None:
        """蚁后巢穴 [passive object] 卵/蚁穴 → type9 + hunt enemy type=3。"""
        rows = mm.parse_map_passive_object_tokens(SAMPLE_MAP)
        codes = [r["code"] for r in rows]
        self.assertEqual(codes.count(18553), 2)
        self.assertIn(18568, codes)
        for r in rows:
            self.assertEqual(r["kind"], "passive_object")
            self.assertEqual(r["type"], 9)
            self.assertEqual(r["quest_enemy_type"], 3)
            self.assertTrue(r["killable"])
            self.assertFalse(r["is_blocking"])
            self.assertFalse(r["grants_exp"])
            self.assertTrue(mm.is_passive_actor(r))
            self.assertTrue(mm.actor_counts_for_combat(r))

    def test_parse_special_passive_monster_spawn(self) -> None:
        raw = """
[special passive object]
10001 10 20 0 2 `[monster]` 61801 5 0 0 0 `[monster]` 59013 3 0 0 0
[/special passive object]
"""
        rows = mm.parse_map_special_passive_tokens(raw)
        codes = [r["code"] for r in rows]
        self.assertIn(10001, codes)
        self.assertIn(61801, codes)
        self.assertIn(59013, codes)
        mon_spawn = [r for r in rows if r.get("from_special_passive")]
        self.assertEqual(len(mon_spawn), 2)
        self.assertFalse(mon_spawn[0]["is_blocking"])
        self.assertEqual(mon_spawn[0]["flag0"], 1)

    def test_load_map_emits_passive_objects_only_for_matching_active_task(self) -> None:
        task_meta = QuestMeta(
            quest_id=9001,
            path="n_quest/task_passive.qst",
            quest_type="[hunt enemy]",
            int_data="77 -1 49014 3 1",
        )

        class QuestIndex:
            @staticmethod
            def get_meta(quest_id: int):
                return task_meta if int(quest_id) == 9001 else None

        with patch.object(mm, "resolve_map_path", return_value="map/test.map"):
            with patch.object(mm, "pvf_runtime") as rt:
                rt.read_text.return_value = SAMPLE_MAP
                with patch.object(mm, "resolve_ai_character_level", return_value=17):
                    with patch(
                        "game.quest.quest_index.get_quest_index",
                        return_value=QuestIndex(),
                    ), patch.dict(os.environ, {"USDOF_DUNGEON_MONSTERS": "1"}, clear=False):
                        mons = mm.load_map_monsters(55691, dungeon_basic_lv=16, first_seq=0)
                        task_mons = mm.load_map_monsters(
                            55691,
                            dungeon_basic_lv=16,
                            first_seq=0,
                            active_quest_ids=[9001],
                            dungeon_id=77,
                            difficulty=0,
                        )
        kinds = [m["kind"] for m in mons]
        self.assertIn("monster", kinds)
        self.assertIn("ai_character", kinds)
        self.assertNotIn("special_passive", kinds)
        self.assertNotIn("passive_object", kinds)
        task_kinds = [m["kind"] for m in task_mons]
        self.assertIn("special_passive", task_kinds)
        self.assertNotIn("passive_object", task_kinds)
        ai = next(m for m in mons if m["kind"] == "ai_character")
        self.assertEqual(ai["code"], 6510)
        self.assertEqual(ai["type"], 8)
        self.assertEqual(ai["quest_enemy_type"], 2)
        self.assertEqual(ai["level"], 17)
        self.assertEqual(ai["seq"], 3)  # after 2 monsters
        sp = next(m for m in task_mons if m["kind"] == "special_passive")
        self.assertEqual(sp["type"], 9)
        self.assertEqual(sp["code"], 49014)

    def test_room_blocking_counts_hostile_ai_not_ally_or_special(self) -> None:
        mons = [
            {"is_blocking": True, "killed": False, "kind": "monster"},
            {"is_blocking": True, "killed": False, "kind": "ai_character", "role": "enemy"},
            {"is_blocking": False, "killed": False, "kind": "ai_character", "role": "ally"},
            {"is_blocking": False, "killed": False, "kind": "special_passive"},
        ]
        # 普通怪 + 敌对 APC 挡门；友方 APC / special 不挡
        self.assertEqual(mm.room_blocking_alive(mons), 2)
        mons[0]["killed"] = True
        self.assertEqual(mm.room_blocking_alive(mons), 1)
        mons[1]["killed"] = True
        self.assertEqual(mm.room_blocking_alive(mons), 0)

    def test_hostile_ai_flags_block_room(self) -> None:
        """敌对 APC 解析后 is_blocking，参与 room_blocking_alive。"""
        rows = mm.parse_map_ai_character_tokens(MIXED_AI_MAP)
        # 模拟 load 后 mon 行（带 killed 默认）
        mons = [{**r, "killed": False} for r in rows]
        # 仅敌对 6510 挡门
        self.assertEqual(mm.room_blocking_alive(mons), 1)
        enemy = next(m for m in mons if m["code"] == 6510)
        enemy["killed"] = True
        self.assertEqual(mm.room_blocking_alive(mons), 0)


@unittest.skipUnless(
    os.environ.get("USDOF_TEST_PVF_INTEGRATION", "1") not in {"0", "false", "off"},
    "pvf integration off",
)
class DragonSealStartMapPvfIntegrationTests(unittest.TestCase):
    """Real PVF regression for dungeon 155 / maze 1 / map 58397."""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            from data import PVF_PATH
            from pvf import pvf_runtime

            pvf_runtime.preload_script_pvf(str(PVF_PATH))
            mm.clear_map_actor_template_cache()
            mm.clear_monster_script_cache()
        except Exception as exc:
            raise unittest.SkipTest("pvf preload failed: %r" % (exc,))

    def test_map_58397_excludes_static_passive_objects_by_default(self) -> None:
        rows = mm.load_map_monsters(
            58397,
            dungeon_basic_lv=54,
            first_seq=0x0191,
        )
        expected = [
            (63510, 54, 0),
            (63510, 54, 0),
            (1528, 91, 5),
        ]
        self.assertEqual(
            [(int(row["code"]), int(row["level"]), int(row["type"])) for row in rows],
            expected,
        )

        body = payloads.build_start_map_001d_us(
            cell_x=0,
            cell_y=1,
            map_index=58397,
            monsters=rows,
        )
        self.assertEqual(len(body), 95)
        self.assertEqual(body[18], 3)
        wire_rows = []
        for index in range(body[18]):
            start = 19 + index * 24
            wire_rows.append(
                (
                    int.from_bytes(body[start + 11:start + 15], "little"),
                    body[start + 15],
                    body[start + 16],
                )
            )
        self.assertEqual(wire_rows, expected)

    def test_arden_quest_2284_keeps_the_standalone_boss_actor(self) -> None:
        rows = mm.load_map_monsters(
            57560,
            dungeon_basic_lv=1,
            first_seq=0,
        )
        self.assertEqual(len(rows), 8)
        boss_rows = [row for row in rows if int(row["code"]) == 66318]
        self.assertEqual(len(boss_rows), 1)
        self.assertEqual(boss_rows[0]["type"], 3)
        self.assertTrue(boss_rows[0]["is_blocking"])

        scene_dummy = next(
            row
            for row in rows
            if int(row["code"]) == 63007 and int(row["x"]) == -238
        )
        self.assertTrue(scene_dummy["is_scene_dummy"])
        self.assertFalse(scene_dummy["is_blocking"])


class HuntEnemyAiDomainTests(unittest.TestCase):
    def test_quest_1791_shape_matches_ai_enemy_type(self) -> None:
        # 营救西尔玛: dungeon=149 mon=6510 enemyType=2 count=1
        meta = QuestMeta(
            quest_id=1791,
            path="n_quest/dummy.qst",
            quest_type="[hunt enemy]",
            int_data="149 -1 6510 2 1",
        )
        ch = match_hunt_kill_channel(
            meta, monster_code=6510, dungeon_id=149, difficulty=0, enemy_type=2
        )
        self.assertEqual(ch, 0)
        # wire mon rank 误当 enemyType 时不应命中
        ch_bad = match_hunt_kill_channel(
            meta, monster_code=6510, dungeon_id=149, difficulty=0, enemy_type=8
        )
        self.assertIsNone(ch_bad)
        changed = apply_kill_to_active_quests(
            [(1791, 1)],
            monster_code=6510,
            dungeon_id=149,
            difficulty=0,
            enemy_type=2,
        )
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0][2], 0)


if __name__ == "__main__":
    unittest.main()
