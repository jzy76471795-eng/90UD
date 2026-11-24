"""SQLite transactions for Stage3 title-book custom storage slots."""
from __future__ import annotations

import threading
from typing import Any, Mapping

from game.character.amplify_option import parse_amplify_state
from game.character.character_db import (
    TITLE_SOURCE_BAG,
    TITLE_SOURCE_TITLEBOOK,
    _userinfo_equipment_raw_entry,
)
from game.character.enchant_by_bead import parse_enchant_state


_SCHEMA_LOCK = threading.RLock()
_MAIN_EQUIPMENT_SLOT_MIN = 9
_MAIN_EQUIPMENT_SLOT_MAX = 64
_PENDING_MAIN_TTL_SECONDS = 120


class TitleBookCustomRepository:
    """Move complete title instances between Main and generic title-book slots.

    ``character_titlebook_items`` remains the 0166 projection. The companion
    table retains fields that 0166 does not carry so a later 019D restores the
    same inventory instance instead of creating a reduced replacement.
    """

    def __init__(self, character_repo: Any) -> None:
        self.character_repo = character_repo
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        if bool(getattr(self.character_repo, "_titlebook_custom_schema_ready", False)):
            return
        with _SCHEMA_LOCK:
            if bool(
                getattr(self.character_repo, "_titlebook_custom_schema_ready", False)
            ):
                return
            ensure = getattr(self.character_repo, "_ensure_schema", None)
            if callable(ensure):
                ensure()
            with self.character_repo.open() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS character_titlebook_custom_items (
                        character_id INTEGER NOT NULL,
                        category INTEGER NOT NULL,
                        book_index INTEGER NOT NULL,
                        item_uid INTEGER NOT NULL,
                        item_template_id INTEGER NOT NULL,
                        item_kind TEXT NOT NULL,
                        stack_count INTEGER NOT NULL,
                        instance_value INTEGER NOT NULL,
                        durability INTEGER NOT NULL,
                        seal_flag INTEGER NOT NULL,
                        option_value INTEGER NOT NULL,
                        expire_time INTEGER NOT NULL,
                        marker_16 INTEGER NOT NULL,
                        pet_serial_or_handle INTEGER NOT NULL,
                        sort_lock_flag INTEGER NOT NULL,
                        equipment_lock_id INTEGER NOT NULL,
                        extra_json TEXT NOT NULL,
                        original_created_at TEXT NOT NULL,
                        original_updated_at TEXT NOT NULL,
                        stored_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (character_id, category, book_index),
                        UNIQUE (item_uid),
                        FOREIGN KEY (character_id) REFERENCES characters(character_id)
                            ON DELETE CASCADE
                    );

                    CREATE TABLE IF NOT EXISTS character_titlebook_equipped_custom_items (
                        character_id INTEGER NOT NULL,
                        equip_slot INTEGER NOT NULL,
                        item_uid INTEGER NOT NULL,
                        item_template_id INTEGER NOT NULL,
                        item_kind TEXT NOT NULL,
                        stack_count INTEGER NOT NULL,
                        instance_value INTEGER NOT NULL,
                        durability INTEGER NOT NULL,
                        seal_flag INTEGER NOT NULL,
                        option_value INTEGER NOT NULL,
                        expire_time INTEGER NOT NULL,
                        marker_16 INTEGER NOT NULL,
                        pet_serial_or_handle INTEGER NOT NULL,
                        sort_lock_flag INTEGER NOT NULL,
                        equipment_lock_id INTEGER NOT NULL,
                        extra_json TEXT NOT NULL,
                        original_created_at TEXT NOT NULL,
                        original_updated_at TEXT NOT NULL,
                        equipped_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (character_id, equip_slot),
                        UNIQUE (item_uid),
                        FOREIGN KEY (character_id) REFERENCES characters(character_id)
                            ON DELETE CASCADE
                    );

                    CREATE TABLE IF NOT EXISTS character_titlebook_pending_main_titles (
                        character_id INTEGER NOT NULL,
                        equip_slot INTEGER NOT NULL,
                        main_slot INTEGER NOT NULL,
                        item_uid INTEGER NOT NULL,
                        item_id INTEGER NOT NULL,
                        staged_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (character_id, equip_slot),
                        UNIQUE (item_uid),
                        FOREIGN KEY (character_id) REFERENCES characters(character_id)
                            ON DELETE CASCADE
                    );
                    """
                )
            try:
                setattr(self.character_repo, "_titlebook_custom_schema_ready", True)
            except Exception:
                pass

    @staticmethod
    def _valid_main_equipment_slot(slot_index: int) -> bool:
        slot = int(slot_index)
        return _MAIN_EQUIPMENT_SLOT_MIN <= slot <= _MAIN_EQUIPMENT_SLOT_MAX

    @staticmethod
    def _inventory_row_at(conn: Any, character_id: int, slot_index: int) -> Any:
        rows = conn.execute(
            "SELECT item_uid, owner_scope, owner_id, character_id, list_type, "
            "slot_index, item_template_id, item_kind, stack_count, instance_value, "
            "durability, seal_flag, option_value, expire_time, marker_16, "
            "pet_serial_or_handle, sort_lock_flag, equipment_lock_id, extra_json, "
            "created_at, updated_at "
            "FROM character_items "
            "WHERE character_id = ? AND list_type = 0 AND slot_index = ? "
            "ORDER BY item_uid ASC",
            (int(character_id), int(slot_index)),
        ).fetchall()
        return rows[0] if len(rows) == 1 else None

    @staticmethod
    def _custom_row_at(
        conn: Any,
        character_id: int,
        category: int,
        book_index: int,
    ) -> Any:
        return conn.execute(
            "SELECT character_id, category, book_index, item_uid, item_template_id, "
            "item_kind, stack_count, instance_value, durability, seal_flag, "
            "option_value, expire_time, marker_16, pet_serial_or_handle, "
            "sort_lock_flag, equipment_lock_id, extra_json, original_created_at, "
            "original_updated_at "
            "FROM character_titlebook_custom_items "
            "WHERE character_id = ? AND category = ? AND book_index = ?",
            (int(character_id), int(category), int(book_index)),
        ).fetchone()

    @staticmethod
    def _equipped_custom_row_at(
        conn: Any,
        character_id: int,
        equip_slot: int,
    ) -> Any:
        return conn.execute(
            "SELECT character_id, equip_slot, item_uid, item_template_id, "
            "item_kind, stack_count, instance_value, durability, seal_flag, "
            "option_value, expire_time, marker_16, pet_serial_or_handle, "
            "sort_lock_flag, equipment_lock_id, extra_json, original_created_at, "
            "original_updated_at "
            "FROM character_titlebook_equipped_custom_items "
            "WHERE character_id = ? AND equip_slot = ?",
            (int(character_id), int(equip_slot)),
        ).fetchone()

    @staticmethod
    def _insert_inventory_row(
        conn: Any,
        *,
        character_id: int,
        slot_index: int,
        stored: Mapping[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO character_items ("
            "item_uid, owner_scope, owner_id, character_id, list_type, slot_index, "
            "item_template_id, item_kind, stack_count, instance_value, durability, "
            "seal_flag, option_value, expire_time, marker_16, pet_serial_or_handle, "
            "sort_lock_flag, equipment_lock_id, extra_json, created_at, updated_at"
            ") VALUES (?, 'character', ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(stored["item_uid"]),
                int(character_id),
                int(character_id),
                int(slot_index),
                int(stored["item_template_id"]),
                str(stored["item_kind"] or "equipment"),
                int(stored["stack_count"] or 0),
                int(stored["instance_value"] or 0),
                int(stored["durability"] or 0),
                int(stored["seal_flag"] or 0),
                int(stored["option_value"] or 0),
                int(stored["expire_time"] or 0),
                int(stored["marker_16"] or 0),
                int(stored["pet_serial_or_handle"] or 0),
                1 if int(stored["sort_lock_flag"] or 0) else 0,
                int(stored["equipment_lock_id"] or 0),
                str(stored["extra_json"] or "{}"),
                str(stored["original_created_at"]),
                str(stored["original_updated_at"]),
            ),
        )

    @staticmethod
    def _replace_projection(
        conn: Any,
        *,
        character_id: int,
        category: int,
        book_index: int,
        source: Mapping[str, Any],
    ) -> None:
        extra_json = source["extra_json"]
        enchant_index, enchant_upgrade_count = parse_enchant_state(extra_json)
        amplify_type, amplify_value = parse_amplify_state(extra_json)
        item_id = int(source["item_template_id"])
        conn.execute(
            "INSERT INTO character_titlebook_items ("
            "character_id, category, book_index, item_id, value, attr, durability, "
            "seal_flag, enchant_index, enchant_upgrade_count, amplify_type, "
            "amplify_value, source_quest_id, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP) "
            "ON CONFLICT(character_id, category, book_index) DO UPDATE SET "
            "item_id=excluded.item_id, value=excluded.value, attr=excluded.attr, "
            "durability=excluded.durability, seal_flag=excluded.seal_flag, "
            "enchant_index=excluded.enchant_index, "
            "enchant_upgrade_count=excluded.enchant_upgrade_count, "
            "amplify_type=excluded.amplify_type, amplify_value=excluded.amplify_value, "
            "source_quest_id=0, updated_at=CURRENT_TIMESTAMP",
            (
                int(character_id),
                int(category),
                int(book_index),
                item_id,
                int(source["instance_value"] or item_id),
                int(source["option_value"] or 0) & 0xFF,
                int(source["durability"] or 0) & 0xFFFF,
                int(source["seal_flag"] or 0) & 0xFF,
                int(enchant_index) & 0xFFFFFFFF,
                int(enchant_upgrade_count) & 0xFF,
                int(amplify_type) & 0xFF,
                int(amplify_value) & 0xFFFF,
            ),
        )

    @staticmethod
    def _replace_custom_row(
        conn: Any,
        *,
        character_id: int,
        category: int,
        book_index: int,
        source: Mapping[str, Any],
    ) -> None:
        source_keys = set(source.keys())
        created_at = (
            source["created_at"]
            if "created_at" in source_keys
            else source["original_created_at"]
        )
        updated_at = (
            source["updated_at"]
            if "updated_at" in source_keys
            else source["original_updated_at"]
        )
        conn.execute(
            "INSERT INTO character_titlebook_custom_items ("
            "character_id, category, book_index, item_uid, item_template_id, item_kind, "
            "stack_count, instance_value, durability, seal_flag, option_value, "
            "expire_time, marker_16, pet_serial_or_handle, sort_lock_flag, "
            "equipment_lock_id, extra_json, original_created_at, original_updated_at, "
            "stored_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "CURRENT_TIMESTAMP) "
            "ON CONFLICT(character_id, category, book_index) DO UPDATE SET "
            "item_uid=excluded.item_uid, item_template_id=excluded.item_template_id, "
            "item_kind=excluded.item_kind, stack_count=excluded.stack_count, "
            "instance_value=excluded.instance_value, durability=excluded.durability, "
            "seal_flag=excluded.seal_flag, option_value=excluded.option_value, "
            "expire_time=excluded.expire_time, marker_16=excluded.marker_16, "
            "pet_serial_or_handle=excluded.pet_serial_or_handle, "
            "sort_lock_flag=excluded.sort_lock_flag, "
            "equipment_lock_id=excluded.equipment_lock_id, extra_json=excluded.extra_json, "
            "original_created_at=excluded.original_created_at, "
            "original_updated_at=excluded.original_updated_at, stored_at=CURRENT_TIMESTAMP",
            (
                int(character_id),
                int(category),
                int(book_index),
                int(source["item_uid"]),
                int(source["item_template_id"]),
                str(source["item_kind"] or "equipment"),
                int(source["stack_count"] or 0),
                int(source["instance_value"] or 0),
                int(source["durability"] or 0),
                int(source["seal_flag"] or 0),
                int(source["option_value"] or 0),
                int(source["expire_time"] or 0),
                int(source["marker_16"] or 0),
                int(source["pet_serial_or_handle"] or 0),
                1 if int(source["sort_lock_flag"] or 0) else 0,
                int(source["equipment_lock_id"] or 0),
                str(source["extra_json"] or "{}"),
                str(created_at),
                str(updated_at),
            ),
        )

    @staticmethod
    def _replace_equipped_custom_row(
        conn: Any,
        *,
        character_id: int,
        equip_slot: int,
        source: Mapping[str, Any],
    ) -> None:
        source_keys = set(source.keys())
        created_at = (
            source["created_at"]
            if "created_at" in source_keys
            else source["original_created_at"]
        )
        updated_at = (
            source["updated_at"]
            if "updated_at" in source_keys
            else source["original_updated_at"]
        )
        conn.execute(
            "INSERT INTO character_titlebook_equipped_custom_items ("
            "character_id, equip_slot, item_uid, item_template_id, item_kind, "
            "stack_count, instance_value, durability, seal_flag, option_value, "
            "expire_time, marker_16, pet_serial_or_handle, sort_lock_flag, "
            "equipment_lock_id, extra_json, original_created_at, original_updated_at, "
            "equipped_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "CURRENT_TIMESTAMP) "
            "ON CONFLICT(character_id, equip_slot) DO UPDATE SET "
            "item_uid=excluded.item_uid, item_template_id=excluded.item_template_id, "
            "item_kind=excluded.item_kind, stack_count=excluded.stack_count, "
            "instance_value=excluded.instance_value, durability=excluded.durability, "
            "seal_flag=excluded.seal_flag, option_value=excluded.option_value, "
            "expire_time=excluded.expire_time, marker_16=excluded.marker_16, "
            "pet_serial_or_handle=excluded.pet_serial_or_handle, "
            "sort_lock_flag=excluded.sort_lock_flag, "
            "equipment_lock_id=excluded.equipment_lock_id, extra_json=excluded.extra_json, "
            "original_created_at=excluded.original_created_at, "
            "original_updated_at=excluded.original_updated_at, "
            "equipped_at=CURRENT_TIMESTAMP",
            (
                int(character_id),
                int(equip_slot),
                int(source["item_uid"]),
                int(source["item_template_id"]),
                str(source["item_kind"] or "equipment"),
                int(source["stack_count"] or 0),
                int(source["instance_value"] or 0),
                int(source["durability"] or 0),
                int(source["seal_flag"] or 0),
                int(source["option_value"] or 0),
                int(source["expire_time"] or 0),
                int(source["marker_16"] or 0),
                int(source["pet_serial_or_handle"] or 0),
                1 if int(source["sort_lock_flag"] or 0) else 0,
                int(source["equipment_lock_id"] or 0),
                str(source["extra_json"] or "{}"),
                str(created_at),
                str(updated_at),
            ),
        )

    def prepare_equipped_custom_for_main(
        self,
        character_id: int,
        *,
        equip_slot: int,
    ) -> tuple[bool, str, dict[str, int]]:
        """Make legacy equipped custom titles follow the normal 0013 unload path."""
        cid = int(character_id)
        wear = int(equip_slot)
        result = {"prepared": 0, "legacy_migrated": 0, "item_id": 0}
        with self.character_repo.open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            worn = conn.execute(
                "SELECT item_id, COALESCE(title_source, 0), "
                "COALESCE(title_category, -1), COALESCE(title_book_index, -1) "
                "FROM character_equipped_entries "
                "WHERE character_id = ? AND slot = ?",
                (cid, wear),
            ).fetchone()
            if worn is None:
                return True, "custom_title_prepare_empty", result

            iid = int(worn["item_id"] or 0) & 0xFFFFFFFF
            result["item_id"] = iid
            equipped_custom = self._equipped_custom_row_at(conn, cid, wear)
            if equipped_custom is not None:
                stored_id = int(equipped_custom["item_template_id"] or 0) & 0xFFFFFFFF
                if stored_id != iid:
                    return False, "equipped_custom_companion_mismatch", result
                conn.execute(
                    "UPDATE character_equipped_entries "
                    "SET title_source = ?, title_category = -1, title_book_index = -1 "
                    "WHERE character_id = ? AND slot = ?",
                    (TITLE_SOURCE_BAG, cid, wear),
                )
                result["prepared"] = 1
                return True, "equipped_custom_ready_for_main", result

            source = int(worn[1] or 0)
            category = int(worn[2] if worn[2] is not None else -1)
            book_index = int(worn[3] if worn[3] is not None else -1)
            if source != TITLE_SOURCE_TITLEBOOK or category < 0 or book_index < 0:
                return True, "equipped_title_not_custom", result

            legacy = self._custom_row_at(conn, cid, category, book_index)
            if legacy is None:
                return True, "equipped_titlebook_fixed", result
            if int(legacy["item_template_id"] or 0) & 0xFFFFFFFF != iid:
                return False, "legacy_custom_companion_mismatch", result

            projection = conn.execute(
                "SELECT item_id FROM character_titlebook_items "
                "WHERE character_id = ? AND category = ? AND book_index = ?",
                (cid, category, book_index),
            ).fetchone()
            if projection is not None and int(projection["item_id"] or 0) & 0xFFFFFFFF != iid:
                return False, "legacy_custom_projection_mismatch", result

            self._replace_equipped_custom_row(
                conn,
                character_id=cid,
                equip_slot=wear,
                source=legacy,
            )
            conn.execute(
                "DELETE FROM character_titlebook_custom_items "
                "WHERE character_id = ? AND category = ? AND book_index = ?",
                (cid, category, book_index),
            )
            conn.execute(
                "DELETE FROM character_titlebook_items "
                "WHERE character_id = ? AND category = ? AND book_index = ?",
                (cid, category, book_index),
            )
            conn.execute(
                "UPDATE character_equipped_entries "
                "SET title_source = ?, title_category = -1, title_book_index = -1 "
                "WHERE character_id = ? AND slot = ?",
                (TITLE_SOURCE_BAG, cid, wear),
            )
            result["prepared"] = 1
            result["legacy_migrated"] = 1
            return True, "legacy_equipped_custom_migrated", result

    def capture_unequipped_main_title(
        self,
        character_id: int,
        *,
        equip_slot: int,
        main_slot: int,
        item_id: int,
    ) -> tuple[bool, str, dict[str, int]]:
        """Bind the exact Main item created by a successful title 0013 unload."""
        cid = int(character_id)
        wear = int(equip_slot)
        slot = int(main_slot)
        iid = int(item_id) & 0xFFFFFFFF
        result = {
            "captured": 0,
            "full_instance_restored": 0,
            "item_id": iid,
            "main_slot": slot,
            "item_uid": 0,
        }
        with self.character_repo.open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            bag = self._inventory_row_at(conn, cid, slot)
            if bag is None or int(bag["item_template_id"] or 0) & 0xFFFFFFFF != iid:
                conn.execute(
                    "DELETE FROM character_titlebook_pending_main_titles "
                    "WHERE character_id = ? AND equip_slot = ?",
                    (cid, wear),
                )
                return True, "title_0013_no_main_item", result

            equipped_custom = self._equipped_custom_row_at(conn, cid, wear)
            if equipped_custom is not None:
                stored_id = int(equipped_custom["item_template_id"] or 0) & 0xFFFFFFFF
                if stored_id != iid:
                    return False, "unequipped_custom_companion_mismatch", result
                original_uid = int(equipped_custom["item_uid"])
                uid_conflict = conn.execute(
                    "SELECT item_uid FROM character_items WHERE item_uid = ?",
                    (original_uid,),
                ).fetchone()
                if uid_conflict is not None and int(uid_conflict["item_uid"]) != int(
                    bag["item_uid"]
                ):
                    return False, "unequipped_custom_uid_conflict", result
                conn.execute(
                    "DELETE FROM character_items WHERE item_uid = ?",
                    (int(bag["item_uid"]),),
                )
                self._insert_inventory_row(
                    conn,
                    character_id=cid,
                    slot_index=slot,
                    stored=equipped_custom,
                )
                conn.execute(
                    "DELETE FROM character_titlebook_equipped_custom_items "
                    "WHERE character_id = ? AND equip_slot = ?",
                    (cid, wear),
                )
                bag = self._inventory_row_at(conn, cid, slot)
                if bag is None:
                    return False, "custom_main_restore_missing", result
                result["full_instance_restored"] = 1

            item_uid = int(bag["item_uid"])
            conn.execute(
                "DELETE FROM character_titlebook_pending_main_titles WHERE item_uid = ?",
                (item_uid,),
            )
            conn.execute(
                "INSERT INTO character_titlebook_pending_main_titles ("
                "character_id, equip_slot, main_slot, item_uid, item_id, staged_at"
                ") VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(character_id, equip_slot) DO UPDATE SET "
                "main_slot=excluded.main_slot, item_uid=excluded.item_uid, "
                "item_id=excluded.item_id, staged_at=CURRENT_TIMESTAMP",
                (cid, wear, slot, item_uid, iid),
            )
            result["captured"] = 1
            result["item_uid"] = item_uid
            return True, "title_0013_main_captured", result

    def equip_from_custom(
        self,
        character_id: int,
        *,
        equip_slot: int,
        item_id: int,
        category: int,
        book_index: int,
    ) -> tuple[bool, str, dict[str, int]]:
        """Equip a custom title and stage the preceding 0013 item for real 019C."""
        cid = int(character_id)
        wear = int(equip_slot)
        iid = int(item_id) & 0xFFFFFFFF
        cat = int(category)
        idx = int(book_index)
        result = {
            "item_space": 3,
            "slot": wear,
            "item_id": iid,
            "category": cat,
            "book_index": idx,
            "equipment_changed": 0,
            "pending_main_relocation": 0,
            "swapped_main_slot": -1,
            "swapped_main_item_id": 0,
            "fixed_title_returned": 0,
        }
        if iid <= 0:
            return False, "invalid_custom_title", result

        with self.character_repo.open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            parent = conn.execute(
                "SELECT 1 FROM characters WHERE character_id = ? AND delete_flag = 0",
                (cid,),
            ).fetchone()
            if parent is None:
                return False, "character_missing", result

            projection = conn.execute(
                "SELECT item_id FROM character_titlebook_items "
                "WHERE character_id = ? AND category = ? AND book_index = ?",
                (cid, cat, idx),
            ).fetchone()
            source = self._custom_row_at(conn, cid, cat, idx)
            if (
                projection is None
                or source is None
                or int(projection["item_id"] or 0) & 0xFFFFFFFF != iid
                or int(source["item_template_id"] or 0) & 0xFFFFFFFF != iid
            ):
                return False, "titlebook_custom_miss", result

            existing_companion = self._equipped_custom_row_at(conn, cid, wear)
            if existing_companion is not None:
                return False, "equipped_custom_companion_occupied", result

            worn = conn.execute(
                "SELECT item_id, COALESCE(title_source, 0), "
                "COALESCE(title_category, -1), COALESCE(title_book_index, -1) "
                "FROM character_equipped_entries "
                "WHERE character_id = ? AND slot = ?",
                (cid, wear),
            ).fetchone()
            if worn is not None:
                worn_id = int(worn["item_id"] or 0) & 0xFFFFFFFF
                worn_source = int(worn[1] or 0)
                worn_cat = int(worn[2] if worn[2] is not None else -1)
                worn_idx = int(worn[3] if worn[3] is not None else -1)
                if worn_source != TITLE_SOURCE_TITLEBOOK or worn_cat < 0 or worn_idx < 0:
                    return False, "custom_equip_slot_occupied item=%s" % worn_id, result
                ok_return, return_note = self.character_repo._return_worn_title_to_titlebook_tx(
                    conn,
                    character_id=cid,
                    wear_slot=wear,
                    item_id=worn_id,
                    category=worn_cat,
                    book_index=worn_idx,
                )
                if not ok_return:
                    return False, "custom_swap_return_fail %s" % return_note, result
                result["fixed_title_returned"] = worn_id

            pending = conn.execute(
                "SELECT main_slot, item_uid, item_id, "
                "CASE WHEN datetime(staged_at) >= datetime('now', ?) "
                "THEN 1 ELSE 0 END AS is_fresh "
                "FROM character_titlebook_pending_main_titles "
                "WHERE character_id = ? AND equip_slot = ?",
                ("-%s seconds" % _PENDING_MAIN_TTL_SECONDS, cid, wear),
            ).fetchone()
            pending_bag = None
            if pending is not None:
                pending_bag = self._inventory_row_at(conn, cid, int(pending["main_slot"]))
                if (
                    pending_bag is None
                    or int(pending_bag["item_uid"]) != int(pending["item_uid"])
                    or int(pending_bag["item_template_id"] or 0) & 0xFFFFFFFF
                    != int(pending["item_id"] or 0) & 0xFFFFFFFF
                    or not int(pending["is_fresh"] or 0)
                ):
                    conn.execute(
                        "DELETE FROM character_titlebook_pending_main_titles "
                        "WHERE character_id = ? AND equip_slot = ?",
                        (cid, wear),
                    )
                    pending = None
                    pending_bag = None

            self._replace_equipped_custom_row(
                conn,
                character_id=cid,
                equip_slot=wear,
                source=source,
            )
            conn.execute(
                "DELETE FROM character_titlebook_custom_items "
                "WHERE character_id = ? AND category = ? AND book_index = ?",
                (cid, cat, idx),
            )
            conn.execute(
                "DELETE FROM character_titlebook_items "
                "WHERE character_id = ? AND category = ? AND book_index = ?",
                (cid, cat, idx),
            )

            if pending is not None and pending_bag is not None:
                deleted = conn.execute(
                    "DELETE FROM character_items WHERE item_uid = ?",
                    (int(pending_bag["item_uid"]),),
                )
                if int(deleted.rowcount or 0) != 1:
                    return False, "pending_main_delete_failed", result
                self._replace_projection(
                    conn,
                    character_id=cid,
                    category=cat,
                    book_index=idx,
                    source=pending_bag,
                )
                self._replace_custom_row(
                    conn,
                    character_id=cid,
                    category=cat,
                    book_index=idx,
                    source=pending_bag,
                )
                result["pending_main_relocation"] = 1
                result["swapped_main_slot"] = int(pending["main_slot"])
                result["swapped_main_item_id"] = int(
                    pending_bag["item_template_id"] or 0
                ) & 0xFFFFFFFF

            expire_time = int(source["expire_time"] or 0) & 0xFFFFFFFF
            raw_blob = _userinfo_equipment_raw_entry(wear, iid, expire_time)
            conn.execute(
                "INSERT INTO character_equipped_entries ("
                "character_id, slot, item_id, expire_time, equipment_lock_id, raw_entry, "
                "title_source, title_category, title_book_index, extra_json, "
                "option_value, marker_16"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, -1, -1, ?, ?, ?)",
                (
                    cid,
                    wear,
                    iid,
                    expire_time,
                    int(source["equipment_lock_id"] or 0) & 0xFF,
                    raw_blob,
                    TITLE_SOURCE_BAG,
                    str(source["extra_json"] or "{}"),
                    int(source["option_value"] or 0) & 0xFF,
                    int(source["marker_16"] or 0) & 0xFFFFFFFF,
                ),
            )

        result["equipment_changed"] = 1
        transition_note = ""
        if int(result["pending_main_relocation"]):
            transition_note = " pending_main=%s@%s" % (
                result["swapped_main_item_id"],
                result["swapped_main_slot"],
            )
        return True, "custom_title_equipped item=%s cat=%s idx=%s%s" % (
            iid,
            cat,
            idx,
            transition_note,
        ), result

    def _put_pending_from_missing_main_tx(
        self,
        conn: Any,
        *,
        character_id: int,
        source_slot: int,
        item_id: int,
        category: int,
        book_index: int,
        result: dict[str, int],
    ) -> tuple[bool, str, dict[str, int]]:
        """Consume a real 019C whose Main source only remains in Stage3 UI state."""
        cid = int(character_id)
        slot = int(source_slot)
        iid = int(item_id) & 0xFFFFFFFF
        cat = int(category)
        idx = int(book_index)
        pending_rows = conn.execute(
            "SELECT equip_slot, main_slot, item_uid, item_id, staged_at, "
            "CASE WHEN datetime(staged_at) >= datetime('now', ?) "
            "THEN 1 ELSE 0 END AS is_fresh "
            "FROM character_titlebook_pending_main_titles "
            "WHERE character_id = ? AND main_slot = ? AND item_id = ? "
            "ORDER BY equip_slot ASC",
            ("-%s seconds" % _PENDING_MAIN_TTL_SECONDS, cid, slot, iid),
        ).fetchall()
        if not pending_rows:
            return False, "main_source_missing_or_ambiguous", result
        if len(pending_rows) != 1:
            return False, "pending_main_match_ambiguous", result

        pending = pending_rows[0]
        if not int(pending["is_fresh"] or 0):
            conn.execute(
                "DELETE FROM character_titlebook_pending_main_titles "
                "WHERE character_id = ? AND equip_slot = ? AND item_uid = ?",
                (cid, int(pending["equip_slot"]), int(pending["item_uid"])),
            )
            return False, "pending_main_expired", result

        source_rows = conn.execute(
            "SELECT character_id, category, book_index, item_uid, item_template_id, "
            "item_kind, stack_count, instance_value, durability, seal_flag, "
            "option_value, expire_time, marker_16, pet_serial_or_handle, "
            "sort_lock_flag, equipment_lock_id, extra_json, original_created_at, "
            "original_updated_at "
            "FROM character_titlebook_custom_items "
            "WHERE character_id = ? AND item_uid = ? AND item_template_id = ?",
            (cid, int(pending["item_uid"]), iid),
        ).fetchall()
        if len(source_rows) != 1:
            return False, "pending_main_custom_missing_or_ambiguous", result
        pending_custom = source_rows[0]
        source_cat = int(pending_custom["category"])
        source_idx = int(pending_custom["book_index"])
        source_projection = conn.execute(
            "SELECT item_id FROM character_titlebook_items "
            "WHERE character_id = ? AND category = ? AND book_index = ?",
            (cid, source_cat, source_idx),
        ).fetchone()
        if (
            source_projection is None
            or int(source_projection["item_id"] or 0) & 0xFFFFFFFF != iid
        ):
            return False, "pending_main_projection_mismatch", result

        result["pending_main_relocated"] = 1
        result["pending_source_category"] = source_cat
        result["pending_source_book_index"] = source_idx
        if source_cat == cat and source_idx == idx:
            deleted_pending = conn.execute(
                "DELETE FROM character_titlebook_pending_main_titles "
                "WHERE character_id = ? AND equip_slot = ? AND item_uid = ?",
                (cid, int(pending["equip_slot"]), int(pending["item_uid"])),
            )
            if int(deleted_pending.rowcount or 0) != 1:
                return False, "pending_main_delete_failed", result
            result["inventory_changed"] = 1
            if int(pending_custom["equipment_lock_id"] or 0):
                result["item_lock_changed"] = 1
            return True, "pending_main_put_confirmed", result

        existing_book = conn.execute(
            "SELECT item_id FROM character_titlebook_items "
            "WHERE character_id = ? AND category = ? AND book_index = ?",
            (cid, cat, idx),
        ).fetchone()
        existing_custom = self._custom_row_at(conn, cid, cat, idx)
        if existing_book is None and existing_custom is not None:
            return False, "titlebook_target_custom_in_use", result
        if existing_book is not None:
            existing_id = int(existing_book["item_id"] or 0) & 0xFFFFFFFF
            if (
                existing_custom is None
                or int(existing_custom["item_template_id"] or 0) & 0xFFFFFFFF
                != existing_id
            ):
                return False, "titlebook_target_not_custom", result

        deleted_custom = conn.execute(
            "DELETE FROM character_titlebook_custom_items "
            "WHERE character_id = ? AND category = ? AND book_index = ? "
            "AND item_uid = ?",
            (cid, source_cat, source_idx, int(pending["item_uid"])),
        )
        if int(deleted_custom.rowcount or 0) != 1:
            return False, "pending_main_custom_delete_failed", result
        conn.execute(
            "DELETE FROM character_titlebook_items "
            "WHERE character_id = ? AND category = ? AND book_index = ?",
            (cid, source_cat, source_idx),
        )

        if existing_custom is not None:
            conn.execute(
                "DELETE FROM character_titlebook_custom_items "
                "WHERE character_id = ? AND category = ? AND book_index = ?",
                (cid, cat, idx),
            )
            conn.execute(
                "DELETE FROM character_titlebook_items "
                "WHERE character_id = ? AND category = ? AND book_index = ?",
                (cid, cat, idx),
            )
            self._insert_inventory_row(
                conn,
                character_id=cid,
                slot_index=slot,
                stored=existing_custom,
            )
            result["swapped_out_item"] = int(
                existing_custom["item_template_id"] or 0
            ) & 0xFFFFFFFF
            if int(existing_custom["equipment_lock_id"] or 0):
                result["item_lock_changed"] = 1

        self._replace_projection(
            conn,
            character_id=cid,
            category=cat,
            book_index=idx,
            source=pending_custom,
        )
        self._replace_custom_row(
            conn,
            character_id=cid,
            category=cat,
            book_index=idx,
            source=pending_custom,
        )
        deleted_pending = conn.execute(
            "DELETE FROM character_titlebook_pending_main_titles "
            "WHERE character_id = ? AND equip_slot = ? AND item_uid = ?",
            (cid, int(pending["equip_slot"]), int(pending["item_uid"])),
        )
        if int(deleted_pending.rowcount or 0) != 1:
            return False, "pending_main_delete_failed", result
        result["inventory_changed"] = 1
        if int(pending_custom["equipment_lock_id"] or 0):
            result["item_lock_changed"] = 1
        swapped = int(result["swapped_out_item"])
        swap_note = " swap_out=%s" % swapped if swapped else ""
        return (
            True,
            "pending_main_put from cat=%s idx=%s to cat=%s idx=%s%s"
            % (source_cat, source_idx, cat, idx, swap_note),
            result,
        )

    def put_from_main(
        self,
        character_id: int,
        *,
        source_slot: int,
        item_id: int,
        category: int,
        book_index: int,
    ) -> tuple[bool, str, dict[str, int]]:
        cid = int(character_id)
        slot = int(source_slot)
        iid = int(item_id) & 0xFFFFFFFF
        cat = int(category)
        idx = int(book_index)
        result = {
            "item_space": 0,
            "slot": slot,
            "item_id": iid,
            "category": cat,
            "book_index": idx,
            "inventory_changed": 0,
            "equipment_changed": 0,
            "item_lock_changed": 0,
            "swapped_out_item": 0,
            "pending_main_relocated": 0,
            "pending_source_category": -1,
            "pending_source_book_index": -1,
        }
        if iid <= 0 or not self._valid_main_equipment_slot(slot):
            return False, "invalid_main_title_slot", result

        with self.character_repo.open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = self._inventory_row_at(conn, cid, slot)
            if source is None:
                source_count = conn.execute(
                    "SELECT COUNT(*) FROM character_items "
                    "WHERE character_id = ? AND list_type = 0 AND slot_index = ?",
                    (cid, slot),
                ).fetchone()[0]
                if int(source_count or 0) != 0:
                    return False, "main_source_missing_or_ambiguous", result
                return self._put_pending_from_missing_main_tx(
                    conn,
                    character_id=cid,
                    source_slot=slot,
                    item_id=iid,
                    category=cat,
                    book_index=idx,
                    result=result,
                )
            if (
                str(source["owner_scope"]) != "character"
                or int(source["owner_id"]) != cid
                or int(source["item_template_id"]) & 0xFFFFFFFF != iid
            ):
                return False, "main_source_mismatch", result
            if str(source["item_kind"] or "") != "equipment" or int(
                source["stack_count"] or 0
            ) != 1:
                return False, "main_source_not_single_equipment", result

            existing_book = conn.execute(
                "SELECT item_id FROM character_titlebook_items "
                "WHERE character_id = ? AND category = ? AND book_index = ?",
                (cid, cat, idx),
            ).fetchone()
            existing_custom = self._custom_row_at(conn, cid, cat, idx)
            if existing_book is None and existing_custom is not None:
                return False, "titlebook_target_custom_in_use", result
            if existing_book is not None:
                existing_id = int(existing_book[0] or 0) & 0xFFFFFFFF
                if (
                    existing_custom is None
                    or int(existing_custom["item_template_id"]) & 0xFFFFFFFF
                    != existing_id
                ):
                    return False, "titlebook_target_not_custom", result

            deleted = conn.execute(
                "DELETE FROM character_items WHERE item_uid = ?",
                (int(source["item_uid"]),),
            )
            if int(deleted.rowcount or 0) != 1:
                return False, "main_source_delete_failed", result
            conn.execute(
                "DELETE FROM character_titlebook_pending_main_titles "
                "WHERE character_id = ? AND item_uid = ?",
                (cid, int(source["item_uid"])),
            )

            if existing_custom is not None:
                self._insert_inventory_row(
                    conn,
                    character_id=cid,
                    slot_index=slot,
                    stored=existing_custom,
                )
                result["swapped_out_item"] = int(
                    existing_custom["item_template_id"]
                ) & 0xFFFFFFFF
                result["item_lock_changed"] = 1 if int(
                    existing_custom["equipment_lock_id"] or 0
                ) else 0

            self._replace_projection(
                conn,
                character_id=cid,
                category=cat,
                book_index=idx,
                source=source,
            )
            self._replace_custom_row(
                conn,
                character_id=cid,
                category=cat,
                book_index=idx,
                source=source,
            )
            result["inventory_changed"] = 1
            if int(source["equipment_lock_id"] or 0):
                result["item_lock_changed"] = 1

        swapped = int(result["swapped_out_item"])
        swap_note = " swap_out=%s" % swapped if swapped else ""
        return (
            True,
            "title_custom_put main=%s item=%s to cat=%s idx=%s%s"
            % (slot, iid, cat, idx, swap_note),
            result,
        )

    def get_to_main(
        self,
        character_id: int,
        *,
        target_slot: int,
        item_id: int,
        category: int,
        book_index: int,
    ) -> tuple[bool, str, dict[str, int]]:
        cid = int(character_id)
        slot = int(target_slot)
        iid = int(item_id) & 0xFFFFFFFF
        cat = int(category)
        idx = int(book_index)
        result = {
            "item_space": 0,
            "slot": slot,
            "item_id": iid,
            "category": cat,
            "book_index": idx,
            "inventory_changed": 0,
            "equipment_changed": 0,
            "item_lock_changed": 0,
        }
        if iid <= 0 or not self._valid_main_equipment_slot(slot):
            return False, "invalid_main_title_slot", result

        with self.character_repo.open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            target = conn.execute(
                "SELECT 1 FROM character_items "
                "WHERE character_id = ? AND list_type = 0 AND slot_index = ? LIMIT 1",
                (cid, slot),
            ).fetchone()
            if target is not None:
                return False, "main_target_occupied", result

            book = conn.execute(
                "SELECT item_id FROM character_titlebook_items "
                "WHERE character_id = ? AND category = ? AND book_index = ?",
                (cid, cat, idx),
            ).fetchone()
            stored = self._custom_row_at(conn, cid, cat, idx)
            if (
                book is None
                or stored is None
                or int(book[0] or 0) & 0xFFFFFFFF != iid
                or int(stored["item_template_id"]) & 0xFFFFFFFF != iid
            ):
                return False, "titlebook_custom_miss", result
            uid_in_use = conn.execute(
                "SELECT 1 FROM character_items WHERE item_uid = ?",
                (int(stored["item_uid"]),),
            ).fetchone()
            if uid_in_use is not None:
                return False, "titlebook_custom_uid_conflict", result

            self._insert_inventory_row(
                conn,
                character_id=cid,
                slot_index=slot,
                stored=stored,
            )
            conn.execute(
                "DELETE FROM character_titlebook_items "
                "WHERE character_id = ? AND category = ? AND book_index = ?",
                (cid, cat, idx),
            )
            conn.execute(
                "DELETE FROM character_titlebook_custom_items "
                "WHERE character_id = ? AND category = ? AND book_index = ?",
                (cid, cat, idx),
            )
            conn.execute(
                "DELETE FROM character_titlebook_pending_main_titles "
                "WHERE character_id = ? AND item_uid = ?",
                (cid, int(stored["item_uid"])),
            )
            result["inventory_changed"] = 1
            if int(stored["equipment_lock_id"] or 0):
                result["item_lock_changed"] = 1

        return (
            True,
            "title_custom_get cat=%s idx=%s item=%s to main=%s"
            % (cat, idx, iid, slot),
            result,
        )


__all__ = ["TitleBookCustomRepository"]
