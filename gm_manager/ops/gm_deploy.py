"""Owner-only encrypted WinRM deployment. Never changes Defender or game files."""
import base64,gzip,hashlib,json,os,sqlite3,sys,time
from pathlib import Path

def validate(doc):
    candidate=Path(doc['candidate']).resolve();backup=Path(doc['backup']).resolve()
    deploy_root = Path(os.environ['USDOF_GM_DEPLOY_ROOT']).resolve(strict=True)
    for path in (candidate, backup):
        if not path.is_relative_to(deploy_root):
            raise ValueError('Candidate and backup must be inside the configured deployment directory')
    raw=candidate.read_bytes()
    if len(raw)<64 or len(raw)>64*1024*1024 or raw[:2]!=b'MZ' or hashlib.sha256(raw).hexdigest()!=doc['new_sha256']:raise ValueError('Candidate hash or PE identity mismatch')
    old=(backup/'gmserver.exe').read_bytes()
    if hashlib.sha256(old).hexdigest()!=doc['old_sha256']:raise ValueError('Local rollback program hash mismatch')
    config=json.loads((backup/'go-manager.json').read_text(encoding='utf-8-sig'))
    if config.get('direct_database') is not True or config.get('game_database')!=r'C:\90US\server_proto\data\inventory_usdof_local.db':raise ValueError('Existing database identity mismatch')
    database=sqlite3.connect((backup/'players.db').as_uri()+'?mode=ro',uri=True)
    try:
        if database.execute('PRAGMA integrity_check').fetchall()!=[('ok',)] or database.execute('PRAGMA foreign_key_check').fetchall():raise ValueError('Local database backup failed validation')
    finally:database.close()
    return raw,old,config

