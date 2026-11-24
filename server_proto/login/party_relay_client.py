"""Server-only authenticated relay control. No player credentials in diagnostics.

The launcher adapter resolves its existing bearer token before issue(). All
network calls run outside party/directory locks. Player code never gets this
control key or chooses account/login/native bindings.
"""
from __future__ import annotations
import hashlib,hmac,json,secrets,ssl,threading,time,urllib.error,urllib.parse,urllib.request
from dataclasses import dataclass
from pathlib import Path

class RelayUnavailable(RuntimeError):
    pass

class RelayBindingRejected(RelayUnavailable):
    """Authenticated control refused only this native session binding."""
    pass

def authority_body(snapshot):
    """MAC v1 canonical ASCII; shared byte vector is covered by Go tests."""
    def number(value,maximum):
        if type(value) is not int or not 0<=value<=maximum:raise RelayUnavailable('invalid authority integer')
        return value
    def rows(values):
        result=[]
        for row in values:
            if set(row)!={'id','scope','epoch','active'} or type(row['active']) is not bool:raise RelayUnavailable('invalid authority member')
            result.append({'id':number(row['id'],2**32-1),'scope':number(row['scope'],2**64-1),'epoch':number(row['epoch'],2**64-1),'active':row['active']})
        return sorted(result,key=lambda row:row['id'])
    boot=snapshot['boot']
    if not isinstance(boot,str) or len(boot)!=32 or any(c not in '0123456789abcdef' for c in boot):raise RelayUnavailable('invalid authority boot')
    value={'boot':boot,'version':number(snapshot['version'],2**64-1),'issued_ms':number(snapshot['issued_ms'],2**63-1),'lease_ms':number(snapshot['lease_ms'],5000),'members':rows(snapshot['members']),'pending':rows(snapshot['pending']),'mac':''}
    return json.dumps(value,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')

def _object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise RelayUnavailable('duplicate relay response field')
        result[key]=value
    return result

def _endpoint(text):
    if not isinstance(text,str):raise RelayUnavailable('invalid relay TLS endpoint')
    p=urllib.parse.urlsplit(text)
    if p.scheme!='https' or not p.hostname or p.username is not None or p.password is not None or p.path not in ('','/') or p.query or p.fragment:
        raise RelayUnavailable('invalid relay TLS endpoint')
    try:p.port
    except ValueError:raise RelayUnavailable('invalid relay TLS endpoint') from None
    return text.rstrip('/')

@dataclass(frozen=True)
class RelayRollout:
    """Frozen startup policy; transport errors never choose the legacy path."""
    accounts:tuple|None=None

    def __post_init__(self):
        if self.accounts is not None and (type(self.accounts) is not tuple or len(self.accounts)>4096
                or any(type(a) is not int or not 1<=a<=4294967295 for a in self.accounts)
                or len(set(self.accounts))!=len(self.accounts)):
            raise RelayUnavailable('invalid relay rollout accounts')

    def contains(self,account):
        return type(account) is int and 1<=account<=4294967295 and (self.accounts is None or account in self.accounts)

    def group(self,accounts):
        values=tuple(accounts)
        if not values or any(type(a) is not int or not 1<=a<=4294967295 for a in values):
            raise RelayUnavailable('invalid relay rollout group')
        enabled=tuple(self.contains(a) for a in values)
        if any(enabled) and not all(enabled):
            raise RelayUnavailable('party relay pilot and legacy accounts cannot form one party')
        return all(enabled)

@dataclass(frozen=True)
class RelayConfig:
    control_url:str
    public_url:str
    ca_file:str
    key_file:str
    rollout_accounts:tuple|None=None

    def __post_init__(self):
        RelayRollout(self.rollout_accounts)

    @classmethod
    def load(cls,path):
        raw=Path(path).read_bytes()
        if len(raw)>16384:raise RelayUnavailable('bounded relay configuration required')
        value=json.loads(raw,object_pairs_hook=_object)
        required={'control_url','public_url','ca_file','key_file'}
        if (not isinstance(value,dict) or set(value) not in (required,required|{'rollout_accounts'})
                or any(not isinstance(value[k],str) or not value[k] for k in required)):
            raise RelayUnavailable('invalid relay configuration')
        accounts=None
        if 'rollout_accounts' in value:
            if type(value['rollout_accounts']) is not list:raise RelayUnavailable('invalid relay rollout accounts')
            accounts=tuple(value['rollout_accounts'])
            RelayRollout(accounts)
        return cls(_endpoint(value['control_url']),_endpoint(value['public_url']),value['ca_file'],value['key_file'],accounts)

class RelayControl:
    def __init__(self,config):
        self.config=config;self.rollout=RelayRollout(config.rollout_accounts);self._key=Path(config.key_file).read_bytes()
        if len(self._key)!=32:raise RelayUnavailable('private relay control key unavailable')
        tls=ssl.create_default_context(cafile=config.ca_file)
        tls.minimum_version=ssl.TLSVersion.TLSv1_2
        self._opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=tls))
        self._capacity=threading.BoundedSemaphore(8);self._lock=threading.RLock()
        self._issued={};self._accounts={};self._account_locks={}

    def request(self,method,path,value=None):
        if path not in {'/v1/control/session','/v1/control/revoke','/v1/control/bind','/v1/control/routes','/v1/control/status'}:
            raise RelayUnavailable('unsupported relay control operation')
        body=b'' if value is None else json.dumps(value,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')
        if len(body)>65536:raise RelayUnavailable('bounded relay request required')
        issued=str(int(time.time()*1000));nonce=secrets.token_hex(16)
        material='\n'.join(['control-v1',method,path,issued,nonce,hashlib.sha256(body).hexdigest()]).encode('ascii')
        request=urllib.request.Request(self.config.control_url+path,data=None if method=='GET' else body,method=method,headers={
            'Content-Type':'application/json','X-Relay-Issued':issued,'X-Relay-Nonce':nonce,'X-Relay-MAC':hmac.new(self._key,material,hashlib.sha256).hexdigest()})
        if not self._capacity.acquire(blocking=False):raise RelayUnavailable('relay control capacity')
        try:
            try:
                with self._opener.open(request,timeout=1.5) as response:
                    if response.status not in (200,201):raise RelayUnavailable('relay refused control request')
                    raw=response.read(65537)
                if len(raw)>65536:raise RelayUnavailable('bounded relay response required')
                result=json.loads(raw,object_pairs_hook=_object)
                if not isinstance(result,dict):raise RelayUnavailable('invalid relay control response')
                return result
            except urllib.error.HTTPError as exc:
                if exc.code==409 and path=='/v1/control/bind':raise RelayBindingRejected('native relay binding rejected') from None
                raise RelayUnavailable('authenticated relay control unavailable') from None
            except (OSError,ValueError,urllib.error.URLError):raise RelayUnavailable('authenticated relay control unavailable') from None
        finally:self._capacity.release()

    def issue(self,identity,request_id):
        account,login=identity.account_id,identity.login_session_id
        if type(account) is not int or account<=0 or not isinstance(login,str) or len(login)!=32 or any(c not in '0123456789abcdef' for c in login):
            raise RelayUnavailable('authenticated launcher identity required')
        if not self.rollout.contains(account):raise RelayUnavailable('account outside party relay pilot')
        if not isinstance(request_id,str) or not 16<=len(request_id)<=128 or any(not(c.isascii() and (c.isalnum() or c in '-_')) for c in request_id):
            raise RelayUnavailable('invalid launch request identity')
        with self._lock:
            if account not in self._account_locks:
                if len(self._account_locks)>=4096:raise RelayUnavailable('relay login directory capacity')
                self._account_locks[account]=threading.Lock()
            guard=self._account_locks[account]
        # This is an account-local launch lock; it is never the party lock.
        with guard:
            grant=self.request('POST','/v1/control/session',{'request_id':request_id,'account_id':account,'login_instance':login,'mtu':1200})
            expected={'boot','member_id','session_id','secret','launch_token','expires_ms','mtu'}
            if set(grant)!=expected or any(type(grant.get(k)) is not int for k in ('member_id','session_id','expires_ms','mtu')):
                raise RelayUnavailable('invalid single-use relay authorization')
            if not 1<=grant['member_id']<=65535 or not 1<=grant['session_id']<2**64 or grant['mtu']!=1200 or not 0<grant['expires_ms']-int(time.time()*1000)<=61000:
                raise RelayUnavailable('invalid single-use relay authorization')
            for name,length in [('boot',32),('secret',64),('launch_token',64)]:
                value=grant[name]
                if not isinstance(value,str) or len(value)!=length or any(c not in '0123456789abcdef' for c in value):raise RelayUnavailable('invalid single-use relay authorization')
            with self._lock:self._accounts[account]=(login,grant['member_id'],grant['boot'])
            return {'protocol':1,'relay_url':self.config.public_url,'grant':grant}

    def member_for(self,account,login):
        with self._lock:
            row=self._accounts.get(account)
            return row[1] if row is not None and row[0]==login else None

    def binding_for(self,account,login):
        with self._lock:
            row=self._accounts.get(account)
            return (row[1],row[2]) if row is not None and row[0]==login else None

    def revoke_login(self,account,login):
        member=self.member_for(account,login)
        if member is None:return
        try:self.request('POST','/v1/control/revoke',{'member_id':member})
        finally:
            with self._lock:
                if self._accounts.get(account,())[0:2]==(login,member):self._accounts.pop(account,None)

    def publish(self,boot,version,members,pending):
        snapshot={'boot':boot,'version':version,'issued_ms':int(time.time()*1000),'lease_ms':5000,'members':members,'pending':pending,'mac':''}
        snapshot['mac']=hmac.new(self._key,authority_body(snapshot),hashlib.sha256).hexdigest()
        return self.request('PUT','/v1/control/routes',snapshot)
