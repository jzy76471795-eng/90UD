"""Persistent guild-name ownership for the local simulation server."""
from __future__ import annotations

import sqlite3
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Union


@dataclass(frozen=True)
class GuildRecord:
    guild_id: int
    display_name: str
    normalized_name: str
    leader_character_id: Optional[int]


@dataclass(frozen=True)
class GuildMemberRecord:
    """One durable guild membership projected with its live character fields."""

    guild_id: int
    character_id: int
    name_bytes: bytes
    job: int
    grow_type: int
    level: int
    member_role: int


@dataclass(frozen=True)
class GuildMemberList:
    """Guild identity and the member rows needed by the Stage3 guild UI."""

    guild: GuildRecord
    members: tuple[GuildMemberRecord, ...]


@dataclass(frozen=True)
class GuildJoinApplication:
    """One pending join request projected into the Stage3 0160 list."""

    application_id: int
    guild_id: int
    character_id: int
    name_bytes: bytes
    job: int
    grow_type: int
    level: int
    application_message_bytes: bytes


@dataclass(frozen=True)
class GuildJoinApplicationList:
    """The selected guild member's pending join requests."""

    guild: GuildRecord
    applications: tuple[GuildJoinApplication, ...]


@dataclass(frozen=True)
class GuildAccountMembership:
    """One account character projected into the Stage3 02E7 guild list."""

    guild: GuildRecord
    character_id: int
    name_bytes: bytes
    member_role: int

    @property
    def is_leader(self) -> bool:
        return int(self.guild.leader_character_id or 0) == int(self.character_id)


@dataclass(frozen=True)
class GuildSearchDetail:
    """The durable guild summary consumed by the Stage3 02E8 search reader."""

    guild: GuildRecord
    leader_name_bytes: bytes
    member_count: int


@dataclass(frozen=True)
class GuildCreatePermit:
    """One local guild-create authentication request."""

    permit_id: int
    creator_character_id: int
    target_character_id: int
    state: str
    guild_id: Optional[int]


@dataclass(frozen=True)
class GuildCreateNotice:
    """A durable S0/0047 delivery still owed to one guild member."""

    permit_id: int
    character_id: int
    guild_id: int


def normalize_guild_name(name: str) -> str:
    """Return the stable comparison key used for guild-name uniqueness."""

    return unicodedata.normalize("NFKC", str(name)).casefold()


def _sqlite_blob_bytes(value: object) -> bytes:
    """Keep persisted wire-name bytes intact across sqlite driver return types."""

    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    return str(value).encode("utf-8")


