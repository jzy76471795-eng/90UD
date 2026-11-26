"""Stream the one current client resource package over the existing HTTPS API."""
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import threading

from login.pvf_releases import PvfReleaseError

_downloads = threading.BoundedSemaphore(3)
_lock = threading.Lock()
_verified = None


def send_current(handler, registry):
    if not isinstance(handler.connection, ssl.SSLSocket):
        raise PvfReleaseError(426, 'HTTPS_REQUIRED', 'client resources require HTTPS')
    if not _downloads.acquire(blocking=False):
        raise PvfReleaseError(503, 'DOWNLOAD_BUSY', 'client download slots are busy; retry shortly')
    try:
        root = Path(os.environ['USDOF_REPO_ROOT']).resolve()
        config = root / 'client_resources.json'
        package = root / 'client_resources.zip'
        if config.is_symlink() or package.is_symlink() or config.stat().st_size > 8192:
            raise ValueError('invalid resource files')
        manifest = json.loads(config.read_text('utf8'))
        if (set(manifest) != {'sha256', 'bytes', 'pvf_sha256'}
                or not all(re.fullmatch('[0-9a-f]{64}', manifest[k]) for k in ('sha256','pvf_sha256'))
                or type(manifest['bytes']) is not int or not 0 < manifest['bytes'] < 32*1024**3):
            raise ValueError('invalid resource manifest')
        latest = registry.latest()
        if not latest['enabled'] or latest['data']['file_sha256'] != manifest['pvf_sha256']:
            raise ValueError('resource version does not match current publication')
        with package.open('rb') as stream:
            stat = os.fstat(stream.fileno())
            identity = (str(package),stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,manifest['sha256'])
            global _verified
            with _lock:
                if stat.st_size != manifest['bytes']:
                    raise ValueError('resource size mismatch')
                if _verified != identity:
                    if hashlib.file_digest(stream,'sha256').hexdigest() != manifest['sha256']:
                        raise ValueError('resource hash mismatch')
                    _verified = identity
                stream.seek(0)
            handler.send_response(200)
            handler.send_header('Content-Type','application/zip')
            handler.send_header('Content-Length',str(stat.st_size))
            handler.send_header('Cache-Control','no-store')
            handler.send_header('Connection','close')
            handler.end_headers()
            while chunk := stream.read(1024*1024):
                handler.wfile.write(chunk)
    except (BrokenPipeError,ConnectionResetError,TimeoutError):
        handler.close_connection = True
    except (OSError,ValueError,KeyError,TypeError):
        raise PvfReleaseError(503,'RESOURCE_UNAVAILABLE','current client resource package is unavailable') from None
    finally:
        _downloads.release()
