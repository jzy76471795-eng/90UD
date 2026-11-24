# -*- coding: utf-8 -*-
"""Share package save initialization and additive old-save migration.

仅用于 ``server-pack`` 启动器 / 打包脚本，**禁止**改正式服 ``server_proto``。

硬规则：
  - 目标存档不存在时，原子创建并验证已确认的零角色成熟母版
  - 目标存档已存在时，只执行 ``CREATE IF NOT EXISTS``、缺列 ``ALTER`` 与
    默认内容屏蔽规则的 ``INSERT OR IGNORE``
  - 已有存档不覆盖角色、背包或显式解除的内容屏蔽规则
  - 旧存档首次启动时自动补齐与成熟本地库一致的表集合和默认屏蔽规则
  - 新旧存档启动迁移固定使用 WAL + FULL，确保结构变更完整落盘
  - 在线 Game 连接使用 WAL + NORMAL，仍在每个业务操作返回前提交
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

LogFn = Callable[[str], None]

SCHEMA_SQL_NAME = "schema_inventory_full.sql"
DEFAULT_SEED_SQL_NAME = "default_inventory_seed.sql"
CONTENT_BLOCK_DEFAULT_RULES_NAME = "content_block_default_rules.json"
SQLITE_RUNTIME_JOURNAL_MODE = "WAL"
SQLITE_RUNTIME_SYNCHRONOUS = "FULL"
# 相对 server 根
REL_SCHEMA_CANDIDATES = (
    "data/resources/%s" % SCHEMA_SQL_NAME,
    "data/%s" % SCHEMA_SQL_NAME,
    "resources/%s" % SCHEMA_SQL_NAME,
)
REL_DEFAULT_SEED_CANDIDATES = (
    "data/resources/%s" % DEFAULT_SEED_SQL_NAME,
    "data/%s" % DEFAULT_SEED_SQL_NAME,
    "resources/%s" % DEFAULT_SEED_SQL_NAME,
)
REL_CONTENT_BLOCK_DEFAULT_RULES_CANDIDATES = (
    "data/resources/%s" % CONTENT_BLOCK_DEFAULT_RULES_NAME,
    "data/%s" % CONTENT_BLOCK_DEFAULT_RULES_NAME,
    "resources/%s" % CONTENT_BLOCK_DEFAULT_RULES_NAME,
)

# 与正式服 _ensure_columns 对齐的「只加不改」列迁移（旧库缺列时补）
_CHARACTERS_COLUMN_ADDITIONS = {
    "pvp_grade": "INTEGER NOT NULL DEFAULT 0",
    "pvp_rating_grade": "INTEGER NOT NULL DEFAULT 0",
    "user_state": "INTEGER NOT NULL DEFAULT 0",
    "direction": "INTEGER NOT NULL DEFAULT 5",
    "area_state": "INTEGER NOT NULL DEFAULT 3",
    "name_bytes": "BLOB",
    "appearance_blob": "BLOB",
    "delete_flag": "INTEGER NOT NULL DEFAULT 0",
    "exp": "INTEGER NOT NULL DEFAULT 0",
    "ex_equip_slot_stat": "INTEGER NOT NULL DEFAULT 8",
    "bonus_sp": "INTEGER NOT NULL DEFAULT 0",
    "bonus_tp": "INTEGER NOT NULL DEFAULT 0",
    "tutorial_state": "INTEGER NOT NULL DEFAULT 0",
    "tutorial_flags_json": "TEXT NOT NULL DEFAULT '[]'",
}
_ACCOUNTS_CUBE_COLUMNS = (
    "cube_black",
    "cube_white",
    "cube_red",
    "cube_blue",
    "cube_clear",
    "cube_gold",
)
_EQUIPPED_COLUMN_ADDITIONS = {
    "expire_time": "INTEGER NOT NULL DEFAULT 0",
    "equipment_lock_id": "INTEGER NOT NULL DEFAULT 0",
    "raw_entry": "BLOB NOT NULL DEFAULT X''",
    "title_source": "INTEGER NOT NULL DEFAULT 0",
    "title_category": "INTEGER NOT NULL DEFAULT -1",
    "title_book_index": "INTEGER NOT NULL DEFAULT -1",
    "extra_json": "TEXT",
    "option_value": "INTEGER NOT NULL DEFAULT 0",
    "marker_16": "INTEGER NOT NULL DEFAULT 0",
    "clear_avatar_id": "INTEGER NOT NULL DEFAULT 0",
}
_CHARACTER_ITEMS_COLUMN_ADDITIONS = {
    "sort_lock_flag": "INTEGER NOT NULL DEFAULT 0",
    "equipment_lock_id": "INTEGER NOT NULL DEFAULT 0",
}
_ACCOUNT_SETTINGS_COLUMN_ADDITIONS = {
    "main_game_option": "BLOB",
    "tutorial_flags_json": "TEXT NOT NULL DEFAULT '[]'",
    "updated_at": "TEXT",
}

# 空库种子（INSERT OR IGNORE，不覆盖已有）
_SEED_SQL = """
INSERT OR IGNORE INTO accounts (account_id, m_id, password_hash)
VALUES (1, '10038', '');
INSERT OR IGNORE INTO get_userinfo_template (
    id, seed_character_id, pkt0_routing_byte7,
    gate_or_count1, gate_or_count2, flag_or_manage
) VALUES (1, 1000, 1, 32, 32, 5);
INSERT OR IGNORE INTO account_cargo_state
    (account_id, selection_key, value32, item_count)
