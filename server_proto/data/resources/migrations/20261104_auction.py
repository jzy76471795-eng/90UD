"""Add auction tables to an existing compatible save, without rewriting player rows.

Usage: python -B migrate_old_database.py --database <db> --server-proto <server_proto>
Stop the server first and retain a verified LOCAL backup before running.
No backup file is created on the runtime host.
"""
import argparse,json,sqlite3,sys
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--database',required=True);p.add_argument('--server-proto',required=True)
args=p.parse_args();db=Path(args.database).resolve();code=Path(args.server_proto).resolve()
assert db.is_file() and code.is_dir()
sys.path.insert(0,str(code))
from game.auction_store import ensure_schema
with sqlite3.connect(db,timeout=30) as conn:
    conn.execute('PRAGMA foreign_keys=ON');conn.execute('BEGIN IMMEDIATE')
    before={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {'characters','character_items','character_wallets','gmkit_mails','gmkit_mail_items'}<=before,'Apply the existing game/mail save compatibility first'
    assert conn.execute('PRAGMA integrity_check').fetchall()==[('ok',)]
    ensure_schema(conn)
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    after={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
print(json.dumps(dict(success=True,added_tables=sorted(after-before),player_rows_rewritten=False)))