class GuildRepository:
    """Own the small guild table without coupling it to character internals."""

    def __init__(self, db_path: Union[str, Path]) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_lock = threading.Lock()
        self._schema_ready = False
        self._ensure_schema()

    @contextmanager
    def _open(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._schema_lock:
            if self._schema_ready:
                return
            with self._open() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS guilds (
                        guild_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        display_name TEXT NOT NULL,
                        normalized_name TEXT NOT NULL UNIQUE,
                        leader_character_id INTEGER,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS guild_members (
                        guild_id INTEGER NOT NULL,
                        character_id INTEGER NOT NULL UNIQUE,
                        member_role INTEGER NOT NULL,
                        joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (guild_id, character_id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_guild_members_guild_id
                    ON guild_members (guild_id, character_id)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS guild_join_applications (
                        application_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER NOT NULL,
                        character_id INTEGER NOT NULL,
                        state TEXT NOT NULL DEFAULT 'pending',
                        message_bytes BLOB NOT NULL DEFAULT X'',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                application_columns = {
                    str(row[1])
                    for row in conn.execute(
                        "PRAGMA table_info(guild_join_applications)"
                    ).fetchall()
                }
                if "message_bytes" not in application_columns:
                    conn.execute(
                        "ALTER TABLE guild_join_applications "
                        "ADD COLUMN message_bytes BLOB NOT NULL DEFAULT X''"
                    )
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS
                        idx_guild_join_applications_guild_character_state
                    ON guild_join_applications (guild_id, character_id, state)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_guild_join_applications_guild_state
                    ON guild_join_applications (guild_id, state, application_id)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS guild_create_drafts (
                        creator_character_id INTEGER PRIMARY KEY,
                        display_name TEXT NOT NULL DEFAULT '',
                        normalized_name TEXT NOT NULL DEFAULT '',
                        target_character_id INTEGER,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS guild_create_permits (
                        permit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        creator_character_id INTEGER NOT NULL,
                        target_character_id INTEGER NOT NULL,
                        state TEXT NOT NULL,
                        guild_id INTEGER,
                        target_created_notified INTEGER NOT NULL DEFAULT 0,
                        creator_created_notified INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_guild_create_permits_target_state
                    ON guild_create_permits (target_character_id, state, permit_id)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_guild_create_permits_creator_state
                    ON guild_create_permits (creator_character_id, state, permit_id)
                    """
                )
                conn.commit()
            self._schema_ready = True

    @staticmethod
    def _character_is_alive_tx(conn: sqlite3.Connection, character_id: int) -> bool:
        row = conn.execute(
            "SELECT 1 FROM characters WHERE character_id = ? AND delete_flag = 0 LIMIT 1",
            (int(character_id),),
        ).fetchone()
        return row is not None

    @staticmethod
    def _character_has_guild_tx(conn: sqlite3.Connection, character_id: int) -> bool:
        row = conn.execute(
            """
            SELECT 1
            FROM guild_members
            WHERE character_id = ?
            UNION ALL
            SELECT 1
            FROM guilds
            WHERE leader_character_id = ?
            LIMIT 1
            """,
            (int(character_id), int(character_id)),
        ).fetchone()
        return row is not None

    @staticmethod
    def _permit_from_row(row: sqlite3.Row) -> GuildCreatePermit:
        guild_id = row["guild_id"]
        return GuildCreatePermit(
            permit_id=int(row["permit_id"]),
            creator_character_id=int(row["creator_character_id"]),
            target_character_id=int(row["target_character_id"]),
            state=str(row["state"]),
            guild_id=None if guild_id is None else int(guild_id),
        )

    def is_name_taken(self, display_name: str) -> bool:
        """Return whether a created guild already owns ``display_name``."""

        normalized_name = normalize_guild_name(display_name)
        if not normalized_name:
            return False
        self._ensure_schema()
        with self._open() as conn:
            row = conn.execute(
                "SELECT 1 FROM guilds WHERE normalized_name = ? LIMIT 1",
                (normalized_name,),
            ).fetchone()
        return row is not None

    def create_guild(
        self,
        *,
        display_name: str,
        leader_character_id: Optional[int] = None,
    ) -> Optional[GuildRecord]:
        """Create one guild atomically, returning ``None`` on a duplicate name."""

        visible_name = str(display_name)
        normalized_name = normalize_guild_name(visible_name)
        if not normalized_name:
            raise ValueError("guild name is empty after normalization")

        leader_id = None if leader_character_id is None else int(leader_character_id)
        self._ensure_schema()
        try:
            with self._open() as conn:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    """
                    INSERT INTO guilds (display_name, normalized_name, leader_character_id)
                    VALUES (?, ?, ?)
                    """,
                    (visible_name, normalized_name, leader_id),
                )
                guild_id = int(cursor.lastrowid)
                conn.commit()
        except sqlite3.IntegrityError:
            return None

        return GuildRecord(
            guild_id=guild_id,
            display_name=visible_name,
            normalized_name=normalized_name,
            leader_character_id=leader_id,
        )

    def is_character_in_guild(self, character_id: int) -> bool:
        """Return whether one live character already belongs to any guild."""

        cid = int(character_id)
        if cid <= 0:
            return False
        self._ensure_schema()
        with self._open() as conn:
            return self._character_has_guild_tx(conn, cid)

    def get_guild_for_character(self, character_id: int) -> Optional[GuildRecord]:
        """Return the durable guild identity projected for one character."""

        cid = int(character_id)
        if cid <= 0:
            return None
        self._ensure_schema()
        with self._open() as conn:
            row = conn.execute(
                """
                SELECT guild_id, display_name, normalized_name, leader_character_id
                FROM (
                    SELECT g.guild_id AS guild_id,
                           g.display_name AS display_name,
                           g.normalized_name AS normalized_name,
                           g.leader_character_id AS leader_character_id,
                           0 AS source_rank
                    FROM guild_members AS m
                    INNER JOIN guilds AS g ON g.guild_id = m.guild_id
                    WHERE m.character_id = ?
                    UNION ALL
                    SELECT g.guild_id AS guild_id,
                           g.display_name AS display_name,
                           g.normalized_name AS normalized_name,
                           g.leader_character_id AS leader_character_id,
                           1 AS source_rank
                    FROM guilds AS g
                    WHERE g.leader_character_id = ?
                )
                ORDER BY source_rank, guild_id
                LIMIT 1
                """,
                (cid, cid),
            ).fetchone()
        if row is None:
            return None
        leader_character_id = row["leader_character_id"]
        return GuildRecord(
            guild_id=int(row["guild_id"]),
            display_name=str(row["display_name"] or ""),
            normalized_name=str(row["normalized_name"] or ""),
            leader_character_id=(
                None if leader_character_id is None else int(leader_character_id)
            ),
        )

    def get_display_name_for_character(self, character_id: int) -> Optional[str]:
        """Return the current guild display name projected for one character."""

        guild = self.get_guild_for_character(character_id)
        if guild is None:
            return None
        return guild.display_name or None

    def request_join_guild(
        self,
        requester_character_id: int,
        guild_id: int,
        guild_display_name: str,
        application_message_bytes: bytes = b"",
    ) -> tuple[Optional[GuildRecord], str]:
        """Persist one pending guild application, deduplicating retries."""

        requester_id = int(requester_character_id)
        target_guild_id = int(guild_id)
        requested_name = str(guild_display_name)
        requested_normalized_name = normalize_guild_name(requested_name)
        message_bytes = bytes(application_message_bytes)
        if requester_id <= 0:
            return None, "invalid_requester"
        if target_guild_id <= 0 or not requested_normalized_name:
            return None, "invalid_guild"
        if len(message_bytes) >= 512:
            return None, "invalid_application_message"

        self._ensure_schema()
        with self._open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            guild_row = conn.execute(
                """
                SELECT guild_id, display_name, normalized_name, leader_character_id
                FROM guilds
                WHERE guild_id = ?
                LIMIT 1
                """,
                (target_guild_id,),
            ).fetchone()
            if guild_row is None:
                conn.rollback()
                return None, "guild_missing"
            if str(guild_row["normalized_name"] or "") != requested_normalized_name:
                conn.rollback()
                return None, "guild_mismatch"
            if not self._character_is_alive_tx(conn, requester_id):
                conn.rollback()
                return None, "requester_missing"
            if self._character_has_guild_tx(conn, requester_id):
                conn.rollback()
                return None, "requester_has_guild"

            existing = conn.execute(
                """
                SELECT application_id
                FROM guild_join_applications
                WHERE guild_id = ?
                  AND character_id = ?
                  AND state = 'pending'
                LIMIT 1
                """,
                (target_guild_id, requester_id),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO guild_join_applications (
                        guild_id, character_id, state, message_bytes
                    )
                    VALUES (?, ?, 'pending', ?)
                    """,
                    (target_guild_id, requester_id, message_bytes),
                )
            else:
                conn.execute(
                    """
                    UPDATE guild_join_applications
                    SET message_bytes = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE application_id = ? AND state = 'pending'
                    """,
                    (message_bytes, int(existing["application_id"])),
                )
            conn.commit()

        leader_character_id = guild_row["leader_character_id"]
        return GuildRecord(
            guild_id=int(guild_row["guild_id"]),
            display_name=str(guild_row["display_name"] or ""),
            normalized_name=str(guild_row["normalized_name"] or ""),
            leader_character_id=(
                None if leader_character_id is None else int(leader_character_id)
            ),
        ), "ok"

    def approve_join_application(
        self,
        approver_character_id: int,
        applicant_character_id: int,
    ) -> tuple[Optional[GuildRecord], str]:
        """Approve one pending application for the approver's current guild.

        The client offers the control only to eligible guild UI roles.  The
        server still verifies that the selected approver belongs to the target
        guild and that the applicant has not joined another guild meanwhile.
        """

        approver_id = int(approver_character_id)
        applicant_id = int(applicant_character_id)
        if approver_id <= 0:
            return None, "invalid_approver"
        if applicant_id <= 0:
            return None, "invalid_applicant"

        self._ensure_schema()
        with self._open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._character_is_alive_tx(conn, approver_id):
                conn.rollback()
                return None, "approver_missing"
            guild_row = conn.execute(
                """
                SELECT guild_id, display_name, normalized_name, leader_character_id
                FROM (
                    SELECT g.guild_id, g.display_name, g.normalized_name,
                           g.leader_character_id, 0 AS source_rank
                    FROM guild_members AS m
                    INNER JOIN guilds AS g ON g.guild_id = m.guild_id
                    WHERE m.character_id = ?
                    UNION ALL
                    SELECT g.guild_id, g.display_name, g.normalized_name,
                           g.leader_character_id, 1 AS source_rank
                    FROM guilds AS g
                    WHERE g.leader_character_id = ?
                )
                ORDER BY source_rank, guild_id
                LIMIT 1
                """,
                (approver_id, approver_id),
            ).fetchone()
            if guild_row is None:
                conn.rollback()
                return None, "approver_has_no_guild"

            guild_id = int(guild_row["guild_id"])
            application_row = conn.execute(
                """
                SELECT application_id, state
                FROM guild_join_applications
                WHERE guild_id = ? AND character_id = ?
                ORDER BY application_id DESC
                LIMIT 1
                """,
                (guild_id, applicant_id),
            ).fetchone()
            if application_row is None:
                conn.rollback()
                return None, "application_missing"
            if not self._character_is_alive_tx(conn, applicant_id):
                conn.rollback()
                return None, "applicant_missing"

            member_guild_row = conn.execute(
                """
                SELECT guild_id
                FROM guild_members
                WHERE character_id = ?
                UNION ALL
                SELECT guild_id
                FROM guilds
                WHERE leader_character_id = ?
                LIMIT 1
                """,
                (applicant_id, applicant_id),
            ).fetchone()
            application_state = str(application_row["state"] or "")
            if application_state == "approved":
                if (
                    member_guild_row is None
                    or int(member_guild_row["guild_id"]) != guild_id
                ):
                    conn.rollback()
                    return None, "approved_application_membership_mismatch"
                conn.commit()
            elif application_state != "pending":
                conn.rollback()
                return None, "application_not_pending"
            elif member_guild_row is not None:
                conn.rollback()
                return None, "applicant_has_guild"
            else:
                conn.execute(
                    """
                    INSERT INTO guild_members (guild_id, character_id, member_role)
                    VALUES (?, ?, ?)
                    """,
                    (guild_id, applicant_id, 1),
                )
                cursor = conn.execute(
                    """
                    UPDATE guild_join_applications
                    SET state = 'approved', updated_at = CURRENT_TIMESTAMP
                    WHERE application_id = ? AND state = 'pending'
                    """,
                    (int(application_row["application_id"]),),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    return None, "application_state_changed"
                conn.commit()

        leader_character_id = guild_row["leader_character_id"]
        return GuildRecord(
            guild_id=guild_id,
            display_name=str(guild_row["display_name"] or ""),
            normalized_name=str(guild_row["normalized_name"] or ""),
            leader_character_id=(
                None if leader_character_id is None else int(leader_character_id)
            ),
        ), "ok"

    def get_join_application_list_for_character(
        self,
        character_id: int,
    ) -> Optional[GuildJoinApplicationList]:
        """Return pending requests only when ``character_id`` belongs to that guild."""

        cid = int(character_id)
        if cid <= 0:
            return None
        self._ensure_schema()
        with self._open() as conn:
            guild_row = conn.execute(
                """
                SELECT guild_id, display_name, normalized_name, leader_character_id
                FROM (
                    SELECT g.guild_id, g.display_name, g.normalized_name,
                           g.leader_character_id, 0 AS source_rank
                    FROM guild_members AS m
                    INNER JOIN guilds AS g ON g.guild_id = m.guild_id
                    WHERE m.character_id = ?
                    UNION ALL
                    SELECT g.guild_id, g.display_name, g.normalized_name,
                           g.leader_character_id, 1 AS source_rank
                    FROM guilds AS g
                    WHERE g.leader_character_id = ?
                )
                ORDER BY source_rank, guild_id
                LIMIT 1
                """,
                (cid, cid),
            ).fetchone()
            if guild_row is None:
                return None

            guild_id = int(guild_row["guild_id"])
            rows = conn.execute(
                """
                SELECT a.application_id, a.guild_id, a.character_id, a.message_bytes,
                       c.name_bytes, CAST(c.name AS BLOB) AS name_blob,
                       c.job, c.grow_type, c.level
                FROM guild_join_applications AS a
                INNER JOIN characters AS c ON c.character_id = a.character_id
                WHERE a.guild_id = ?
                  AND a.state = 'pending'
                  AND c.delete_flag = 0
                  AND NOT EXISTS (
                      SELECT 1
                      FROM guild_members AS pending_member
                      WHERE pending_member.character_id = a.character_id
                      UNION ALL
                      SELECT 1
                      FROM guilds AS pending_leader
                      WHERE pending_leader.leader_character_id = a.character_id
                      LIMIT 1
                  )
                ORDER BY a.application_id
                """,
                (guild_id,),
            ).fetchall()

        leader_character_id = guild_row["leader_character_id"]
        guild = GuildRecord(
            guild_id=guild_id,
            display_name=str(guild_row["display_name"] or ""),
            normalized_name=str(guild_row["normalized_name"] or ""),
            leader_character_id=(
                None if leader_character_id is None else int(leader_character_id)
            ),
        )
        applications = tuple(
            GuildJoinApplication(
                application_id=int(row["application_id"]),
                guild_id=int(row["guild_id"]),
                character_id=int(row["character_id"]),
                name_bytes=(
                    _sqlite_blob_bytes(row["name_bytes"])
                    or _sqlite_blob_bytes(row["name_blob"])
                ),
                job=int(row["job"]) & 0xFF,
                grow_type=int(row["grow_type"]) & 0x7F,
                level=max(0, int(row["level"])) & 0xFF,
                application_message_bytes=_sqlite_blob_bytes(row["message_bytes"]),
            )
            for row in rows
        )
        return GuildJoinApplicationList(guild=guild, applications=applications)

    def get_first_member_character_id_for_account(
        self,
        account_id: int,
    ) -> Optional[int]:
        """Return one live guild member for an account's pre-select snapshot."""

        aid = int(account_id)
        if aid <= 0:
            return None
        self._ensure_schema()
        with self._open() as conn:
            row = conn.execute(
                """
                SELECT m.character_id
                FROM guild_members AS m
                INNER JOIN characters AS c ON c.character_id = m.character_id
                WHERE c.account_id = ? AND c.delete_flag = 0
                ORDER BY m.member_role, m.character_id
                LIMIT 1
                """,
                (aid,),
            ).fetchone()
        if row is None:
            return None
        return int(row["character_id"])

    def list_account_guild_memberships(
        self,
        account_id: int,
    ) -> tuple[GuildAccountMembership, ...]:
        """Return all live account characters associated with a local guild.

        Guild creation writes leader and helper rows into ``guild_members``.
        The leader fallback keeps hand-created legacy rows visible as well.
        """

        aid = int(account_id)
        if aid <= 0:
            return ()
        self._ensure_schema()
        with self._open() as conn:
            rows = conn.execute(
                """
                SELECT guild_id, display_name, normalized_name, leader_character_id,
                       character_id, name_bytes, name_blob, member_role
                FROM (
                    SELECT g.guild_id AS guild_id,
                           g.display_name AS display_name,
                           g.normalized_name AS normalized_name,
                           g.leader_character_id AS leader_character_id,
                           c.character_id AS character_id,
                           c.name_bytes AS name_bytes,
                           CAST(c.name AS BLOB) AS name_blob,
                           m.member_role AS member_role,
                           0 AS source_rank
                    FROM guild_members AS m
                    INNER JOIN characters AS c ON c.character_id = m.character_id
                    INNER JOIN guilds AS g ON g.guild_id = m.guild_id
                    WHERE c.account_id = ? AND c.delete_flag = 0

                    UNION ALL

                    SELECT g.guild_id AS guild_id,
                           g.display_name AS display_name,
                           g.normalized_name AS normalized_name,
                           g.leader_character_id AS leader_character_id,
                           c.character_id AS character_id,
                           c.name_bytes AS name_bytes,
                           CAST(c.name AS BLOB) AS name_blob,
                           0 AS member_role,
                           1 AS source_rank
                    FROM guilds AS g
                    INNER JOIN characters AS c
                      ON c.character_id = g.leader_character_id
                    WHERE c.account_id = ?
                      AND c.delete_flag = 0
                      AND NOT EXISTS (
                          SELECT 1
                          FROM guild_members AS m
                          WHERE m.guild_id = g.guild_id
                            AND m.character_id = c.character_id
                      )
                )
                ORDER BY character_id, guild_id, source_rank
                """,
                (aid, aid),
            ).fetchall()

        memberships: list[GuildAccountMembership] = []
        for row in rows:
            leader_character_id = row["leader_character_id"]
            guild = GuildRecord(
                guild_id=int(row["guild_id"]),
                display_name=str(row["display_name"] or ""),
                normalized_name=str(row["normalized_name"] or ""),
                leader_character_id=(
                    None
                    if leader_character_id is None
                    else int(leader_character_id)
                ),
            )
            memberships.append(
                GuildAccountMembership(
                    guild=guild,
                    character_id=int(row["character_id"]),
                    name_bytes=(
                        _sqlite_blob_bytes(row["name_bytes"])
                        or _sqlite_blob_bytes(row["name_blob"])
                    ),
                    member_role=int(row["member_role"]) & 0xFF,
                )
            )
        return tuple(memberships)

    def get_guild_search_detail(
        self,
        display_name: str,
    ) -> Optional[GuildSearchDetail]:
        """Find one current guild by the normalized name used by C1/02E8."""

        normalized_name = normalize_guild_name(display_name)
        if not normalized_name:
            return None
        self._ensure_schema()
        with self._open() as conn:
            guild_row = conn.execute(
                """
                SELECT guild_id, display_name, normalized_name, leader_character_id
                FROM guilds
                WHERE normalized_name = ?
                LIMIT 1
                """,
                (normalized_name,),
            ).fetchone()
            if guild_row is None:
                return None

            guild_id = int(guild_row["guild_id"])
            member_rows = conn.execute(
                """
                SELECT m.character_id, m.member_role,
                       c.name_bytes, CAST(c.name AS BLOB) AS name_blob
                FROM guild_members AS m
                INNER JOIN characters AS c ON c.character_id = m.character_id
                WHERE m.guild_id = ? AND c.delete_flag = 0
                ORDER BY m.member_role, m.character_id
                """,
                (guild_id,),
            ).fetchall()

            leader_row = None
            leader_character_id = guild_row["leader_character_id"]
            if leader_character_id is not None:
                leader_row = conn.execute(
                    """
                    SELECT name_bytes, CAST(name AS BLOB) AS name_blob
                    FROM characters
                    WHERE character_id = ? AND delete_flag = 0
                    LIMIT 1
                    """,
                    (int(leader_character_id),),
                ).fetchone()

        guild = GuildRecord(
            guild_id=guild_id,
            display_name=str(guild_row["display_name"] or ""),
            normalized_name=str(guild_row["normalized_name"] or ""),
            leader_character_id=(
                None
                if leader_character_id is None
                else int(leader_character_id)
            ),
        )
        if leader_row is None and member_rows:
            leader_row = member_rows[0]
        leader_name_bytes = b""
        if leader_row is not None:
            leader_name_bytes = (
                _sqlite_blob_bytes(leader_row["name_bytes"])
                or _sqlite_blob_bytes(leader_row["name_blob"])
            )
        member_count = len(member_rows)
        if member_count == 0 and leader_name_bytes:
            member_count = 1
        return GuildSearchDetail(
            guild=guild,
            leader_name_bytes=leader_name_bytes,
            member_count=member_count,
        )

    def get_member_list_for_character(
        self,
        character_id: int,
    ) -> Optional[GuildMemberList]:
        """Return the selected character's guild and ordered live member rows."""

        cid = int(character_id)
        if cid <= 0:
            return None
        self._ensure_schema()
        with self._open() as conn:
            guild_row = conn.execute(
                """
                SELECT guild_id, display_name, normalized_name, leader_character_id
                FROM (
                    SELECT g.guild_id, g.display_name, g.normalized_name,
                           g.leader_character_id, 0 AS source_rank
                    FROM guild_members AS m
                    INNER JOIN guilds AS g ON g.guild_id = m.guild_id
                    WHERE m.character_id = ?
                    UNION ALL
                    SELECT g.guild_id, g.display_name, g.normalized_name,
                           g.leader_character_id, 1 AS source_rank
                    FROM guilds AS g
                    WHERE g.leader_character_id = ?
                )
                ORDER BY source_rank, guild_id
                LIMIT 1
                """,
                (cid, cid),
            ).fetchone()
            if guild_row is None:
                return None

            guild_id = int(guild_row["guild_id"])
            member_rows = conn.execute(
                """
                SELECT m.guild_id, m.character_id, m.member_role,
                       c.name_bytes, CAST(c.name AS BLOB) AS name_blob,
                       c.job, c.grow_type, c.level
                FROM guild_members AS m
                INNER JOIN characters AS c ON c.character_id = m.character_id
                WHERE m.guild_id = ? AND c.delete_flag = 0
                ORDER BY m.member_role, m.character_id
                """,
                (guild_id,),
            ).fetchall()

        leader_character_id = guild_row["leader_character_id"]
        guild = GuildRecord(
            guild_id=guild_id,
            display_name=str(guild_row["display_name"] or ""),
            normalized_name=str(guild_row["normalized_name"] or ""),
            leader_character_id=(
                None if leader_character_id is None else int(leader_character_id)
            ),
        )
        members = tuple(
            GuildMemberRecord(
                guild_id=int(row["guild_id"]),
                character_id=int(row["character_id"]),
                name_bytes=(
                    _sqlite_blob_bytes(row["name_bytes"])
                    or _sqlite_blob_bytes(row["name_blob"])
                ),
                job=int(row["job"]) & 0xFF,
                grow_type=int(row["grow_type"]) & 0xFF,
                level=max(0, int(row["level"])) & 0xFFFF,
                member_role=int(row["member_role"]) & 0xFF,
            )
            for row in member_rows
        )
        return GuildMemberList(guild=guild, members=members)

    def save_create_draft_name(self, creator_character_id: int, display_name: str) -> bool:
        """Persist the successful guild-name check for a later local permit flow."""

        creator_id = int(creator_character_id)
        visible_name = str(display_name)
        normalized_name = normalize_guild_name(visible_name)
        if creator_id <= 0 or not normalized_name:
            return False
        self._ensure_schema()
        with self._open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._character_is_alive_tx(conn, creator_id):
                conn.rollback()
                return False
            conn.execute(
                """
                INSERT INTO guild_create_drafts
                    (creator_character_id, display_name, normalized_name, target_character_id)
                VALUES (?, ?, ?, NULL)
                ON CONFLICT(creator_character_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    normalized_name = excluded.normalized_name,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (creator_id, visible_name, normalized_name),
            )
            conn.commit()
        return True

    def set_create_target(
        self,
        creator_character_id: int,
        target_character_id: int,
    ) -> str:
        """Bind one local authentication character to the creator's draft."""

        creator_id = int(creator_character_id)
        target_id = int(target_character_id)
        if creator_id <= 0 or target_id <= 0:
            return "invalid_character"
        if creator_id == target_id:
            return "same_character"

        self._ensure_schema()
        with self._open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._character_is_alive_tx(conn, creator_id):
                conn.rollback()
                return "creator_missing"
            if not self._character_is_alive_tx(conn, target_id):
                conn.rollback()
                return "target_missing"
            if self._character_has_guild_tx(conn, creator_id):
                conn.rollback()
                return "creator_has_guild"
            if self._character_has_guild_tx(conn, target_id):
                conn.rollback()
                return "target_has_guild"
            conn.execute(
                """
                INSERT INTO guild_create_drafts
                    (creator_character_id, display_name, normalized_name, target_character_id)
                VALUES (?, '', '', ?)
                ON CONFLICT(creator_character_id) DO UPDATE SET
                    target_character_id = excluded.target_character_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (creator_id, target_id),
            )
            conn.commit()
        return "ok"

    def request_create_permit(
        self,
        creator_character_id: int,
    ) -> tuple[Optional[GuildCreatePermit], str]:
        """Create one pending local authorization request from the saved draft."""

        creator_id = int(creator_character_id)
        if creator_id <= 0:
            return None, "invalid_creator"

        self._ensure_schema()
        with self._open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            draft = conn.execute(
                """
                SELECT display_name, normalized_name, target_character_id
                FROM guild_create_drafts
                WHERE creator_character_id = ?
                """,
                (creator_id,),
            ).fetchone()
            if draft is None or draft["target_character_id"] is None:
                conn.rollback()
                return None, "missing_target"
            visible_name = str(draft["display_name"] or "")
            normalized_name = str(draft["normalized_name"] or "")
            if not visible_name or not normalized_name:
                conn.rollback()
                return None, "missing_guild_name"
            target_id = int(draft["target_character_id"])
            if target_id <= 0 or target_id == creator_id:
                conn.rollback()
                return None, "invalid_target"
            if not self._character_is_alive_tx(conn, creator_id):
                conn.rollback()
                return None, "creator_missing"
            if not self._character_is_alive_tx(conn, target_id):
                conn.rollback()
                return None, "target_missing"
            if self._character_has_guild_tx(conn, creator_id):
                conn.rollback()
                return None, "creator_has_guild"
            if self._character_has_guild_tx(conn, target_id):
                conn.rollback()
                return None, "target_has_guild"

            existing = conn.execute(
                """
                SELECT permit_id, creator_character_id, target_character_id, state, guild_id
                FROM guild_create_permits
                WHERE creator_character_id = ?
                  AND target_character_id = ?
                  AND state = 'pending'
                ORDER BY permit_id DESC
                LIMIT 1
                """,
                (creator_id, target_id),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return self._permit_from_row(existing), "ok"

            # The local UI has one visible invite slot. Replacing pending work
            # prevents a switched character from accepting an ambiguous invite.
            conn.execute(
                """
                UPDATE guild_create_permits
                SET state = 'cancelled', updated_at = CURRENT_TIMESTAMP
                WHERE state = 'pending'
                  AND (creator_character_id = ? OR target_character_id = ?)
                """,
                (creator_id, target_id),
            )
            cursor = conn.execute(
                """
                INSERT INTO guild_create_permits
                    (creator_character_id, target_character_id, state)
                VALUES (?, ?, 'pending')
                """,
                (creator_id, target_id),
            )
            permit_id = int(cursor.lastrowid)
            conn.commit()

        return GuildCreatePermit(
            permit_id=permit_id,
            creator_character_id=creator_id,
            target_character_id=target_id,
            state="pending",
            guild_id=None,
        ), "ok"

    def accept_create_permit_and_create_guild(
        self,
        target_character_id: int,
    ) -> tuple[Optional[GuildRecord], Optional[GuildCreatePermit], str]:
        """Accept the target's pending invite and atomically create the guild.

        The local single-player path completes creation at acceptance, because
        switching to the helper character tears down the creator's waiting UI.
        """

        target_id = int(target_character_id)
        if target_id <= 0:
            return None, None, "invalid_target"

        self._ensure_schema()
        try:
            with self._open() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    """
                    SELECT p.permit_id, p.creator_character_id, p.target_character_id,
                           p.state, p.guild_id, d.display_name, d.normalized_name
                    FROM guild_create_permits AS p
                    LEFT JOIN guild_create_drafts AS d
                      ON d.creator_character_id = p.creator_character_id
                    WHERE p.target_character_id = ? AND p.state = 'pending'
                    ORDER BY p.permit_id DESC
                    LIMIT 1
                    """,
                    (target_id,),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return None, None, "permit_missing"

                creator_id = int(row["creator_character_id"])
                visible_name = str(row["display_name"] or "")
                normalized_name = str(row["normalized_name"] or "")
                if not visible_name or not normalized_name:
                    conn.rollback()
                    return None, None, "missing_guild_name"
                if not self._character_is_alive_tx(conn, creator_id):
                    conn.rollback()
                    return None, None, "creator_missing"
                if not self._character_is_alive_tx(conn, target_id):
                    conn.rollback()
                    return None, None, "target_missing"
                if self._character_has_guild_tx(conn, creator_id):
                    conn.rollback()
                    return None, None, "creator_has_guild"
                if self._character_has_guild_tx(conn, target_id):
                    conn.rollback()
                    return None, None, "target_has_guild"
                if conn.execute(
                    "SELECT 1 FROM guilds WHERE normalized_name = ? LIMIT 1",
                    (normalized_name,),
                ).fetchone() is not None:
                    conn.rollback()
                    return None, None, "guild_name_taken"

                cursor = conn.execute(
                    """
                    INSERT INTO guilds (display_name, normalized_name, leader_character_id)
                    VALUES (?, ?, ?)
                    """,
                    (visible_name, normalized_name, creator_id),
                )
                guild_id = int(cursor.lastrowid)
                conn.executemany(
                    """
                    INSERT INTO guild_members (guild_id, character_id, member_role)
                    VALUES (?, ?, ?)
                    """,
                    (
                        (guild_id, creator_id, 0),
                        (guild_id, target_id, 1),
                    ),
                )
                conn.execute(
                    """
                    UPDATE guild_create_permits
                    SET state = 'created', guild_id = ?,
                        target_created_notified = 0,
                        creator_created_notified = 0,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE permit_id = ? AND state = 'pending'
                    """,
                    (guild_id, int(row["permit_id"])),
                )
                conn.commit()
        except sqlite3.IntegrityError:
            return None, None, "guild_name_taken_or_member_exists"

        guild = GuildRecord(
            guild_id=guild_id,
            display_name=visible_name,
            normalized_name=normalized_name,
            leader_character_id=creator_id,
        )
        permit = GuildCreatePermit(
            permit_id=int(row["permit_id"]),
            creator_character_id=creator_id,
            target_character_id=target_id,
            state="created",
            guild_id=guild_id,
        )
        return guild, permit, "ok"

    def cancel_create_permit(self, permit_id: int) -> bool:
        """Cancel a pending permit that could not complete its local flow."""

        pid = int(permit_id)
        if pid <= 0:
            return False
        self._ensure_schema()
        with self._open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE guild_create_permits
                SET state = 'cancelled', updated_at = CURRENT_TIMESTAMP
                WHERE permit_id = ? AND state = 'pending'
                """,
                (pid,),
            )
            conn.commit()
        return cursor.rowcount == 1

    def has_pending_create_permit_for_target(self, character_id: int) -> bool:
        """Return whether this selected helper character has one invite to show."""

        cid = int(character_id)
        if cid <= 0:
            return False
        self._ensure_schema()
        with self._open() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM guild_create_permits
                WHERE target_character_id = ? AND state = 'pending'
                LIMIT 1
                """,
                (cid,),
            ).fetchone()
        return row is not None

    def pending_create_notices(self, character_id: int) -> list[GuildCreateNotice]:
        """Return unconsumed local guild-create completion notices."""

        cid = int(character_id)
        if cid <= 0:
            return []
        self._ensure_schema()
        with self._open() as conn:
            rows = conn.execute(
                """
                SELECT permit_id, creator_character_id, target_character_id, guild_id
                FROM guild_create_permits
                WHERE state = 'created'
                  AND guild_id IS NOT NULL
                  AND (
                    (creator_character_id = ? AND creator_created_notified = 0)
                    OR (target_character_id = ? AND target_created_notified = 0)
                  )
                ORDER BY permit_id
                """,
                (cid, cid),
            ).fetchall()
        return [
            GuildCreateNotice(
                permit_id=int(row["permit_id"]),
                character_id=cid,
                guild_id=int(row["guild_id"]),
            )
            for row in rows
        ]

    def mark_create_notice_delivered(self, permit_id: int, character_id: int) -> bool:
        """Mark a successfully queued S0/0047 delivery for its recipient."""

        pid = int(permit_id)
        cid = int(character_id)
        if pid <= 0 or cid <= 0:
            return False
        self._ensure_schema()
        with self._open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE guild_create_permits
                SET target_created_notified = CASE
                        WHEN target_character_id = ? THEN 1 ELSE target_created_notified END,
                    creator_created_notified = CASE
                        WHEN creator_character_id = ? THEN 1 ELSE creator_created_notified END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE permit_id = ? AND state = 'created'
                  AND (target_character_id = ? OR creator_character_id = ?)
                """,
                (cid, cid, pid, cid, cid),
            )
            conn.commit()
        return cursor.rowcount == 1

    def list_guild_member_ids(self, guild_id: int) -> list[int]:
        """Return guild member character ids in the stored role order."""

        gid = int(guild_id)
        if gid <= 0:
            return []
        self._ensure_schema()
        with self._open() as conn:
            rows = conn.execute(
                """
                SELECT character_id
                FROM guild_members
                WHERE guild_id = ?
                ORDER BY member_role, character_id
                """,
                (gid,),
            ).fetchall()
        return [int(row["character_id"]) for row in rows]