VALUES (1, 80, 0, 0);
"""


def resolve_schema_sql_path(
    server_home: Path,
    *,
    extra_candidates: Sequence[Path] = (),
) -> Optional[Path]:
    """在分享包 server 目录内查找全量 schema SQL。"""
    for rel in REL_SCHEMA_CANDIDATES:
        p = Path(server_home) / rel
        if p.is_file():
            return p
    for p in extra_candidates:
        if p is not None and Path(p).is_file():
            return Path(p)
    return None


def resolve_default_seed_sql_path(
    server_home: Path,
    *,
    extra_candidates: Sequence[Path] = (),
) -> Optional[Path]:
    """在分享包 server 目录内查找首次建库母版种子。"""
    for rel in REL_DEFAULT_SEED_CANDIDATES:
        p = Path(server_home) / rel
        if p.is_file():
            return p
    for p in extra_candidates:
        if p is not None and Path(p).is_file():
            return Path(p)
    return None


def resolve_content_block_default_rules_path(
    server_home: Path,
    *,
    extra_candidates: Sequence[Path] = (),
) -> Optional[Path]:
    """Locate the share runtime's account-wide content-block seed."""
    for rel in REL_CONTENT_BLOCK_DEFAULT_RULES_CANDIDATES:
        p = Path(server_home) / rel
        if p.is_file():
            return p
    for p in extra_candidates:
        if p is not None and Path(p).is_file():
            return Path(p)
    return None


def _table_columns(conn: sqlite3.Connection, table: str) -> set:
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}
    except sqlite3.Error:
        return set()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _count_user_tables(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
    )


def configure_sqlite_runtime(conn: sqlite3.Connection) -> Tuple[str, str]:
    """Migrate an opened save to the runtime durability profile."""
    if conn.in_transaction:
        conn.commit()
    row = conn.execute(
        "PRAGMA journal_mode=%s" % SQLITE_RUNTIME_JOURNAL_MODE
    ).fetchone()
    journal_mode = str(row[0] if row else "").strip().upper()
    if journal_mode != SQLITE_RUNTIME_JOURNAL_MODE:
        raise RuntimeError(
            "journal_mode mismatch expected=%s actual=%s"
            % (SQLITE_RUNTIME_JOURNAL_MODE, journal_mode or "unknown")
        )

    conn.execute("PRAGMA synchronous=%s" % SQLITE_RUNTIME_SYNCHRONOUS)
    row = conn.execute("PRAGMA synchronous").fetchone()
    synchronous_level = int(row[0]) if row else -1
    if synchronous_level != 2:
        raise RuntimeError(
            "synchronous mismatch expected=2 actual=%s" % synchronous_level
        )

    return journal_mode.lower(), SQLITE_RUNTIME_SYNCHRONOUS.lower()


