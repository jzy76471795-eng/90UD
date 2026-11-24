"""Authenticate each raw native TCP stream before routing it to an account.

Only the paired launcher sends this small prefix. Original game bytes after it
are unchanged. Legacy clients retain the reviewed single-account-per-IP route.
"""
from contextvars import ContextVar
import ipaddress,socket,secrets
from threading import RLock
from weakref import WeakKeyDictionary
from time import monotonic

MAGIC=b'PY90NT2\n'
_tunneled=ContextVar('py90_native_tunnel',default=False)
_proof_lock=RLock()
_proved_sockets=WeakKeyDictionary()
AUTH_TIMEOUT_SECONDS=5.0


def authenticated_native_peer(client,identity):
 """Recognize only this socket's completed session proof, never a shared IP."""
 if not isinstance(client,socket.socket) or client.fileno()<0:return False
 try:remote=str(ipaddress.ip_address(client.getpeername()[0]))
 except (OSError,ValueError):return False
 with _proof_lock:
  proof=_proved_sockets.get(client)
  return proof is not None and proof[0] is identity and proof[1]==remote

# Transport liveness, independent of game input/AFK. On Windows the per-socket
# idle/interval are milliseconds and the OS uses ten unanswered probes.
KEEPALIVE_IDLE_MS=60000
KEEPALIVE_INTERVAL_MS=10000

def _keepalive(client):
 client.setsockopt(socket.SOL_SOCKET,socket.SO_KEEPALIVE,1)
 if hasattr(socket,'SIO_KEEPALIVE_VALS'):
  client.ioctl(socket.SIO_KEEPALIVE_VALS,(1,KEEPALIVE_IDLE_MS,KEEPALIVE_INTERVAL_MS))
 elif all(hasattr(socket,k) for k in ('TCP_KEEPIDLE','TCP_KEEPINTVL','TCP_KEEPCNT')):
  client.setsockopt(socket.IPPROTO_TCP,socket.TCP_KEEPIDLE,KEEPALIVE_IDLE_MS//1000)
  client.setsockopt(socket.IPPROTO_TCP,socket.TCP_KEEPINTVL,KEEPALIVE_INTERVAL_MS//1000)
  client.setsockopt(socket.IPPROTO_TCP,socket.TCP_KEEPCNT,10)
 else:raise OSError('bounded native TCP keepalive is unavailable')

def local_game_endpoints():return _tunneled.get()
def enter_tunnel(enabled):return _tunneled.set(bool(enabled))
def leave_tunnel(token):_tunneled.reset(token)

def _remaining(deadline):
 remaining=deadline-monotonic()
 if remaining<=0:raise TimeoutError('native authentication deadline exceeded')
 return remaining


def _exact(client,length,deadline):
 parts=[];left=length
 while left:
  client.settimeout(_remaining(deadline))
  part=client.recv(left)
  if not part:raise ValueError('native authentication prefix incomplete')
  parts.append(part);left-=len(part)
 return b''.join(parts)


def _legacy_identity(registry,remote_ip,deadline):
 _remaining(deadline)
 identity=registry.identity_for_remote_ip(remote_ip)
 _remaining(deadline)
 return identity,False

def identify(client,remote_ip,registry):
 deadline=monotonic()+AUTH_TIMEOUT_SECONDS
 remote=str(ipaddress.ip_address(remote_ip))
 # A socket must not retain an earlier proof when reused by a test/adapter.
 with _proof_lock:_proved_sockets.pop(client,None)
 old=client.gettimeout()
 try:
  port=client.getsockname()[1]
  if port in registry.legacy_game_ports:
   identity,_=_legacy_identity(registry,remote,deadline)
   if identity is None:raise ValueError('legacy Game session unavailable')
   return identity,False
  # Standalone loopback tools can still send their original flag-zero wire
  # packet when there is no launcher service in this process.
  standalone_loopback=(not getattr(registry,'_go_launcher_guard',False)
                       and ipaddress.ip_address(client.getpeername()[0]).is_loopback)
  if port in registry.native_client_first_ports or standalone_loopback:
   client.settimeout(_remaining(deadline))
   first=client.recv(1,socket.MSG_PEEK)
   # The legacy Channel client sends a flag-zero request before any response.
   if first==b'\x00':return _legacy_identity(registry,remote,deadline)
  if _exact(client,len(MAGIC),deadline)!=MAGIC:raise ValueError('invalid native authentication prefix')
  session_id=_exact(client,16,deadline).hex()
  nonce=secrets.token_bytes(32)
  client.settimeout(_remaining(deadline));client.sendall(nonce)
  identity=registry.authenticate_native_from_peer(session_id,nonce,_exact(client,32,deadline),remote)
  _remaining(deadline)
  if identity is None or not getattr(identity,'device_id',''):
   raise ValueError('native authentication rejected')
  _keepalive(client)
  _remaining(deadline)
  # The canonical login identity stays unchanged for ownership/revocation.
  # The actual TCP peer is bound only to this proved socket, with weak lifetime.
  with _proof_lock:_proved_sockets[client]=(identity,remote)
  return identity,True
 finally:client.settimeout(old)
