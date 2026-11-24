"""SQLite persistence for the current Daily Challenge day."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable

from game.quest.daily_challenge_index import (
    STAGE3_CLAIM_FLAG_COUNT,
    DailyChallengeIndex,
    DailyChallengeReward,
)


_SCHEMA_LOCK = threading.RLock()


@dataclass(frozen=True)
class DailyChallengeEntryState:
    group_index: int
    entry_index: int
    slot_number: int
    quest_id: int
    target_value: int
    current_value: int
    reward_claimed: bool


@dataclass(frozen=True)
class DailyChallengeGroupState:
    group_index: int
    entries: tuple[DailyChallengeEntryState, ...]


@dataclass(frozen=True)
class DailyChallengeState:
    character_id: int
    day_key: str
    pvf_sha256: str
    generated_level: int
    groups: tuple[DailyChallengeGroupState, ...]
    claim_flags: tuple[int, ...]

    def entry_for_quest(self, quest_id: int) -> DailyChallengeEntryState | None:
        wanted = int(quest_id) & 0xFFFF
        for group in self.groups:
            for entry in group.entries:
                if entry.quest_id == wanted:
                    return entry
        return None


@dataclass(frozen=True)
class DailyChallengeMutation:
    # ``found`` alone is not enough for protocol routing: a claimed entry and
    # an entry that is absent from today's generated run both have no mutable
    # row.  Keep the reason explicit so callers never turn a stale claim into
    # another quest-completion packet.
    status: str
    found: bool
    changed: bool
    previous_value: int
    current_value: int
    target_value: int
    state: DailyChallengeState | None


@dataclass(frozen=True)
class DailyChallengeClaimReservation:
    status: str
    state: DailyChallengeState | None
    entry: DailyChallengeEntryState | None = None
    reward: DailyChallengeReward | None = None
    required_count: int = 0
    completed_count: int = 0


class DailyChallengeRepository:
    def __init__(self, character_repo: Any) -> None:
        self.character_repo = character_repo
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        if bool(getattr(self.character_repo, "_daily_challenge_schema_ready", False)):
            return
        with _SCHEMA_LOCK:
            if bool(getattr(self.character_repo, "_daily_challenge_schema_ready", False)):
                return
            with self.character_repo.open() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS character_daily_challenge_runs (
                        character_id INTEGER PRIMARY KEY,
                        day_key TEXT NOT NULL,
                        pvf_sha256 TEXT NOT NULL,
                        generated_level INTEGER NOT NULL CHECK (generated_level > 0),
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (character_id) REFERENCES characters(character_id)
                            ON DELETE CASCADE
                    );

                    CREATE TABLE IF NOT EXISTS character_daily_challenge_entries (
                        character_id INTEGER NOT NULL,
                        group_index INTEGER NOT NULL,
                        entry_index INTEGER NOT NULL,
                        slot_number INTEGER NOT NULL,
                        quest_id INTEGER NOT NULL,
                        target_value INTEGER NOT NULL,
                        current_value INTEGER NOT NULL,
                        reward_claimed INTEGER NOT NULL DEFAULT 0
                            CHECK (reward_claimed IN (0, 1)),
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (character_id, group_index, entry_index),
                        UNIQUE (character_id, quest_id),
                        FOREIGN KEY (character_id)
                            REFERENCES character_daily_challenge_runs(character_id)
                            ON DELETE CASCADE
                    );

                    CREATE INDEX IF NOT EXISTS idx_daily_challenge_entries_quest
                        ON character_daily_challenge_entries(character_id, quest_id);

                    CREATE TABLE IF NOT EXISTS character_daily_challenge_group_claims (
                        character_id INTEGER NOT NULL,
                        group_index INTEGER NOT NULL,
                        claimed INTEGER NOT NULL DEFAULT 0 CHECK (claimed IN (0, 1)),
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (character_id, group_index),
                        FOREIGN KEY (character_id)
                            REFERENCES character_daily_challenge_runs(character_id)
                            ON DELETE CASCADE
                    );
                    """
                )
            try:
                setattr(self.character_repo, "_daily_challenge_schema_ready", True)
            except Exception:
                pass

    @staticmethod
    def _run_matches(
        conn: Any,
        character_id: int,
        day_key: str,
        pvf_sha256: str,
    ) -> bool:
        row = conn.execute(
            "SELECT day_key, pvf_sha256 FROM character_daily_challenge_runs "
            "WHERE character_id = ?",
            (int(character_id),),
        ).fetchone()
        return bool(
            row is not None
            and str(row[0]) == str(day_key)
            and str(row[1]) == str(pvf_sha256)
        )

    @staticmethod
    def _load_state_tx(conn: Any, character_id: int) -> DailyChallengeState | None:
        run = conn.execute(
            "SELECT day_key, pvf_sha256, generated_level "
            "FROM character_daily_challenge_runs WHERE character_id = ?",
            (int(character_id),),
        ).fetchone()
        if run is None:
            return None
        rows = conn.execute(
            "SELECT group_index, entry_index, slot_number, quest_id, target_value, "
            "current_value, reward_claimed "
            "FROM character_daily_challenge_entries WHERE character_id = ? "
            "ORDER BY group_index, entry_index",
            (int(character_id),),
        ).fetchall()
        grouped: dict[int, list[DailyChallengeEntryState]] = {}
        for row in rows:
            entry = DailyChallengeEntryState(
                group_index=int(row[0]),
                entry_index=int(row[1]),
                slot_number=int(row[2]),
                quest_id=int(row[3]) & 0xFFFF,
                target_value=int(row[4]) & 0xFFFFFFFF,
                current_value=int(row[5]) & 0xFFFFFFFF,
                reward_claimed=bool(int(row[6])),
            )
            grouped.setdefault(entry.group_index, []).append(entry)
        groups = tuple(
            DailyChallengeGroupState(group_index, tuple(entries))
            for group_index, entries in sorted(grouped.items())
        )
        flags = [0] * STAGE3_CLAIM_FLAG_COUNT
        for row in conn.execute(
            "SELECT group_index, claimed FROM character_daily_challenge_group_claims "
            "WHERE character_id = ? ORDER BY group_index",
            (int(character_id),),
        ).fetchall():
            group_index = int(row[0])
            if 0 <= group_index < len(flags):
                flags[group_index] = 1 if int(row[1]) else 0
        return DailyChallengeState(
            character_id=int(character_id),
            day_key=str(run[0]),
            pvf_sha256=str(run[1]),
            generated_level=max(1, int(run[2])),
            groups=groups,
            claim_flags=tuple(flags),
        )

    def ensure_state(
        self,
        index: DailyChallengeIndex,
        *,
        character_id: int,
        day_key: str,
        level: int,
    ) -> DailyChallengeState:
        cid = int(character_id)
        generated_level = max(1, int(level))
        day = str(day_key or "").strip()
        fingerprint = str(index.pvf_sha256 or "").strip()
        if cid <= 0 or not day or not fingerprint:
            raise ValueError("daily challenge state identity is incomplete")

        with self.character_repo.open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            parent = conn.execute(
                "SELECT 1 FROM characters WHERE character_id = ? AND delete_flag = 0",
                (cid,),
            ).fetchone()
            if parent is None:
                raise ValueError("daily challenge character %d does not exist" % cid)
            if self._run_matches(conn, cid, day, fingerprint):
                state = self._load_state_tx(conn, cid)
                if state is None:
                    raise RuntimeError("matching daily challenge run has no state")
                return state

            selected = index.select_groups(
                character_id=cid,
                day_key=day,
                level=generated_level,
            )
            conn.execute(
                "DELETE FROM character_daily_challenge_runs WHERE character_id = ?",
                (cid,),
            )
            conn.execute(
                "INSERT INTO character_daily_challenge_runs "
                "(character_id, day_key, pvf_sha256, generated_level) "
                "VALUES (?, ?, ?, ?)",
                (cid, day, fingerprint, generated_level),
            )
            conn.executemany(
                "INSERT INTO character_daily_challenge_group_claims "
                "(character_id, group_index, claimed) VALUES (?, ?, 0)",
                [(cid, group_index) for group_index in range(STAGE3_CLAIM_FLAG_COUNT)],
            )
            entry_rows: list[tuple[int, int, int, int, int, int, int]] = []
            for group in selected:
                for entry in group.entries:
                    entry_rows.append(
                        (
                            cid,
                            int(group.group_index),
                            int(entry.entry_index),
                            int(entry.slot_number),
                            int(entry.quest_id) & 0xFFFF,
                            int(entry.target_value) & 0xFFFFFFFF,
                            int(entry.target_value) & 0xFFFFFFFF,
                        )
                    )
            conn.executemany(
                "INSERT INTO character_daily_challenge_entries "
                "(character_id, group_index, entry_index, slot_number, quest_id, "
                "target_value, current_value) VALUES (?, ?, ?, ?, ?, ?, ?)",
                entry_rows,
            )
            state = self._load_state_tx(conn, cid)
            if state is None:
                raise RuntimeError("daily challenge state rebuild produced no run")
            return state

    def load_matching_state(
        self,
        *,
        character_id: int,
        day_key: str,
        pvf_sha256: str,
    ) -> DailyChallengeState | None:
        with self.character_repo.open() as conn:
            if not self._run_matches(
                conn, int(character_id), str(day_key), str(pvf_sha256)
            ):
                return None
            return self._load_state_tx(conn, int(character_id))

    def mutate_entry(
        self,
        *,
        character_id: int,
        day_key: str,
        pvf_sha256: str,
        quest_id: int,
        mutation: Callable[[int, int], int],
    ) -> DailyChallengeMutation:
        cid = int(character_id)
        qid = int(quest_id) & 0xFFFF
        with self.character_repo.open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._run_matches(conn, cid, day_key, pvf_sha256):
                return DailyChallengeMutation(
                    status="unavailable",
                    found=False,
                    changed=False,
                    previous_value=0,
                    current_value=0,
                    target_value=0,
                    state=None,
                )
            row = conn.execute(
                "SELECT target_value, current_value, reward_claimed "
                "FROM character_daily_challenge_entries "
                "WHERE character_id = ? AND quest_id = ?",
                (cid, qid),
            ).fetchone()
            if row is None:
                state = self._load_state_tx(conn, cid)
                return DailyChallengeMutation(
                    status="missing",
                    found=False,
                    changed=False,
                    previous_value=0,
                    current_value=0,
                    target_value=0,
                    state=state,
                )
            if int(row[2]) != 0:
                state = self._load_state_tx(conn, cid)
                return DailyChallengeMutation(
                    status="claimed",
                    found=False,
                    changed=False,
                    previous_value=0,
                    current_value=0,
                    target_value=int(row[0]) & 0xFFFFFFFF,
                    state=state,
                )
            target = int(row[0]) & 0xFFFFFFFF
            previous = min(target, int(row[1]) & 0xFFFFFFFF)
            current = max(0, min(target, int(mutation(target, previous))))
            changed = current != previous
            if changed:
                conn.execute(
                    "UPDATE character_daily_challenge_entries "
                    "SET current_value = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE character_id = ? AND quest_id = ?",
                    (current, cid, qid),
                )
            state = self._load_state_tx(conn, cid)
            return DailyChallengeMutation(
                status="active",
                found=True,
                changed=changed,
                previous_value=previous,
                current_value=current,
                target_value=target,
                state=state,
            )

    def reserve_entry_claim(
        self,
        *,
        character_id: int,
        day_key: str,
        pvf_sha256: str,
        quest_id: int,
    ) -> DailyChallengeClaimReservation:
        cid = int(character_id)
        qid = int(quest_id) & 0xFFFF
        with self.character_repo.open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._run_matches(conn, cid, day_key, pvf_sha256):
                return DailyChallengeClaimReservation("unavailable", None)
            row = conn.execute(
                "SELECT group_index, entry_index, slot_number, target_value, "
                "current_value, reward_claimed "
                "FROM character_daily_challenge_entries "
                "WHERE character_id = ? AND quest_id = ?",
                (cid, qid),
            ).fetchone()
            state = self._load_state_tx(conn, cid)
            if row is None:
                return DailyChallengeClaimReservation("unavailable", state)
            entry = DailyChallengeEntryState(
                group_index=int(row[0]),
                entry_index=int(row[1]),
                slot_number=int(row[2]),
                quest_id=qid,
                target_value=int(row[3]) & 0xFFFFFFFF,
                current_value=int(row[4]) & 0xFFFFFFFF,
                reward_claimed=bool(int(row[5])),
            )
            if entry.reward_claimed:
                return DailyChallengeClaimReservation("already_claimed", state, entry)
            if entry.current_value != 0:
                return DailyChallengeClaimReservation("incomplete", state, entry)
            updated = conn.execute(
                "UPDATE character_daily_challenge_entries "
                "SET reward_claimed = 1, updated_at = CURRENT_TIMESTAMP "
                "WHERE character_id = ? AND quest_id = ? "
                "AND current_value = 0 AND reward_claimed = 0",
                (cid, qid),
            )
            if int(updated.rowcount or 0) != 1:
                state = self._load_state_tx(conn, cid)
                return DailyChallengeClaimReservation("already_claimed", state, entry)
            state = self._load_state_tx(conn, cid)
            return DailyChallengeClaimReservation("reserved", state, entry)

    def release_entry_claim(self, *, character_id: int, quest_id: int) -> None:
        with self.character_repo.open() as conn:
            conn.execute(
                "UPDATE character_daily_challenge_entries "
                "SET reward_claimed = 0, updated_at = CURRENT_TIMESTAMP "
                "WHERE character_id = ? AND quest_id = ?",
                (int(character_id), int(quest_id) & 0xFFFF),
            )

    def reserve_group_claim(
        self,
        index: DailyChallengeIndex,
        *,
        character_id: int,
        day_key: str,
        pvf_sha256: str,
        group_index: int,
    ) -> DailyChallengeClaimReservation:
        cid = int(character_id)
        group_id = int(group_index)
        with self.character_repo.open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._run_matches(conn, cid, day_key, pvf_sha256):
                return DailyChallengeClaimReservation("unavailable", None)
            state = self._load_state_tx(conn, cid)
            if state is None:
                return DailyChallengeClaimReservation("unavailable", None)
            entries = conn.execute(
                "SELECT current_value FROM character_daily_challenge_entries "
                "WHERE character_id = ? AND group_index = ?",
                (cid, group_id),
            ).fetchall()
            if not entries:
                return DailyChallengeClaimReservation("unavailable", state)
            claim = conn.execute(
                "SELECT claimed FROM character_daily_challenge_group_claims "
                "WHERE character_id = ? AND group_index = ?",
                (cid, group_id),
            ).fetchone()
            if claim is None:
                return DailyChallengeClaimReservation("unavailable", state)
            completed = sum(1 for row in entries if (int(row[0]) & 0xFFFFFFFF) == 0)
            required = index.required_completion_count(
                group_id, state.generated_level, len(entries)
            )
            reward = index.group_reward(group_id, state.generated_level)
            if required <= 0 or reward is None:
                return DailyChallengeClaimReservation(
                    "unavailable", state, reward=reward
                )
            if int(claim[0]) != 0:
                return DailyChallengeClaimReservation(
                    "already_claimed",
                    state,
                    reward=reward,
                    required_count=required,
                    completed_count=completed,
                )
            if completed < required:
                return DailyChallengeClaimReservation(
                    "incomplete",
                    state,
                    reward=reward,
                    required_count=required,
                    completed_count=completed,
                )
            updated = conn.execute(
                "UPDATE character_daily_challenge_group_claims "
                "SET claimed = 1, updated_at = CURRENT_TIMESTAMP "
                "WHERE character_id = ? AND group_index = ? AND claimed = 0",
                (cid, group_id),
            )
            if int(updated.rowcount or 0) != 1:
                state = self._load_state_tx(conn, cid)
                return DailyChallengeClaimReservation(
                    "already_claimed",
                    state,
                    reward=reward,
                    required_count=required,
                    completed_count=completed,
                )
            state = self._load_state_tx(conn, cid)
            return DailyChallengeClaimReservation(
                "reserved",
                state,
                reward=reward,
                required_count=required,
                completed_count=completed,
            )

    def release_group_claim(self, *, character_id: int, group_index: int) -> None:
        with self.character_repo.open() as conn:
            conn.execute(
                "UPDATE character_daily_challenge_group_claims "
                "SET claimed = 0, updated_at = CURRENT_TIMESTAMP "
                "WHERE character_id = ? AND group_index = ?",
                (int(character_id), int(group_index)),
            )

    def list_claimed_entry_ids(
        self,
        *,
        character_id: int,
        day_key: str,
        pvf_sha256: str,
    ) -> list[int]:
        with self.character_repo.open() as conn:
            if not self._run_matches(
                conn, int(character_id), str(day_key), str(pvf_sha256)
            ):
                return []
            rows = conn.execute(
                "SELECT quest_id FROM character_daily_challenge_entries "
                "WHERE character_id = ? AND reward_claimed = 1 "
                "ORDER BY group_index, entry_index",
                (int(character_id),),
            ).fetchall()
        return [int(row[0]) & 0xFFFF for row in rows]