def _filter_schema_sql_to_structure_only(sql_text: str) -> str:
    """去掉 schema 文件里的 INSERT（种子改由 _SEED_SQL 统一写，避免重复/歧义）。

    仅保留 CREATE TABLE/INDEX 与 PRAGMA。支持多行 INSERT … ; 语句。
    """
    # 按分号切语句，整句丢弃 INSERT
    parts: List[str] = []
    buf: List[str] = []
    for line in sql_text.splitlines():
        buf.append(line)
        if line.rstrip().endswith(";"):
            parts.append("\n".join(buf))
            buf = []
    if buf:
        parts.append("\n".join(buf))

    keep: List[str] = []
    for stmt in parts:
        stripped = stmt.strip()
        if not stripped:
            continue
        # 注释块：若整段只有注释/空行则保留
        code_lines = [
            ln
            for ln in stripped.splitlines()
            if ln.strip() and not ln.strip().startswith("--")
        ]
        if not code_lines:
            keep.append(stmt)
            continue
        first = code_lines[0].lstrip().upper()
        if first.startswith("INSERT "):
            continue
        keep.append(stmt)
    return "\n\n".join(keep) + "\n"


def _load_content_block_default_rules(path: Path) -> List[Tuple[str, int]]:
    """Parse the shared server default rules before touching a player save."""
    rules_path = Path(path)
    try:
        raw = json.loads(rules_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "content block default rules unavailable: %s" % rules_path
        ) from exc
    if not isinstance(raw, dict):
        raise RuntimeError(
            "content block default rules must be a JSON object: %s" % rules_path
        )

    rows: List[Tuple[str, int]] = []
    seen = set()
    for content_type, field_name, maximum in (
        ("quest", "quest_ids", 0xFFFF),
        ("item", "item_ids", 0xFFFFFFFF),
    ):
        values = raw.get(field_name)
        if not isinstance(values, list):
            raise RuntimeError(
                "content block default field %s must be a list: %s"
                % (field_name, rules_path)
            )
        for value in values:
            if isinstance(value, bool):
                raise RuntimeError(
                    "content block default contains a non-numeric ID: %s" % rules_path
                )
            try:
                content_id = int(value)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "content block default contains a non-numeric ID: %s"
                    % rules_path
                ) from exc
            if content_id <= 0 or content_id > maximum:
                raise RuntimeError(
                    "content block default ID is out of range: %s" % rules_path
                )
            row = (content_type, content_id)
            if row not in seen:
                seen.add(row)
                rows.append(row)
    return rows


def apply_content_block_default_rules(
    conn: sqlite3.Connection,
    rules_path: Path,
) -> Tuple[int, int]:
    """Seed base rules for all accounts without replacing player overrides.

    The server-side repository later performs the versioned legacy-rule merge
    and abandons matching active quests.  Keeping this launcher step additive
    prevents an old per-character unblock from being overwritten before that
    merge runs.
    """
    required_tables = (
        "accounts",
        "content_block_default_rules",
        "account_content_block_rules",
    )
    missing = [table for table in required_tables if not _table_exists(conn, table)]
    if missing:
        raise RuntimeError(
            "content block schema missing tables: %s" % ",".join(missing)
        )

    rules = _load_content_block_default_rules(rules_path)
    before_defaults = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO content_block_default_rules "
        "(content_type, content_id, is_blocked) VALUES (?, ?, 1)",
        rules,
    )
    defaults_added = conn.total_changes - before_defaults

    before_accounts = conn.total_changes
    conn.execute(
        """
        INSERT OR IGNORE INTO account_content_block_rules
            (account_id, content_type, content_id, is_blocked)
        SELECT accounts.account_id, defaults.content_type, defaults.content_id, 1
        FROM accounts
        CROSS JOIN content_block_default_rules AS defaults
        WHERE accounts.account_id > 0
          AND defaults.is_blocked = 1
        """
    )
    account_rules_added = conn.total_changes - before_accounts
    return int(defaults_added), int(account_rules_added)


