"""Game-owned mailbox persistence; inventory claims share the native transaction.

Go creates letters in the same tables. This module does not expose an HTTP API,
register accounts, redeem CDKs, or run an administrative worker.
"""
from __future__ import annotations
import contextlib
import hashlib
import hmac
import json
import sqlite3
from datetime import datetime
from pathlib import Path


class MailError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def stamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def number(value, label, minimum=0, maximum=2_000_000_000):
    if isinstance(value, bool):
        raise MailError(label + "必须为整数")
    try:
        result = int(value)
        if str(result) != str(value) or not minimum <= result <= maximum:
            raise ValueError()
    except (ValueError, TypeError, OverflowError):
        raise MailError(label + "整数范围无效") from None
    return result


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class MailStore:
    def __init__(self, database):
        self.database = Path(database).resolve()

    def connect(self):
        # A missing database must fail; do not create an empty replacement.
        conn = sqlite3.connect(self.database.as_uri() + "?mode=rw", uri=True,
                               timeout=5, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @contextlib.contextmanager
    def transaction(self, conn=None):
        if conn is not None:
            yield conn
            return
        opened = self.connect()
        try:
            opened.execute("BEGIN IMMEDIATE")
            yield opened
            opened.commit()
        except BaseException:
            opened.rollback()
            raise
        finally:
            opened.close()

    def audit(self, action, summary, actor="game", request_id="", outcome="confirmed", conn=None):
        with self.transaction(conn) as tx:
            tx.execute("INSERT INTO gmkit_audit(action,summary,actor,request_id,outcome,created_at) VALUES(?,?,?,?,?,?)",
                       (action, summary[:600], actor[:100], request_id[:128], outcome, stamp()))

    def claim_mail(self, mail_id, character_id, award, conn):
        row = conn.execute("SELECT * FROM gmkit_mails WHERE id=?", (number(mail_id, "邮件ID", 1),)).fetchone()
        if row is None:
            raise MailError("邮件不存在", 404)
        if row["character_id"] != int(character_id):
            raise MailError("邮件不属于当前角色", 403)
        if row["status"] != "pending":
            raise MailError("邮件已领取，请勿重复领取", 409)
        result = award(dict(row))
        changed = conn.execute("UPDATE gmkit_mails SET status='claimed',claimed_at=? WHERE id=? AND status='pending'",
                               (stamp(), row["id"])).rowcount
        if changed != 1:
            raise MailError("邮件状态已变化", 409)
        self.audit("mail.claim", f"邮件#{row['id']}已发入角色#{character_id}", str(character_id), conn=conn)
        return result

    def send_mail(self, payload, conn):
        """Insert a player letter using the caller's inventory transaction.

        The native player-mail owner validates and removes the sender's assets
        in this same transaction. Administrative mail is written by Go.
        """
        if not conn.in_transaction:
            raise MailError("玩家邮件必须与资产扣除使用同一事务", 409)
        cid = number(payload.get("characterId"), "角色ID", 1)
        if conn.execute("SELECT 1 FROM characters WHERE character_id=? AND delete_flag=0", (cid,)).fetchone() is None:
            raise MailError("收件角色不存在", 404)
        title, body = payload.get("title"), payload.get("body", "")
        if not isinstance(title, str) or not 1 <= len(title) <= 80 or not isinstance(body, str) or len(body) > 2000:
            raise MailError("邮件内容格式无效")
        gold = number(payload.get("gold", 0), "金币", 0, 999999999)
        return int(conn.execute("INSERT INTO gmkit_mails(character_id,title,body,gold,created_at) VALUES(?,?,?,?,?)",
                               (cid, title, body, gold, stamp())).lastrowid)

    @staticmethod
    def config(conn, section):
        row = conn.execute("SELECT value FROM gmkit_config WHERE section=?", (section,)).fetchone()
        value = json.loads(row[0]) if row else {}
        if not isinstance(value, dict):
            raise ValueError("invalid game policy")
        return value

    @staticmethod
    def _active(expiry):
        if not expiry:
            return True
        try:
            return datetime.fromisoformat(expiry).timestamp() > datetime.now().timestamp()
        except (ValueError, TypeError):
            return True

    def allows(self, conn, aid, remote_ip="", device_id=""):
        bans = conn.execute("""SELECT expires_at FROM gmkit_ban_rules
            WHERE (revoked_at IS NULL OR revoked_at='') AND
            ((kind='account' AND value=?) OR (kind='ip' AND value=? AND value<>'')
            OR (kind='machine' AND value=? AND value<>''))
            UNION ALL SELECT expires_at FROM gmkit_bans WHERE account_id=?""",
            (str(aid), remote_ip, device_id, aid))
        if any(self._active(row[0]) for row in bans):
            return False
        policy = self.config(conn, "maintenancePolicy")
        if not policy:
            policy = {"enabled": self.config(conn, "server").get("maintenance")}
        if type(policy.get("enabled")) is not bool:
            return False
        return not policy["enabled"] or aid in policy.get("allowAccountIds", [])

    def identity_allowed(self, identity):
        with self.connect() as conn:
            row = conn.execute("SELECT password_hash FROM accounts WHERE account_id=?", (identity.account_id,)).fetchone()
            expected = getattr(identity, "credential_digest", "")
            return bool(row and expected and hmac.compare_digest(
                hashlib.sha256(str(row[0]).encode()).hexdigest(), expected)
                and self.allows(conn, identity.account_id, identity.remote_ip, identity.device_id))


ITEM_ID_BASE = 0x40000000


def attachments(conn, mid):
    return [dict(r) for r in conn.execute("SELECT * FROM gmkit_mail_items WHERE mail_id=? ORDER BY position", (mid,))]


def resolve_claim(conn, cid, ids):
    result, seen = [], set()
    for wire in ids:
        if wire >= ITEM_ID_BASE:
            row = conn.execute("SELECT i.*,m.character_id,m.status AS mail_status FROM gmkit_mail_items i JOIN gmkit_mails m ON m.id=i.mail_id WHERE i.id=?", (wire - ITEM_ID_BASE,)).fetchone()
            if row is None or row['character_id'] != cid or row['mail_status'] not in ('pending', 'claimed'):
                raise ValueError('附件不存在或不属于当前角色。')
            candidates = [dict(row)]
        else:
            parent = conn.execute("SELECT * FROM gmkit_mails WHERE id=? AND character_id=? AND status IN ('pending','claimed')", (wire, cid)).fetchone()
            if parent is None:
                raise ValueError('邮件不存在或不属于当前角色。')
            candidates = attachments(conn, wire) or [dict(parent, legacy=True)]
        for row in candidates:
            key = (bool(row.get('legacy')), row['id'])
            if key not in seen:
                seen.add(key)
                result.append(row)
    return result
