"""Private client-PVF release registry and authenticated HTTPS key delivery.

GM records live in the existing player repository; publication records metadata
and never replaces Script.pvf or changes the running game reader. Legacy import
is an explicit startup/management operation, never a public GET side effect.
Management calls belong only to the existing encrypted WinRM worker.
"""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import ssl
import uuid
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit


class PvfReleaseError(ValueError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _require(condition, message, status=400, code='INVALID_PVF_RELEASE'):
    if not condition:
        raise PvfReleaseError(status, code, message)


def _hex(value, length, label):
    _require(isinstance(value, str) and re.fullmatch('[0-9a-fA-F]{%d}' % length, value) is not None,
             label + ' must be exact hexadecimal')
    return value.lower()


def _url(value, legacy=False):
    _require(isinstance(value, str) and len(value) <= 4096, 'download URL is invalid')
    if not value:
        return value
    _require(not any(ord(char) <= 32 or ord(char) == 127 for char in value), 'download URL contains whitespace or control characters')
    try:
        parsed = urlsplit(value)
        valid = ((parsed.scheme == 'https' or (legacy and parsed.scheme == 'http')) and bool(parsed.hostname) and parsed.username is None
                 and parsed.password is None and not parsed.fragment and (parsed.port is None or 1 <= parsed.port <= 65535))
    except ValueError:
        valid = False
    _require(valid, 'download URL must be an HTTPS URL without embedded credentials')
    return value


def _payload(document, legacy=False):
    fields = {'pvf_version', 'file_sha256', 'key_set', 'download_url', 'client_download_url', 'enabled'}
    _require(isinstance(document, dict) and set(document) == fields, 'publication fields are invalid')
    version = document['pvf_version']
    _require(isinstance(version, str) and re.fullmatch(r'[A-Za-z0-9._-]{1,64}', version) is not None,
             'PVF version must be 1..64 letters, digits, dot, underscore or hyphen')
    _require(type(document['enabled']) is bool, 'enabled must be a boolean')
    return {'pvf_version': version, 'file_sha256': _hex(document['file_sha256'], 64, 'file SHA256'),
            'key_set': _hex(document['key_set'], 96, 'PVF key set'),
            'download_url': _url(document['download_url'],legacy), 'client_download_url': _url(document['client_download_url'],legacy),
            'enabled': document['enabled']}


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


MIGRATION_ID = 'legacy_launcher_pvf_releases_v1'
PVF_TABLES = ('gmkit_pvf_releases', 'gmkit_pvf_current', 'gmkit_pvf_publish_receipts')
GM_TABLES = PVF_TABLES + ('gmkit_storage_migrations',)
LEGACY_TABLES = ('pvf_releases', 'pvf_current', 'pvf_publish_receipts')
COLUMNS = (
    ('id', 'pvf_version', 'file_sha256', 'key_set', 'download_url', 'client_download_url', 'enabled', 'created_at', 'updated_at'),
    ('slot', 'release_id'),
    ('request_id', 'fingerprint', 'result'),
    ('migration_id', 'source_path', 'source_fingerprint', 'source_counts', 'source_backup_path',
     'source_backup_sha256', 'player_backup_path', 'player_backup_sha256', 'applied_at'),
)
SCHEMA = (
    '''CREATE TABLE gmkit_pvf_releases(
        id INTEGER PRIMARY KEY,pvf_version TEXT NOT NULL UNIQUE,file_sha256 TEXT NOT NULL,
        key_set TEXT NOT NULL,download_url TEXT NOT NULL,client_download_url TEXT NOT NULL,
        enabled INTEGER NOT NULL CHECK(enabled IN(0,1)),created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''',
    '''CREATE TABLE gmkit_pvf_current(slot INTEGER PRIMARY KEY CHECK(slot=1),
        release_id INTEGER NOT NULL REFERENCES gmkit_pvf_releases(id))''',
    '''CREATE TABLE gmkit_pvf_publish_receipts(request_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,result TEXT NOT NULL)''',
    '''CREATE TABLE gmkit_storage_migrations(migration_id TEXT PRIMARY KEY,source_path TEXT NOT NULL,
        source_fingerprint TEXT NOT NULL,source_counts TEXT NOT NULL,source_backup_path TEXT NOT NULL,
        source_backup_sha256 TEXT NOT NULL,player_backup_path TEXT NOT NULL,player_backup_sha256 TEXT NOT NULL,
        applied_at TEXT NOT NULL)''',
)


def _tables(connection):
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _validate_player(connection):
    names = _tables(connection)
    for name, identity in (('accounts', 'account_id'), ('characters', 'character_id')):
        _require(name in names and identity in {row[1] for row in connection.execute('PRAGMA table_info(' + name + ')')},
                 'existing player database schema is required; an empty player database will not be created',
                 503, 'PLAYER_DATABASE_UNAVAILABLE')


def _connect_player(path, write=False):
    path = Path(path).resolve()
    _require(path.is_file() and path.stat().st_size > 0, 'existing player database is unavailable', 503, 'PLAYER_DATABASE_UNAVAILABLE')
    connection = sqlite3.connect(path.as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=30 if write else 10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA busy_timeout=30000' if write else 'PRAGMA busy_timeout=10000')
        if write:
            connection.execute('PRAGMA synchronous=FULL')
        else:
            connection.execute('PRAGMA query_only=ON')
        _validate_player(connection)
        return connection
    except BaseException:
        connection.close()
        raise


def _check_schema(connection, names, columns, allow_absent=True):
    present = _tables(connection).intersection(names)
    if not present and allow_absent:
        return False
    _require(present == set(names), 'PVF management tables are incomplete; refusing partial state', 503, 'PVF_REGISTRY_UNAVAILABLE')
    for name, expected in zip(names, columns):
        info = list(connection.execute('PRAGMA table_info(' + name + ')'))
        _require({row[1] for row in info} == set(expected) and any(row[1] == expected[0] and row[5] == 1 for row in info),
                 'PVF management table schema is incompatible', 503, 'PVF_REGISTRY_UNAVAILABLE')
    return True


def _ensure_schema(connection):
    if not _check_schema(connection, GM_TABLES, COLUMNS):
        for sql in SCHEMA:
            connection.execute(sql)


def _snapshot(connection, names):
    if not _check_schema(connection, names, COLUMNS[:3]):
        return ([], [], [])
    rows = tuple([dict(row) for row in connection.execute('SELECT ' + ','.join(columns) + ' FROM ' + name + ' ORDER BY ' + columns[0])]
                 for name, columns in zip(names, COLUMNS[:3]))
    identities = {row['id'] for row in rows[0]}
    versions = {row['pvf_version'] for row in rows[0]}
    _require(len(identities) == len(rows[0]) and len(versions) == len(rows[0]), 'PVF release identities are invalid', 503, 'PVF_REGISTRY_UNAVAILABLE')
    for row in rows[0]:
        _require(type(row['id']) is int and row['enabled'] in (0, 1)
                 and isinstance(row['created_at'], str) and isinstance(row['updated_at'], str),
                 'PVF release record is invalid', 503, 'PVF_REGISTRY_UNAVAILABLE')
        _payload({key: bool(row[key]) if key == 'enabled' else row[key]
                  for key in ('pvf_version', 'file_sha256', 'key_set', 'download_url', 'client_download_url', 'enabled')},legacy=True)
    _require(len(rows[1]) <= 1 and all(row['slot'] == 1 and row['release_id'] in identities for row in rows[1]),
             'PVF current release reference is invalid', 503, 'PVF_REGISTRY_UNAVAILABLE')
    for row in rows[2]:
        _require(isinstance(row['request_id'], str) and 1 <= len(row['request_id']) <= 128
                 and not any(char in row['request_id'] for char in '\x00\r\n'), 'PVF publication receipt is invalid', 503, 'PVF_REGISTRY_UNAVAILABLE')
        _hex(row['fingerprint'], 64, 'receipt fingerprint')
        try:
            result = json.loads(row['result'])
        except (ValueError, TypeError):
            result = None
        _require(isinstance(result, dict) and type(result.get('id')) is int and result['id'] in identities,
                 'PVF publication receipt reference is invalid', 503, 'PVF_REGISTRY_UNAVAILABLE')
    return rows


def _fingerprint(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('utf-8')).hexdigest()


def _counts(rows):
    return dict(zip(('releases', 'current', 'receipts'), map(len, rows)))


def _marker_counts(marker, target_rows):
    """Validate retained metadata without requiring historical backup files."""
    try:
        counts = json.loads(marker['source_counts'])
    except (ValueError, TypeError):
        counts = None
    keys = {'releases', 'current', 'receipts'}
    _require(isinstance(counts, dict) and set(counts) == keys
             and all(type(value) is int and value >= 0 for value in counts.values()),
             'PVF migration marker counts are invalid', 503, 'PVF_MIGRATION_MARKER_INVALID')
    actual = _counts(target_rows)
    _require(counts['current'] <= 1 and all(counts[key] <= actual[key] for key in keys)
             and (counts['releases'] > 0 or counts['current'] == counts['receipts'] == 0),
             'PVF migration marker does not match the retained GM state', 503, 'PVF_MIGRATION_MARKER_INVALID')
    for field in ('source_fingerprint', 'player_backup_sha256'):
        _require(isinstance(marker[field], str) and re.fullmatch('[0-9a-fA-F]{64}', marker[field]) is not None,
                 'PVF migration marker digest is invalid', 503, 'PVF_MIGRATION_MARKER_INVALID')
    for field in ('source_path', 'player_backup_path'):
        value = marker[field]
        _require(isinstance(value, str) and bool(value) and not any(char in value for char in '\x00\r\n') and Path(value).is_absolute(),
                 'PVF migration marker historical path is invalid', 503, 'PVF_MIGRATION_MARKER_INVALID')
    path, digest = marker['source_backup_path'], marker['source_backup_sha256']
    _require((path == digest == '' and not any(counts.values()))
             or (isinstance(path, str) and bool(path) and not any(char in path for char in '\x00\r\n')
                 and Path(path).is_absolute() and isinstance(digest, str) and re.fullmatch('[0-9a-fA-F]{64}', digest) is not None),
             'PVF migration marker source backup metadata is invalid', 503, 'PVF_MIGRATION_MARKER_INVALID')
    try:
        applied = datetime.fromisoformat(marker['applied_at'].replace('Z', '+00:00'))
    except (ValueError, TypeError, AttributeError):
        applied = None
    _require(applied is not None and applied.tzinfo is not None,
             'PVF migration marker timestamp is invalid', 503, 'PVF_MIGRATION_MARKER_INVALID')
    if not any(counts.values()):
        _require(marker['source_fingerprint'] == _fingerprint(([], [], [])),
                 'empty PVF migration marker fingerprint is invalid', 503, 'PVF_MIGRATION_MARKER_INVALID')
    return counts


def _compatible_import(source, target):
    for old, new, columns in zip(source, target, COLUMNS[:3]):
        keyed = {row[columns[0]]: row for row in new}
        for row in old:
            _require(row[columns[0]] not in keyed or keyed[row[columns[0]]] == row,
                     'existing GM PVF state conflicts with legacy data; nothing was migrated', 409, 'PVF_MIGRATION_CONFLICT')
    versions = {row['pvf_version']: row for row in target[0]}
    for row in source[0]:
        _require(row['pvf_version'] not in versions or versions[row['pvf_version']] == row,
                 'existing GM PVF version conflicts with legacy data; nothing was migrated', 409, 'PVF_MIGRATION_CONFLICT')


def _sha_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _backup(connection, path):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with closing(sqlite3.connect(path)) as backup:
        connection.backup(backup)
        _require(backup.execute('PRAGMA quick_check').fetchone()[0] == 'ok',
                 'consistent migration backup failed integrity validation', 503, 'PVF_MIGRATION_BACKUP_FAILED')
    return _sha_file(path)


def _backup_pair(player, source, directory, rows):
    directory = Path(directory).resolve()
    _require(not any(part.lower() in ('wwwroot', 'public', 'static', 'htdocs', 'public_html') for part in directory.parts),
             'migration backups require a private server directory', 400, 'PVF_MIGRATION_BACKUP_FAILED')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex
    player_copy = directory / ('player-before-pvf-' + token + '.sqlite3')
    # A separate read connection can snapshot the player while the destination
    # connection holds BEGIN IMMEDIATE, before its first DDL/data change.
    with closing(_connect_player(player)) as reader:
        player_sha = _backup(reader, player_copy)
    source_copy = directory / ('legacy-pvf-' + token + '.sqlite3') if source is not None else None
    source_sha = _backup(source, source_copy) if source_copy is not None else ''
    result = {'source_backup_path': str(source_copy) if source_copy else '', 'source_backup_sha256': source_sha,
              'player_backup_path': str(player_copy), 'player_backup_sha256': player_sha}
    manifest = directory / ('pvf-migration-' + token + '.json')
    descriptor = os.open(manifest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        json.dump(dict(result, source_counts=_counts(rows), source_fingerprint=_fingerprint(rows), created_at=_stamp()), handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    return dict(result, backup_manifest=str(manifest))


def migrate_legacy_registry(player_database, legacy_database=None, backup_directory=None, *, apply=False):
    """Explicit private startup/admin migration. Dry-run and all public reads never write.

    Copies a fixed SQLite snapshot, including WAL, into a single destination
    transaction. A marker prevents re-import over later legitimate publications.
    """
    _require(type(apply) is bool, 'migration apply must be a boolean', 400, 'INVALID_PVF_MIGRATION')
    player = Path(player_database).resolve()
    legacy = Path(legacy_database).resolve() if legacy_database is not None else player.parent / 'launcher_pvf_releases.sqlite3'
    directory = Path(backup_directory) if backup_directory is not None else player.parent / 'gmkit_migration_backups'
    _require(player != legacy, 'player database and legacy registry must be different files', 400, 'PVF_MIGRATION_CONFLICT')
    source = destination = None
    try:
        destination = _connect_player(player, write=apply)
        _require(destination.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'player database integrity check failed', 503, 'PLAYER_DATABASE_UNAVAILABLE')
        if apply:
            destination.execute('BEGIN IMMEDIATE')
        else:
            destination.execute('BEGIN')
        source_rows = ([], [], [])
        if legacy.exists():
            _require(legacy.is_file(), 'legacy registry is not a file', 503, 'PVF_MIGRATION_SOURCE_INVALID')
            source = sqlite3.connect(legacy.as_uri() + '?mode=ro', uri=True, timeout=30)
            source.row_factory = sqlite3.Row
            source.execute('PRAGMA query_only=ON')
            source.execute('BEGIN')
            _require(source.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'legacy PVF registry integrity check failed', 503, 'PVF_MIGRATION_SOURCE_INVALID')
            source_names = _tables(source) - {'sqlite_sequence', 'sqlite_stat1', 'sqlite_stat4'}
            _require(not source_names or source_names == set(LEGACY_TABLES),
                     'legacy SQLite database has an unknown schema; refusing to treat it as an empty PVF registry', 503, 'PVF_MIGRATION_SOURCE_INVALID')
            source_rows = _snapshot(source, LEGACY_TABLES)
            if not _tables(source):
                source_rows = ([], [], [])
        fingerprint = _fingerprint(source_rows)
        have_schema = _check_schema(destination, GM_TABLES, COLUMNS)
        marker = destination.execute('SELECT * FROM gmkit_storage_migrations WHERE migration_id=?', (MIGRATION_ID,)).fetchone() if have_schema else None
        report = {'player_database': str(player), 'legacy_database': str(legacy), 'source_counts': _counts(source_rows), 'source_fingerprint': fingerprint, 'applied': False}
        target_rows = _snapshot(destination, PVF_TABLES) if have_schema else ([], [], [])
        if marker is not None:
            counts = _marker_counts(marker, target_rows)
            # Once committed, the player DB is the sole runtime store. The old
            # file and all backup paths are optional historical artifacts. If a
            # legacy file is still present, detect a stale writer's changes.
            if source is not None:
                _require(marker['source_fingerprint'] == fingerprint and counts == _counts(source_rows),
                         'legacy PVF snapshot changed after migration; refusing to overwrite current GM publications', 409, 'PVF_MIGRATION_SOURCE_CHANGED')
            report.update(source_counts=counts, source_fingerprint=marker['source_fingerprint'])
            destination.rollback()
            return dict(report, status='already_migrated')
        _require(not have_schema or source is not None,
                 'existing GM PVF tables have no migration marker or legacy source; refusing an invented empty migration', 503, 'PVF_MIGRATION_MARKER_INVALID')
        _compatible_import(source_rows, target_rows)
        if not apply:
            destination.rollback()
            return dict(report, status='planned')
        backups = _backup_pair(player, source, directory, source_rows)
        _ensure_schema(destination)
        for names, columns, old, new in zip(PVF_TABLES, COLUMNS[:3], source_rows, target_rows):
            identities = {row[columns[0]] for row in new}
            for row in old:
                if row[columns[0]] not in identities:
                    destination.execute('INSERT INTO ' + names + '(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')', tuple(row[column] for column in columns))
        destination.execute('INSERT INTO gmkit_storage_migrations VALUES(?,?,?,?,?,?,?,?,?)',
            (MIGRATION_ID, str(legacy), fingerprint, json.dumps(_counts(source_rows), sort_keys=True),
             backups['source_backup_path'], backups['source_backup_sha256'], backups['player_backup_path'], backups['player_backup_sha256'], _stamp()))
        _require(not list(destination.execute('PRAGMA foreign_key_check(gmkit_pvf_current)')),
                 'imported PVF current reference failed validation', 503, 'PVF_MIGRATION_CONFLICT')
        destination.commit()
        return dict(report, status='migrated', applied=True, **backups)
    except (sqlite3.Error, OSError, ValueError) as error:
        if destination is not None:
            destination.rollback()
        if isinstance(error, PvfReleaseError):
            raise
        raise PvfReleaseError(503, 'PVF_MIGRATION_FAILED', 'PVF management initialization failed; player transaction was not committed and existing data were not overwritten') from None
    finally:
        if source is not None:
            source.close()
        if destination is not None:
            destination.close()


class PvfReleaseRegistry:
    def __init__(self, path, *, tls_configuration_path=None):
        self.path = Path(path).resolve()
        self.tls_configuration_path = Path(tls_configuration_path).resolve() if tls_configuration_path is not None else None

    def initialize(self, *, legacy_path=None, backup_directory=None):
        return migrate_legacy_registry(self.path, legacy_path, backup_directory, apply=True)

    @contextmanager
    def _write(self):
        connection = _connect_player(self.path, write=True)
        try:
            connection.execute('BEGIN IMMEDIATE')
            _ensure_schema(connection)
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def publish(self, payload, request_id):
        values = _payload(payload)
        _require(isinstance(request_id, str) and 1 <= len(request_id) <= 128
                 and not any(char in request_id for char in '\x00\r\n'), 'publication request ID is invalid')
        fingerprint = hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
        try:
            with self._write() as connection:
                previous = connection.execute('SELECT fingerprint,result FROM gmkit_pvf_publish_receipts WHERE request_id=?', (request_id,)).fetchone()
                if previous is not None:
                    _require(previous['fingerprint'] == fingerprint, 'request ID already belongs to another publication', 409, 'REQUEST_ID_CONFLICT')
                    return json.loads(previous['result'])
                existing = connection.execute('SELECT * FROM gmkit_pvf_releases WHERE pvf_version=?', (values['pvf_version'],)).fetchone()
                now = _stamp()
                if existing is None:
                    cursor = connection.execute('INSERT INTO gmkit_pvf_releases(pvf_version,file_sha256,key_set,download_url,client_download_url,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                        (values['pvf_version'], values['file_sha256'], values['key_set'], values['download_url'], values['client_download_url'], int(values['enabled']), now, now))
                    identity = cursor.lastrowid
                else:
                    _require(existing['file_sha256'] == values['file_sha256'] and existing['key_set'] == values['key_set'],
                             'version already identifies a different file or key; use a new version', 409, 'PVF_VERSION_CONFLICT')
                    identity = existing['id']
                    connection.execute('UPDATE gmkit_pvf_releases SET download_url=?,client_download_url=?,enabled=?,updated_at=? WHERE id=?',
                        (values['download_url'], values['client_download_url'], int(values['enabled']), now, identity))
                if values['enabled']:
                    connection.execute('INSERT INTO gmkit_pvf_current(slot,release_id) VALUES(1,?) ON CONFLICT(slot) DO UPDATE SET release_id=excluded.release_id', (identity,))
                result = {'id': identity}
                connection.execute('INSERT INTO gmkit_pvf_publish_receipts VALUES(?,?,?)', (request_id, fingerprint, json.dumps(result)))
                return result
        except sqlite3.Error:
            raise PvfReleaseError(503, 'PVF_REGISTRY_UNAVAILABLE', 'PVF publication could not be committed; check the original request receipt before retrying') from None

    def _rows(self, current=False):
        try:
            with closing(_connect_player(self.path)) as connection:
                if not _check_schema(connection, GM_TABLES, COLUMNS):
                    return []
                _require(connection.execute('SELECT 1 FROM gmkit_pvf_current c LEFT JOIN gmkit_pvf_releases r ON c.release_id=r.id WHERE c.slot!=1 OR r.id IS NULL').fetchone() is None,
                         'PVF current release reference is invalid', 503, 'PVF_REGISTRY_UNAVAILABLE')
                sql = ('SELECT r.* FROM gmkit_pvf_releases r JOIN gmkit_pvf_current c ON c.release_id=r.id WHERE c.slot=1'
                       if current else 'SELECT * FROM gmkit_pvf_releases ORDER BY id DESC')
                return [dict(row, enabled=bool(row['enabled'])) for row in connection.execute(sql)]
        except sqlite3.Error:
            raise PvfReleaseError(503, 'PVF_REGISTRY_UNAVAILABLE', 'PVF release registry cannot be read; no fallback key was returned') from None

    def management_items(self):
        """Private management result. Never expose this through a public HTTP route."""
        return self._rows()

    @staticmethod
    def _public(record):
        return {key: record[key] for key in ('pvf_version', 'file_sha256', 'download_url', 'client_download_url')}

    def latest(self):
        rows = self._rows(current=True)
        return {'enabled': rows[0]['enabled'], 'data': self._public(rows[0])} if rows else {'enabled': False, 'data': None}

    def key_for_file(self, file_sha256):
        requested = _hex(file_sha256, 64, 'client file SHA256')
        rows = self._rows(current=True)
        if not rows or not rows[0]['enabled']:
            return {'enabled': False, 'data': None}
        current = rows[0]
        _require(current['file_sha256'] == requested, 'client PVF does not match the current publication', 404, 'PVF_UPDATE_REQUIRED')
        return {'enabled': True, 'data': {'pvf_version': current['pvf_version'], 'file_sha256': current['file_sha256'],
                                         'key_set': current['key_set'], 'pvf_profile': 'keyset96'}}


def launcher_pvf_get(registry, sessions, path, query, headers, remote_ip, secure_transport=False):
    """C# HTTPS response envelope. Source-IP checks use the existing login registry."""
    if path == '/launcher/v1/pvf/latest':
        return registry.latest()
    if path != '/launcher/v1/pvf/key':
        return NotImplemented
    _require(secure_transport is True, 'PVF key delivery requires HTTPS', 426, 'HTTPS_REQUIRED')
    # A ticket in a query string would appear in proxy/access logs. The Py branch
    # of the C# launcher must send its existing access_token as a Bearer header.
    _require(isinstance(query, dict) and set(query) == {'file_sha256'}, 'only file_sha256 may appear in the query')
    auth = str(headers.get('Authorization') or '')
    token = auth[7:] if auth.startswith('Bearer ') else ''
    _require(bool(token) and len(token) <= 4096, 'invalid launcher session', 401, 'AUTH_FAILED')
    identity = sessions.authenticate(token)
    _require(identity is not None, 'invalid launcher session', 401, 'AUTH_FAILED')
    try:
        matches = str(ipaddress.ip_address(identity.remote_ip)) == str(ipaddress.ip_address(remote_ip))
    except (ValueError, AttributeError):
        matches = False
    _require(matches, 'launcher session source IP mismatch', 401, 'AUTH_FAILED')
    values = query['file_sha256']
    _require(isinstance(values, list) and len(values) == 1, 'one file_sha256 is required')
    return registry.key_for_file(values[0])


def decorate_meta(document, registry):
    """Keep public meta key-free while advertising the active client PVF identity."""
    latest = registry.latest()
    result = dict(document)
    result['pvf_release'] = latest
    if latest['enabled'] and latest['data'] is not None:
        stage = dict(document.get('stage3') or {})
        stage.update(pvf_profile='keyset96', pvf_sha256=latest['data']['file_sha256'], pvf_version=latest['data']['pvf_version'])
        result['stage3'] = stage
    return result


def worker_pvf_action(bridge, action, payload, request_id):
    """Insert before Bridge.execute's normal dispatch; no new listener is added."""
    if action not in ('pvf.publish', 'pvf.status'):
        return NotImplemented
    registry = worker_pvf_registry(bridge)
    if action == 'pvf.publish':
        result = registry.publish(payload, request_id)
        return {'message': '客户端 PVF 版本与密钥已记录；未替换服务器 Script.pvf，未重启游戏服务。', 'result': result}
    _require(isinstance(payload, dict) and not payload, 'PVF status takes no arbitrary file paths')
    return {'message': '已读取受信管理通道中的 PVF 发布记录。', 'result': {'items': registry.management_items()}}


def worker_pvf_registry(bridge):
    """Bind to the same existing player DB as both trusted store and backend."""
    player = Path(bridge.store.database).resolve()
    _require(player == Path(bridge.backend.db).resolve(),
             'GM store and player repository database paths disagree; refusing PVF access', 503, 'PLAYER_DATABASE_MISMATCH')
    return PvfReleaseRegistry(player, tls_configuration_path=player.parent / 'launcher_tls.json')


def create_launcher_tls(owner, handler_class, environ=None):
    """Optional additional listener; never replace the existing HTTP listener.

    The caller owns serve_forever/shutdown. Certificate pin distribution belongs
    to the encrypted management channel; no trust-all client policy is implied.
    """
    environment = os.environ if environ is None else environ
    cert, key = environment.get('USDOF_LAUNCHER_TLS_CERT', ''), environment.get('USDOF_LAUNCHER_TLS_KEY', '')
    port_text = environment.get('USDOF_LAUNCHER_TLS_PORT', '')
    if not cert and not key and not port_text:
        # Passing environ explicitly is an isolated test/embedding contract and
        # never reads an ambient configuration file. In production the fixed
        # private JSON survives the launcher's deliberate USDOF_* env rebuild.
        registry = getattr(owner, 'pvf_release_registry', None)
        if environ is not None or registry is None:
            return None
        configuration = registry.tls_configuration_path or registry.path.parent / 'launcher_tls.json'
        if not configuration.exists():
            return None
        try:
            _require(configuration.is_file() and configuration.stat().st_size <= 8192,
                     'launcher TLS configuration file is invalid', 503, 'TLS_CONFIG_ERROR')
            document = json.loads(configuration.read_text(encoding='utf-8-sig'))
            _require(isinstance(document, dict) and set(document) == {'certificate_file', 'private_key_file', 'port'},
                     'launcher TLS configuration fields are invalid', 503, 'TLS_CONFIG_ERROR')
            cert, key, port_text = document['certificate_file'], document['private_key_file'], document['port']
            _require(type(port_text) is int and 1 <= port_text <= 65535,
                     'launcher TLS configuration port must be an integer', 503, 'TLS_CONFIG_ERROR')
            for value in (cert, key):
                _require(isinstance(value, str) and bool(value) and len(value) <= 4096
                         and not any(char in value for char in '\x00\r\n') and Path(value).is_absolute(),
                         'launcher TLS certificate paths must be absolute local file paths', 503, 'TLS_CONFIG_ERROR')
        except (OSError, ValueError) as error:
            if isinstance(error, PvfReleaseError):
                raise
            raise PvfReleaseError(503, 'TLS_CONFIG_ERROR', 'launcher TLS configuration cannot be read or parsed') from None
    _require(isinstance(cert, str) and bool(cert) and isinstance(key, str) and bool(key),
             'launcher TLS certificate and private key must both be configured', 503, 'TLS_CONFIG_ERROR')
    try:
        port = int(port_text or '17605')
    except (ValueError, TypeError):
        raise PvfReleaseError(503, 'TLS_CONFIG_ERROR', 'launcher TLS port must be a valid integer') from None
    _require(1 <= port <= 65535 and port != owner.port, 'launcher TLS port is invalid or conflicts with the existing HTTP port', 503, 'TLS_CONFIG_ERROR')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=cert, keyfile=key)
    server = ThreadingHTTPServer((owner.host, port), handler_class)
    try:
        # Defer handshake to Handler.setup in the per-connection thread. An idle
        # TCP peer must not block the listener's accept loop indefinitely.
        server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    except Exception:
        server.server_close()
        raise
    return server