def apply_missing_columns(conn: sqlite3.Connection) -> List[str]:
    """对已有表补缺列；不改已有列类型/默认值，不删列。"""
    notes: List[str] = []

    if _table_exists(conn, "characters"):
        cols = _table_columns(conn, "characters")
        for name, ddl in _CHARACTERS_COLUMN_ADDITIONS.items():
            if name not in cols:
                conn.execute("ALTER TABLE characters ADD COLUMN %s %s" % (name, ddl))
                notes.append("characters.+%s" % name)

    if _table_exists(conn, "accounts"):
        cols = _table_columns(conn, "accounts")
        for cube_col in _ACCOUNTS_CUBE_COLUMNS:
            if cube_col not in cols:
                conn.execute(
                    "ALTER TABLE accounts ADD COLUMN %s INTEGER NOT NULL DEFAULT 0"
                    % cube_col
                )
                notes.append("accounts.+%s" % cube_col)

    if _table_exists(conn, "account_character_entries"):
        cols = _table_columns(conn, "account_character_entries")
        if "name_bytes" not in cols:
            conn.execute(
                "ALTER TABLE account_character_entries ADD COLUMN name_bytes BLOB"
            )
            notes.append("account_character_entries.+name_bytes")

    if _table_exists(conn, "character_equipped_entries"):
        cols = _table_columns(conn, "character_equipped_entries")
        for name, ddl in _EQUIPPED_COLUMN_ADDITIONS.items():
            if name not in cols:
                conn.execute(
                    "ALTER TABLE character_equipped_entries ADD COLUMN %s %s"
                    % (name, ddl)
                )
                notes.append("character_equipped_entries.+%s" % name)

    if _table_exists(conn, "character_items"):
        cols = _table_columns(conn, "character_items")
        for name, ddl in _CHARACTER_ITEMS_COLUMN_ADDITIONS.items():
            if name not in cols:
                conn.execute(
                    "ALTER TABLE character_items ADD COLUMN %s %s" % (name, ddl)
                )
                notes.append("character_items.+%s" % name)

    if _table_exists(conn, "account_settings"):
        cols = _table_columns(conn, "account_settings")
        for name, ddl in _ACCOUNT_SETTINGS_COLUMN_ADDITIONS.items():
            if name not in cols:
                conn.execute(
                    "ALTER TABLE account_settings ADD COLUMN %s %s" % (name, ddl)
                )
                notes.append("account_settings.+%s" % name)

    return notes


def fix_userinfo_template_placeholder_only(conn: sqlite3.Connection) -> List[str]:
    """仅当模板仍是空库占位值时修正 routing/flag/gate；不覆盖用户非 0 取值。"""
    notes: List[str] = []
    if not _table_exists(conn, "get_userinfo_template"):
        return notes
    row = conn.execute(
        """
        SELECT pkt0_routing_byte7, flag_or_manage, gate_or_count1, gate_or_count2
        FROM get_userinfo_template WHERE id = 1
        """
    ).fetchone()
    if row is None:
        return notes
    updates: List[str] = []
    routing = int(row[0] or 0) & 0xFF
    flag_or_manage = int(row[1] or 0)
    gate1 = int(row[2] or 0)
    gate2 = int(row[3] or 0)
    if routing == 0:
        updates.append("pkt0_routing_byte7 = 1")
    if flag_or_manage == 0:
        updates.append("flag_or_manage = 5")
    if gate1 <= 0:
        updates.append("gate_or_count1 = 32")
    if gate2 <= 0:
        updates.append("gate_or_count2 = 32")
    if updates:
        conn.execute(
            "UPDATE get_userinfo_template SET %s WHERE id = 1" % ", ".join(updates)
        )
        notes.append("userinfo_template_placeholder_fix=%s" % ",".join(updates))
    return notes