def perform(doc):
    raw,old,original_config=validate(doc)
    import transport
    transport.EXPECTED_HOST=doc['host']
    original=dict(original_config);config=dict(original);config.update(manager_directory=r'C:\90US\gm_manager',manager_task_name='90US-Go-GM-Manager')
    payload={'request_id':doc['id'],'old_sha256':doc['old_sha256'],'new_sha256':doc['new_sha256'],'data':base64.b64encode(gzip.compress(raw,mtime=0)).decode(),'config':config}
    code=r'''import base64,gzip,hashlib,json,psutil,sqlite3,time
from pathlib import Path
doc=json.loads(PAYLOAD);root=Path(r'C:\90US\gm_manager');exe=root/'gmserver.exe';target=str(exe).lower();prior=exe.read_bytes();assert hashlib.sha256(prior).hexdigest()==doc['old_sha256'],'Current program changed since local backup'
data=gzip.decompress(base64.b64decode(doc['data']));assert hashlib.sha256(data).hexdigest()==doc['new_sha256'] and data[:2]==b'MZ'
processes=[p for p in psutil.process_iter(['exe']) if p.info['exe'] and p.info['exe'].lower()==target];assert len(processes)<=1,'Ambiguous GM process ownership'
old_pid=processes[0].pid if processes else 0;created=processes[0].create_time() if processes else 0
configpath=root/'go-manager.json';oldconfig=configpath.read_bytes();player=Path(doc['config']['game_database']);assert player.is_file()
db=sqlite3.connect(player.as_uri()+'?mode=rw',uri=True,timeout=5);db.execute('PRAGMA synchronous=FULL')
fingerprint=hashlib.sha256(json.dumps({k:doc[k] for k in ['old_sha256','new_sha256','request_id']},sort_keys=True).encode()).hexdigest()
receipt={'request_id':doc['request_id'],'action':'manager.deploy','state':'pending','applied_to_game':False,'message':'Encrypted WinRM deployment prepared','created_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'result':{'old_pid':old_pid,'old_sha256':doc['old_sha256'],'new_sha256':doc['new_sha256']}}
reply={'ok':True,'request_id':doc['request_id'],'data':receipt}
try:
 db.execute('BEGIN IMMEDIATE');existing=db.execute('SELECT fingerprint,reply_json FROM manager_requests WHERE request_id=?',(doc['request_id'],)).fetchone()
 if existing:
  assert existing[0]==fingerprint,'Request id conflict';raise RuntimeError('Original deployment already registered; query original receipt')
 db.execute('INSERT INTO manager_requests VALUES(?,?,?,?)',(doc['request_id'],fingerprint,200,json.dumps(reply)));db.commit()
 for process in processes:
  assert process.create_time()==created and process.exe().lower()==target
  process.terminate()
  try:process.wait(8)
  except psutil.TimeoutExpired:process.kill();process.wait(5)
 try:
  exe.write_bytes(data);assert hashlib.sha256(exe.read_bytes()).hexdigest()==doc['new_sha256'];configpath.write_text(json.dumps(doc['config'],ensure_ascii=False,indent=2),encoding='utf-8')
 except Exception:exe.write_bytes(prior);configpath.write_bytes(oldconfig);raise
 print(json.dumps({'installed':True,'request_id':doc['request_id'],'old_pid':old_pid,'new_sha256':doc['new_sha256']}))
finally:db.close()
'''.replace('PAYLOAD',repr(json.dumps(payload)),1)
    applied=json.loads(transport.python(code,private=True))
    task_script=r'''$name='90US-Go-GM-Manager';$root='C:\90US\gm_manager';$existing=Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue;if($existing -and $existing.Actions[0].Execute -ine "$root\gmserver.exe"){throw 'Task target differs'};$action=New-ScheduledTaskAction -Execute "$root\gmserver.exe" -Argument '-config C:\90US\gm_manager\go-manager.json' -WorkingDirectory $root;$trigger=New-ScheduledTaskTrigger -AtStartup;$settings=New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries;Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -User 'SYSTEM' -RunLevel Highest -Force -Description '独立Go GM HTTPS管理服务。游戏计划任务与本任务分开。'|Out-Null;Start-ScheduledTask -TaskName $name;'GM task started' '''
    check=r'''import hashlib,http.client,json,psutil,sqlite3,ssl,time
from pathlib import Path
doc=json.loads(PAYLOAD);root=Path(r'C:\90US\gm_manager');config=json.loads((root/'go-manager.json').read_text(encoding='utf-8'));cert=Path(config['tls_certificate']);pin=hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).digest();context=ssl.create_default_context(cafile=str(cert));context.check_hostname=False
healthy=False;pid=0
for attempt in range(30):
 try:
  connection=http.client.HTTPSConnection('127.0.0.1',17890,context=context,timeout=2);connection.connect();assert hashlib.sha256(connection.sock.getpeercert(binary_form=True)).digest()==pin
  connection.request('GET','/health');response=connection.getresponse();body=json.loads(response.read());connection.close();pid=int(body['data']['pid']);process=psutil.Process(pid);healthy=body['ok'] is True and process.exe().lower()==str(root/'gmserver.exe').lower() and hashlib.sha256((root/'gmserver.exe').read_bytes()).hexdigest()==doc['new_sha256']
  if healthy:break
 except Exception:pass
 time.sleep(.5)
assert healthy,'New GM did not pass pinned loopback TLS health'
db=sqlite3.connect(Path(config['game_database']).as_uri()+'?mode=rw',uri=True,timeout=5)
try:
 prior=db.execute('SELECT reply_json FROM manager_requests WHERE request_id=?',(doc['request_id'],)).fetchone();reply=json.loads(prior[0]);reply['data'].update(state='confirmed',message='Encrypted WinRM deployment and TLS/PID health confirmed');reply['data']['result'].update(new_pid=pid,task='90US-Go-GM-Manager');db.execute('UPDATE manager_requests SET reply_json=? WHERE request_id=?',(json.dumps(reply),doc['request_id']))
 for id,raw in db.execute("SELECT request_id,reply_json FROM manager_requests WHERE instr(reply_json,'manager.update')>0").fetchall():
  old=json.loads(raw);value=old.get('data',{})
  if isinstance(value,dict) and value.get('action')=='manager.update' and value.get('state')=='pending':value.update(state='failed',message='The retired host was quarantined by Windows Defender; this update was not completed');db.execute('UPDATE manager_requests SET reply_json=? WHERE request_id=?',(json.dumps(old),id))
 db.execute('INSERT INTO manager_audit(at,actor,module,action,target,result,request_id,detail) VALUES(?,?,?,?,?,?,?,?)',(time.strftime('%Y-%m-%dT%H:%M:%S.000000000Z',time.gmtime()),'administrator','一键部署','manager.deploy',str(root),'confirmed',doc['request_id'],'Encrypted WinRM; game process untouched'));db.commit();print(json.dumps(reply['data']))
finally:db.close()
'''.replace('PAYLOAD',repr(json.dumps(payload)),1)
    try:
        transport.ps(task_script,private=True)
        receipt=json.loads(transport.python(check,private=True))
        return receipt
    except Exception:
        rollback={'data':base64.b64encode(gzip.compress(old,mtime=0)).decode(),'sha256':doc['old_sha256'],'config':original,'id':doc['id']}
        restore=r'''import base64,gzip,hashlib,json,psutil,sqlite3
from pathlib import Path
doc=json.loads(PAYLOAD);root=Path(r'C:\90US\gm_manager');target=root/'gmserver.exe'
for p in psutil.process_iter(['exe']):
 if p.info['exe'] and p.info['exe'].lower()==str(target).lower():p.terminate();p.wait(8)
data=gzip.decompress(base64.b64decode(doc['data']));assert hashlib.sha256(data).hexdigest()==doc['sha256'];target.write_bytes(data);(root/'go-manager.json').write_text(json.dumps(doc['config'],ensure_ascii=False,indent=2),encoding='utf-8')
db=sqlite3.connect(Path(doc['config']['game_database']).as_uri()+'?mode=rw',uri=True)
try:
 row=db.execute('SELECT reply_json FROM manager_requests WHERE request_id=?',(doc['id'],)).fetchone()
 if row:
  reply=json.loads(row[0]);reply['data'].update(state='failed',message='Deployment did not pass health; old verified bytes restored');db.execute('UPDATE manager_requests SET reply_json=? WHERE request_id=?',(json.dumps(reply),doc['id']));db.commit()
finally:db.close()
print(json.dumps({'restored':True}))'''.replace('PAYLOAD',repr(json.dumps(rollback)),1)
        transport.python(restore,private=True);transport.ps(task_script,private=True)
        raise RuntimeError('New service failed health; restored verified local rollback program') from None

if __name__=='__main__':
    try:
        doc=json.load(sys.stdin);receipt=perform(doc);print(json.dumps({'ok':True,'receipt':receipt},ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'ok':False,'error':'Encrypted deployment did not complete; query original receipt before retrying','error_type':type(error).__name__},ensure_ascii=False));sys.exit(1)
