"""Personal raid-clear credit for the active Anton nerve-cell daily quest."""
QUEST_ID = 10019
RAID_DUNGEON_ID = 219
HIDE_ID = 10157840
CELL_ID = 10157918


class MissingQuestReward(RuntimeError):
    """Roll back a completion when the normal reward could not be delivered."""


def record_quest_clear(repo, character_id, account_id):
    with repo.open() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS anton_volcano_quest_credit (
            character_id INTEGER PRIMARY KEY, accepted_at INTEGER NOT NULL
        )""")
        conn.execute("""CREATE TRIGGER IF NOT EXISTS clear_anton_credit_on_quest_removal
            AFTER DELETE ON character_active_quests WHEN OLD.quest_id = 10019
            BEGIN DELETE FROM anton_volcano_quest_credit WHERE character_id = OLD.character_id; END""")
        conn.execute("""INSERT INTO anton_volcano_quest_credit (character_id, accepted_at)
            SELECT a.character_id, a.accepted_at FROM character_active_quests a
            JOIN characters c ON c.character_id = a.character_id
            WHERE a.character_id = ? AND a.quest_id = ? AND c.account_id = ?
            ON CONFLICT(character_id) DO UPDATE SET accepted_at=excluded.accepted_at""",
            (character_id, QUEST_ID, account_id))


def has_quest_clear(repo, character_id):
    with repo.open() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='anton_volcano_quest_credit'").fetchone():
            return False
        return conn.execute("""SELECT 1 FROM anton_volcano_quest_credit c
            JOIN character_active_quests a ON a.character_id=c.character_id AND a.accepted_at=c.accepted_at
            WHERE c.character_id=? AND a.quest_id=?""", (character_id, QUEST_ID)).fetchone() is not None