def _sha256_blob(value: object) -> str:
    if not isinstance(value, bytes):
        raise RuntimeError("母版字段不是 BLOB")
    return hashlib.sha256(value).hexdigest()


def validate_default_inventory_template(conn: sqlite3.Connection) -> None:
    """验证首次释放库与 638 母版的系统状态契约一致。"""
    required_tables = (
        "accounts",
        "account_settings",
        "account_content_block_rules",
        "content_block_default_rules",
        "content_block_rule_migrations",
        "get_userinfo_template",
        "getuserinfo_extra_packets",
        "account_cargo_state",
        "account_material_ledger_migrations",
        "account_wallets",
        "characters",
    )
    missing = [t for t in required_tables if not _table_exists(conn, t)]
    if missing:
        raise RuntimeError("母版缺表: %s" % ",".join(missing))

    account = conn.execute(
        "SELECT account_id, m_id, cera, token_cera, happy_token_cera, "
        "lucky_star, cube_black, cube_white, cube_red, cube_blue, "
        "cube_clear, cube_gold FROM accounts"
    ).fetchall()
    if account != [(1, "10038", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)]:
        raise RuntimeError("母版账号或共享货币异常: %r" % (account,))

    settings = conn.execute(
        "SELECT main_game_option, quickchat_bank0, quickchat_bank1, "
        "hotkey_key_type, hotkey_slots, tutorial_flags_json "
        "FROM account_settings WHERE account_id = 1"
    ).fetchall()
    if len(settings) != 1:
        raise RuntimeError("母版账号设置行数异常: %s" % len(settings))
    setting = settings[0]
    setting_hashes = tuple(_sha256_blob(v) for v in setting[:3])
    expected_setting_hashes = (
        "c60ed7011441a1fbccb86f0f8a3a4c5afa7ab621b96b42bc043f24d980051b9f",
        "d13d4a8b3b8add19b5970157f09d00c12cbda4fed4d74d8493156523f7069b66",
        "5cbdca92ad90376d4a2f549677d17dc9e51e742df860c9ca857a8b4c0dcaef46",
    )
    if setting_hashes != expected_setting_hashes:
        raise RuntimeError("母版账号设置 BLOB 校验失败")
    if setting[3:] != (
        0,
        b"\x02\x00",
        "[36,1,68,7,79,15,88,2,69,4,76,9,81,13,85,8,80,40,14,87,0,67]",
    ):
        raise RuntimeError("母版账号设置标量字段异常: %r" % (setting[3:],))

    template = conn.execute(
        "SELECT id, seed_character_id, response_blob, pkt0_routing_byte7, "
        "gate_or_count1, gate_or_count2, flag_or_manage, key_or_point, "
        "unknown16, unknown32, pkt2_result_code, pkt2_character_key, "
        "pkt2_slot_flag1, pkt2_slot_flag2, pkt2_state_flag, pkt2_flag3, "
        "pkt2_reserved FROM get_userinfo_template"
    ).fetchall()
    if len(template) != 1:
        raise RuntimeError("母版 USERINFO 模板行数异常: %s" % len(template))
    template_row = template[0]
    expected_template_scalars = (
        1, 1002, 1, 128, 128, 5, 43140, 0, 0, 1, 11043845, 0, 4, 36, 2, 0
    )
    if template_row[:2] + template_row[3:] != expected_template_scalars:
        raise RuntimeError("母版 USERINFO 模板标量异常")
    if _sha256_blob(template_row[2]) != (
        "9337ebf494c9395664e02a4b4865edde2e9a916ce926aa6d11575d99a46ba8f0"
    ):
        raise RuntimeError("母版 USERINFO response_blob 校验失败")

    extra_rows = conn.execute(
        "SELECT seq, command, noti_type, body "
        "FROM getuserinfo_extra_packets ORDER BY seq"
    ).fetchall()
    extra_contract = [
        (int(r[0]), int(r[1]), int(r[2]), _sha256_blob(r[3])) for r in extra_rows
    ]
    if extra_contract != [
        (0, 1, 646, "96a296d224f285c67bee93c30f8a309157f0daa35dc5b87e410b78630a09cfc7"),
        (1, 1, 442, "961197847c703244ebf52899bb37c4db9b9d62ae770e8a687297d7f278671102"),
    ]:
        raise RuntimeError("母版附加 USERINFO 包异常")

    cargo = conn.execute(
        "SELECT account_id, selection_key, value32, item_count "
        "FROM account_cargo_state"
    ).fetchall()
    if cargo != [(1, 80, 0, 0)]:
        raise RuntimeError("母版账号金库状态异常: %r" % (cargo,))
    wallets = conn.execute(
        "SELECT account_id, gold, revive_coin FROM account_wallets"
    ).fetchall()
    if wallets != [(1, 0, 0)]:
        raise RuntimeError("母版共享钱包异常: %r" % (wallets,))
    migrations = conn.execute(
        "SELECT migration_key FROM account_material_ledger_migrations"
    ).fetchall()
    if migrations != [("account_soul_ledger_v1",)]:
        raise RuntimeError("母版材料账本迁移状态异常: %r" % (migrations,))

    allowed_nonempty = {
        "accounts",
        "account_settings",
        "get_userinfo_template",
        "getuserinfo_extra_packets",
        "account_cargo_state",
        "account_material_ledger_migrations",
        "account_wallets",
        "account_content_block_rules",
        "content_block_default_rules",
    }
    table_names = [
        str(r[0])
        for r in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    unexpected = []
    for table in table_names:
        if table in allowed_nonempty:
            continue
        count = int(conn.execute("SELECT COUNT(*) FROM [%s]" % table).fetchone()[0])
        if count:
            unexpected.append("%s=%s" % (table, count))
    if unexpected:
        raise RuntimeError("母版含玩家数据: %s" % ",".join(unexpected))

    integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity.lower() != "ok":
        raise RuntimeError("母版 integrity_check=%s" % integrity)
    foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        raise RuntimeError("母版 foreign_key_check=%r" % (foreign_keys,))


def create_default_inventory_db(
    dst_path: Path,
    schema_sql_path: Path,
    default_seed_sql_path: Path,
    content_block_default_rules_path: Path,
    *,
    temp_dir: Optional[Path] = None,
) -> Path:
    """在临时库完成母版创建与验证，再原子释放到目标路径。"""
    dst = Path(dst_path)
    schema_path = Path(schema_sql_path)
    seed_path = Path(default_seed_sql_path)
    content_rules_path = Path(content_block_default_rules_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        raise RuntimeError("目标存档已存在，停止释放母版: %s" % dst)
    if not schema_path.is_file():
        raise RuntimeError("schema SQL 不存在: %s" % schema_path)
    if not seed_path.is_file():
        raise RuntimeError("默认母版 SQL 不存在: %s" % seed_path)
    if not content_rules_path.is_file():
        raise RuntimeError("默认屏蔽规则不存在: %s" % content_rules_path)

    schema_text = schema_path.read_text(encoding="utf-8")
    seed_text = seed_path.read_text(encoding="utf-8")
    if not schema_text.strip() or not seed_text.strip():
        raise RuntimeError("默认母版结构或种子 SQL 为空")
    structure = _filter_schema_sql_to_structure_only(schema_text)
    temporary_root = Path(temp_dir) if temp_dir is not None else dst.parent
    temporary_root.mkdir(parents=True, exist_ok=True)
    # os.replace is atomic only within one filesystem. Development tests and
    # maintenance tools may place TEMP on another drive than the target save.
    try:
        if temporary_root.stat().st_dev != dst.parent.stat().st_dev:
            temporary_root = dst.parent
    except OSError:
        temporary_root = dst.parent
    tmp = temporary_root / (dst.name + ".default.%s.tmp" % os.getpid())
    try:
        if tmp.exists():
            tmp.unlink()
        conn = sqlite3.connect(str(tmp))
        try:
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.executescript(structure)
            apply_missing_columns(conn)
            conn.executescript(seed_text)
            apply_content_block_default_rules(conn, content_rules_path)
            conn.commit()
            validate_default_inventory_template(conn)
            configure_sqlite_runtime(conn)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()
        if dst.exists():
            raise RuntimeError("目标存档在母版创建期间出现，停止覆盖: %s" % dst)
        tmp.replace(dst)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise
    return dst


def create_empty_full_schema_db(
    dst_path: Path,
    schema_sql_path: Path,
    *,
    overwrite: bool = True,
) -> Path:
    """打包用：生成完整表结构、无角色行的空库。"""
    dst = Path(dst_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_file() and overwrite:
        try:
            dst.unlink()
        except OSError:
            pass
    sql_text = Path(schema_sql_path).read_text(encoding="utf-8")
    if not sql_text.strip():
        raise RuntimeError("schema SQL 为空: %s" % schema_sql_path)
    structure = _filter_schema_sql_to_structure_only(sql_text)
    conn = sqlite3.connect(str(dst))
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript(structure)
        conn.executescript(_SEED_SQL)
        apply_missing_columns(conn)
        fix_userinfo_template_placeholder_only(conn)
        conn.commit()
        n_tables = _count_user_tables(conn)
        n_chars = 0
        if _table_exists(conn, "characters"):
            n_chars = int(
                conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0]
            )
        if n_tables < 40:
            raise RuntimeError(
                "空库表数异常 n_tables=%s（期望≥40） schema=%s"
                % (n_tables, schema_sql_path)
            )
        if n_chars != 0:
            raise RuntimeError("空库不应含角色行 n_chars=%s" % n_chars)
        configure_sqlite_runtime(conn)
    finally:
        conn.close()
    return dst


def sync_inventory_schema(
    db_path: Path,
    schema_sql_path: Path,
    *,
    default_seed_sql_path: Optional[Path] = None,
    content_block_default_rules_path: Optional[Path] = None,
    temp_dir: Optional[Path] = None,
    log: Optional[LogFn] = None,
) -> Tuple[bool, str]:
    """Release a mature template or migrate an existing player save.

    Existing saves receive only additive schema changes plus the published
    account-wide content-block defaults. Character, inventory, and explicit
    unblock rows remain intact.

    返回 (ok, note)。
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    db_path = Path(db_path)
    schema_sql_path = Path(schema_sql_path)
    if not schema_sql_path.is_file():
        return False, "schema_sql_missing:%s" % schema_sql_path

    db_path.parent.mkdir(parents=True, exist_ok=True)
    existed = db_path.is_file()
    content_rules_path = (
        Path(content_block_default_rules_path)
        if content_block_default_rules_path is not None
        else schema_sql_path.with_name(CONTENT_BLOCK_DEFAULT_RULES_NAME)
    )
    if not existed:
        seed_path = (
            Path(default_seed_sql_path)
            if default_seed_sql_path is not None
            else schema_sql_path.with_name(DEFAULT_SEED_SQL_NAME)
        )
        if not seed_path.is_file():
            return False, "default_seed_sql_missing:%s" % seed_path
        if not content_rules_path.is_file():
            return False, "content_block_default_rules_missing:%s" % content_rules_path
        try:
            create_default_inventory_db(
                db_path,
                schema_sql_path,
                seed_path,
                content_rules_path,
                temp_dir=temp_dir,
            )
        except Exception as exc:
            return False, "default_create_fail:%s:%s" % (type(exc).__name__, exc)
        conn = sqlite3.connect(str(db_path))
        try:
            tables = _count_user_tables(conn)
            chars = int(conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0])
            journal_mode, synchronous = configure_sqlite_runtime(conn)
        finally:
            conn.close()
        note = (
            "schema_sync ok mode=default_template tables=%s chars=%s "
            "schema=%s seed=%s content_blocks=%s sqlite=%s/%s"
            % (
                tables,
                chars,
                schema_sql_path.name,
                seed_path.name,
                content_rules_path.name,
                journal_mode,
                synchronous,
            )
        )
        _log("[SchemaSync] %s" % note)
        return True, note

    before_tables = 0
    before_chars = 0
    try:
        probe = sqlite3.connect(str(db_path))
        try:
            before_tables = _count_user_tables(probe)
            if _table_exists(probe, "characters"):
                before_chars = int(
                    probe.execute("SELECT COUNT(*) FROM characters").fetchone()[0]
                )
        finally:
            probe.close()
    except sqlite3.Error as exc:
        return False, "open_existing_fail:%s" % type(exc).__name__

    sql_text = schema_sql_path.read_text(encoding="utf-8")
    structure = _filter_schema_sql_to_structure_only(sql_text)
    if not content_rules_path.is_file():
        return False, "content_block_default_rules_missing:%s" % content_rules_path

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        # CREATE IF NOT EXISTS — 旧库已有表原样保留（含数据）
        conn.executescript(structure)
        col_notes = apply_missing_columns(conn)
        defaults_added, account_rules_added = apply_content_block_default_rules(
            conn,
            content_rules_path,
        )
        conn.commit()

        after_tables = _count_user_tables(conn)
        after_chars = 0
        if _table_exists(conn, "characters"):
            after_chars = int(
                conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0]
            )
        # 数据完整性：角色行数不得减少
        if after_chars < before_chars:
            return False, (
                "abort_char_count_decreased before=%s after=%s"
                % (before_chars, after_chars)
            )
        journal_mode, synchronous = configure_sqlite_runtime(conn)
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        return False, "sync_fail:%s:%s" % (type(exc).__name__, exc)
    finally:
        conn.close()

    added = max(0, after_tables - before_tables)
    note = (
        "schema_sync ok mode=existing_structure tables=%s→%s (+%s) "
        "chars=%s→%s cols=%s content_defaults=+%s account_defaults=+%s "
        "schema=%s sqlite=%s/%s"
        % (
            before_tables,
            after_tables,
            added,
            before_chars,
            after_chars,
            ",".join(col_notes) if col_notes else "-",
            defaults_added,
            account_rules_added,
            schema_sql_path.name,
            journal_mode,
            synchronous,
        )
    )
    _log("[SchemaSync] %s" % note)
    return True, note


def ensure_share_inventory_db(
    server_home: Path,
    *,
    log: Optional[LogFn] = None,
    schema_sql_path: Optional[Path] = None,
    default_seed_sql_path: Optional[Path] = None,
    content_block_default_rules_path: Optional[Path] = None,
    db_path: Optional[Path] = None,
) -> Tuple[bool, str]:
    """分享包开服前：确保角色库结构完整。

    - 无 db：释放并验证已确认的零角色成熟母版
    - 有旧 db：补缺表/缺列/缺索引，并补齐发布默认屏蔽任务；不碰角色/背包数据
    - ``db_path`` 允许单文件启动器把角色库固定在 ``runtime/``
    """
    server_home = Path(server_home)
    target_db = Path(db_path) if db_path is not None else (
        server_home / "data" / "inventory_usdof_local.db"
    )
    schema = schema_sql_path or resolve_schema_sql_path(server_home)
    if schema is None:
        msg = "缺少 schema SQL（%s），跳过结构同步" % SCHEMA_SQL_NAME
        if log:
            log("[SchemaSync][warn] %s" % msg)
        return False, msg
    seed = default_seed_sql_path or resolve_default_seed_sql_path(server_home)
    content_rules = (
        content_block_default_rules_path
        or resolve_content_block_default_rules_path(server_home)
    )
    return sync_inventory_schema(
        target_db,
        schema,
        default_seed_sql_path=seed,
        content_block_default_rules_path=content_rules,
        temp_dir=server_home.parent / "temp",
        log=log,
    )
