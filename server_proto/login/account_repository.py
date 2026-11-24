from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from data import DB_PATH


@dataclass(frozen=True)
class LoginAccount:
    account_id: int
    m_id: str
    password_hash: str


class AccountExistsError(RuntimeError):
    pass


class AccountLoginRepository:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path or DB_PATH).resolve()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def normalize_account(value: str) -> str:
        account = str(value or "").strip()
        if not 1 <= len(account) <= 64:
            raise ValueError("account length must be 1..64 characters")
        if any(char in account for char in ("\x00", "\r", "\n", "?")):
            raise ValueError("account contains an invalid character")
        return account

    def create_account(self, m_id: str, password_hash: str) -> LoginAccount:
        account = self.normalize_account(m_id)
        encoded = str(password_hash or "").strip()
        if not encoded:
            raise ValueError("password hash is required")
        conn = self._open()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = conn.execute(
                "INSERT INTO accounts (m_id, password_hash) VALUES (?, ?)",
                (account, encoded),
            )
            account_id = int(result.lastrowid)
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            if str(exc) == "UNIQUE constraint failed: accounts.m_id":
                raise AccountExistsError("account already exists") from exc
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return LoginAccount(account_id, account, encoded)

    def find_by_m_id(self, m_id: str) -> LoginAccount | None:
        account = self.normalize_account(m_id)
        conn = self._open()
        try:
            row = conn.execute(
                "SELECT account_id, m_id, password_hash FROM accounts WHERE m_id = ?",
                (account,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return LoginAccount(
            account_id=int(row["account_id"]),
            m_id=str(row["m_id"]),
            password_hash=str(row["password_hash"] or ""),
        )

    def set_password_hash(self, account_id: int, password_hash: str) -> None:
        encoded = str(password_hash or "").strip()
        if not encoded:
            raise ValueError("password hash is required")
        conn = self._open()
        try:
            result = conn.execute(
                "UPDATE accounts SET password_hash = ? WHERE account_id = ?",
                (encoded, int(account_id)),
            )
            if int(result.rowcount or 0) != 1:
                raise LookupError("account does not exist")
            conn.commit()
        finally:
            conn.close()

    def record_successful_login(self, account_id: int, remote_ip: str) -> None:
        timestamp = datetime.now().isoformat(timespec="seconds")
        conn = self._open()
        try:
            result = conn.execute(
                "UPDATE accounts SET last_login_ip = ?, last_login_at = ? "
                "WHERE account_id = ?",
                (str(remote_ip), timestamp, int(account_id)),
            )
            if int(result.rowcount or 0) != 1:
                raise LookupError("account does not exist")
            conn.commit()
        finally:
            conn.close()
